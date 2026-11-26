"""`/join`・`/leave` のボイス接続処理。

`VoiceSessionCommands`（直列化された順番で実行する、参加・退出・音声会話の開始の実処理）と、接続前の権限案内・
ステージ昇格・接続結果の案内を持つ。参加・退出の操作世代と直列化を担う `VoiceCommandCoordinator` は
`voice_coordinator.py` に分けてあり（#495）、従来の名前でこのモジュールからも再公開する。
（`commands.register_commands` は、コマンドの登録と共通のゲート配線だけを担当する。）

★ ログの出力元は `persona_bot.core.voice_commands`。以前は `persona_bot.core.commands` だった。
"""

import asyncio
import logging
from typing import Callable

import discord

from persona_bot.audio.voice_receive import LiveVoiceClient
from persona_bot.config.constants import VOICE_CONNECT_TIMEOUT_SECONDS
from persona_bot.core.app_state import BotState
from persona_bot.core.background import wait_for_cleanup
from persona_bot.core.voice_coordinator import (  # noqa: F401（従来の import 先として再公開）
    VoiceCommandCoordinator,
    _GuildOperations,
    skip_superseded_voice_operation,
)
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.autonomous_stage import enable_stage_speaker
from persona_bot.services.live_session import LiveUnavailableError
from persona_bot.services.voice_conversation import (
    NOTICE_CONSENT_PANEL_FAILED,
    NOTICE_LIVE_CAPACITY,
    NOTICE_LIVE_QUOTA,
    NOTICE_LIVE_UNAVAILABLE,
    ConsentPanelError,
    VoiceConversation,
)
from persona_bot.services.voice_conversation_service import VoiceConversationService
from persona_bot.services.voice_guard import VoiceGuard, release_and_disconnect
from persona_bot.utils.discord_helpers import NO_MENTIONS
from persona_bot.utils.voice_permissions import missing_voice_permissions


logger = logging.getLogger(__name__)


def join_permission_notice(
    voice_channel: discord.VoiceChannel | discord.StageChannel, bot_member: discord.Member | None,
) -> str | None:
    """接続を試す前に確かめたボットの権限が不足していれば、その案内文を返す。足りていれば None。

    権限不足は接続失敗の最も多い原因だが、connect() の Forbidden として出るため、後段でまとめて
    拾うと「接続に失敗しました」としか伝えられず原因にたどり着けない。
    ボットのMemberを取得できないときは判定できないので None（接続を試す）。
    """
    if bot_member is None:
        return None
    is_stage = isinstance(voice_channel, discord.StageChannel)
    missing_permissions = missing_voice_permissions(voice_channel, bot_member, stage_moderation=is_stage)
    if not missing_permissions:
        return None
    permission_names = "・".join(f"「{name}」" for name in missing_permissions)
    return (
        f"⚠️ {voice_channel.name} でボットに{permission_names}の権限がありません。\n"
        "サーバー設定 →「連携サービス」か、そのボイスチャンネルの権限設定で、"
        f"ボットのロールに{permission_names}を許可してください"
        "（チャンネル個別の権限で拒否されている場合もあります）。"
        + ("ステージでは観客から発言者になるため「メンバーをミュート」が必要です。" if is_stage else "")
    )


async def promote_stage_speaker(
    interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel,
    *, leaving: bool = False,
) -> bool:
    """ステージでボットを発言者へ昇格する。昇格できなければ案内して False を返す。

    ステージ以外では何もせず True を返す。leaving が真なら、呼び出し側がこの後に退出する
    ため、「接続しています」ではなく退出する旨を案内する。
    """
    if not isinstance(channel, discord.StageChannel):
        return True
    try:
        await enable_stage_speaker(interaction.guild, channel)
    except Exception as error:
        logger.error("ステージの発言者への昇格に失敗しました: %s", type(error).__name__, exc_info=error)
        hint = "ボットを発言者へ招待するか、管理者にステージの「メンバーをミュート」権限を確認してください。"
        if leaving:
            text = f"⚠️ {channel.name} で、ステージの発言者になれなかったため、声で会話できずに退出しました。{hint}"
        else:
            text = f"⚠️ {channel.name} へ接続していますが、ステージの発言者になれませんでした。{hint}"
        await interaction.followup.send(text, allowed_mentions=NO_MENTIONS)
        return False
    return True


async def announce_voice_join(
    interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel,
    *, already_connected: bool = False, live: bool = False, speaker_ready: bool = False,
) -> None:
    """ステージでは発言者へ昇格してから、接続の結果を案内する。

    live が真なら、音声会話（Gemini Live）の同意パネルへの案内を添える。
    speaker_ready が真なら、呼び出し側が昇格を済ませているので再度は行わない。
    """
    if not speaker_ready and not await promote_stage_speaker(interaction, channel):
        return
    message =(f"{channel.name} には既に接続しています。" if already_connected
               else f"{channel.name} に接続しました。")
    if isinstance(channel, discord.StageChannel):
        message += "ステージの発言者として話します。"
    if live:
        message += (
            "\n🎙️ ボイスチャンネルの参加者全員が同意パネルの「同意して参加」を押すと、"
            "ボットと声で会話できます。"
        )
    await interaction.followup.send(message, allowed_mentions=NO_MENTIONS)


async def _cleanup_failed_voice_join(
    guild: discord.Guild | None, voice_client: discord.VoiceClient | None,
    voice_guard: VoiceGuard | None,
) -> None:
    """今回の試行が残した未接続のVoiceClientだけを切断し、SDKの登録も回収する。"""
    if (guild is None or voice_client is None or guild.voice_client is not voice_client
            or voice_client.is_connected()):
        return
    try:
        await release_and_disconnect(
            voice_guard, guild.id, voice_client, label="接続失敗後のボイス切断",
            failure_message="接続失敗後のボイス切断に失敗しました。", log=logger,
        )
    finally:
        # 切断自体が失敗しても、半接続の登録を残して次回の参加を妨げない。
        # 待つ間に差し替わった接続や、正常につながった接続は解除しない。
        if guild.voice_client is voice_client and not voice_client.is_connected():
            try:
                voice_client.cleanup()
            except Exception:
                logger.warning("接続失敗後のボイス登録の解除に失敗しました。", exc_info=True)


class VoiceSessionCommands:
    """`/join`・`/leave` の接続処理（#414）。VoiceCommandCoordinator が直列化した順に呼ばれる。

    live_voice が None なら音声会話（Gemini Live）は使わず、接続だけを行う。
    """

    def __init__(
        self,
        state: BotState,
        autonomous: AutonomousService,
        *,
        voice_guard: VoiceGuard | None = None,
        live_voice: VoiceConversationService | None = None,
    ) -> None:
        self._state = state
        self._autonomous = autonomous
        self._voice_guard = voice_guard
        self._live_voice = live_voice

    async def _requester_moved(
        self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        """実行者が対象のボイスチャンネルから離れていたら案内して True を返す。"""
        current_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
        if getattr(current_channel, "id", None) == voice_channel.id:
            return False
        await interaction.followup.send(
            "⚠️ ボイスチャンネルの参加状態が変わったため、接続を中止しました。もう一度 /join を実行してください。"
        )
        return True

    async def _disconnect_voice(
        self, guild_id: int, voice_client: discord.VoiceClient, *, label: str, failure_message: str,
        stop_live_reason: str | None = None,
    ) -> bool:
        """保護の解除 →（必要なら）音声会話の停止 → 切断 を、この順序でまとめて行う。

        切断に失敗したら警告だけを残して False を返す（呼出元が案内の要否を決める）。
        """
        live_voice = self._live_voice
        stop = None
        if stop_live_reason is not None and live_voice is not None:
            async def stop() -> None:
                await live_voice.stop(guild_id, reason=stop_live_reason)
        return await release_and_disconnect(
            self._voice_guard, guild_id, voice_client,
            label=label, failure_message=failure_message, log=logger, stop=stop,
        )

    async def join(
        self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        is_current: Callable[[], bool],
    ) -> None:
        guild = interaction.guild
        if await self._requester_moved(interaction, voice_channel):
            return
        # 自律行動でボットが自分から通話に入っている最中かもしれない。人が呼んだ
        # 接続を優先するため、ステージ枠の後始末まで含めて先に配信を終了させる
        # （ここを素の disconnect だけで済ませると、ボットが開いたステージ枠が
        # 「配信中」の看板を出したまま残ってしまう）。
        if guild is not None:
            await self._autonomous.stop_session(
                guild.id, reason="/join による上書き"
            )
            if await skip_superseded_voice_operation(interaction, is_current):
                return
            if await self._requester_moved(interaction, voice_channel):
                return
            voice_client = guild.voice_client
            if voice_client is not None:
                # 既に同じチャンネルへ接続中なら、そのまま使う。切り直すと、話している途中の
                # 音声が止まり、音声会話の文脈も失われてしまう。
                # （自律行動の配信だった場合は、上の stop_session が接続ごと片付けている。）
                if self._can_reuse_connection(voice_client, voice_channel):
                    await self._reuse_connection(
                        interaction, voice_client, voice_channel, is_current=is_current,
                    )
                    return
                if not await self._switch_connection(interaction, voice_client):
                    return

        if await skip_superseded_voice_operation(interaction, is_current):
            return
        if await self._requester_moved(interaction, voice_channel):
            return
        await self._connect_new(interaction, voice_channel)

    def _can_reuse_connection(
        self, voice_client: object, voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        """既存の接続を切り直さずに使える（同じチャンネルで接続中）か。

        音声会話は受信のできる LiveVoiceClient で接続している必要がある。
        """
        return (
            isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected()
            and getattr(voice_client.channel, "id", None) == voice_channel.id
            and (self._live_voice is None or isinstance(voice_client, LiveVoiceClient))
        )

    async def _reuse_connection(
        self, interaction: discord.Interaction, voice_client: discord.VoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, is_current: Callable[[], bool] = lambda: True,
    ) -> None:
        """同じチャンネルの既存の接続を使い続ける。音声会話が止まっていれば始め直す。"""
        guild = interaction.guild
        live_voice = self._live_voice
        conversation = live_voice.get(guild.id) if live_voice is not None else None
        restarting = live_voice is not None and (
            conversation is None or conversation.stopped
            or conversation.voice_client is not voice_client
            or conversation.channel_id != voice_channel.id
        )
        # 始め直しのとき確保した予約票。停止待機の中断時に、自分の票だけを解放するため持ち回る。
        first_reservation = live_voice.reserve(guild.id) if restarting else None
        if restarting and not first_reservation:
            # 新規接続のパスと同じく、保護の引継ぎやステージ昇格の API の前に同時実行の上限を
            # 確かめる（後で断ると、その分を無駄にした上で接続を切ることになる）。
            # 会話が動いていない接続は残しても音声会話に使えないため、開始失敗時と同じ片付け
            # （保護の解除と切断）を行う。
            await self._disconnect_voice(
                guild.id, voice_client, label="同時実行上限での切断",
                failure_message="同時実行の上限に達した後の切断に失敗しました。",
            )
            await interaction.followup.send(NOTICE_LIVE_CAPACITY, allowed_mentions=NO_MENTIONS)
            return
        handed_off = False
        try:
            # 移された先で /join された場合も、ここを意図した接続先にする（元へ戻さない）。
            if self._voice_guard is not None:
                self._voice_guard.adopt(guild.id, voice_channel.id)
            if live_voice is not None:
                if restarting:
                    handed_off = True
                    resumed = await self._restart_live_on_connection(
                        interaction, voice_client, voice_channel, conversation,
                        first_reservation=first_reservation, is_current=is_current,
                    )
                else:
                    resumed = await self._ensure_live_speaker(interaction, voice_client, voice_channel)
                if not resumed:
                    return
        finally:
            if first_reservation and not handed_off and live_voice is not None:
                live_voice.release_reservation(guild.id, reservation=first_reservation)
        await announce_voice_join(
            interaction, voice_channel, already_connected=True, live=live_voice is not None,
            speaker_ready=live_voice is not None,
        )

    async def _restart_live_on_connection(
        self, interaction: discord.Interaction, voice_client: discord.VoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        conversation: VoiceConversation | None, *, first_reservation: object | None = None,
        is_current: Callable[[], bool] = lambda: True,
    ) -> bool:
        """接続はあるがこのチャンネルの音声会話が動いていない（止まっている・移された先で /join された）
        とき、同じ接続のまま始め直す。会話を始められたら True。"""
        guild = interaction.guild
        live_voice = self._live_voice
        reservation = None
        second_reserved = False
        # start_live_voice へ予約の所有権を渡したか。渡した後は向こうが成否に関わらず解放する。
        transferred = False
        try:
            # まず旧チャンネル用に動いていた会話を止める（この後の昇格に失敗して退出するときも、
            # 会話タスクを残留させない）。ステージでは先に発言者へ昇格し、声を出せないまま会話を始めない。
            if conversation is not None and not conversation.stopped:
                try:
                    await live_voice.stop(guild.id, reason="/join による音声会話の始め直し")
                    superseded = await skip_superseded_voice_operation(interaction, is_current)
                    moved = False if superseded else await self._requester_moved(interaction, voice_channel)
                except (Exception, asyncio.CancelledError):
                    # 停止や案内の待機中の中断。旧会話は登録から外れて会話の無い接続と保護（adopt 済み）だけが
                    # 残るので、昇格失敗時と同じ片付けを済ませてから伝える（繰り返しのキャンセルでも止めない）。
                    await wait_for_cleanup(
                        self._disconnect_voice(
                            guild.id, voice_client, label="音声会話の始め直しの中断後の切断",
                            failure_message="音声会話の始め直しの中断後の切断に失敗しました。",
                        ),
                        label="音声会話の始め直しの中断後の切断",
                    )
                    raise
                if superseded or moved:
                    # 停止を待つ間に後続の操作が受け付けられた、または実行者が離脱した場合は、
                    # 会話の無い接続と保護（adopt 済み）を残さずに終了する（#509）。
                    await self._disconnect_voice(
                        guild.id, voice_client, label="音声会話の始め直しの中止時の切断",
                        failure_message="音声会話の始め直しを中止した後の切断に失敗しました。",
                    )
                    return False
            # live_voice.stop() は予約枠も解放するため、開始の移譲前に再度枠を確保する
            #（旧予約票の自己破棄の防止）。
            second_reserved = True
            reservation = live_voice.reserve(guild.id)
            if not reservation:
                # 停止を待つ間に、別のサーバーの /join が最後の枠を取った。会話を始められない接続と
                # 保護（adopt 済み）を残さない（#465。新規接続・再利用の上限と同じ片付け）。
                await self._disconnect_voice(
                    guild.id, voice_client, label="同時実行上限での切断",
                    failure_message="同時実行の上限に達した後の切断に失敗しました。",
                )
                await interaction.followup.send(NOTICE_LIVE_CAPACITY, allowed_mentions=NO_MENTIONS)
                return False
            if not await self._prepare_stage_or_disconnect(interaction, voice_client, voice_channel):
                return False
            transferred = True
            return await self.start_live_voice(interaction, voice_client, voice_channel, reservation=reservation)
        finally:
            # 開始の移譲前の失敗・例外・キャンセル（CancelledError は Exception ではない）でも、
            # 確保した枠を残さない（#465・#509）。
            if first_reservation and not second_reserved:
                live_voice.release_reservation(guild.id, reservation=first_reservation)
            if reservation and not transferred:
                live_voice.release_reservation(guild.id, reservation=reservation)

    async def _ensure_live_speaker(
        self, interaction: discord.Interaction, voice_client: discord.VoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        """同じチャンネルで会話が続いている接続を、ステージの発言者として使い続けられるようにする。

        ステージで聴衆へ降格されていた場合に備えて発言者へ戻す（戻せないなら、声を出せない
        会話を続けさせない）。ここで昇格を済ませるため、案内側の昇格は行わない。
        昇格を待つ間の中断でも、保護の解除・会話の停止・切断を済ませる（#465）。
        """
        return await self._prepare_stage_or_disconnect(
            interaction, voice_client, voice_channel,
            stop_live_reason="ステージの発言者に戻れなかったため会話を終了",
        )

    async def _switch_connection(self, interaction: discord.Interaction, voice_client: discord.VoiceClient) -> bool:
        """別のチャンネルへ接続中のボットを切断して、接続先を切り替える準備をする。切れなければ False。

        切り替えの前に保護を解く（/leave と同じ順序）。移動を戻している最中の処理が、
        切る予定の接続を元のチャンネルへ動かそうとしないようにする。
        ★ 音声会話を止めた後は、実行者が離脱していても途中で戻らず、必ず既存の接続を
        切断する（戻ると、会話だけ止まって切断されない VC 接続が残る）。実行者の
        参加状態は、切断の後にある呼出元の確認で見る。
        """
        disconnected = await self._disconnect_voice(
            interaction.guild.id, voice_client, label="参加前のボイス切断",
            failure_message="接続切替時に既存のボイス接続を切断できませんでした。",
            stop_live_reason="/join による接続先の切り替え",
        )
        if not disconnected:
            await interaction.followup.send(
                "⚠️ 既存のボイス接続を切断できなかったため、接続先を切り替えられませんでした。"
                "しばらく待ってからもう一度お試しください。"
            )
        return disconnected

    async def _connect_new(
        self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> None:
        """新規に接続する。音声会話の同時実行枠は、接続より前に確保して終了時に必ず解放する。"""
        guild = interaction.guild
        live_voice = self._live_voice
        reservation = live_voice.reserve(guild.id) if live_voice is not None and guild is not None else None
        if live_voice is not None and guild is not None and not reservation:
            # 同時に会話できる VC の上限。接続してから断ると無駄に出入りするため、先に確かめる。
            await interaction.followup.send(NOTICE_LIVE_CAPACITY, allowed_mentions=NO_MENTIONS)
            return
        try:
            await self.connect_voice(interaction, voice_channel, reservation=reservation)
        finally:
            # 開始に進まなかった（接続の失敗・中断）場合も、確保した枠を残さない。
            if live_voice is not None and guild is not None:
                live_voice.release_reservation(guild.id, reservation=reservation)

    async def prepare_stage_for_live(
        self, interaction: discord.Interaction, voice_client: discord.VoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> bool:
        """音声会話を始める前に、ステージでは発言者へ昇格する。昇格できなければ退出して False を返す。

        ステージ以外のチャンネルでは何もせず True を返す。
        """
        # 声を出せないまま接続を残さないため、False のときは呼び出し側が `_disconnect_voice` で
        # 「保護解除 → 停止 → 切断」を行う（保護の解除もそちらに集約している）。
        # 呼び出し側は `_prepare_stage_or_disconnect` を通す（中断時の片付けもそこにある）。
        return await promote_stage_speaker(interaction, voice_channel, leaving=True)

    async def _prepare_stage_or_disconnect(
        self, interaction: discord.Interaction, voice_client: discord.VoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, stop_live_reason: str | None = None,
    ) -> bool:
        """ステージでは発言者へ昇格する。昇格できなければ、保護を解いて切断し False を返す。

        昇格を待つ間に例外やキャンセルで中断した場合も、同じ片付け（保護の解除 →（あれば）会話の停止
        → 切断）を済ませてから伝える（#465）。片付けは繰り返しのキャンセルでも途中で止めない。
        中断を素通しすると、保護（adopt 済み）と声を出せない接続が残る。
        """
        guild_id = interaction.guild.id

        async def leave() -> None:
            await self._disconnect_voice(
                guild_id, voice_client, label="ステージ昇格失敗時の切断",
                failure_message="ステージの発言者になれなかった後の切断に失敗しました。",
                stop_live_reason=stop_live_reason,
            )

        try:
            promoted = await self.prepare_stage_for_live(interaction, voice_client, voice_channel)
        except (Exception, asyncio.CancelledError):
            await wait_for_cleanup(leave(), label="ステージ昇格の中断後の切断")
            raise
        if not promoted:
            await leave()
        return promoted

    async def start_live_voice(
        self, interaction: discord.Interaction, voice_client: LiveVoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, reservation: object | None = None,
    ) -> bool:
        """音声会話を始める。始められなければ案内して退出し、False を返す。"""
        guild = interaction.guild
        live_voice = self._live_voice
        reservation = reservation if reservation is not None else live_voice.reserve(guild.id)
        started = False
        notice = NOTICE_LIVE_CAPACITY
        try:
            if reservation:
                guild_settings = await self._state.get_guild_async(guild.id)
                await live_voice.start(
                    guild, voice_client, voice_channel, guild_settings.snapshot_response(),
                    fallback_channel=interaction.channel, reservation=reservation,
                )
                started = True
        except Exception as error:
            if not isinstance(error, (LiveUnavailableError, ConsentPanelError)):
                # ペルソナの読込失敗など。声で会話できない接続をボイスチャンネルに残さない。
                logger.error("音声会話を始められませんでした。", exc_info=error)
            if isinstance(error, ConsentPanelError):
                notice = NOTICE_CONSENT_PANEL_FAILED
            elif isinstance(error, LiveUnavailableError) and error.quota:
                notice = NOTICE_LIVE_QUOTA
            else:
                notice = NOTICE_LIVE_UNAVAILABLE
        finally:
            if reservation:
                live_voice.release_reservation(guild.id, reservation=reservation)
            if not started and guild.voice_client is voice_client:
                # 設定読込・開始のキャンセルや上限到達でも、会話できない接続を残さない。
                await self._disconnect_voice(
                    guild.id, voice_client, label="音声会話の開始失敗時の切断",
                    failure_message="音声会話を始められなかった後の切断に失敗しました。",
                )
        if not started:
            await interaction.followup.send(notice, allowed_mentions=NO_MENTIONS)
        return started

    async def connect_voice(
        self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, reservation: object | None = None,
    ) -> None:
        guild = interaction.guild
        live_voice = self._live_voice
        voice_guard = self._voice_guard
        # 例外の型ごとに原因を伝える。ここをまとめて握りつぶすと、利用者からは
        # 権限・ネットワーク・接続残りのどれなのか判別できなくなる。
        attempted_voice_client = None
        # 音声会話では人の声を受信するため、受信に対応した VoiceClient で接続する。
        voice_client_class = LiveVoiceClient if live_voice is not None else discord.VoiceClient

        def create_voice_client(client, channel):
            # SDKの登録直前に参照を控え、接続失敗時もこの試行の接続だけを回収する。
            nonlocal attempted_voice_client
            attempted_voice_client = voice_client_class(client, channel)
            return attempted_voice_client

        try:
            try:
                await wait_for_cleanup(
                    voice_channel.connect(timeout=VOICE_CONNECT_TIMEOUT_SECONDS, cls=create_voice_client),
                    label="ボイス参加の接続完了",
                )
            except (Exception, asyncio.CancelledError):
                await wait_for_cleanup(
                    _cleanup_failed_voice_join(guild, attempted_voice_client, voice_guard),
                    label="ボイス参加失敗の後片付け",
                )
                raise
        except discord.Forbidden:
            logger.warning("ボイス接続が権限不足で拒否されました。", exc_info=True)
            await interaction.followup.send(
                f"⚠️ {voice_channel.name} への接続を拒否されました（権限不足）。"
                "チャンネルの権限設定でボットに「接続」を許可してください。",
                allowed_mentions=NO_MENTIONS,
            )
        except asyncio.TimeoutError:
            logger.warning("ボイス接続がタイムアウトしました。", exc_info=True)
            await interaction.followup.send(
                "⚠️ 接続がタイムアウトしました。ボイス通信に必要な UDP が"
                "ネットワーク側（ファイアウォール・学内ネットワークなど）で遮断されている可能性があります。"
            )
        except discord.ClientException:
            # 例: 直前の接続が残っている（Already connected to a voice channel.）
            logger.warning("ボイス接続の状態が一致しませんでした。", exc_info=True)
            await interaction.followup.send(
                "⚠️ ボイス接続の状態を確認できず、接続できませんでした。"
                "しばらく待つか、ボットを再起動してからもう一度お試しください。"
            )
        except Exception:
            logger.error("ボイス接続に失敗しました。", exc_info=True)
            await interaction.followup.send(
                "⚠️ ボイス接続に失敗しました。しばらく待ってからもう一度お試しください。"
                "解消しない場合は管理者にログの確認を依頼してください。"
            )
        else:
            # 接続イベントでも登録されるが、人が選んだ接続先を明示的に意図した接続先にする。
            if voice_guard is not None and guild is not None:
                voice_guard.adopt(guild.id, voice_channel.id)
            speaker_ready = False
            if live_voice is not None and guild is not None:
                # ステージでは先に発言者へ昇格する。昇格できないまま会話と同意パネルを始めると、
                # 観客のまま同時枠だけを占有してしまう。昇格できない・待機中に中断したら、
                # 会話を開始できないので、保護を解いて接続を切断する（#465）。
                if not await self._prepare_stage_or_disconnect(interaction, attempted_voice_client, voice_channel):
                    return
                if not await self.start_live_voice(
                    interaction, attempted_voice_client, voice_channel, reservation=reservation,
                ):
                    return
                speaker_ready = True
            await announce_voice_join(
                interaction, voice_channel, live=live_voice is not None, speaker_ready=speaker_ready,
            )

    async def leave(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        # 退出の後に、移動を戻す処理が元のチャンネルへ入り直さないよう先に保護を解く。
        if self._voice_guard is not None:
            self._voice_guard.release(guild.id)
        # 音声会話は切断の前に止める（受信・再生・Live の接続と同意パネルを片付ける）。
        # /leave の後は Live へ自動で再接続しない。停止待機中に中断されても接続を残さない（#509）。
        try:
            if self._live_voice is not None:
                await self._live_voice.stop(guild.id, reason="/leave")

            # 自律行動で自分から入っている場合は、締めの投稿・ステージ枠の後片付け・
            # セッション記録の破棄・切断までをサービス側がまとめて行う
            # （idea.md の「既存の /leave コマンドで強制的に退出できるようにする」）。
            # 配信していなければ False が返るので、従来どおりの切断へ進む。
            stopped = await self._autonomous.stop_session(guild.id, reason="/leave")
        except (Exception, asyncio.CancelledError):
            voice_client = guild.voice_client
            if voice_client is not None:
                try:
                    await wait_for_cleanup(
                        voice_client.disconnect(force=True), label="退出時のボイス切断",
                    )
                except Exception:
                    logger.warning("退出中断後のボイス切断に失敗しました。", exc_info=True)
            raise
        if stopped:
            await interaction.followup.send("退出しました。（自律行動の配信を停止しました）")
            return

        if guild.voice_client is None:
            await interaction.followup.send("ボットはボイスチャンネルに参加していません。")
            return

        try:
            # stop_session を経ていない経路のみここへ来る。切断済みの可能性は無いが、
            # 念のため取り直してから切る。
            voice_client = guild.voice_client
            if voice_client is not None:
                await wait_for_cleanup(
                    voice_client.disconnect(force=True), label="退出時のボイス切断",
                )
            await interaction.followup.send("退出しました。")
        except Exception as error:
            logger.error("切断エラー: %s", type(error).__name__, exc_info=error)
            await interaction.followup.send("退出に失敗しました。")
