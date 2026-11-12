"""`/join`・`/leave` のボイス接続処理。

次の2つを持つ（`commands.register_commands` は、コマンドの登録と共通のゲート配線だけを担当する）。

- `VoiceCommandCoordinator` … 参加・退出の受付順を記録し、同じサーバーの接続変更を直列化する。
- `VoiceSessionCommands` … 直列化された順番で実行する、参加・退出・音声会話（Gemini Live）の開始の実処理。
  依存（状態・自律行動・接続先の保護・音声会話）は生成時に明示的に受け取る。

★ ログの出力元は `persona_bot.core.voice_commands`。以前は `persona_bot.core.commands` だった。
"""

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Awaitable, Callable

import discord

from persona_bot.audio.voice_receive import LiveVoiceClient
from persona_bot.config.constants import VOICE_CONNECT_TIMEOUT_SECONDS
from persona_bot.core.app_state import BotState
from persona_bot.core.background import wait_for_cleanup
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.autonomous_stage import enable_stage_speaker
from persona_bot.services.live_session import LiveUnavailableError
from persona_bot.services.voice_conversation import (
    NOTICE_CONSENT_PANEL_FAILED,
    NOTICE_LIVE_CAPACITY,
    NOTICE_LIVE_QUOTA,
    NOTICE_LIVE_UNAVAILABLE,
    ConsentPanelError,
    VoiceConversationService,
)
from persona_bot.services.voice_guard import VoiceGuard
from persona_bot.utils.voice_permissions import missing_voice_permissions


logger = logging.getLogger(__name__)


@dataclass
class _GuildOperations:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tokens: list[object] = field(default_factory=list)
    # 応答待ち以外の有効なtokenは、ロック待ちまたは実行中。
    deferring: set[object] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending: int = 0

    @property
    def latest(self) -> object | None:
        return self.tokens[-1] if self.tokens else None

    def notify_changed(self) -> None:
        # 待機者のEventを起こしてから差し替え、確認と待機の間の通知も取りこぼさない。
        self.changed.set()
        self.changed = asyncio.Event()


async def skip_superseded_voice_operation(
    interaction: discord.Interaction, is_current: Callable[[], bool],
) -> bool:
    """後続の操作が受け付けられていれば、古い操作の接続変更を見送る。"""
    if is_current():
        return False
    await interaction.followup.send("後から受け付けた参加・退出操作を優先しました。")
    return True


class VoiceCommandCoordinator:
    """応答待ちから操作完了まで、サーバー単位でロックと操作世代を共有する。"""

    def __init__(self) -> None:
        self._guilds: dict[int | None, _GuildOperations] = {}

    async def run(
        self, interaction: discord.Interaction,
        change: Callable[[Callable[[], bool]], Awaitable[None]],
    ) -> None:
        guild_id = interaction.guild.id if interaction.guild is not None else None
        operations = self._guilds.setdefault(guild_id, _GuildOperations())
        token = object()
        # deferより前に同期的に記録する。応答待ちの古い参加も後続の退出から識別できる。
        operations.tokens.append(token)
        operations.deferring.add(token)
        operations.pending += 1
        operations.notify_changed()

        def is_current() -> bool:
            return operations.latest is token

        try:
            await interaction.response.defer()
            operations.deferring.discard(token)
            operations.notify_changed()
            notice_sent = False
            notice_error: Exception | None = None
            while True:
                changed = operations.changed
                async with operations.lock:
                    superseded = token not in operations.tokens
                    if not superseded and is_current():
                        # 実行を始めた操作より前の要求は、後続の脱落でも復活させない。
                        operations.tokens[:] = [token]
                        operations.notify_changed()
                        await change(is_current)
                        return
                    newer = (
                        operations.tokens[operations.tokens.index(token) + 1:]
                        if not superseded else []
                    )
                    can_notify = not any(item in operations.deferring for item in newer)

                if can_notify and not notice_sent:
                    try:
                        notice_sent = await skip_superseded_voice_operation(interaction, is_current)
                    except Exception as error:
                        # 通知の失敗でも、後続が実行を始めるまでは元の要求を残す。
                        notice_sent = True
                        notice_error = error
                if superseded:
                    if notice_error is not None:
                        raise notice_error
                    return
                # 応答待ちと通知中の脱落をロック外で待つ。後続の実行開始までは再判定する。
                await changed.wait()
        finally:
            # ロック前に脱落した最新要求を外すと、直前の有効な要求へ戻る。
            if token in operations.tokens:
                operations.tokens.remove(token)
            operations.deferring.discard(token)
            operations.pending -= 1
            operations.notify_changed()
            # 応答待ちも含めて最後の操作が終わるまで、ロックと世代を共有する。
            if operations.pending == 0:
                self._guilds.pop(guild_id, None)


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
        logger.error(f'ステージの発言者への昇格に失敗しました: {type(error).__name__}', exc_info=error)
        hint = "ボットを発言者へ招待するか、管理者にステージの「メンバーをミュート」権限を確認してください。"
        if leaving:
            text = f"⚠️ {channel.name} で、ステージの発言者になれなかったため、声で会話できずに退出しました。{hint}"
        else:
            text = f"⚠️ {channel.name} へ接続していますが、ステージの発言者になれませんでした。{hint}"
        await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none())
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
    await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions.none())


async def _cleanup_failed_voice_join(
    guild: discord.Guild | None, voice_client: discord.VoiceClient | None,
    voice_guard: VoiceGuard | None,
) -> None:
    """今回の試行が残した未接続のVoiceClientだけを切断し、SDKの登録も回収する。"""
    if (guild is None or voice_client is None or guild.voice_client is not voice_client
            or voice_client.is_connected()):
        return
    if voice_guard is not None:
        voice_guard.release(guild.id)
    try:
        await voice_client.disconnect(force=True)
    except Exception:
        logger.warning("接続失敗後のボイス切断に失敗しました。", exc_info=True)
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

    async def join(
        self, interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        is_current: Callable[[], bool],
    ) -> None:
        guild = interaction.guild
        live_voice = self._live_voice
        voice_guard = self._voice_guard

        async def requester_moved() -> bool:
            current_channel = getattr(getattr(interaction.user, "voice", None), "channel", None)
            if getattr(current_channel, "id", None) == voice_channel.id:
                return False
            await interaction.followup.send(
                "⚠️ ボイスチャンネルの参加状態が変わったため、接続を中止しました。もう一度 /join を実行してください。"
            )
            return True

        if await requester_moved():
            return
        # 自律行動でボットが自分から通話に入っている最中かもしれない。人が呼んだ
        # 接続を優先するため、ステージ枠の後始末まで含めて先に配信を終了させる
        # （ここを素の disconnect だけで済ませると、ボットが開いたステージ枠が
        # 「配信中」の看板を出したまま残ってしまう）。
        if guild is not None:
            await self._autonomous.stop_session(
                guild.id, reason=f"/join による上書き（{interaction.user}）"
            )
            if await skip_superseded_voice_operation(interaction, is_current):
                return
            if await requester_moved():
                return
            voice_client = guild.voice_client
            if voice_client is not None:
                # 既に同じチャンネルへ接続中なら、そのまま使う。切り直すと、話している途中の
                # 音声が止まり、音声会話の文脈も失われてしまう。
                # （自律行動の配信だった場合は、上の stop_session が接続ごと片付けている。）
                # 音声会話は受信のできる LiveVoiceClient で接続している必要がある。
                if (isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected()
                        and getattr(voice_client.channel, "id", None) == voice_channel.id
                        and (live_voice is None or isinstance(voice_client, LiveVoiceClient))):
                    # 移された先で /join された場合も、ここを意図した接続先にする（元へ戻さない）。
                    if voice_guard is not None:
                        voice_guard.adopt(guild.id, voice_channel.id)
                    speaker_ready = False
                    if live_voice is not None:
                        conversation = live_voice.get(guild.id)
                        if (conversation is None or conversation.stopped
                                or conversation.voice_client is not voice_client
                                or conversation.channel_id != voice_channel.id):
                            # 接続はあるが、このチャンネルの音声会話が動いていない（止まっている・
                            # 移された先で /join された）。同じ接続のまま、このチャンネル用に始め直す。
                            # ステージでは先に発言者へ昇格し、声を出せないまま会話を始めない。
                            if not await self.prepare_stage_for_live(interaction, voice_client, voice_channel):
                                return
                            if not await self.start_live_voice(interaction, voice_client, voice_channel):
                                return
                            speaker_ready = True
                    await announce_voice_join(
                        interaction, voice_channel, already_connected=True, live=live_voice is not None,
                        speaker_ready=speaker_ready,
                    )
                    return
                # 切り替えの前に保護を解く（/leave と同じ順序）。移動を戻している最中の処理が、
                # 切る予定の接続を元のチャンネルへ動かそうとしないようにする。音声会話の停止は
                # 同意パネルの編集などで待たされるため、保護の解除はその待機より先に行う。
                if voice_guard is not None:
                    voice_guard.release(guild.id)
                if live_voice is not None:
                    await live_voice.stop(guild.id, reason="/join による接続先の切り替え")
                # ★ 音声会話を止めた後は、実行者が離脱していても途中で戻らず、必ず既存の接続を
                # 切断する（戻ると、会話だけ止まって切断されない VC 接続が残る）。実行者の
                # 参加状態は、切断の後にある下の確認で見る。
                try:
                    await wait_for_cleanup(
                        voice_client.disconnect(force=True), label="参加前のボイス切断",
                    )
                except Exception:
                    logger.warning("接続切替時に既存のボイス接続を切断できませんでした。", exc_info=True)
                    await interaction.followup.send(
                        "⚠️ 既存のボイス接続を切断できなかったため、接続先を切り替えられませんでした。"
                        "しばらく待ってからもう一度お試しください。"
                    )
                    return

        if await skip_superseded_voice_operation(interaction, is_current):
            return
        if await requester_moved():
            return
        reservation = live_voice.reserve(guild.id) if live_voice is not None and guild is not None else None
        if live_voice is not None and guild is not None and not reservation:
            # 同時に会話できる VC の上限。接続してから断ると無駄に出入りするため、先に確かめる。
            await interaction.followup.send(NOTICE_LIVE_CAPACITY, allowed_mentions=discord.AllowedMentions.none())
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
        if await promote_stage_speaker(interaction, voice_channel, leaving=True):
            return True
        guild = interaction.guild
        if guild is not None and guild.voice_client is voice_client:
            # 声を出せない接続を残さない（start_live_voice の失敗時と同じ片付け）。
            if self._voice_guard is not None:
                self._voice_guard.release(guild.id)
            try:
                await wait_for_cleanup(voice_client.disconnect(force=True), label="ステージ昇格失敗時の切断")
            except Exception:
                logger.warning("ステージの発言者になれなかった後の切断に失敗しました。", exc_info=True)
        return False

    async def start_live_voice(
        self, interaction: discord.Interaction, voice_client: LiveVoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, reservation: object | None = None,
    ) -> bool:
        """音声会話を始める。始められなければ案内して退出し、False を返す。"""
        guild = interaction.guild
        live_voice = self._live_voice
        voice_guard = self._voice_guard
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
                if voice_guard is not None:
                    voice_guard.release(guild.id)
                try:
                    await wait_for_cleanup(voice_client.disconnect(force=True), label="音声会話の開始失敗時の切断")
                except Exception:
                    logger.warning("音声会話を始められなかった後の切断に失敗しました。", exc_info=True)
        if not started:
            await interaction.followup.send(notice, allowed_mentions=discord.AllowedMentions.none())
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
                allowed_mentions=discord.AllowedMentions.none(),
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
                # 観客のまま同時枠だけを占有してしまう。
                if not await self.prepare_stage_for_live(interaction, attempted_voice_client, voice_channel):
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
        # /leave の後は Live へ自動で再接続しない。
        if self._live_voice is not None:
            await self._live_voice.stop(guild.id, reason="/leave")

        # 自律行動で自分から入っている場合は、締めの投稿・ステージ枠の後片付け・
        # セッション記録の破棄・切断までをサービス側がまとめて行う
        # （idea.md の「既存の /leave コマンドで強制的に退出できるようにする」）。
        # 配信していなければ False が返るので、従来どおりの切断へ進む。
        stopped = await self._autonomous.stop_session(guild.id, reason=f"/leave（{interaction.user}）")
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
            logger.error(f'切断エラー: {type(error).__name__}', exc_info=error)
            await interaction.followup.send("退出に失敗しました。")
