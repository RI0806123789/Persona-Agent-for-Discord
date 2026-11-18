"""自律行動（ボットが自分からボイス/ステージチャンネルへ出て「配信」する）。

既存の自発的な会話（`auto_topic_service.py`）が「テキストチャンネルへ話題を
1つ投稿する」だけなのに対して、こちらは **ボットが自分の意志で通話に入り、
独り言を続けながら、聞きに来た人のコメントへ配信者のように応答する**。

    1. サーバーごとに「話したいゲージ」（0〜100）を持つ。無会話時間が伸びるほど
       増え、直近の会話が活発なサーバーほど早く増える（`compute_talk_desire`）。
    2. ゲージが `AUTONOMOUS_GAUGE_THRESHOLD` に達したら、ステージ優先で
       チャンネルを1つ選んで接続し、第一声（会話相手の募集）を投稿・発話する。
    3. 以後 `AUTONOMOUS_TALK_INTERVAL_MINUTES` ごとに独り言を続ける。
    4. そのチャンネル付属のチャットに届いた発言だけを「コメント」として拾い、
       配信者の距離感で応答する（`handle_comment`）。
    5. コメントが `AUTONOMOUS_IDLE_STOP_MINUTES` 途絶えたら自動終了する。

■ 設計上の約束（変更するときは理由まで含めて確認すること）

**ボットの自発発言に AI モデレーションは掛けない。** 独り言・第一声は
`_screen_speech()` で感情タグ除去と NG ワードのマスクだけを通す。
これは idea.md の明示的な方針（発言のたびに Gemini をもう1回呼ぶコストを
避ける）で、既存の `auto_topic_service._screen_auto_post` とは扱いが違う。
一方、**コメントへの応答は「ユーザーからのインプットへの応答」なので、
従来どおり `screen_bot_response` で NG マスクと AI モデレーションを通す。**

**長期記憶（profile / projects）は一切渡さない。** ボイス/ステージは共有の
場であり、特定ユーザーの私的な記憶を材料にすると本人以外にも漏れる。
材料はそのチャンネルの履歴だけにする（自発的な会話・記念日と同じ方針）。

**接続の唯一の情報源は `guild.voice_client`。** `AutonomousSession` に
VoiceClient を持たせない。持たせると、管理者にキックされた・回線が切れた
などで実態と食い違った参照が残り、切断済みのクライアントへ喋り続ける。

**セッションの状態は永続化しない。** ボイス接続自体が再起動をまたげない
ため、復元しても意味がない（`GuildSettings.autonomous_session` は
`auto_topic_channels` と同じ「実行時に自然に再構築される一時状態」）。
永続化するのは有効/無効（`autonomous_enabled`）だけ。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord

from persona_bot.core.app_state import AutonomousSession, BotState, GuildSettings

# パネルからの既存importも維持し、計算本体はDiscordを使わないモジュールへ置く。
from persona_bot.services.autonomous_metrics import (  # noqa: F401（describe_quiet_hours はパネル向けの再公開）
    compute_talk_desire,
    describe_quiet_hours,
    is_quiet_hours,
    updated_activity_samples,
)
from persona_bot.services import autonomous_stage
from persona_bot.core.background import spawn
from persona_bot.config.constants import (
    AUTONOMOUS_CLOSING_NOTICE,
    AUTONOMOUS_CLOSING_NOTICE_DISABLED,
    AUTONOMOUS_CLOSING_NOTICE_MOVED,
    AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS,
    AUTONOMOUS_CLOSING_NOTICE_QUIET,
    AUTONOMOUS_CLOSING_NOTICE_TIMEUP,
    AUTONOMOUS_COMMENT_COOLDOWN_SECONDS,
    AUTONOMOUS_GAUGE_THRESHOLD,
    AUTONOMOUS_IDLE_STOP_MINUTES,
    AUTONOMOUS_SESSION_MAX_MINUTES,
    AUTONOMOUS_TALK_INTERVAL_MINUTES,
)
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import can_bot_send_messages
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
# 配信の開始・停止は autonomous_lifecycle.py（#469）、発話とコメントへの応答は autonomous_speech.py（#415）。
from persona_bot.services.autonomous_lifecycle import AutonomousLifecycleMixin, _StartAttempt, _StopAttempt
from persona_bot.services.autonomous_speech import AutonomousSpeechMixin
from persona_bot.utils.usage_graph import UsageTracker

logger = logging.getLogger(__name__)


class AutonomousService(AutonomousLifecycleMixin, AutonomousSpeechMixin):
    """自律行動（ボイス/ステージでの配信）を管理するサービス。

    見回りと停止条件の判定をここに置き、配信の開始・停止の非同期状態機械は
    `AutonomousLifecycleMixin`（`autonomous_lifecycle.py`、#469）、発話（第一声・独り言）と
    コメントへの応答の生成・投稿・読み上げは `AutonomousSpeechMixin`（`autonomous_speech.py`、#415）に分けている。

    見回り（`run_pending_checks`）はサーバーを横断して次の3つを行う:
      ・配信していないサーバー … ゲージを見て、満タンなら配信を始める
      ・配信中のサーバー       … 停止条件の確認と、独り言の投下
      ・接続が失われたサーバー … 自分のステージ枠・接続・セッション記録の片付け

    コメントの受け取り（`mark_activity` / `is_comment_channel` /
    `handle_comment`）は `bot.py` の `on_message` から呼ばれる。
    """

    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
        *,
        is_blocked: Callable[[int], bool] | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._gemini = gemini
        self._moderator = moderator
        self._ng_filter = ng_filter
        self._usage = usage
        self._is_blocked = is_blocked
        self._starting: dict[int, _StartAttempt] = {}
        self._stopping: dict[int, _StopAttempt] = {}

    # ==========================================================================
    # on_message から呼ばれる入口
    # ==========================================================================

    def mark_activity(self, channel_id: int, guild_id: int | None) -> None:
        """人間の発言を記録する（ゲージの基準時刻と、配信の無応答タイマーの更新）。

        引数の順番は `AutoTopicService.mark_activity` / `watch_channel` と**同じ
        (channel_id, guild_id) に揃えてある**。同じ `on_message` から並べて呼ぶ
        メソッドで順番が違うと、取り違えても型が同じ（int）ため静かに誤動作する。

        メンション判定より**前**に呼ぶこと。ボット宛てでない雑談も「このサーバーで
        会話が行われている」という観測であり、ゲージはそれを見て動くため。

        更新する範囲は2つで意味が違う:
          ・ゲージの基準（`autonomous_last_activity`）… サーバー内のどのチャンネルでも更新する。
          ・配信の無応答タイマー（`session.last_human_at`）… **配信中のチャンネルの
            発言だけ**で更新する。他のテキストチャンネルの発言をコメントとして
            認めない以上、それで配信を延命させるのも筋が通らないため
            （idea.md「他のテキストチャンネルからの受け付けはコメントとして認めない」）。

        DM はボイスチャンネルを持たないため対象外。
        """
        if guild_id is None:
            return

        guild_settings = self._state.get_guild_if_ready(guild_id)
        if guild_settings is None:
            return
        now = datetime.now(timezone.utc)
        guild_settings.autonomous_last_activity = now

        guild_settings.autonomous_recent_activity = updated_activity_samples(
            guild_settings.autonomous_recent_activity, now=now,
        )

        session = guild_settings.autonomous_session
        if session is not None and session.channel_id == channel_id:
            session.last_human_at = now

    def is_comment_channel(self, channel_id: int, guild_id: int | None) -> bool:
        """そのチャンネルが「配信中のコメント欄」かどうかを返す。

        True のとき `bot.py` はメンション不要でその発言を拾い、
        `handle_comment` へ回す（傍観者は配信のチャットへ気軽に書き込むため、
        いちいちメンションを求めるのは配信の体裁に合わない）。

        引数の順番は `mark_activity` および `AutoTopicService` 側と同じ
        (channel_id, guild_id) に揃えてある。
        """
        if guild_id is None:
            return False
        guild_settings = self._state.get_guild_if_ready(guild_id)
        session = guild_settings.autonomous_session if guild_settings is not None else None
        return session is not None and session.channel_id == channel_id

    def claim_comment_slot(self, channel_id: int, guild_id: int | None) -> AutonomousSession | None:
        """入力モデレーション前に枠を確保し、その配信を予約の証明として返す。

        判定と時刻更新を待機なしで済ませ、後段では同じ配信の枠を再確保しない。
        """
        if guild_id is None:
            return None
        guild_settings = self._state.get_guild_if_ready(guild_id)
        session = guild_settings.autonomous_session if guild_settings is not None else None
        if session is None or session.channel_id != channel_id:
            return None
        return session if self._claim_comment_slot(session) else None

    def _claim_comment_slot(self, session: AutonomousSession) -> bool:
        """入力AI判定の前に受付枠を確保し、同時に受付時刻を記録する。

        配信中のチャンネルではメンション不要でコメントを拾うため、何もしないと
        「そのチャンネルの全メッセージ × (モデレーション + 応答生成)」で Gemini 呼び出しが
        発散する（通常のメンション必須モードでは、メンションが自然な流量制限だった）。
        直前の受付から `AUTONOMOUS_COMMENT_COOLDOWN_SECONDS` 未満のコメントは見送る。
        これは配信単位の制限で、同じユーザーの連投にも適用される。
        入力AIで拒否された場合も枠は消費するが、受付前のローカルNG判定では消費しない。

        **判定と時刻の記録を1つのメソッドにまとめてあるのは、間に `await` を
        挟ませないため。** 挟むと同時に届いた2件が両方とも通り、クールダウンが効かない。
        そのためこのメソッドは同期関数のままにすること。

        見送ったコメントも、無応答タイマー（`last_human_at`）は `mark_activity` が
        既に更新済みなので、配信が早く終わることはない。
        """
        if AUTONOMOUS_COMMENT_COOLDOWN_SECONDS <= 0:
            return True  # 制限なし（全コメントに応答する）

        now = datetime.now(timezone.utc)
        previous = session.last_comment_reply_at
        if previous is not None:
            elapsed = (now - previous).total_seconds()
            if elapsed < AUTONOMOUS_COMMENT_COOLDOWN_SECONDS:
                logger.info("自律行動: コメントが続いているため、この1件は見送ります（前回の応答から %.1f 秒）。", elapsed)
                return False

        session.last_comment_reply_at = now
        # 応答を作っている最中に見回りが独り言を投下すると、返事と独り言がぶつかる。
        # 生成に入る前から発話時刻も進めて、独り言の間隔判定に引っかからせる。
        session.last_spoke_at = now
        return True

    # ==========================================================================
    # 見回り（bot.py の autonomous_loop から1分ごとに呼ばれる）
    # ==========================================================================

    async def run_pending_checks(self) -> None:
        """全サーバーを見回り、配信の開始・継続・終了を判断する。

        1サーバーの失敗で他のサーバーの見回りを止めないよう、例外はここで受ける。
        DM（guild_id が None）はボイスチャンネルを持たないため対象外。
        """
        for guild_id, guild_settings in list(self._state.iter_all_guild_settings()):
            if guild_id is None:
                continue
            try:
                await self._check_guild(guild_id, guild_settings)
            except Exception as error:
                logger.error("自律行動: 見回りに失敗しました。", exc_info=error)

    async def _check_guild(self, guild_id: int, guild_settings: GuildSettings) -> None:
        guild = self._client.get_guild(guild_id)
        if guild is None:
            # サーバーから追い出された・キャッシュから引けなくなった場合。
            # セッション記録を残したままにすると is_comment_channel が真を返し続け、
            # さらに自発会話の競合抑止（auto_topic_service）も効いたままになって、
            # そのサーバーの話題提供が二度と動かなくなる。ここで片付ける。
            if guild_settings.autonomous_session is not None:
                logger.warning("自律行動: サーバーを参照できないため配信の記録を片付けます。")
                self._clear_session(guild_settings)
            return

        # 配信中なら、続けるか終えるかの判断が先（機能を無効化された場合も
        # ここで拾って後片付けする必要があるため、enabled の判定より前に置く）。
        if guild_settings.autonomous_session is not None:
            await self._tend_session(guild, guild_settings)
            return

        if not guild_settings.autonomous_enabled:
            return
        if is_quiet_hours():
            return
        # /join で人が呼んだ接続を横取りしない（読み上げ用に呼ばれている最中かもしれない）。
        if guild.voice_client is not None:
            return
        # 直前の開始が失敗していれば、その待ち時間が明けるまで試さない。
        # 失敗してもゲージは満タンのままなので、この歯止めが無いと毎分ずっと
        # 接続→失敗→切断を繰り返す（_record_start_failure の説明を参照）。
        retry_at = guild_settings.autonomous_start_retry_at
        if retry_at is not None and datetime.now(timezone.utc) < retry_at:
            return
        if compute_talk_desire(guild_settings) < AUTONOMOUS_GAUGE_THRESHOLD:
            return

        await self._start_session(guild, guild_settings)

    async def _tend_session(self, guild: discord.Guild, guild_settings: GuildSettings) -> None:
        """配信中のサーバーの面倒を見る（停止条件の確認 → 独り言の投下）。"""
        session = guild_settings.autonomous_session
        if session is None:
            return

        # 実態（接続）が先。管理者に切断された・回線が切れた場合はセッションの
        # 記録だけが残るので、喋ろうとする前に片付ける。
        voice_client = guild.voice_client
        if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
            await self.stop_session(guild.id, reason="ボイス接続が失われた")
            return

        # 別のチャンネルへ移された配信を畳む。通常は voice_guard が移動の直後に
        # handle_external_move から畳むので、ここはイベントを取りこぼした場合などの受け皿。
        if await self._stop_if_moved(guild, session):
            return

        channel = self._client.get_channel(session.channel_id)
        bot_member = guild.me
        # キャッシュが無いだけでは権限を失ったと判断しない。従来の無効化・
        # 時間制限などの停止判定は続け、次の見回りで権限を確かめ直す。
        if channel is not None and bot_member is not None:
            missing = autonomous_stage.missing_session_permissions(channel, bot_member)
            if missing:
                logger.warning("自律行動: 配信の継続に必要な権限・状態がありません（不足: %s）。", "、".join(missing))
                # 発言できなくてもチャットへは終了理由を残す。閲覧・送信が
                # 許可されていない場合は、送れないと分かっている投稿を試さない。
                notice = AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS if can_bot_send_messages(channel, bot_member) else None
                await self.stop_session(guild.id, reason="チャンネルの権限不足", notice=notice)
                return

        now = datetime.now(timezone.utc)
        if not guild_settings.autonomous_enabled:
            # 管理者がパネルから止めた場合。パネルの確認文は操作した本人にしか
            # 見えない（エフェメラル）ため、聞いていた人向けにひと言残してから抜ける。
            await self.stop_session(
                guild.id, reason="設定が無効化されました", notice=AUTONOMOUS_CLOSING_NOTICE_DISABLED
            )
            return
        if is_quiet_hours():
            await self.stop_session(
                guild.id, reason="時間帯制限（深夜）", notice=AUTONOMOUS_CLOSING_NOTICE_QUIET
            )
            return
        if now - session.started_at >= timedelta(minutes=AUTONOMOUS_SESSION_MAX_MINUTES):
            await self.stop_session(
                guild.id, reason="最長時間に到達", notice=AUTONOMOUS_CLOSING_NOTICE_TIMEUP
            )
            return
        if now - session.last_human_at >= timedelta(minutes=AUTONOMOUS_IDLE_STOP_MINUTES):
            await self.stop_session(
                guild.id, reason="コメントが途絶えた", notice=AUTONOMOUS_CLOSING_NOTICE
            )
            return

        if session.busy:
            return
        if now - session.last_spoke_at < timedelta(minutes=AUTONOMOUS_TALK_INTERVAL_MINUTES):
            return

        # 生成と読み上げは数十秒かかりうる。見回りループの中で待つと他のサーバーの
        # 停止判定まで遅れるため、裏へ逃がす（多重起動は busy フラグで防ぐ）。
        session.busy = True
        spawn(self._speak_monologue(guild, guild_settings, session), label="自律行動の独り言")

    async def _stop_if_moved(self, guild: discord.Guild, session: AutonomousSession) -> bool:
        """接続が配信のチャンネルから別のチャンネルへ移されていれば、配信を畳む。

        管理者にドラッグで別のチャンネルへ移されることがある。接続は生きたままなので
        接続の有無だけでは気づけず、放置すると「テキストは移動前のチャンネルへ投稿し、
        音声だけ移動後のチャンネルで流れる」というちぐはぐな状態になる
        （コメントの受付口も移動前のままになる）。追従はしない —— セッションは
        開いたステージ枠を channel_id で覚えているため、行き先を差し替えると
        元の枠を閉じられなくなる。素直に畳んで、必要ならまた出直す。
        """
        current_channel = getattr(guild.voice_client, "channel", None)
        if current_channel is None or current_channel.id == session.channel_id:
            return False
        await self.stop_session(
            guild.id, reason="別のチャンネルへ移動された", notice=AUTONOMOUS_CLOSING_NOTICE_MOVED
        )
        return True

    def manages_connection(self, guild_id: int) -> bool:
        """このサーバーのボイス接続を配信（開始待機を含む）が使っているか。

        voice_guard が、移された接続を「元へ戻す」か「配信側に畳ませる」かを決めるのに使う。
        """
        if guild_id in self._starting:
            return True
        guild_settings = self._state.guild_settings.get(guild_id)
        return guild_settings is not None and guild_settings.autonomous_session is not None

    async def handle_external_move(self, guild: discord.Guild) -> None:
        """外部から別のチャンネルへ移された配信を、毎分の見回りを待たずに畳む（#171）。

        開始待機中は何もしない。開始処理自身の照合（_can_finish_start）が移動に気づいて
        接続ごと片付け、再試行の待ち時間も記録する（ここで停止要求を出すと「利用者の停止」
        扱いになって待ち時間が入らず、毎分の再挑戦と移動が繰り返されうる）。
        """
        if guild.id in self._starting:
            return
        guild_settings = self._state.guild_settings.get(guild.id)
        session = guild_settings.autonomous_session if guild_settings is not None else None
        if session is None:
            return
        await self._stop_if_moved(guild, session)
