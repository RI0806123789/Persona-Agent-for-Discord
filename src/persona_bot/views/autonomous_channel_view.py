"""自律行動の配信先パネル（/functions → ⚙️ キャラ設定 → 🎙️ 配信先。管理者専用）。

自律行動でボットが自分から出ていくステージ／ボイスチャンネルを、種別ごとに1つずつ
指定する（#266）。指定は `GuildSettings.autonomous_stage_channel_id` /
`autonomous_voice_channel_id` に入り、`autonomous_stage.pick_channel` が使う。

  ・ステージの指定を優先し、使えなければボイスの指定、どちらも使えなければ
    従来どおり自動で選ぶ（削除済み・権限不足のチャンネルは使えない）。
  ・確定を押すまで `GuildSettings` を変更しない（キャラ設定・自発会話と同じ作法）。
  ・Discord の仕様上、管理者が手動でボットを別のチャンネルへ移すことは防げない。
    移された場合は、従来どおり配信を終了する（`VoiceGuard` と自律行動の停止条件）。

/functions のハブとキャラ設定パネルは5行を使い切っていてセレクトを足せないため、
専用のパネルに分けている。
"""

from collections.abc import Callable, Sequence
from typing import Any

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS
from persona_bot.core.app_state import GuildSettings
from persona_bot.services.autonomous_stage import can_use_stage, can_use_voice, find_channel
from persona_bot.utils.select_options import limit_keeping_selected
from persona_bot.views.view_parts import (
    authorize_administrator_operation, begin_panel_operation, edit_panel_message, BackButton,
    HubFactory, PanelView, select_option_text,
)

# セレクトで「指定なし（自動）」を表す値。SelectOption の value は文字列のみ。
_UNSET_VALUE = "0"
# 先頭の「指定なし」で1枠を使うため、チャンネルを並べられるのは残りの枠だけ。
_MAX_CHANNEL_OPTIONS = DISCORD_SELECT_MAX_OPTIONS - 1
_UNUSABLE_DESCRIPTION = "⚠️ 権限が足りないため、いまは使えません"
NOT_ADMIN_NOTICE = "この機能はサーバー管理者のみ利用できます。設定は変更されていません。"


def _visible_channels(channels: Sequence[Any], selected_id: int | None) -> list[Any]:
    """セレクトに並べるチャンネル。上限を超える分は出さないが、指定中のものは残す。"""
    return limit_keeping_selected(
        channels, lambda channel: bool(selected_id) and channel.id == selected_id, _MAX_CHANNEL_OPTIONS,
    )


def _channel_options(
    channels: Sequence[Any], selected_id: int | None,
    bot_member: Any, can_use: Callable[[Any, Any], bool],
) -> list[discord.SelectOption]:
    """先頭に「指定なし（自動）」を置いた選択肢を作る。使えないチャンネルは説明で知らせる。"""
    options = [discord.SelectOption(
        label="指定なし（自動）", value=_UNSET_VALUE, default=selected_id is None,
    )]
    for channel in _visible_channels(channels, selected_id):
        usable = bot_member is None or can_use(channel, bot_member)
        options.append(discord.SelectOption(
            label=select_option_text(channel.name, fallback=str(channel.id)),
            value=str(channel.id),
            default=channel.id == selected_id,
            description=None if usable else _UNUSABLE_DESCRIPTION,
        ))
    return options


def _describe_target(
    channels: Sequence[Any], channel_id: int | None,
    bot_member: Any, can_use: Callable[[Any, Any], bool],
) -> str:
    """指定の表示。使えない指定は、自動選択へ戻ることを添える。"""
    if channel_id is None:
        return "指定なし（自動で選びます）"
    channel = find_channel(channels, channel_id)
    if channel is None:
        return "⚠️ 見つかりません（削除された可能性があります。自動で選びます）"
    # チャンネル名の解決を Discord に任せる（名前に展開されるメンションで表示する）
    if bot_member is not None and not can_use(channel, bot_member):
        return f"<#{channel.id}>（⚠️ 権限が足りないため、いまは使えません。自動で選びます）"
    return f"<#{channel.id}>"


def _selected_id(values: list[str]) -> int | None:
    value = int(values[0])
    return value or None


class StageChannelSelect(ui.Select):
    """配信先のステージチャンネルを選ぶセレクト（単一選択）。"""

    def __init__(self, guild: discord.Guild, current_value: int | None) -> None:
        super().__init__(
            placeholder="🎭 配信先のステージチャンネルを選択…",
            options=_channel_options(guild.stage_channels, current_value, guild.me, can_use_stage),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AutonomousChannelView = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_stage_channel_id", _selected_id(self.values)))


class VoiceChannelSelect(ui.Select):
    """配信先のボイスチャンネルを選ぶセレクト（単一選択）。"""

    def __init__(self, guild: discord.Guild, current_value: int | None) -> None:
        super().__init__(
            placeholder="🔊 配信先のボイスチャンネルを選択…",
            options=_channel_options(guild.voice_channels, current_value, guild.me, can_use_voice),
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AutonomousChannelView = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_voice_channel_id", _selected_id(self.values)))


class AutonomousChannelConfirmButton(ui.Button):
    """確定ボタン。管理者であることを最新の Member で確かめてから保存する。"""

    def __init__(self) -> None:
        super().__init__(label="確定", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: AutonomousChannelView = self.view  # type: ignore[assignment]
        # パネルを開いた後で管理者でなくなった人には保存させない。押した時点の
        # interaction.user は後から更新されないため、Member を取り直して判定する。
        if not await authorize_administrator_operation(
            view, interaction, notice=NOT_ADMIN_NOTICE,
        ):
            return

        if not await view.ensure_settings_current(interaction):
            return
        view.guild_settings.autonomous_stage_channel_id = view.temp_stage_channel_id
        view.guild_settings.autonomous_voice_channel_id = view.temp_voice_channel_id
        # 開始に失敗し続けたときの待ち時間（最長4時間）も解除する。使える配信先へ
        # 指定し直したのに何時間も動かないのでは、直したか確かめようがないため
        # （💬 自発会話 パネルの確定と同じ）。
        view.guild_settings.reset_autonomous_retry()
        await view.guild_settings.persist()
        if not await view.ensure_settings_current(interaction):
            return

        await edit_panel_message(interaction, deferred=True, content=view.build_preview(confirmed=True), view=None)


class AutonomousChannelView(PanelView):
    """自律行動の配信先パネル全体を表す View（管理者専用）。

    ステージ・ボイスのセレクトと確定/戻るを持ち、確定を押すまで GuildSettings を
    変更しない。選択肢と表示は、開いているサーバーのチャンネル一覧から作る。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        guild: discord.Guild,
        *,
        make_hub: HubFactory | None = None,
        make_settings: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 配信先パネルは時間切れになりました。設定は変更されていません。"
                "再度 /functions を実行してください。"
            )
        )
        self.guild_settings = guild_settings
        self.guild = guild
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        # 配信先はキャラ設定の子パネルなので、戻るときはその親を作り直す。
        self.make_settings = make_settings

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_stage_channel_id: int | None = guild_settings.autonomous_stage_channel_id
        self.temp_voice_channel_id: int | None = guild_settings.autonomous_voice_channel_id

        self._add_all_items()

    def _add_all_items(self) -> None:
        """セレクト（row0〜1）と確定/戻る（row2）を並べる。選択のたびに組み直す。"""
        self.add_item(StageChannelSelect(self.guild, self.temp_stage_channel_id))
        self.add_item(VoiceChannelSelect(self.guild, self.temp_voice_channel_id))
        self.add_item(AutonomousChannelConfirmButton())
        self.add_item(BackButton(row=2, make_parent=self.make_settings))

    def build_preview(self, *, confirmed: bool = False) -> str:
        """一時選択値からパネルの表示テキストを組み立てる。"""
        guild = self.guild
        bot_member = guild.me
        if confirmed:
            header = "✅ 自律行動の配信先を更新しました（配信中なら、次の配信から反映されます）。"
        else:
            header = "🎙️ **配信先パネル** — 自律行動でボットが出ていくチャンネルを指定します（管理者のみ）。"
        autonomous_display = "✅ 有効" if self.guild_settings.autonomous_enabled else "❌ 無効"
        lines = [
            header,
            "・**ステージチャンネル**: "
            + _describe_target(guild.stage_channels, self.temp_stage_channel_id, bot_member, can_use_stage),
            "・**ボイスチャンネル**: "
            + _describe_target(guild.voice_channels, self.temp_voice_channel_id, bot_member, can_use_voice),
            f"・**自律行動**: {autonomous_display}（ON/OFF は 💬 自発会話 パネルで切り替えます）",
            "※ ステージの指定を優先し、使えなければボイスの指定、どちらも使えなければ"
            "これまでどおり自動で選びます（削除されたチャンネルや、権限が足りないチャンネルは使えません）。",
            "※ Discord の仕様上、管理者が手動でボットを別のチャンネルへ移すことは防げません。"
            "移された場合は配信を終了します。",
        ]
        for label, channels in (("ステージ", guild.stage_channels), ("ボイス", guild.voice_channels)):
            if len(channels) > _MAX_CHANNEL_OPTIONS:
                lines.append(
                    f"ℹ️ {label}チャンネルが多いため、先頭の{_MAX_CHANNEL_OPTIONS}件だけを選択肢に表示しています"
                    "（指定中のチャンネルは常に表示します）。"
                )
        return "\n".join(lines)
