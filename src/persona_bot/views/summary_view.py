import logging

import discord
from discord import ui

from persona_bot.core.app_state import GuildSettings
from persona_bot.config.choices import SUMMARY_PERIOD_CHOICES
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.utils.discord_helpers import (
    is_supported_message_channel,
    send_long_message,
)
from persona_bot.services.summary_service import generate_channel_summary
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.utils.usage_graph import UsageTracker
from persona_bot.views.view_parts import (
    begin_panel_operation, confirm_panel_not_blocked, edit_panel_message, panel_operation_is_current,
    BackButton, HubFactory, PanelView, report_panel_failure, send_panel_result,
)


logger = logging.getLogger(__name__)

# 期間選択のデフォルト（SUMMARY_PERIOD_CHOICES の「1日」）
_DEFAULT_PERIOD = next(c for c in SUMMARY_PERIOD_CHOICES if c.value == 24)

# 要約の途中で例外が起きたときにパネルへ出す案内
_FAILURE_NOTICE = "⚠️ 要約の処理中にエラーが発生しました。時間をおいて再度お試しください。"
# 「全員に表示」でチャンネルへの投稿を拒否された（権限不足）
_FORBIDDEN_NOTICE = (
    "⚠️ このチャンネルへ要約を投稿する権限がありません。"
    "ボットの権限設定を確認するか、表示範囲を「🔒 自分だけ」にして再度お試しください。"
)
# 「全員に表示」の投稿が途中で失敗した（分割した一部だけが投稿されていることがある）
_PARTIAL_POST_NOTICE = (
    "⚠️ 要約の投稿に失敗しました。一部が投稿されている可能性があります。チャンネルを確認してください。"
)




class SummaryPeriodSelect(ui.Select):
    """要約する期間を選ぶセレクトメニュー。"""

    def __init__(self, current_value: int) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                # SelectOption の value は文字列のみ。int は str に変換して保持する。
                value=str(choice.value),
                default=(choice.value == current_value),
            )
            for choice in SUMMARY_PERIOD_CHOICES
        ]
        super().__init__(placeholder="🕒 要約する期間を選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(
            view, "temp_period", next(c for c in SUMMARY_PERIOD_CHOICES if c.value == int(self.values[0])),
        ))


class PrivateToggleButton(ui.Button):
    """要約結果の表示範囲（自分だけ / 全員）を切り替えるトグルボタン。"""

    def __init__(self, private: bool) -> None:
        label = "🔒 表示: 自分だけ" if private else "🌐 表示: 全員"
        style = discord.ButtonStyle.primary if private else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_private", not view.temp_private))


class RunSummaryButton(ui.Button):
    """要約を実行するボタン。"""

    def __init__(self) -> None:
        super().__init__(label="要約実行", style=discord.ButtonStyle.success, emoji="📋", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "SummaryView" = self.view  # type: ignore[assignment]
        begin_panel_operation(view, interaction)

        channel = interaction.channel
        if not is_supported_message_channel(channel):
            await edit_panel_message(interaction,
                content="このチャンネルでは要約を実行できません。", view=None
            )
            return

        # パネル（エフェメラル）を「要約中」に更新してコンポーネント応答を返す。
        period, private = view.temp_period, view.temp_private
        if not await edit_panel_message(interaction, content="🔄 要約中...", view=None):
            return

        bot_user = interaction.client.user
        # パネルに要約の1通目を表示済みか（失敗の案内でそれを上書きしないため）
        result_shown = [False]
        # 「要約中...」のまま残さないよう、生成から送信までの想定外の例外をパネルへ伝える。
        # Gemini の通信エラーやタイムアウトは generate_channel_summary の中で案内文に
        # 変わり、全員表示の投稿の失敗は下で個別に案内する。ここで拾うのは、それ以外の
        # 例外（プロンプトの読み込み失敗・パネルの更新失敗など）。
        # キャンセル（CancelledError）は Exception ではないので伝わる。
        try:
            is_summary, text = await generate_channel_summary(
                channel,
                bot_user,
                period,
                guild_settings=view.guild_settings,
                gemini=view.gemini,
                moderator=view.moderator,
                ng_filter=view.ng_filter,
                usage=view.usage,
                is_blocked=view.blocked_user_check,
            )

            if not panel_operation_is_current(view, interaction):
                return
            if not await view.ensure_settings_current(interaction, deferred=True):
                return
            if not await confirm_panel_not_blocked(view, interaction):
                return

            # 通知・エラーはパネル（本人だけに見える）にそのまま表示して終了。
            if not is_summary:
                await edit_panel_message(interaction, deferred=True, content=text)
                return

            if not text.strip():
                await report_panel_failure(interaction, _FAILURE_NOTICE, keep_result=False, label="要約")
                return

            if private:
                # 結果を自分だけに: パネルを結果で上書きし、続きも本人だけの followup で送る。
                if not await send_panel_result(interaction, text, result_shown):
                    return
            else:
                # 結果を全員に: チャンネルへ投稿し、パネルは完了表示にする。
                # 投稿の失敗だけは原因に合わせて案内する（権限不足か、途中まで投稿されたか）。
                try:
                    await send_long_message(channel, text, allowed_mentions=discord.AllowedMentions.none())
                # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること。
                except discord.Forbidden as error:
                    logger.warning("要約: チャンネルへの投稿を拒否されました。", exc_info=error)
                    await report_panel_failure(interaction, _FORBIDDEN_NOTICE, keep_result=False, label="要約")
                    return
                except discord.HTTPException as error:
                    logger.error("要約: チャンネルへの投稿に失敗しました。", exc_info=error)
                    await report_panel_failure(interaction, _PARTIAL_POST_NOTICE, keep_result=False, label="要約")
                    return
                await edit_panel_message(interaction, deferred=True, content="✅ 要約をチャンネルに投稿しました。")
        except Exception as error:
            logger.error("要約: 生成・送信中にエラーが発生しました。", exc_info=error)
            # 例外の文面には内部の情報が含まれうるため、利用者には定型文だけを見せる。
            await report_panel_failure(interaction, _FAILURE_NOTICE, keep_result=result_shown[0], label="要約")


class SummaryView(PanelView):
    """会話要約の設定パネル全体を表す View。

    期間セレクト・表示範囲トグル・要約実行/閉じるボタンを保持する。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 要約パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.guild_settings = guild_settings
        self.gemini = gemini
        self.moderator = moderator
        self.ng_filter = ng_filter
        self.usage = usage
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        # 一時選択値
        self.temp_period = _DEFAULT_PERIOD
        self.temp_private: bool = False  # 結果を自分だけに表示するか（デフォルト: 全員）

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。選択・トグル更新時にも使用する。"""
        self.add_item(SummaryPeriodSelect(self.temp_period.value))
        self.add_item(PrivateToggleButton(self.temp_private))
        self.add_item(RunSummaryButton())
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        visibility = "🔒 自分だけ" if self.temp_private else "🌐 全員に表示"
        return (
            "📋 **会話要約パネル** — 期間と表示範囲を選んで「要約実行」を押してください。\n"
            f"・**期間**: {self.temp_period.name}\n"
            f"・**表示範囲**: {visibility}"
        )
