import asyncio
from functools import wraps
import logging
from typing import Callable

import discord
from discord import app_commands

from persona_bot.audio.voice_receive import LiveVoiceClient
from persona_bot.core.app_state import BotState
from persona_bot.core.background import TaskOwner, wait_for_cleanup
from persona_bot.core.voice_commands import VoiceCommandCoordinator, skip_superseded_voice_operation
from persona_bot.config.constants import LIVE_ENABLED, VOICE_CONNECT_TIMEOUT_SECONDS
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.live_session import LiveUnavailableError
from persona_bot.services.voice_conversation import (
    NOTICE_CONSENT_PANEL_FAILED,
    NOTICE_LIVE_CAPACITY,
    NOTICE_LIVE_QUOTA,
    NOTICE_LIVE_UNAVAILABLE,
    ConsentPanelError,
    VoiceConversationService,
)
from persona_bot.services.autonomous_stage import enable_stage_speaker
from persona_bot.utils.voice_permissions import missing_voice_permissions
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import is_administrator, is_bot_owner, reject_blocked_interaction
from persona_bot.views.functions_view import FunctionsView
from persona_bot.views.view_parts import edit_panel_message
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.reminder_service import ReminderService
from persona_bot.services.stats_service import StatsService
from persona_bot.services.voice_guard import VoiceGuard
from persona_bot.utils.usage_graph import UsageTracker


logger = logging.getLogger(__name__)


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


async def handle_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError,
) -> None:
    """コマンドの失敗を応答状態に合わせて本人へ通知する。例外本文は公開しない。"""
    original = getattr(error, "original", error)
    logger.error(f'スラッシュコマンドの実行に失敗しました: {type(original).__name__}', exc_info=original)
    if isinstance(error, app_commands.NoPrivateMessage):
        notice = "このコマンドはサーバー内で実行してください。"
    elif (
        isinstance(error, (app_commands.MissingPermissions, app_commands.BotMissingPermissions))
        or isinstance(original, discord.Forbidden)
    ):
        notice = "⚠️ コマンドの実行に必要な権限がありません。サーバーとチャンネルの権限設定を確認してください。"
    elif isinstance(error, app_commands.CommandOnCooldown):
        notice = "⚠️ 操作の間隔が短すぎます。しばらく待ってからもう一度お試しください。"
    elif isinstance(error, app_commands.CheckFailure):
        notice = "⚠️ このコマンドを実行する条件を満たしていません。"
    else:
        notice = "⚠️ コマンドの実行中にエラーが発生しました。しばらく待ってからもう一度お試しください。"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(notice, ephemeral=True)
        else:
            await interaction.response.send_message(notice, ephemeral=True)
    except Exception as send_error:
        logger.error(f'スラッシュコマンドの失敗通知を送信できませんでした: {type(send_error).__name__}', exc_info=send_error)


async def announce_voice_join(
    interaction: discord.Interaction, channel: discord.VoiceChannel | discord.StageChannel,
    *, already_connected: bool = False, live: bool = False,
) -> None:
    """ステージでは発言者へ昇格してから、接続の結果を案内する。

    live が真なら、音声会話（Gemini Live）の同意パネルへの案内を添える。
    """
    if isinstance(channel, discord.StageChannel):
        try:
            await enable_stage_speaker(interaction.guild, channel)
        except Exception as error:
            logger.error(f'ステージの発言者への昇格に失敗しました: {type(error).__name__}', exc_info=error)
            await interaction.followup.send(
                f"⚠️ {channel.name} へ接続していますが、ステージの発言者になれませんでした。"
                "ボットを発言者へ招待するか、管理者にステージの「メンバーをミュート」権限を確認してください。",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
    message = (f"{channel.name} には既に接続しています。" if already_connected
               else f"{channel.name} に接続しました。")
    if isinstance(channel, discord.StageChannel):
        message += "ステージの発言者として話します。"
    if live:
        message += (
            "\n🎙️ ボイスチャンネルの参加者全員が同意パネルの「同意して参加」を押すと、"
            "ボットと声で会話できます。"
        )
    await interaction.followup.send(message, allowed_mentions=discord.AllowedMentions.none())


def register_commands(
    tree: app_commands.CommandTree,
    state: BotState,
    usage: UsageTracker,
    ng_filter: NgWordFilter,
    blacklist: BlacklistService,
    gemini: GeminiService,
    moderator: ContentModerator,
    stats: StatsService,
    reminders: ReminderService,
    autonomous: AutonomousService,
    *,
    voice_guard: VoiceGuard | None = None,
    task_owner: TaskOwner | None = None,
    voice_conversation: VoiceConversationService | None = None,
) -> None:
    tree.on_error = handle_command_error
    command_tasks = task_owner if task_owner is not None else TaskOwner()
    voice_commands = VoiceCommandCoordinator()
    # 音声会話（Gemini Live。#268）。None・無効なら /join は接続だけを行う。
    live_voice = voice_conversation if LIVE_ENABLED else None

    def owned_command(callback):
        """Discordが生成したコマンドの本体と失敗通知を、所有者の終了待機へ登録する。"""
        @wraps(callback)
        async def invoke(interaction: discord.Interaction) -> None:
            async def run_command() -> None:
                try:
                    await callback(interaction)
                except Exception as error:
                    # SDKと同じ例外形式を使い、失敗通知を所有期間内で一度だけ行う。
                    interaction.command_failed = True
                    if not isinstance(error, app_commands.AppCommandError):
                        error = app_commands.CommandInvokeError(interaction.command, error)
                    await wait_for_cleanup(
                        handle_command_error(interaction, error), label="スラッシュコマンドの失敗通知",
                    )

            await command_tasks.run(run_command())

        return invoke

    @tree.command(name="functions", description="各種機能パネルを開きます（キャラ設定 / 会話要約 / 会話統計 / 自発会話 / リマインダー）。")
    @owned_command
    async def functions(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        # 統計などの管理者機能と、所有者限定のブロック解除を分けて判定する。
        is_admin = is_administrator(interaction.user)
        # ペルソナ・モデル等はサーバー（ギルド）ごとに独立している。DM では guild_id が
        # 無いため、全DM共有の設定スロット（state.dm_settings）が使われる。
        deferred = False
        try:
            guild_settings = state.get_guild(interaction.guild_id)
        except RuntimeError:
            # 初回復元だけDB待機を伴うため、Discordの応答期限より先に受領を返す。
            await interaction.response.defer(ephemeral=True)
            deferred = True
            guild_settings = await state.get_guild_async(interaction.guild_id)
        view = FunctionsView(
            guild_settings, usage, ng_filter, blacklist, gemini, moderator, stats, reminders,
            is_admin=is_admin, is_owner=is_bot_owner(interaction),
        )
        # 機能パネルは本人だけに見えるよう ephemeral で送信する
        if deferred:
            await edit_panel_message(interaction, content=view.build_preview(), view=view, deferred=True)
        else:
            await interaction.response.send_message(view.build_preview(), view=view, ephemeral=True)
        # タイムアウト時に UI を取り除けるよう、送信したパネルメッセージを View に持たせる
        view.message = await interaction.original_response()

    @tree.command(name="join", description="ボットがボイスチャンネルに参加します")
    @owned_command
    async def join(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("先にボイスチャンネルに参加してください。")
            return

        voice_channel = member.voice.channel
        guild = interaction.guild
        is_stage = isinstance(voice_channel, discord.StageChannel)

        # 接続を試す前に権限を確かめる。権限不足は接続失敗の最も多い原因だが、
        # connect() の Forbidden として出るため、後段でまとめて拾うと
        # 「接続に失敗しました」としか伝えられず原因にたどり着けない。
        bot_member = guild.me if guild is not None else None
        if bot_member is not None:
            missing_permissions = missing_voice_permissions(voice_channel, bot_member, stage_moderation=is_stage)
            if missing_permissions:
                permission_names = "・".join(f"「{name}」" for name in missing_permissions)
                await interaction.response.send_message(
                    f"⚠️ {voice_channel.name} でボットに{permission_names}の権限がありません。\n"
                    "サーバー設定 →「連携サービス」か、そのボイスチャンネルの権限設定で、"
                    f"ボットのロールに{permission_names}を許可してください"
                    "（チャンネル個別の権限で拒否されている場合もあります）。"
                    + ("ステージでは観客から発言者になるため「メンバーをミュート」が必要です。" if is_stage else ""),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

        await voice_commands.run(
            interaction, lambda is_current: join_voice(interaction, voice_channel, is_current),
        )

    async def join_voice(
        interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        is_current: Callable[[], bool],
    ) -> None:
        guild = interaction.guild

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
            await autonomous.stop_session(
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
                    if live_voice is not None:
                        conversation = live_voice.get(guild.id)
                        if (conversation is None or conversation.stopped
                                or conversation.voice_client is not voice_client
                                or conversation.channel_id != voice_channel.id):
                            # 接続はあるが、このチャンネルの音声会話が動いていない（止まっている・
                            # 移された先で /join された）。同じ接続のまま、このチャンネル用に始め直す。
                            if not await start_live_voice(interaction, voice_client, voice_channel):
                                return
                    await announce_voice_join(
                        interaction, voice_channel, already_connected=True, live=live_voice is not None,
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
            await connect_voice(interaction, voice_channel, reservation=reservation)
        finally:
            # 開始に進まなかった（接続の失敗・中断）場合も、確保した枠を残さない。
            if live_voice is not None and guild is not None:
                live_voice.release_reservation(guild.id, reservation=reservation)

    async def start_live_voice(
        interaction: discord.Interaction, voice_client: LiveVoiceClient,
        voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, reservation: object | None = None,
    ) -> bool:
        """音声会話を始める。始められなければ案内して退出し、False を返す。"""
        guild = interaction.guild
        reservation = reservation if reservation is not None else live_voice.reserve(guild.id)
        started = False
        notice = NOTICE_LIVE_CAPACITY
        try:
            if reservation:
                guild_settings = await state.get_guild_async(guild.id)
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
        interaction: discord.Interaction, voice_channel: discord.VoiceChannel | discord.StageChannel,
        *, reservation: object | None = None,
    ) -> None:
        guild = interaction.guild
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
            if live_voice is not None and guild is not None:
                if not await start_live_voice(
                    interaction, attempted_voice_client, voice_channel, reservation=reservation,
                ):
                    return
            await announce_voice_join(interaction, voice_channel, live=live_voice is not None)

    @tree.command(
        name="leave",
        description="ボットがボイスチャンネルから退出します（自律行動の配信もここで止められます）",
    )
    @owned_command
    async def leave(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
            return

        await voice_commands.run(interaction, lambda _: leave_voice(interaction))

    async def leave_voice(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        # 退出の後に、移動を戻す処理が元のチャンネルへ入り直さないよう先に保護を解く。
        if voice_guard is not None:
            voice_guard.release(guild.id)
        # 音声会話は切断の前に止める（受信・再生・Live の接続と同意パネルを片付ける）。
        # /leave の後は Live へ自動で再接続しない。
        if live_voice is not None:
            await live_voice.stop(guild.id, reason="/leave")

        # 自律行動で自分から入っている場合は、締めの投稿・ステージ枠の後片付け・
        # セッション記録の破棄・切断までをサービス側がまとめて行う
        # （idea.md の「既存の /leave コマンドで強制的に退出できるようにする」）。
        # 配信していなければ False が返るので、従来どおりの切断へ進む。
        stopped = await autonomous.stop_session(guild.id, reason=f"/leave（{interaction.user}）")
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

