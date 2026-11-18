"""Geminiを使ったAIベースのコンテンツモデレーションサービス。

ルールベースのNGワードフィルタを補完し、スペース挿入・記号混入・
カタカナ⇔ひらがな変換・伏字・隠語などの回避テクニックにも対応する。
APIエラーやタイムアウト時は安全側に倒して通過させる（fail-open）。

プロンプト定義・出力スキーマ・判定結果の正規化は `moderation_prompts.py`（#469）に分けてあり、
ここから従来の名前で再公開する（テストが `_build_prompt` などをこのモジュールで差し替えるため、
それを呼ぶ `ContentModerator` はここに残す）。
"""

import logging
import asyncio
from typing import Any

from persona_bot.config.constants import (
    BOT_RESPONSE_REFUSAL_TEXT, MODERATION_MODEL_NAME, MODERATION_TIMEOUT_SECONDS, REMINDER_TIMEZONE,
)
from persona_bot.services.agent_plan import AgentPlan, normalize_agent_plan
from persona_bot.services.moderation_prompts import (  # noqa: F401（従来の import 先として再公開する名前を含む）
    VERDICT_SAFE,
    VERDICT_UNKNOWN,
    VERDICT_UNSAFE,
    _DATA_END,
    _DATA_START,
    _SYSTEM_PROMPT,
    _VOICE_TURN_SYSTEM_PROMPT,
    _build_combined_system_prompt,
    _build_prompt,
    _normalize_emotion_delta,
    _normalize_reminder,
    _normalize_safe_flag,
    _response_schema,
)
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.genai_client import generate_content, generate_json_content
from persona_bot.utils.storage import read_json_response
from persona_bot.utils.timeutil import format_prompt_now, resolve_timezone, timezone_label
from persona_bot.utils.normalization import normalize_memory_category

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ContentModerator クラス
# ---------------------------------------------------------------------------

class ContentModerator:
    """Gemini flash-lite を使ってテキストの安全性をAI判定するモデレーター。"""

    def __init__(
        self,
        genai_client: Any,
        ng_filter: NgWordFilter,
        *,
        model_name: str = MODERATION_MODEL_NAME,
        timeout: float = MODERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._genai_client = genai_client
        self._ng_filter = ng_filter
        self._model_name = model_name
        self._timeout = timeout
        # リマインダーの相対表現（「30分後」など）を絶対日時へ直させるための基準時刻に使う。
        # ここで一度だけ解決しておく — メッセージごとに組み立てると、タイムゾーンデータが
        # 無い環境では毎回例外になり、fail-open のはずのモデレーションが
        # on_message ごと落として全メッセージを無応答にしてしまう。
        self._tz = resolve_timezone(REMINDER_TIMEZONE, purpose="リマインダーの日時解釈")

    async def _judge(
        self, prompt: tuple[str, str], *,
        route: bool = False, include_agent: bool = False,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """モデレーション判定を Gemini に1回問い合わせ、結果を正規化して返す。

        check() と check_and_route() の共通部分（API 呼び出し・プロンプトブロック判定・
        テキスト取得・JSON パース・safe フラグの正規化）をここに集約する。
        追加項目（記憶カテゴリ・リマインダー）の取り出しは呼び出し側が parsed から行う。

        Returns:
            (is_safe, reason, parsed) のタプル。parsed は JSON を読めなかった場合 None。

        **この関数から例外を外へ漏らさないこと。** on_message の主経路から呼ばれるため、
        抜けた例外はそのメッセージの処理ごと落として無応答にしてしまう。
        """
        verdict, reason, parsed = await self._judge_verdict(
            prompt, schema=_response_schema(route=route, include_agent=include_agent),
        )
        # 判定不能は従来どおり通過扱い（fail-open）。
        return verdict != VERDICT_UNSAFE, reason, parsed

    async def _judge_verdict(
        self, prompt: tuple[str, str], *, schema: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any] | None]:
        """判定を「安全・不適切・判定不能」の3段階で返す。例外は外へ漏らさない。"""
        try:
            response = await generate_json_content(
                generate_content, self._genai_client, model=self._model_name, contents=prompt[1],
                system_instruction=prompt[0],
                timeout=self._timeout,
                response_schema=schema,
            )

            # Gemini がプロンプト自体をブロックした場合
            # → コンテンツが不適切だと Gemini が判断したので unsafe 扱い
            reason_name, result_text, parsed = read_json_response(response, required_keys=("safe",))
            if reason_name is not None:
                logger.warning("モデレーション: プロンプトがブロックされました。")
                return VERDICT_UNSAFE, f"安全フィルタによりブロック ({reason_name})", None

            # SDKの応答差異や空候補を各呼出側で処理せず、共通入口で安全に読む。
            if result_text is None:
                # 明示的なプロンプトブロックは上で判定済み。本文の欠落だけでは違反としない。
                logger.warning("モデレーション: テキスト取得失敗のため通過扱いにします。")
                return VERDICT_UNKNOWN, "", None

            if parsed is None:
                # パース失敗は通過扱い（fail-open）。
                logger.warning("モデレーション: 応答パース失敗のため通過扱いにします。")
                return VERDICT_UNKNOWN, "", None

            # {"safe": "false"} のように文字列で返るケースに備えて明示的に正規化する。
            # bool("false") は True になるため、単純な bool() では誤判定する。
            is_safe = _normalize_safe_flag(parsed.get("safe", True))
            raw_reason = parsed.get("reason")
            reason = raw_reason.strip() if not is_safe and isinstance(raw_reason, str) else ""

            if not is_safe:
                logger.warning("モデレーション: 不適切な内容を検出しました。")
                return VERDICT_UNSAFE, reason, parsed
            # 曖昧な値（"yes" など）での通過は、安全と確定したことにしない。
            return (VERDICT_SAFE if parsed.get("safe") is True else VERDICT_UNKNOWN), reason, parsed

        except asyncio.TimeoutError:
            logger.warning("モデレーション: タイムアウトのため通過扱いにします。", exc_info=True)
            return VERDICT_UNKNOWN, "", None
        except Exception as error:
            logger.error("モデレーション: 判定に失敗したため通過扱いにします。", exc_info=error)
            return VERDICT_UNKNOWN, "", None

    async def check(
        self,
        text: str,
        *,
        direction: str = "ユーザー入力",
    ) -> tuple[bool, str]:
        """テキストの安全性をAIで判定する。

        Returns:
            (is_safe, reason) のタプル。
            is_safe が True なら安全、False なら不適切。
            reason は不適切と判定された場合の理由。
        """
        if not text or not text.strip():
            return True, ""

        # Bot応答は既にNGワードフィルタ済みのため、モデレーションプロンプトに
        # NGワードを含めない。含めるとNGワード自体がGeminiの安全フィルタを
        # 発動させ、安全な応答まで誤ブロックされる。
        ng_words = self._ng_filter.words if direction != "Bot応答" else []
        prompt = _build_prompt(text, direction, ng_words)

        is_safe, reason, _parsed = await self._judge(prompt)
        return is_safe, reason

    async def judge_voice_input(self, text: str) -> str:
        """音声会話の入力（参加者の発言の文字起こし）を3段階で判定する（#268）。

        Returns:
            VERDICT_SAFE / VERDICT_UNSAFE / VERDICT_UNKNOWN のいずれか。
            文字起こしが空なら判定する内容が無いので VERDICT_UNKNOWN。
        """
        if not text or not text.strip():
            return VERDICT_UNKNOWN
        prompt = _build_prompt(text, "ユーザー入力", self._ng_filter.words)
        verdict, _reason, _parsed = await self._judge_verdict(prompt, schema=_response_schema())
        return verdict

    async def judge_voice_turn(
        self, user_text: str, bot_text: str,
    ) -> tuple[str, tuple[int, int] | None]:
        """音声会話の返答を判定し、同じ1回で感情の変化量も推定する（#268）。

        Returns:
            (判定, (機嫌の変化量, テンションの変化量)) のタプル。変化量は安全と判定され、
            両方を整数で読めたときだけ返す（それ以外は None で、親密度を更新しない）。
        """
        if not bot_text or not bot_text.strip():
            return VERDICT_UNKNOWN, None
        # Bot応答は既にNGワードフィルタ済みのため、check() と同じく NG ワードを載せない。
        prompt = _build_prompt(
            bot_text, "Bot応答", [], system_prompt=_VOICE_TURN_SYSTEM_PROMPT, history_text=user_text,
        )
        verdict, _reason, parsed = await self._judge_verdict(prompt, schema=_response_schema(emotion=True))
        if verdict != VERDICT_SAFE or parsed is None:
            return verdict, None
        valence = _normalize_emotion_delta(parsed.get("valence_change"))
        arousal = _normalize_emotion_delta(parsed.get("arousal_change"))
        if valence is None or arousal is None:
            return verdict, None
        return verdict, (valence, arousal)

    async def check_and_route(self, text: str) -> tuple[bool, str, str, dict[str, str] | None]:
        """ユーザー入力のモデレーション・記憶ルーティング・リマインダー抽出を1回で行う。

        チャットの主経路（メンションのたびに走る）で、本来3回になるはずの Gemini 呼び出し
        （入力モデレーション＋記憶ルーティング＋リマインダー判定）を1回に統合して
        API コストを抑える。

        Returns:
            (is_safe, reason, category, reminder) のタプル。
            category は "profile" / "projects" / "none" のいずれか。
            reminder はリマインダー依頼なら
            {"at": "YYYY-MM-DD HH:MM", "message": ..., "repeat": "none|daily|weekly|monthly"}、
            依頼でなければ None。
            タイムアウトや例外時は安全側かつ記憶不要側に倒し (True, "", "none", None) を返す。
        """
        is_safe, reason, category, reminder, _agent = await self._route(text, None)
        return is_safe, reason, category, reminder

    async def check_route_and_plan(
        self, text: str, history_text: str = "",
    ) -> tuple[bool, str, str, dict[str, str] | None, AgentPlan | None]:
        """check_and_route と同じ1回の呼び出しで、サブエージェントへの委任も判定する（#170）。

        発言にサブエージェントの使用が明記されているとき（agent_plan.requests_subagent）だけ使う。
        明記の無い普段の会話は check_and_route のまま（プロンプトを増やさない）。

        Args:
            history_text: 委任内容の整理に使うチャンネルの直近の会話（安全性の判定対象ではない）。

        Returns:
            (is_safe, reason, category, reminder, agent) のタプル。agent は委任するなら
            delegate=True の AgentPlan、不要と判断したなら delegate=False の AgentPlan、
            依頼ではない・判定を読めなかった・不適切な入力なら None。
        """
        return await self._route(text, history_text)

    async def _route(
        self, text: str, agent_history: str | None,
    ) -> tuple[bool, str, str, dict[str, str] | None, AgentPlan | None]:
        """統合判定の本体。agent_history が None なら委任の判定を足さない。"""
        if not text or not text.strip():
            return True, "", "none", None, None

        # 「30分後」などの相対表現を解釈させるため、基準となる現在日時を渡す。
        # 曜日名（「来週の月曜」などの解釈を助ける）は timeutil の唯一のテーブルから取る
        # — ここで独自に持つと、リマインダーの表示側と表記が食い違う。
        now_text = format_prompt_now(self._tz)

        # ユーザー入力の判定なので、単独モデレーションと同様に NG ワードを参考提示する。
        prompt = _build_prompt(
            text,
            "ユーザー入力",
            self._ng_filter.words,
            # 基準タイムゾーン名は定数ではなく「実際に解決できた tz」から作る。
            # tzdata の無い環境では self._tz が UTC へフォールバックしており、
            # 定数をそのまま名乗ると now_text（UTC の時刻）と食い違って
            # 9時間ずれた日時でリマインダーが登録されてしまう。
            system_prompt=_build_combined_system_prompt(
                now_text, timezone_label(self._tz), include_agent=agent_history is not None,
            ),
            history_text=agent_history,
        )

        is_safe, reason, parsed = await self._judge(
            prompt, route=True, include_agent=agent_history is not None,
        )
        if parsed is None or parsed.get("safe") is not True:
            # JSON が読めなかった場合。安全判定は _judge の結果に従い、
            # 記憶カテゴリ・リマインダー・委任は「無し」に倒す。曖昧なsafe値での
            # 通過（fail-open）も、登録や委任を実行する根拠にはしない。
            return is_safe, reason, "none", None, None

        category = normalize_memory_category(parsed.get("category", "none"))
        # 不適切と判定した入力からはリマインダーを作らない（そのまま通知文になるため）。
        reminder = _normalize_reminder(parsed.get("reminder")) if is_safe else None
        # 委任内容も同じ理由で、不適切な入力からは作らない（子への指示文になるため）。
        agent = normalize_agent_plan(parsed.get("agent")) if is_safe and agent_history is not None else None
        return is_safe, reason, category, reminder, agent


async def screen_bot_response(
    response_text: str,
    ng_filter: NgWordFilter | None,
    moderator: "ContentModerator | None",
    *,
    log_label: str = "Bot応答",
) -> str:
    """Bot が生成した応答に NG ワードマスクと AI モデレーションを適用する。

    NG ワードは ● でマスクし、モデレーションで不適切と判定された場合は
    定型の謝罪文に差し替える。チャット応答と文書応答で共通の後処理。
    """
    text, _accepted = await screen_bot_response_with_status(
        response_text, ng_filter, moderator, log_label=log_label,
    )
    return text


async def screen_bot_response_with_status(
    response_text: str,
    ng_filter: NgWordFilter | None,
    moderator: "ContentModerator | None",
    *,
    log_label: str = "Bot応答",
) -> tuple[str, bool]:
    """表示本文と判定の成否を返し、謝罪文の記憶保存や親密度更新を防ぐ。"""
    if ng_filter is not None:
        response_text = ng_filter.mask_with_log(response_text, label=log_label)

    if moderator is not None:
        is_safe, reason = await moderator.check(response_text, direction="Bot応答")
        if not is_safe:
            logger.warning("Bot応答モデレーション: 投稿を見送りました。")
            return BOT_RESPONSE_REFUSAL_TEXT, False

    return response_text, True
