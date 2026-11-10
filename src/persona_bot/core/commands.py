"""スラッシュコマンドの登録と、共通のゲート配線（#414）。

`register_commands` はコマンドの登録と、失敗通知・所有者の終了待機・ブラックリスト判定といった
共通の入口だけを担当する。ボイスの参加・退出・音声会話の開始の実処理は
`voice_commands.VoiceSessionCommands` にある。
"""

from functools import wraps
import logging

import discord
from discord import app_commands

from persona_bot.core.app_state import BotState
from persona_bot.core.background import TaskOwner, wait_for_cleanup
from persona_bot.core.voice_commands import (
    VoiceCommandCoordinator,
    VoiceSessionCommands,
    join_permission_notice,
)
from persona_bot.config.constants import LIVE_ENABLED
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.voice_conversation import VoiceConversationService
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
    voice_coordinator = VoiceCommandCoordinator()
    # 音声会話（Gemini Live。#268）。None・無効なら /join は接続だけを行う。
    voice_session = VoiceSessionCommands(
        state, autonomous, voice_guard=voice_guard,
        live_voice=voice_conversation if LIVE_ENABLED else None,
    )

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
        # 接続を試す前に権限を確かめ、不足していれば原因を具体的に案内する。
        notice = join_permission_notice(voice_channel, guild.me if guild is not None else None)
        if notice is not None:
            await interaction.response.send_message(notice, allowed_mentions=discord.AllowedMentions.none())
            return

        await voice_coordinator.run(
            interaction, lambda is_current: voice_session.join(interaction, voice_channel, is_current),
        )

    @tree.command(
        name="leave",
        description="ボットがボイスチャンネルから退出します（自律行動の配信もここで止められます）",
    )
    @owned_command
    async def leave(interaction: discord.Interaction) -> None:
        if await reject_blocked_interaction(interaction, blacklist.is_blocked):
            return
        if interaction.guild is None:
            await interaction.response.send_message("ボットはボイスチャンネルに参加していません。")
            return

        await voice_coordinator.run(interaction, lambda _: voice_session.leave(interaction))
