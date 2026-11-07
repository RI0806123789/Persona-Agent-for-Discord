import logging
import discord
from discord import ui

from persona_bot.config.personas import refresh_persona_catalog
from persona_bot.core.app_state import GuildSettings
from persona_bot.core.background import run_in_thread
from persona_bot.views.auto_topic_view import AutoTopicView
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import is_administrator, is_bot_owner, is_current_bot_owner
from persona_bot.services.gemini_service import GeminiService
from persona_bot.views.help_view import HelpView, resolve_bot_name
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.reminder_service import ReminderService, ReminderStorageError
from persona_bot.views.reminder_view import ReminderView
from persona_bot.views.settings_view import SettingsView
from persona_bot.views.stats_privacy_view import StatsPrivacyView
from persona_bot.services.stats_service import StatsService
from persona_bot.views.stats_view import StatsView
from persona_bot.views.summary_view import SummaryView
from persona_bot.views.unblock_view import UnblockView
from persona_bot.utils.usage_graph import UsageTracker
from persona_bot.views.view_parts import (
    authorize_administrator_operation, authorize_panel_operation, begin_panel_operation, edit_panel_message,
    PanelView, send_graph_ephemeral, swap_panel,
)


logger = logging.getLogger(__name__)

GROUNDING_ADMIN_NOTICE = "サーバーのGoogle検索の設定を切り替えられるのは管理者のみです。設定は変更されていません。"


class OpenSettingsButton(ui.Button):
    """キャラ設定パネル（SettingsView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="キャラ設定", style=discord.ButtonStyle.primary, emoji="⚙️", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        # prompts/ に足したペルソナを再起動なしで選べるよう、開くたびに一覧を読み直す。
        # ファイルを読むので先に応答を保留する。
        await interaction.response.defer(ephemeral=True)
        await refresh_persona_catalog()
        # 全体の違反情報は、サーバー管理者にも件数を開示しない。
        blacklist_status = "非公開（ボット所有者のみ）"
        if is_bot_owner(interaction) and await is_current_bot_owner(interaction):
            blocked_users = hub.blacklist.get_all_blocked_users()
            blacklist_status = f"{len(blocked_users)}人" if blocked_users else "なし"
        settings_view = SettingsView(
            hub.guild_settings,
            ng_count=hub.ng_filter.word_count,
            ng_count_provider=lambda: hub.ng_filter.word_count,
            blacklist_status=blacklist_status,
            blacklist_provider=hub.blacklist.get_all_blocked_users,
            make_hub=hub.clone,
            # 押した時点の権限で、管理者専用の 🎙️ 配信先 を出すかを決める
            is_admin=is_administrator(interaction.user),
        )
        # 同じ（エフェメラルな）メッセージをキャラ設定パネルに差し替える
        await swap_panel(interaction, hub, settings_view, settings_view.build_preview())


class OpenSummaryButton(ui.Button):
    """会話要約パネル（SummaryView）を開くボタン。"""

    def __init__(self) -> None:
        super().__init__(label="会話要約", style=discord.ButtonStyle.success, emoji="📋", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        summary_view = SummaryView(
            hub.guild_settings,
            hub.gemini,
            hub.moderator,
            hub.ng_filter,
            hub.usage,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを要約パネルに差し替える
        await swap_panel(interaction, hub, summary_view, summary_view.build_preview())


class GraphButton(ui.Button):
    """利用トークングラフを表示するボタン（その場でエフェメラルに表示）。"""

    def __init__(self) -> None:
        super().__init__(label="グラフ", style=discord.ButtonStyle.secondary, emoji="📊", row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not hub.usage.has_data():
            await interaction.response.send_message("まだ利用データがありません。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        buffer = None
        try:
            buffer = await hub.usage.build_graph_buffer()
        except Exception as error:
            logger.error("グラフ生成中にエラーが発生しました。", exc_info=error)
            await interaction.followup.send("エラー: グラフの生成に失敗しました。", ephemeral=True)
            return
        # ハブは残したまま、グラフを新しいエフェメラルメッセージとして送る。
        await send_graph_ephemeral(
            interaction, buffer, "usage_graph.png", "📊 これまでの利用結果のグラフです。",
            followup=True, error_log="グラフ送信中に予期せぬエラー",
            error_notice="エラー: グラフの送信に失敗しました。",
        )


class GroundingToggleButton(ui.Button):
    """Google検索グラウンディングON/OFFトグルボタン。

    確定を待たず、押した瞬間にそのサーバーの GuildSettings.grounding_enabled を変更して保存する。
    """

    def __init__(self, enabled: bool) -> None:
        label = "🔍 Google検索: ON" if enabled else "🔍 Google検索: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        # サーバー全体の設定なので、サーバーでは管理者だけが切り替えられる（#398）。押した時点の写しで
        # 即時に断り、保留の後に最新の Member で確かめてから書き込む。DM は従来どおり本人が操作できる。
        if not await authorize_administrator_operation(
            hub, interaction, allow_dm=True, notice=GROUNDING_ADMIN_NOTICE,
        ):
            return
        hub.guild_settings.grounding_enabled = not hub.guild_settings.grounding_enabled
        # このトグルは確定操作が無く押した瞬間に反映されるため、その場で保存する
        await hub.guild_settings.persist()
        if not await hub.ensure_settings_current(interaction):
            return

        # ボタンの表示（ON/OFF）を更新するために View を再構築
        hub.refresh_items()
        await edit_panel_message(interaction, deferred=True, content=hub.build_preview(), view=hub)


class NgReloadButton(ui.Button):
    """NGワードリストを再読み込みするボタン。"""

    def __init__(self) -> None:
        super().__init__(label="NGワード再読込", style=discord.ButtonStyle.secondary, emoji="🔄", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await run_in_thread(hub.ng_filter.reload)
        except (OSError, UnicodeError):
            await interaction.followup.send(
                "⚠️ NGワードリストの再読み込みに失敗しました。従来の設定を保持しています。"
                "ファイルの読み取り権限と文字コードを確認してください。", ephemeral=True,
            )
            return
        # ハブは残したまま、結果を新しいエフェメラルメッセージとして送る
        await interaction.followup.send(
            f"✅ NGワードリストを再読み込みしました。（{hub.ng_filter.word_count}件）", ephemeral=True
        )


class StatsButton(ui.Button):
    """会話統計パネル（StatsView）を開くボタン。

    一般ユーザーは自分の統計のみ、管理者は他メンバーの統計・全体比較も閲覧できる。
    """

    def __init__(self) -> None:
        super().__init__(label="会話統計", style=discord.ButtonStyle.secondary, emoji="📈", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        # 全体の収集中データがなくても、本人は停止前の統計を閲覧できる。
        # データ不足の案内は、本人を初期対象にしたダッシュボード表示に任せる。
        begin_panel_operation(hub, interaction)
        member = interaction.user
        is_admin = is_administrator(member)
        stats_view = StatsView(hub.stats, viewer_id=str(member.id), is_admin=is_admin, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを統計パネルに差し替える
        await swap_panel(interaction, hub, stats_view, stats_view.build_preview())


class AutoTopicButton(ui.Button):
    """自発会話パネル（AutoTopicView）を開くボタン。

    「ボットが自分から話し出す」系の設定はすべてこのパネルにまとめている
    （テキストへの話題提供＝自発的な会話、ボイス/ステージでの配信＝自律行動）。
    入口を分けると、どちらを切ったのか分からなくなるため統一している。
    """

    def __init__(self) -> None:
        super().__init__(label="自発会話", style=discord.ButtonStyle.primary, emoji="💬", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        auto_topic_view = AutoTopicView(hub.guild_settings, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージを自発会話パネルに差し替える
        await swap_panel(interaction, hub, auto_topic_view, auto_topic_view.build_preview())


class ReminderButton(ui.Button):
    """リマインダーパネル（ReminderView）を開くボタン。

    表示するのは押した本人のリマインダーだけ（内容に予定などの私的情報が入るため）。
    """

    def __init__(self) -> None:
        super().__init__(label="リマインダー", style=discord.ButtonStyle.primary, emoji="⏰", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        await interaction.response.defer(ephemeral=True)
        viewer_id = str(interaction.user.id)
        try:
            records = await hub.reminders.list_pending(viewer_id)
        except ReminderStorageError:
            notice = (
                "⚠️ リマインダーの一覧を取得できませんでした。時間をおいて「⏰ リマインダー」を押してください。\n"
                "パネルが時間切れの場合は /functions から開き直してください。"
            )
            if not await edit_panel_message(interaction, deferred=True, content=notice, view=hub):
                await interaction.followup.send(notice, ephemeral=True)
            return
        reminder_view = ReminderView(hub.reminders, records, viewer_id, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージをリマインダーパネルに差し替える
        await swap_panel(interaction, hub, reminder_view, reminder_view.build_preview())


class HelpButton(ui.Button):
    """使い方パネル（HelpView）を開くボタン。

    メンションで会話できることや各機能の入口は、招待しただけでは気づけない。
    その説明をまとめた読み物パネルへの入口で、権限による制限は設けない
    （使い方は全員が見てよい情報のため）。
    """

    def __init__(self) -> None:
        super().__init__(label="使い方", style=discord.ButtonStyle.secondary, emoji="❓", row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        # ボット名はハードコードせず、押された時点のクライアントから解決する
        # （別名のボットで同じコードを動かしてもメンション例が正しくなるように）。
        help_view = HelpView(
            bot_name=resolve_bot_name(interaction.client.user),
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを使い方パネルに差し替える
        await swap_panel(interaction, hub, help_view, help_view.build_preview())


class StatsPrivacyButton(ui.Button):
    """統計プライバシーパネル（StatsPrivacyView）を開くボタン。

    会話統計・性格推定の収集を本人が停止したり、蓄積データを削除したりする入口。
    対象は常に押した本人の分だけで、他人の設定は見ることも変えることもできない。
    """

    def __init__(self) -> None:
        super().__init__(label="統計プライバシー", style=discord.ButtonStyle.secondary, emoji="🔒", row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]
        begin_panel_operation(hub, interaction)

        privacy_view = StatsPrivacyView(
            hub.stats,
            viewer_id=str(interaction.user.id),
            viewer_name=interaction.user.display_name,
            make_hub=hub.clone,
        )
        # 同じ（エフェメラルな）メッセージを統計プライバシーパネルに差し替える
        await swap_panel(interaction, hub, privacy_view, privacy_view.build_preview())


class UnblockButton(ui.Button):
    """ブロック解除パネル（UnblockView）を開くボタン（ボット所有者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="ブロック解除", style=discord.ButtonStyle.danger, emoji="🚫", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        hub: "FunctionsView" = self.view  # type: ignore[assignment]

        if not await authorize_panel_operation(
            hub, interaction, allowed=is_bot_owner(interaction), recheck=is_current_bot_owner,
            notice="この機能はボット所有者のみ利用できます。",
        ):
            return
        blocked_users = hub.blacklist.get_all_blocked_users()
        if not blocked_users:
            await edit_panel_message(
                interaction, deferred=True,
                content="現在ブラックリストに登録されているユーザーはいません。", view=None,
            )
            return

        unblock_view = UnblockView(hub.blacklist, blocked_users, make_hub=hub.clone)
        # 同じ（エフェメラルな）メッセージをブロック解除パネルに差し替える
        await swap_panel(
            interaction, hub, unblock_view, unblock_view.build_preview()
        )


class FunctionsView(PanelView):
    """各種機能への入口となるハブパネル。

    Discord の View は最大5行までのため、キャラ設定パネルと要約パネルを1枚に合体できない。
    そこで、ボタンで選んだ機能のパネルに同じメッセージを差し替える方式で統合する。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        usage: UsageTracker,
        ng_filter: NgWordFilter,
        blacklist: BlacklistService,
        gemini: GeminiService,
        moderator: ContentModerator,
        stats: StatsService,
        reminders: ReminderService,
        *,
        is_admin: bool = False,
        is_owner: bool = False,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 機能パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        # このパネルが開かれたサーバー（DM の場合は全DM共有スロット）専用の設定。
        self.guild_settings = guild_settings
        self.usage = usage
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.gemini = gemini
        self.blocked_user_check = blacklist.is_blocked
        self.moderator = moderator
        self.stats = stats
        self.reminders = reminders
        self.is_admin = is_admin
        self.is_owner = is_owner

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。Google検索トグル更新時にも使用する。"""
        # row0: パネルを開く系（キャラ設定 / 会話要約）＋ その場で実行するグラフ
        self.add_item(OpenSettingsButton())
        self.add_item(OpenSummaryButton())
        self.add_item(GraphButton())
        # row1: その場で実行する系（NG再読込）＋ パネルを開く会話統計・自発会話
        self.add_item(NgReloadButton())
        self.add_item(StatsButton())
        self.add_item(AutoTopicButton())
        # 全サーバー共通のブロック解除は、ボット所有者本人だけに表示する。
        if self.is_owner:
            self.add_item(UnblockButton())
        # row2: パネルを開くリマインダー ＋ Google検索のその場トグル
        self.add_item(ReminderButton())
        self.add_item(GroundingToggleButton(self.guild_settings.grounding_enabled))
        # row3: 使い方（各機能の説明。オンボーディングの入口）
        self.add_item(HelpButton())
        # row4: 統計プライバシー（本人の統計収集停止・データ削除）
        self.add_item(StatsPrivacyButton())

    def clone(self, interaction: discord.Interaction) -> "FunctionsView":
        """同じ依存関係と、戻る操作時の管理者権限で新しいハブを作る。"""
        return FunctionsView(
            self.guild_settings,
            self.usage,
            self.ng_filter,
            self.blacklist,
            self.gemini,
            self.moderator,
            self.stats,
            self.reminders,
            is_admin=is_administrator(interaction.user),
            is_owner=is_bot_owner(interaction),
        )

    def build_preview(self) -> str:
        """ハブパネルの表示テキスト。ボット所有者のみブロック解除の案内を出す。"""
        grounding_display = "✅ 有効" if self.guild_settings.grounding_enabled else "❌ 無効"
        lines = [
            "🛠️ **機能パネル** — 使いたい機能を選んでください。",
            "・⚙️ キャラ設定 / 📋 会話要約 / 📈 会話統計 / 💬 自発会話 / ⏰ リマインダー … パネルを開く",
            "　└ 💬 自発会話 … テキストへの話題提供と、🎙️ 自律行動"
            "（ボットが自分からボイス/ステージへ出て配信する）の ON/OFF",
            "・📊 グラフ / 🔄 NGワード再読込 … その場で実行",
            f"・🔍 Google検索: {grounding_display} … ボタンでその場で切替（サーバーでは管理者のみ）",
            "・❓ 使い方 … 会話のしかたと各機能の説明を表示",
            "・🔒 統計プライバシー … 会話統計の収集停止・データ削除（あなた自身の分だけ）",
        ]
        if self.is_owner:
            lines.append("・🚫 ブロック解除 … ボット所有者専用")
        return "\n".join(lines)
