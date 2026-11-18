"""自律行動の配信の開始・停止（#469。`AutonomousService` から分離）。

`_start_session`（開始要求の登録と多重開始の抑止）・`_connect_session`（接続とステージ準備）・
`stop_session`（終了案内と所有枠・接続の共通回収）と、その状態を持つ `_StartAttempt` / `_StopAttempt`。
発話は `autonomous_speech.py`、見回りと停止条件の判定は `autonomous_service.py` に残る。
`AutonomousService` が継承するので、公開メソッドの名前・呼び出し方は変わらない。

★ ログは分割前と同じロガー名 `persona_bot.services.autonomous_service` へ出す。ミックスインは
`_client` / `_state` / `_starting` / `_stopping` と、`_speak_opening`（発話側）を `AutonomousService` から
受け取る前提で、単独では使わない。テストが `spawn` / `send_long_message` / `is_quiet_hours` を差し替えるときは、
`tests/helpers/autonomous_patch.patch_autonomous` で、この名前を持つ全モジュールへ当てること。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from persona_bot.config.constants import AUTONOMOUS_CONNECT_TIMEOUT_SECONDS
from persona_bot.core.app_state import AutonomousSession, GuildSettings
from persona_bot.core.background import spawn, wait_for_cleanup, wait_for_task
from persona_bot.services import autonomous_stage
from persona_bot.services.autonomous_metrics import is_quiet_hours, start_retry_wait_minutes
from persona_bot.utils.discord_helpers import NO_MENTIONS, send_long_message

logger = logging.getLogger("persona_bot.services.autonomous_service")


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


class AutonomousLifecycleMixin:
    """配信の開始（接続・ステージ準備・失敗時の待ち時間）と停止（案内・枠と接続の回収）。"""

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
        guild_settings.reset_autonomous_retry()
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
            reason: ログに残す終了理由。**固定の定型句だけを渡すこと**（利用者名・ID・
                チャンネル名・サーバー名を含めない。運用ログへそのまま出るため、管理コンソールの
                伏せ字（privacy.ts）が知らない名前が混ざると露出する）。
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
                    await send_long_message(channel, notice, allowed_mentions=NO_MENTIONS)
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

        logger.info("自律行動: 配信を終了しました（理由: %s）。", reason)
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
