import bisect
import logging
import asyncio
import copy
import json
import re
from typing import Any

from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread
from persona_bot.config.constants import (
    ANNIVERSARY_PROFILE_KEY,
    DOCUMENT_MEMORY_CONTEXT_LIMIT,
    DOCUMENT_SUMMARY_LIMIT,
    LEGACY_MEMORY_OWNER_ID,
    MEMORY_COMPACT_MAX_FAILURES,
    MEMORY_COMPACT_TIMEOUT_SECONDS,
    MEMORY_DIR,
    MEMORY_FLUSH_IDLE_SECONDS,
    MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS,
    MEMORY_FLUSH_MAX_USERS_PER_RUN,
    MEMORY_FLUSH_THRESHOLD_ENTRIES,
    PROFILE_MEMORY_PATH,
    PROJECTS_MEMORY_PATH,
    ROUTER_MODEL_NAME,
    ROUTER_TIMEOUT_SECONDS,
)
from persona_bot.core.database import Database
from persona_bot.services.genai_client import generate_content, generate_json_content
from persona_bot.utils.anniversaries import parse_anniversary
from persona_bot.utils.normalization import normalize_memory_category
from persona_bot.utils.timeutil import as_utc, parse_iso_datetime, utc_now, utc_now_iso
from persona_bot.services.prompting import build_memory_compaction_prompt, build_router_prompt
from persona_bot.utils.storage import (
    read_json_response,
    load_json_file,
    write_json_file,
)

logger = logging.getLogger(__name__)

# projects.json のユーザー別スキーマ識別子。users 辞書がある場合は識別子欠落時も保護する。
_PROJECTS_SCHEMA = "per_user_v1"

# 記憶参照が明らかに不要な短い定型発言（あいさつ・相槌など）。ルーターの LLM 呼び出しを
# 省いて category="none" を即決するための簡易判定に使う（API 呼び出し削減）。
_TRIVIAL_MESSAGE_PATTERN = re.compile(
    r"^(おはよう|おはよ|こんにちは|こんばんは|やあ|やっほー?|ハロー|hello|hi|hey"
    r"|ありがと(う|ね)?|thanks?|thx|了解|りょうかい|おk|ok|おけ|うん|はい|いいえ"
    r"|なるほど|そうだね|そっか|わかった|分かった|おやすみ|おつかれ?|お疲れ様?でした?"
    r"|バイバイ|またね|乙|w+|笑|草)[！!。、.?？~〜ー\s]*$",
    re.IGNORECASE,
)


def default_profile_memory() -> dict[str, Any]:
    return {}


def default_projects_memory() -> dict[str, Any]:
    """projects.json の空の初期構造（ユーザー別スキーマ）。"""
    return {"_schema": _PROJECTS_SCHEMA, "users": {}}


def _anniversaries_by_label(profile: dict[str, Any]) -> dict[str, Any]:
    """生成待機中の更新を比較するため、有効な記念日をラベルで束ねる。"""
    entries = profile.get(ANNIVERSARY_PROFILE_KEY)
    result = {}
    if isinstance(entries, list):
        for entry in entries:
            anniversary = parse_anniversary(entry)
            if anniversary is not None:
                result[anniversary[0]] = entry
    return result


def merge_anniversary_profile(
    snapshot: dict[str, Any], compacted: dict[str, Any], *, previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """入力を変更せず既存の記念日を引き継ぎ、生成待機中の編集を優先する。"""
    result = copy.deepcopy(compacted)
    incoming = compacted.get(ANNIVERSARY_PROFILE_KEY)
    if previous_snapshot is not None and isinstance(incoming, list):
        # モデルは入力時の古い日付も出力するため、最新値との単純な統合では訂正が戻る。
        # 未変更ラベルへのモデルの訂正は採用し、同時に編集されたラベルだけを保護する。
        previous = _anniversaries_by_label(previous_snapshot)
        current = _anniversaries_by_label(snapshot)
        changed_labels = {
            label for label in previous.keys() | current.keys()
            if previous.get(label) != current.get(label)
        }
        if not isinstance(snapshot.get(ANNIVERSARY_PROFILE_KEY), list) and (
            snapshot.get(ANNIVERSARY_PROFILE_KEY) != previous_snapshot.get(ANNIVERSARY_PROFILE_KEY)
            or (ANNIVERSARY_PROFILE_KEY in snapshot) != (ANNIVERSARY_PROFILE_KEY in previous_snapshot)
        ):
            # キー全体の削除や手書き途中の値も、古い生成結果で元へ戻さない。
            incoming = None
        else:
            incoming = [
                entry for entry in incoming
                if (anniversary := parse_anniversary(entry)) is None or anniversary[0] not in changed_labels
            ]
    if not isinstance(incoming, list):
        if ANNIVERSARY_PROFILE_KEY in snapshot:
            result[ANNIVERSARY_PROFILE_KEY] = copy.deepcopy(snapshot[ANNIVERSARY_PROFILE_KEY])
        else:
            result.pop(ANNIVERSARY_PROFILE_KEY, None)
        return result

    # LLMだけでは削除できなくなるが、省略で失う記念日は戻せないため保持を優先する。
    # 既存の不正な要素も消さず、読取側の共通判定で読み飛ばす。新しい不正な要素は採用しない。
    existing = snapshot.get(ANNIVERSARY_PROFILE_KEY)
    entries: list[Any] = []
    positions: dict[str, int] = {}
    for source, preserve_invalid in ((existing, True), (incoming, False)):
        if not isinstance(source, list):
            continue
        for entry in source:
            anniversary = parse_anniversary(entry)
            if anniversary is None:
                if preserve_invalid:
                    entries.append(copy.deepcopy(entry))
                continue
            label, _, _ = anniversary
            if label in positions:
                entries[positions[label]] = copy.deepcopy(entry)
            else:
                positions[label] = len(entries)
                entries.append(copy.deepcopy(entry))
    result[ANNIVERSARY_PROFILE_KEY] = entries
    return result


class MemoryService:
    def __init__(self, state: BotState, genai_client: Any, db: Database) -> None:
        self._state = state
        self._genai_client = genai_client
        self._db = db
        self.io_lock = asyncio.Lock()
        self.flush_lock = asyncio.Lock()
        # 記憶圧縮の連続失敗回数をユーザー別に数える（あるユーザーの失敗が
        # 他ユーザーの pending_log クリア判定に影響しないようにする）。
        self._compact_fail_count: dict[str, int] = {}
        # 前回の巡回で最後に確認したユーザーID（#380）。次の巡回はその次のユーザーから始める。
        # 一覧は常に昇順なので、毎回先頭から始めると、先頭のユーザーが例外を出し続けたときに
        # 連続失敗の上限で毎回そこで打ち切られ、後続のユーザーが永久に見送られる。
        # 再起動で先頭へ戻ってよい一時状態のため永続化しない。flush_lock の下でだけ更新する。
        self._flush_cursor: str | None = None

    def ensure_storage(self) -> None:
        # プロフィール記憶は DB の profile テーブルが本体（ユーザー別）。profile.json は
        # 「手書き追加用の受け皿」で、起動時に DB へ吸収され {} に戻される。
        # projects.json は LLM が構造を決める自由形式 JSON をユーザー別に束ねたファイル。
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        if not PROFILE_MEMORY_PATH.exists():
            write_json_file(PROFILE_MEMORY_PATH, default_profile_memory())
        if not PROJECTS_MEMORY_PATH.exists():
            write_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())

    # ---- projects.json（ユーザー別・自由形式JSON） ---------------------------

    def _load_projects_wrapper(self) -> dict[str, Any]:
        """projects.json をユーザー別スキーマの {"_schema":.., "users":{uid:{...}}} で返す。

        旧・全体共有形式（トップレベルがそのまま自由形式JSON）を読み込んだ場合は、
        その中身を丸ごと owner のプロジェクト記憶として引き継ぐ形へ移行し、
        ファイルへ書き戻す（冪等。移行済みなら何もしない）。
        """
        data = load_json_file(PROJECTS_MEMORY_PATH, default_projects_memory())
        if not isinstance(data, dict):
            data = default_projects_memory()

        if data.get("_schema") != _PROJECTS_SCHEMA:
            if isinstance(data.get("users"), dict):
                # 手編集などで識別子だけ欠けても、既存ユーザーの記憶を捨てない。
                data["_schema"] = _PROJECTS_SCHEMA
            else:
                migrated = default_projects_memory()
                # 旧・全体共有形式の中身を owner の記憶として引き継ぐ。
                # 値がすべて空（初期シードのみ）の場合は引き継がない。
                legacy = {k: v for k, v in data.items() if k not in ("_schema", "users")}
                has_content = any(value for value in legacy.values())
                if has_content and LEGACY_MEMORY_OWNER_ID:
                    migrated["users"][LEGACY_MEMORY_OWNER_ID] = legacy
                data = migrated
            write_json_file(PROJECTS_MEMORY_PATH, data)
            logger.info("記憶: projects.json をユーザー別構造へ移行しました。")

        if not isinstance(data.get("users"), dict):
            data["users"] = {}
        return data

    def _user_projects(self, wrapper: dict[str, Any], user_id: str) -> dict[str, Any]:
        users = wrapper.get("users", {})
        value = users.get(str(user_id), {})
        return value if isinstance(value, dict) else {}

    # ---- プロンプトへ差し込む記憶コンテキスト --------------------------------

    async def build_contexts(
        self, category: str, user_id: str, limit: int = DOCUMENT_MEMORY_CONTEXT_LIMIT,
        *, pending_summaries: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        """同じスナップショットから通常記憶と文書記憶を作り、JSONを1回だけ読む。"""
        async with self.io_lock:
            return await run_in_thread(self._build_contexts, category, str(user_id), limit, pending_summaries)

    def _build_contexts(
        self, category: str, user_id: str, limit: int, pending_summaries: list[dict[str, Any]] | None = None,
    ) -> tuple[str, str]:
        self.ensure_storage()
        wrapper = self._load_projects_wrapper()
        projects = self._user_projects(wrapper, user_id)
        context = ""
        if category == "profile":
            context = json.dumps(self._db.profile_load(user_id), ensure_ascii=False, indent=2)
        elif category == "projects":
            context = json.dumps(
                {key: value for key, value in projects.items() if key != "document_summaries"},
                ensure_ascii=False, indent=2,
            )
        summaries = projects.get("document_summaries", [])
        if pending_summaries:
            # 今回の文書は応答の安全確認が済むまで保存せず、この依頼だけの文脈へ足す。
            summaries = self._merge_document_summaries(summaries, pending_summaries)
        lines = []
        if isinstance(summaries, list) and limit > 0:
            for summary in summaries[-limit:]:
                if not isinstance(summary, dict):
                    continue
                raw_name = summary.get("document_name")
                name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "unknown"
                raw_text = summary.get("summary")
                text = raw_text.strip() if isinstance(raw_text, str) else ""
                if text:
                    lines.append(f"- {name}: {text}")
        return context, "\n".join(lines)

    async def build_context(self, category: str, user_id: str) -> str:
        context, _ = await self.build_contexts(category, user_id)
        return context

    async def build_document_memory_context(
        self, user_id: str, limit: int = DOCUMENT_MEMORY_CONTEXT_LIMIT,
    ) -> str:
        _, documents = await self.build_contexts("none", user_id, limit)
        return documents

    async def append_pending_log(self, user_id: str, question: str, response_text: str) -> None:
        timestamp = utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")
        async with self.io_lock:
            await run_in_thread(
                self._db.pending_append, user_id, timestamp, question, response_text
            )

    @staticmethod
    def _looks_memory_irrelevant(question: str) -> bool:
        """長期記憶の参照が明らかに不要そうな発言か（あいさつ・相槌・短文）を簡易判定する。

        True の場合、ルーターの LLM 呼び出しを省いて category="none" とする。
        取りこぼしても route_category は失敗時 none にフォールバックする設計なので、
        安全側（少し多めに none 判定）に倒しても実害は小さい。
        """
        text = question.strip()
        if not text:
            return True
        if _TRIVIAL_MESSAGE_PATTERN.match(text):
            return True
        return False

    async def route_category(self, question: str) -> str:
        if not question.strip():
            return "none"

        # 記憶参照が不要そうな短い定型発言は LLM を呼ばずに none で即決する（API削減）。
        if self._looks_memory_irrelevant(question):
            return "none"

        try:
            router_prompt = build_router_prompt(question)
            response = await generate_json_content(
                generate_content, self._genai_client, model=ROUTER_MODEL_NAME, contents=router_prompt,
                timeout=ROUTER_TIMEOUT_SECONDS,
            )
            _, _, parsed = read_json_response(response, required_keys=("category",))
            return normalize_memory_category((parsed or {}).get("category"))
        except Exception as error:
            logger.error("ルーティング判定に失敗しました。", exc_info=error)
        return "none"

    async def store_document_summary(self, user_id: str, summary_data: dict[str, Any]) -> None:
        summary_data = {**summary_data, "created_at": utc_now_iso()}
        async with self.io_lock:
            await run_in_thread(self._store_document_summary, str(user_id), summary_data)

    def _store_document_summary(self, user_id: str, summary_data: dict[str, Any]) -> None:
        self._store_document_summaries(user_id, [summary_data])

    async def store_document_summaries(self, user_id: str, summaries: list[dict[str, Any]]) -> None:
        """同じ投稿の要約をまとめて保存する。後続添付の失敗で先行要約だけを残さない。"""
        prepared = [{**summary, "created_at": utc_now_iso()} for summary in summaries]
        async with self.io_lock:
            await run_in_thread(self._store_document_summaries, str(user_id), prepared)

    def _store_document_summaries(self, user_id: str, incoming: list[dict[str, Any]]) -> None:
        self.ensure_storage()
        wrapper = self._load_projects_wrapper()
        user_projects = wrapper["users"].setdefault(user_id, {})
        if not isinstance(user_projects, dict):
            user_projects = {}
            wrapper["users"][user_id] = user_projects
        summaries = self._merge_document_summaries(user_projects.get("document_summaries", []), incoming)
        user_projects["document_summaries"] = summaries[-DOCUMENT_SUMMARY_LIMIT:]
        write_json_file(PROJECTS_MEMORY_PATH, wrapper)

    @staticmethod
    def _merge_document_summaries(summaries: Any, incoming: list[dict[str, Any]]) -> list:
        """保存前の文脈と保存時で、同名・同内容の置換規則を揃える。"""
        if not isinstance(summaries, list):
            summaries = []
        else:
            summaries = list(summaries)
        for summary_data in incoming:
            # 再試行で同じファイルを再解析しても、同名・同内容の要約を重複させない。
            digest = summary_data.get("document_hash")
            if digest:
                summaries = [
                    old for old in summaries
                    if not isinstance(old, dict) or (old.get("document_hash"), old.get("document_name"))
                    != (digest, summary_data.get("document_name"))
                ]
            summaries.append(summary_data)
        return summaries

    # ---- 記憶圧縮（ユーザー単位） -------------------------------------------

    async def flush_if_needed(self, force: bool = False) -> None:
        if not force and self.flush_lock.locked():
            return

        # ロックの役割分担:
        #   flush_lock … フラッシュ処理そのものが同時に二重に走らないようにする（全体を保持）。
        #   io_lock    … ファイルI/O・DB書き込みの直列化だけに使う（読み書きのたびに短く取る）。
        # io_lock を最後まで保持したままにすると、Gemini 呼び出し
        # （最大 MEMORY_FLUSH_MAX_USERS_PER_RUN 人 × MEMORY_COMPACT_TIMEOUT_SECONDS）の間、
        # 無関係な別ユーザーの append_pending_log までブロックされてしまう。
        # 取得順序は必ず flush_lock → io_lock（逆順で取る箇所を作らないこと）。
        async with self.flush_lock:
            async with self.io_lock:
                user_ids = await run_in_thread(self._pending_user_ids)
            if not user_ids:
                return

            # 他人の会話ログを同じ Gemini 呼び出しに混ぜないため、ユーザーごとに
            # 個別に圧縮する。1 回のフラッシュで処理する人数は上限で区切り、
            # 一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。
            processed = 0
            consecutive_errors = 0
            for user_id in self._order_from_cursor(user_ids):
                if processed >= MEMORY_FLUSH_MAX_USERS_PER_RUN:
                    break
                counted = False
                try:
                    async with self.io_lock:
                        entries = await run_in_thread(self._db.pending_fetch_entries, str(user_id))
                    if not entries:
                        consecutive_errors = 0
                        continue
                    # _is_flush_required は同期の純粋判定なのでロックは要らない。
                    if not force and not self._is_flush_required(len(entries), entries[-1].get("created_at")):
                        consecutive_errors = 0
                        continue
                    processed += 1
                    counted = True
                    await self._flush_user(str(user_id), entries)
                except Exception as error:
                    # 読込・保存の失敗を生成失敗回数へ加算せず、原データを残して再試行する。
                    if not counted:
                        processed += 1
                    consecutive_errors += 1
                    logger.error("記憶圧縮: ユーザー処理に失敗しました。ログを保持します。", exc_info=error)
                    if consecutive_errors >= MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS:
                        logger.warning("記憶圧縮: 読込・保存の失敗が続いたため、この巡回を終了し、次回に再試行します。", exc_info=error)
                        break
                else:
                    consecutive_errors = 0
                finally:
                    # 成功・見送り・失敗・上限による中断のどれでも、確認したユーザーの次から再開する。
                    # 失敗したユーザーの原データと失敗回数は残るので、一巡した後に再試行される。
                    self._flush_cursor = str(user_id)

    def _order_from_cursor(self, user_ids: list[str]) -> list[str]:
        """前回の巡回の続きから始まる順にユーザーIDを並べる（#380）。

        全員を一度ずつ含む1周分の並びで、先頭の人数を数える回数・連続失敗の上限は
        呼び出し側がそのまま適用する。失敗が続くユーザーを先頭に固定しないことと、
        共有ストレージの障害時に1回の巡回での試行回数を増やさないことを両立する。
        """
        ordered = sorted(str(user_id) for user_id in user_ids)
        if self._flush_cursor is None:
            return ordered
        start = bisect.bisect_right(ordered, self._flush_cursor)
        return ordered[start:] + ordered[:start]

    def _pending_user_ids(self) -> list[str]:
        """同じ io_lock 内の初期化と取得を、1回のワーカー呼出で完了する。"""
        self.ensure_storage()
        return self._db.pending_distinct_user_ids()

    def _load_memory_snapshot(self, user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """プロフィールとプロジェクトを同じ io_lock 内でまとめて読み込む。"""
        return self._db.profile_load(user_id), self._load_projects_wrapper()

    async def _flush_user(self, user_id: str, entries: list[dict[str, Any]]) -> None:
        """1 ユーザー分の pending_log を profile / projects へ圧縮統合する。

        入力プロンプトにはこのユーザーの会話ログ・記憶だけを含める
        （他ユーザーの情報は一切混ぜない）。フェイルセーフ設計で、パース失敗時は
        pending_log を保持（リトライ）するが、連続失敗やセーフティブロック時は
        無限ループを避けるためそのユーザーの pending_log をクリアする。

        **Gemini 呼び出し（`_compact_memory`）の間は io_lock を手放す**（他ユーザーの
        会話ログ追記を数十秒ブロックしないため）。そのぶん「待っている間に増えたもの」を
        巻き込まないよう、次の3点を守ること:
          - pending_log の削除は圧縮に使った行の最大 id までに限定する。
          - 記念日は保存直前のプロフィールと統合し、待機中の追加や訂正を引き継ぐ。
          - projects.json は書き込み直前に読み直し、文書要約はその最新の一覧を引き継ぐ。

        **文書要約（document_summaries）は LLM へ渡さない。** 圧縮プロンプトは
        「不要な古い情報は削除」と指示するため、渡すと LLM の判断ひとつで要約が
        消えたり別のキーへ書き換えられたりして、元に戻せなくなる。要約は
        `store_document_summary` が件数の上限まで管理しているので、圧縮では触らない。
        """
        # 圧縮対象に含まれた行の最大 id。削除範囲はここまでに限定する
        # （Gemini を待っている間に追記された新しい会話ログを道連れに消さないため）。
        max_entry_id = self._max_entry_id(entries)
        if max_entry_id <= 0:
            # 削除範囲を確定できないログへ生成を繰り返さず、原データと失敗回数を残す。
            raise ValueError("記憶圧縮: 対象ログの有効な行IDを取得できませんでした。")
        pending_text = self._format_pending_entries(entries)

        async with self.io_lock:
            profile_snapshot, wrapper = await run_in_thread(self._load_memory_snapshot, user_id)
        projects_snapshot = {
            key: value
            for key, value in self._user_projects(wrapper, user_id).items()
            if key != "document_summaries"
        }
        profile_text = json.dumps(profile_snapshot, ensure_ascii=False, indent=2)
        projects_text = json.dumps(projects_snapshot, ensure_ascii=False, indent=2)

        try:
            profile_data, projects_data = await self._compact_memory(
                pending_text,
                profile_text,
                projects_text,
            )
        except _PromptBlockedError as error:
            # pending_log に不適切な内容が含まれており永久に処理できない
            # → このユーザーの pending_log をクリアして無限ループを防止
            logger.warning("記憶圧縮: プロンプトがブロックされました。", exc_info=error)
            logger.warning("記憶圧縮: 処理不能な pending_log をクリアします。")
            async with self.io_lock:
                await run_in_thread(self._clear_compact_pending, user_id, max_entry_id)
            return
        except Exception as error:
            await self._register_compact_failure(
                user_id, error, max_entry_id
            )
            return

        if profile_data is None or projects_data is None:
            await self._register_compact_failure(
                user_id,
                None,
                max_entry_id,
            )
            return

        async with self.io_lock:
            await run_in_thread(
                self._save_compacted_memory, user_id, profile_data, projects_data, max_entry_id, profile_snapshot,
            )

    def _clear_compact_pending(self, user_id: str, max_entry_id: int) -> None:
        """ログ消去と失敗回数のリセットを、キャンセルで分断せずに完了する。"""
        if max_entry_id <= 0:
            raise ValueError("記憶圧縮: 対象ログの有効な行IDを取得できませんでした。")
        self._db.pending_clear_upto(user_id, max_entry_id)
        # 消去が成功した場合だけ戻す。新しいログへ過去の失敗回数を持ち越さない。
        self._compact_fail_count[user_id] = 0

    def _save_compacted_memory(
        self, user_id: str, profile_data: dict[str, Any], projects_data: dict[str, Any], max_entry_id: int,
        profile_snapshot: dict[str, Any],
    ) -> None:
        """io_lock の下で全保存を完了し、途中のキャンセルでは分断しない。"""
        # 生成を待つ間に更新された記念日を、古いスナップショットで巻き戻さない。
        profile_data = merge_anniversary_profile(
            self._db.profile_load(user_id), profile_data, previous_snapshot=profile_snapshot,
        )
        # 最新の文書要約と他ユーザーの記憶を引き継ぐ。DB確定失敗時の復元用にも残す。
        wrapper = self._load_projects_wrapper()
        original = copy.deepcopy(wrapper)
        projects_saved = False

        def save_projects() -> None:
            nonlocal projects_saved
            self._save_compacted_projects(wrapper, user_id, projects_data)
            projects_saved = True

        try:
            self._db.save_compacted_memory(user_id, profile_data, max_entry_id, save_projects)
        except Exception:
            if projects_saved:
                try:
                    write_json_file(PROJECTS_MEMORY_PATH, original)
                except Exception as error:
                    logger.error("記憶圧縮: JSONの復元に失敗しました。", exc_info=error)
            raise
        # キャンセルを伝播する前も、全保存が成功した場合だけリセットする。
        self._compact_fail_count[user_id] = 0

    def _save_compacted_projects(
        self, wrapper: dict[str, Any], user_id: str, projects_data: dict[str, Any],
    ) -> None:
        """圧縮結果でこのユーザーのスライスを差し替える。文書要約は最新の一覧を引き継ぐ。

        **呼び出し側が io_lock を保持していること**（この関数はロックを取らない）。

        `wrapper` は書き込み直前に読み直したものなので、圧縮前からある要約も、
        `_compact_memory` を待っている間に `store_document_summary` が追記した要約も
        そのまま含まれる。LLM には要約を渡していないため、出力に同名のキーがあっても
        作り話として捨てる。既存の値は型が崩れていても書き換えずに残す
        （読み出し側の `_build_contexts` が不正な型を読み飛ばす）。
        """
        projects_data.pop("document_summaries", None)
        latest = self._user_projects(wrapper, user_id)
        if "document_summaries" in latest:
            projects_data["document_summaries"] = latest["document_summaries"]
        wrapper["users"][user_id] = projects_data
        write_json_file(PROJECTS_MEMORY_PATH, wrapper)

    @staticmethod
    def _max_entry_id(entries: list[dict[str, Any]]) -> int:
        """圧縮対象の pending_log 行の最大 id を返す（削除範囲の上限に使う）。

        `pending_fetch_entries` は `SELECT *` なので各行に AUTOINCREMENT の id が入る。
        万一 id を読めない行しか無い場合は 0 を返す。呼出元は生成・保存・削除を
        進めずに読込エラーとして扱い、原データを保護する。
        """
        ids: list[int] = []
        for entry in entries:
            try:
                ids.append(int(entry["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return max(ids) if ids else 0

    @staticmethod
    def _format_pending_entries(entries: list[dict[str, Any]]) -> str:
        """DB の pending_log 行を、記憶圧縮プロンプトへ渡すテキスト形式に整形する。"""
        lines: list[str] = []
        for entry in entries:
            lines.append(f"[{entry['created_at']}] User: {entry['question']}")
            lines.append(f"[{entry['created_at']}] Bot: {entry['response']}")
        return "\n".join(lines)

    async def _register_compact_failure(
        self, user_id: str, error: BaseException | None, max_entry_id: int,
    ) -> None:
        """記憶圧縮の失敗を記録する。基本は pending_log を保持してリトライするが、
        連続失敗が上限に達したらそのユーザーの分をクリアして無限リトライを防ぐ。

        `max_entry_id` は圧縮対象だった行の最大 id。クリアはそこまでに限定する
        （Gemini 呼び出し中に追記された新しい会話ログまで消さないため）。
        """
        # 例外本文や利用者IDを事前整形せず、診断は共通formatterへ渡す。
        if error is not None:
            logger.error("記憶圧縮: 処理に失敗したため pending_log を保持します。", exc_info=error)
        else:
            logger.warning("記憶圧縮: JSONの生成またはパースに失敗したため pending_log を保持します。")
        count = self._compact_fail_count.get(user_id, 0) + 1
        self._compact_fail_count[user_id] = count
        if count >= MEMORY_COMPACT_MAX_FAILURES:
            logger.warning("記憶圧縮: 連続失敗のため pending_log をクリアします。")
            async with self.io_lock:
                await run_in_thread(self._clear_compact_pending, user_id, max_entry_id)

    def _is_flush_required(self, entry_count: int, last_activity: str | None = None) -> bool:
        if entry_count >= MEMORY_FLUSH_THRESHOLD_ENTRIES:
            return True
        # 他の利用者の会話で待機を延ばさない。再起動後も、その利用者の最新ログから判断する。
        try:
            stamp = str(last_activity or "").replace(" UTC", "+00:00")
            last = as_utc(parse_iso_datetime(stamp))
        except ValueError:
            # 旧形式など時刻不明のログも永久に残さず、次の巡回で圧縮する。
            return True
        elapsed_seconds = (utc_now() - last).total_seconds()
        return elapsed_seconds >= MEMORY_FLUSH_IDLE_SECONDS

    async def _compact_memory(
        self,
        pending_text: str,
        profile_text: str,
        projects_text: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        compactor_prompt = build_memory_compaction_prompt(
            pending_text,
            profile_text,
            projects_text,
            ANNIVERSARY_PROFILE_KEY,
        )

        response = await generate_json_content(
            generate_content, self._genai_client, model=ROUTER_MODEL_NAME, contents=compactor_prompt,
            timeout=MEMORY_COMPACT_TIMEOUT_SECONDS,
        )

        # Gemini がプロンプトをブロックした場合を検知
        reason_name, _, parsed = read_json_response(response, required_keys=("profile", "projects"))
        if reason_name is not None:
            raise _PromptBlockedError(reason_name)

        if not parsed:
            return None, None

        profile_data = parsed.get("profile")
        projects_data = parsed.get("projects")
        if not isinstance(profile_data, dict) or not isinstance(projects_data, dict):
            return None, None
        return profile_data, projects_data


class _PromptBlockedError(Exception):
    """記憶圧縮プロンプトが Gemini の安全フィルタでブロックされた場合の例外。"""
