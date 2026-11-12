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
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord

from persona_bot.core.app_state import AutonomousSession, BotState, GuildSettings

# パネルからの既存importも維持し、計算本体はDiscordを使わないモジュールへ置く。
from persona_bot.services.autonomous_metrics import (
    compute_talk_desire,
    describe_quiet_hours,
    is_quiet_hours,
    start_retry_wait_minutes,
    updated_activity_samples,
)
from persona_bot.services import autonomous_stage
from persona_bot.core.background import spawn, wait_for_cleanup, wait_for_task
from persona_bot.config.constants import (
    AUTONOMOUS_CLOSING_NOTICE,
    AUTONOMOUS_CLOSING_NOTICE_DISABLED,
    AUTONOMOUS_CLOSING_NOTICE_MOVED,
    AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS,
    AUTONOMOUS_CLOSING_NOTICE_QUIET,
    AUTONOMOUS_CLOSING_NOTICE_TIMEUP,
    AUTONOMOUS_COMMENT_COOLDOWN_SECONDS,
    AUTONOMOUS_CONNECT_TIMEOUT_SECONDS,
    AUTONOMOUS_GAUGE_THRESHOLD,
    AUTONOMOUS_IDLE_STOP_MINUTES,
    AUTONOMOUS_SESSION_MAX_MINUTES,
    AUTONOMOUS_TALK_INTERVAL_MINUTES,
)
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import can_bot_send_messages, send_long_message
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
# 発話（第一声・独り言）とコメントへの応答は autonomous_speech.py（#415）。
from persona_bot.services.autonomous_speech import _NO_MENTIONS, AutonomousSpeechMixin
from persona_bot.utils.usage_graph import UsageTracker

logger = logging.getLogger(__name__)

@dataclass
class _StartAttempt:
    """開始待機中の停止要求。接続そのものは保持せず、同時開始も抑止する。"""

    stopped: bool = False
    task: asyncio.Task | None = None
    finished: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _StopAttempt:
    """終了案内の送信者と、所有枠・接続の共通回収タスクを保持する。"""

    owner: asyncio.Task | None
    task: asyncio.Task | None = None
    notice_finished: asyncio.Event = field(default_factory=asyncio.Event)
    channel: Any = None


class AutonomousService(AutonomousSpeechMixin):
    """自律行動（ボイス/ステージでの配信）を管理するサービス。

    開始・停止の非同期状態機械と見回りをここに置き、発話（第一声・独り言）とコメントへの
    応答の生成・投稿・読み上げは `AutonomousSpeechMixin`（`autonomous_speech.py`）に分けている（#415）。

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

    # ==========================================================================
    # 配信の開始・終了
    # ==========================================================================

    async def _start_session(self, guild: discord.Guild, guild_settings: GuildSettings) -> None:
        """開始要求を登録し、未登録セッションへの停止要求と多重開始を扱う。"""
        if guild.id in self._starting or guild_settings.autonomous_session is not None:
            return
        attempt = _StartAttempt(task=asyncio.current_task())
        self._starting[guild.id] = attempt
        try:
            await self._connect_session(guild, guild_settings, attempt)
        finally:
            self._starting.pop(guild.id, None)
            attempt.finished.set()

    async def _connect_session(
        self, guild: discord.Guild, guild_settings: GuildSettings, attempt: _StartAttempt,
    ) -> None:
        """チャンネルを1つ選んで接続し、配信を開始する。

        接続異常では再試行を抑止する。利用者の停止・無効化による中断は失敗にしない。
        """
        # 配信先の指定はサービスが BotState を読まずに済むよう、設定の値を渡す。
        channel = autonomous_stage.pick_channel(
            guild,
            stage_channel_id=guild_settings.autonomous_stage_channel_id,
            voice_channel_id=guild_settings.autonomous_voice_channel_id,
        )
        if channel is None:
            # 出ていける先が無いサーバー（権限不足・ボイスチャンネルが無いなど）。
            # ここは API を1回も叩かず、チャンネル一覧の権限を見るだけなので
            # 待ち時間を入れない（チャンネルが作られたらすぐ出ていけるようにする）。
            return

        is_stage = isinstance(channel, discord.StageChannel)
        voice_client = None

        def create_voice_client(client, target_channel):
            # connect()が戻る前にキャンセルされても、この試行が作った接続だけを片付ける。
            nonlocal voice_client
            voice_client = discord.VoiceClient(client, target_channel)
            return voice_client

        try:
            voice_client = await channel.connect(
                timeout=AUTONOMOUS_CONNECT_TIMEOUT_SECONDS, cls=create_voice_client,
            )
        except asyncio.CancelledError:
            if voice_client is not None:
                await wait_for_cleanup(
                    autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                    label="自律行動の接続キャンセルの切断",
                )
            raise
        except Exception as error:
            if isinstance(error, discord.Forbidden):
                logger.warning("自律行動: 配信先への接続を拒否されました（権限不足）。", exc_info=error)
            elif isinstance(error, asyncio.TimeoutError):
                logger.warning("自律行動: 配信先への接続がタイムアウトしました。", exc_info=error)
            else:
                logger.error("自律行動: 配信先への接続に失敗しました。", exc_info=error)
            if voice_client is not None:
                await wait_for_cleanup(
                    autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                    label="自律行動の接続失敗の切断",
                )
            self._record_interrupted_start(guild_settings, attempt)
            return

        # 接続待機中の停止・無効化・静音時間への突入や、接続の差し替えを取り直す。
        if not self._can_finish_start(guild, guild_settings, attempt, voice_client, channel.id):
            if voice_client is not None:
                await wait_for_cleanup(
                    autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                    label="自律行動の接続後の開始中止",
                )
            self._record_interrupted_start(guild_settings, attempt)
            return

        stage_instance_id = None
        if is_stage:
            try:
                stage_instance_id = await autonomous_stage.prepare_stage(guild, channel)
            except asyncio.CancelledError:
                if voice_client is not None:
                    await wait_for_cleanup(
                        autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                        label="自律行動の開始キャンセルの切断",
                    )
                raise
            except Exception as error:
                # 発言者になれないステージで喋っても誰にも聞こえないため、
                # 中途半端に居座らず接続ごと諦める。
                logger.error("自律行動: ステージの準備に失敗しました。", exc_info=error)
                if voice_client is not None:
                    await wait_for_cleanup(
                        autonomous_stage.disconnect(guild, expected_voice_client=voice_client),
                        label="自律行動の開始失敗の切断",
                    )
                self._record_interrupted_start(guild_settings, attempt)
                return

        now = datetime.now(timezone.utc)
        session = AutonomousSession(
            channel_id=channel.id,
            is_stage=is_stage,
            started_at=now,
            last_human_at=now,
            last_spoke_at=now,
            stage_started_by_bot=stage_instance_id is not None,
            stage_instance_id=stage_instance_id,
            # 第一声を作っている間に独り言が重ならないよう、最初から busy にしておく。
            busy=True,
        )
        # ステージ準備も待機を伴う。条件が変わっていれば、未登録の所有枠と元の接続を片付ける。
        if not self._can_finish_start(guild, guild_settings, attempt, voice_client, channel.id):
            await wait_for_cleanup(
                autonomous_stage.finish_session_cleanup(
                    guild.id, guild_settings, session, channel, guild, voice_client,
                ),
                label="自律行動の準備後の開始中止",
            )
            self._record_interrupted_start(guild_settings, attempt)
            return

        # ここまで来たら開始成功。失敗の記録は消して、次回は待たずに出られるようにする。
        guild_settings.autonomous_start_failures = 0
        guild_settings.autonomous_start_retry_at = None
        guild_settings.autonomous_session = session
        kind = "ステージ" if is_stage else "ボイスチャンネル"
        logger.info("自律行動: %sで配信を開始しました。", kind)

        spawn(self._speak_opening(guild, guild_settings, session, channel), label="自律行動の第一声")

    @staticmethod
    def _can_finish_start(
        guild: discord.Guild, guild_settings: GuildSettings, attempt: _StartAttempt,
        voice_client: discord.VoiceClient | None, channel_id: int,
    ) -> bool:
        """待機後も開始要求が有効で、元の接続・チャンネルを所有しているか。"""
        return bool(
            not attempt.stopped and guild_settings.autonomous_enabled and not is_quiet_hours()
            and guild_settings.autonomous_session is None
            and voice_client is not None and guild.voice_client is voice_client
            and voice_client.is_connected() and getattr(voice_client.channel, "id", None) == channel_id
        )

    def _record_interrupted_start(self, guild_settings: GuildSettings, attempt: _StartAttempt) -> None:
        """利用者の停止や時間帯制限は失敗にせず、接続異常では既存の再試行待ちを保つ。"""
        if (not attempt.stopped and guild_settings.autonomous_enabled and not is_quiet_hours()
                and guild_settings.autonomous_session is None):
            self._record_start_failure(guild_settings)

    def _record_start_failure(self, guild_settings: GuildSettings) -> None:
        """配信の開始に失敗したことを記録し、次に試すまでの待ち時間を設定する。

        **この歯止めが無いと、失敗したサーバーへ毎分ずっと接続を試み続ける。**
        「話したいゲージ」は時間が経つほど増えるだけで、開始に失敗しても下がらない
        ため、見回りのたびに同じ判定が通ってしまうため（実測で確認済み）。
        権限設定が中途半端なサーバーが1つあるだけで、1日1440回の接続と切断が走る。

        待ち時間は連続失敗のたびに延び、`AUTONOMOUS_START_RETRY_MAX_MULTIPLIER` 倍で
        頭打ちになる（既定で 30分 → 最長4時間）。ゲージ自体はリセットしない —
        リセットすると、権限を直したあともしきい値時間ぶん待たされてしまう。
        なお `/functions` のパネルで自律行動を確定すると、この待ち時間は即座に
        解除される（設定を直した直後に試せるようにするため）。
        """
        guild_settings.autonomous_start_failures += 1
        wait_minutes = start_retry_wait_minutes(guild_settings.autonomous_start_failures)
        guild_settings.autonomous_start_retry_at = datetime.now(timezone.utc) + timedelta(minutes=wait_minutes)
        logger.warning("自律行動: 配信の開始に失敗しました（連続 %d 回目）。次に試すのは約 %.0f 分後です。", guild_settings.autonomous_start_failures, wait_minutes)

    async def stop_all_sessions(self) -> None:
        """周期処理の終了後、所有するすべての配信枠と接続を回収する。"""
        canceled = False
        for guild_id in set(self._state.guild_settings) | set(self._starting) | set(self._stopping):
            try:
                await self.stop_session(guild_id, reason="ボットの終了")
            except asyncio.CancelledError:
                # 1件の停止完了後に届いたキャンセルで、残りのサーバーを取り残さない。
                canceled = True
            except Exception as error:
                logger.error("自律行動: 終了時の後片付けに失敗しました。", exc_info=error)
        if canceled:
            raise asyncio.CancelledError

    async def stop_session(
        self,
        guild_id: int,
        *,
        reason: str,
        notice: str | None = None,
    ) -> bool:
        """配信と開始待機を停止する。停止対象があれば True、無ければ False。

        `/leave`・`/join` からも呼ばれる（人の操作による強制終了）。

        締めの投稿やステージ枠の後始末が失敗しても、
        ボイス接続の切断とセッション記録の破棄までは必ず進める（片付けが
        途中で止まると、接続だけが残って誰も止められなくなるため）。
        キャンセルだけは後片付けの完了後に呼出元へ伝える。

        Args:
            reason: ログに残す終了理由。
            notice: 配信チャンネルへ残す締めの一言（None なら何も投稿しない）。
        """
        stopping = self._stopping.get(guild_id)
        if stopping is not None:
            # 終了案内や後片付け自身から再入した場合だけ、自己待機を避ける。
            if asyncio.current_task() not in (stopping.owner, stopping.task):
                await wait_for_task(stopping.task)
            return True

        guild_settings = self._state.guild_settings.get(guild_id)
        starting = self._starting.get(guild_id)
        if guild_settings is None:
            if starting is None:
                return False
            starting.stopped = True
            if starting.task is not asyncio.current_task():
                await wait_for_cleanup(starting.finished.wait(), label="自律行動の開始中止待機")
            return True
        if starting is not None:
            # connect()がまだ戻っていない場合にも、後着するセッションの登録を止める。
            starting.stopped = True
        session = guild_settings.autonomous_session
        if session is None:
            if starting is not None:
                self._clear_session(guild_settings)
                logger.info("自律行動: 配信の開始を中止します。")
                # /joinが開始途中の接続を再利用する前に、元の接続と所有枠を片付ける。
                # 開始処理自身からの中止要求だけは、自分の終了を待たない。
                if starting.task is not asyncio.current_task():
                    await wait_for_cleanup(starting.finished.wait(), label="自律行動の開始中止待機")
                return True
            return False

        # 待機中に /join や別の配信へ接続が差し替わっても、その接続を切らない。
        # 接続の情報源は常に guild.voice_client とし、ここでは照合用の参照だけを控える。
        guild = self._client.get_guild(guild_id)
        voice_client = guild.voice_client if guild is not None else None
        # 先に記録を消す。ここから切断までの間に見回りが来ても、二重に停止処理を
        # 走らせない（同じステージ枠を2回消そうとするのを防ぐ）。
        self._clear_session(guild_settings)

        stopping = _StopAttempt(owner=asyncio.current_task())
        self._stopping[guild_id] = stopping
        stopping.task = spawn(
            self._finish_session_stop(guild_id, guild_settings, session, guild, voice_client, stopping),
            label="自律行動の終了後片付け", _protected=True,
        )
        canceled = False
        try:
            channel = self._client.get_channel(session.channel_id)
            stopping.channel = channel
            # 終了案内の送信だけは呼出元と一緒にキャンセルし、後片付けへ進む。
            if notice and channel is not None:
                try:
                    await send_long_message(channel, notice, allowed_mentions=_NO_MENTIONS)
                except discord.HTTPException as error:
                    logger.error("自律行動: 締めの投稿に失敗しました。", exc_info=error)
                except Exception as error:
                    logger.error("自律行動: 締めの投稿で予期せぬエラーが発生しました。", exc_info=error)
        except asyncio.CancelledError:
            canceled = True
        finally:
            stopping.notice_finished.set()
            try:
                await wait_for_task(stopping.task)
            except asyncio.CancelledError:
                canceled = True

        logger.info("自律行動: 配信を終了しました。")
        if canceled:
            raise asyncio.CancelledError
        return True

    async def _finish_session_stop(
        self, guild_id: int, guild_settings: GuildSettings, session: AutonomousSession,
        guild: discord.Guild | None, voice_client: discord.VoiceProtocol | None, stopping: _StopAttempt,
    ) -> None:
        """後続の停止・参加・退出へ、枠と接続の回収完了を共有する。"""
        try:
            await stopping.notice_finished.wait()
            await autonomous_stage.finish_session_cleanup(
                guild_id, guild_settings, session, stopping.channel, guild, voice_client,
            )
        finally:
            if self._stopping.get(guild_id) is stopping:
                self._stopping.pop(guild_id)

    def _clear_session(self, guild_settings: GuildSettings) -> None:
        """セッション記録を破棄し、「話したいゲージ」を空にする。

        ゲージを空に戻さないと、終了直後の見回りで満タンのまま再判定され、
        同じサーバーで配信が延々と再開してしまう。
        """
        guild_settings.autonomous_session = None
        guild_settings.autonomous_last_activity = datetime.now(timezone.utc)
        guild_settings.autonomous_recent_activity.clear()
