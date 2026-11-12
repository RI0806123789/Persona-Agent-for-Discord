import logging
import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS
from persona_bot.utils.discord_helpers import is_administrator, is_current_administrator, split_message
from persona_bot.services.feedback_service import FeedbackService, get_default_service
from persona_bot.services.stats_service import StatsService
from persona_bot.views.view_parts import (
    begin_panel_operation, confirm_panel_permission, edit_panel_message, panel_operation_is_current,
    report_panel_failure, send_panel_result,
    BackButton, HubFactory, PanelView,
    select_option_text, send_graph_ephemeral,
)


logger = logging.getLogger(__name__)

# 同じ集計の実行中にもう一度押されたときの案内（#398）
AGGREGATION_BUSY_NOTICE = "集計中です。結果が届くまでお待ちください。"

# 項目セレクトの値 → 表示名
_ITEM_LABELS = {
    "all": "全部",
    "time": "時間帯",
    "topic": "話題",
    "personality": "性格推定",
}


class StatsItemSelect(ui.Select):
    """表示する統計項目を選ぶセレクトメニュー。"""

    def __init__(self, current: str) -> None:
        options = [
            discord.SelectOption(label=label, value=value, default=(value == current))
            for value, label in _ITEM_LABELS.items()
        ]
        super().__init__(placeholder="📊 表示する項目を選択…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]
        def update() -> None:
            view.refresh_permissions(interaction.user)
            view.temp_item = self.values[0]
        await view.update_preview(interaction, update)


class StatsUserSelect(ui.Select):
    """集計対象ユーザーを選ぶセレクトメニュー（管理者のみ表示）。"""

    def __init__(self, users: list[tuple[str, str, int]], current: str) -> None:
        options = []
        selected = users[:DISCORD_SELECT_MAX_OPTIONS]
        if not any(str(uid) == current for uid, _, _ in selected):
            target = next((user for user in users if str(user[0]) == current), None)
            if target is not None:
                selected = selected[:DISCORD_SELECT_MAX_OPTIONS - 1] + [target]
        for user_id, name, count in selected:
            options.append(
                discord.SelectOption(
                    label=select_option_text(name, fallback=str(user_id)),
                    value=str(user_id),
                    description=f"発言 {count} 回",
                    default=(str(user_id) == current),
                )
            )
        super().__init__(placeholder="👤 対象ユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]
        def update() -> None:
            view.refresh_permissions(interaction.user)
            if view.is_admin:
                view.temp_target_id = self.values[0]
        await view.update_preview(interaction, update)


class RunStatsButton(ui.Button):
    """選択中の項目でダッシュボードを表示するボタン。"""

    def __init__(self) -> None:
        super().__init__(label="表示", style=discord.ButtonStyle.success, emoji="📈", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        # 一般ユーザーは自分のデータに固定する（他人のデータは管理者専用）。
        target_id = view.temp_target_id if view.is_admin else view.viewer_id
        if target_id != view.viewer_id and not is_administrator(interaction.user):
            await interaction.response.send_message("この機能はサーバー管理者のみ利用できます。", ephemeral=True)
            return

        begin_panel_operation(view, interaction)
        # パネル（エフェメラル）を「集計中」に更新してから長時間処理に入る。
        item = view.temp_item
        if not await edit_panel_message(interaction, content="🔄 集計中...", view=None):
            return

        # viewer_id（誰が見ているか）を必ず渡す。省略すると build_dashboard が
        # 「本人が見ている」とみなし、管理者が他人の統計を開いたときに
        # 🔕 収集停止中の注記まで見せてしまう（＝相手の opt-out が露見する）。
        # 管理者のユーザー選択は収集停止中の人を除外するため通常は選べないが、
        # 「選んだ後に相手が停止した」場合はパネルが開いたままなので到達しうる。
        images = []
        files = []
        result_shown = [False]
        try:
            caption, images = await view.stats.build_dashboard(
                target_id, item, viewer_id=view.viewer_id
            )
            # 描画・解析の待機中にも権限は変わりうる。interaction.user は押した時点の写しなので、
            # 送信直前に最新の Member で他人のデータを守る（本人の統計は管理者でなくてよい）。
            allowed = target_id == view.viewer_id or await is_current_administrator(interaction)
            if not panel_operation_is_current(view, interaction):
                return
            if not await confirm_panel_permission(
                interaction, allowed, notice="この機能はサーバー管理者のみ利用できます。",
            ):
                return
            # 途中の生成失敗でも、それまでに作成したFileをfinallyで回収する。
            for filename, buffer in images:
                files.append(discord.File(buffer, filename=filename))
            if not await send_panel_result(interaction, caption, result_shown, fallback="表示できる統計の本文がありません。"):
                return
            if files:
                # グラフ画像は本人だけに見えるエフェメラルの followup として送る。
                await interaction.followup.send(files=files, ephemeral=True)
        except Exception as error:
            logger.error("会話統計の生成・送信中にエラーが発生しました。", exc_info=error)
            await report_panel_failure(
                interaction, "エラー: 統計の表示に失敗しました。", keep_result=result_shown[0], label="会話統計",
            )
        finally:
            # discord.Fileが差し替えたcloseを戻してから、元のバッファを閉じる。
            for file in files:
                file.close()
            for _filename, buffer in images:
                buffer.close()


class RankingButton(ui.Button):
    """発言回数ランキング（全体比較）を表示するボタン（管理者専用）。"""

    def __init__(self) -> None:
        super().__init__(label="全体比較", style=discord.ButtonStyle.secondary, emoji="📊", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        if not is_administrator(interaction.user):
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return
        if not view.try_begin_aggregation("ranking"):
            await interaction.response.send_message(AGGREGATION_BUSY_NOTICE, ephemeral=True)
            return
        try:
            await self._show_ranking(view, interaction)
        finally:
            view.end_aggregation("ranking")

    @staticmethod
    async def _show_ranking(view: "StatsView", interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        buffer = None
        try:
            buffer = await view.stats.build_ranking_graph()
        except Exception as error:
            logger.error("グラフ生成中にエラーが発生しました。", exc_info=error)
            await interaction.followup.send("エラー: グラフの生成に失敗しました。", ephemeral=True)
            return
        # 描画の待機中にロールが外されうるので、送る直前に最新の Member で確かめる。
        # 拒否・取得失敗・キャンセルのどれでも、作った画像のバッファを回収する。
        allowed = False
        try:
            allowed = await is_current_administrator(interaction)
        finally:
            if not allowed and buffer is not None:
                buffer.close()
        if not await confirm_panel_permission(
            interaction, allowed, notice="この機能はサーバー管理者のみ利用できます。", followup=True,
        ):
            return
        if buffer is None:
            await interaction.followup.send(
                "ランキングを表示できるデータがありません。", ephemeral=True
            )
            return
        # パネルは残したまま、ランキングを新しいエフェメラルメッセージで送る。
        await send_graph_ephemeral(
            interaction, buffer, "stats_ranking.png", "📊 発言回数ランキング（全体比較）です。",
            followup=True, error_log="ランキング送信中にエラー",
            error_notice="エラー: ランキングの送信に失敗しました。",
        )


class FeedbackButton(ui.Button):
    """ボット返信への 👍/👎 の集計を表示するボタン（管理者専用）。

    「どのモデル・どのペルソナの返信が good だったか」を見るためのもので、
    会話品質の分析という点で会話統計パネルの目的と一致するためここへ置く。
    """

    def __init__(self) -> None:
        super().__init__(label="フィードバック", style=discord.ButtonStyle.secondary, emoji="👍", row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsView" = self.view  # type: ignore[assignment]

        # ボタンを出す条件（管理者のみ）とは別に、押された時点でも再判定する
        # （RankingButton と同じ二重チェック。権限はサーバー側の値で判断する）。
        if not is_administrator(interaction.user):
            await interaction.response.send_message(
                "この機能はサーバー管理者のみ利用できます。", ephemeral=True
            )
            return

        # 集計は「そのサーバー分だけ」に限定する。DM 分（guild_id=0）は誰にも見せない。
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "フィードバックの集計はサーバー内でのみ確認できます。", ephemeral=True
            )
            return

        feedback = view.feedback
        if feedback is None:
            await interaction.response.send_message(
                "フィードバック機能が利用できません。", ephemeral=True
            )
            return

        if not view.try_begin_aggregation("feedback"):
            await interaction.response.send_message(AGGREGATION_BUSY_NOTICE, ephemeral=True)
            return
        try:
            await self._show_summary(feedback, interaction)
        finally:
            view.end_aggregation("feedback")

    @staticmethod
    async def _show_summary(feedback: FeedbackService, interaction: discord.Interaction) -> None:
        # パネルは残したまま、集計を新しいエフェメラルメッセージとして送る。
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            summary = await feedback.build_summary_text(interaction.guild_id)
            # 集計の待機中に権限を失った場合も、管理者用の結果を送らない。
            if not await confirm_panel_permission(
                interaction, await is_current_administrator(interaction),
                notice="この機能はサーバー管理者のみ利用できます。", followup=True,
            ):
                return
            for chunk in split_message(summary) or ["表示できるフィードバックの集計がありません。"]:
                await interaction.followup.send(chunk, ephemeral=True)
        except Exception as error:
            logger.error("フィードバックの集計・送信中にエラーが発生しました。", exc_info=error)
            await interaction.followup.send("エラー: フィードバックの表示に失敗しました。", ephemeral=True)


class StatsView(PanelView):
    """会話統計の表示パネル。

    一般ユーザーは自分の統計のみ閲覧でき、管理者はユーザーを選んで
    他メンバーの統計や全体比較も閲覧できる。パネルはエフェメラル前提。
    """

    def __init__(
        self,
        stats: StatsService,
        *,
        viewer_id: str,
        is_admin: bool,
        make_hub: HubFactory | None = None,
        feedback: FeedbackService | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 会話統計パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.stats = stats
        self.viewer_id = viewer_id
        self.is_admin = is_admin
        # 👍 フィードバック集計に使うサービス。生成元（機能ハブ）から渡されなかった
        # 場合は bot.py が起動時に登録した既定インスタンスを使う。
        self.feedback = feedback if feedback is not None else get_default_service()
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        # 一時選択値
        self.temp_item: str = "all"
        # 対象ユーザー。一般ユーザーは自分固定、管理者は選択で変更できる。
        self.temp_target_id: str = viewer_id
        # 実行中の集計（全体比較・フィードバック）の排他は PanelView の
        # try_begin_aggregation / end_aggregation を使う（#398、#415）。

        self._add_all_items()

    def refresh_permissions(self, user: discord.Member | discord.User) -> None:
        """操作時の権限に合わせ、管理者でなくなったら対象を本人へ戻す。"""
        self.is_admin = is_administrator(user)
        if not self.is_admin:
            self.temp_target_id = self.viewer_id

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。選択更新時にも呼び出す。"""
        if self.is_admin:
            users = self.stats.get_user_options(self.temp_target_id)
            if users:
                self.add_item(StatsUserSelect(users, self.temp_target_id))
        self.add_item(StatsItemSelect(self.temp_item))
        self.add_item(RunStatsButton())
        if self.is_admin:
            self.add_item(RankingButton())
            # フィードバック集計も管理者専用。一般ユーザーにはボタン自体を出さない。
            if self.feedback is not None:
                self.add_item(FeedbackButton())
        # 「戻る」は他のパネルと同じく行の最後に置く。
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        lines = [
            "📈 **会話統計パネル** — 項目を選んで「表示」を押してください。",
            f"・**項目**: {_ITEM_LABELS.get(self.temp_item, self.temp_item)}",
        ]
        if self.is_admin:
            lines.append(f"・**対象**: {self.stats.display_name(self.temp_target_id)} さん")
            lines.append("※ 管理者はユーザーを選んで他メンバーの統計・全体比較も閲覧できます。")
            if self.feedback is not None:
                lines.append("※ 👍 フィードバック … 返信への 👍/👎 の集計（このサーバー分）を表示します。")
        else:
            lines.append("・**対象**: あなた自身（他メンバーの統計は管理者のみ閲覧可）")
        return "\n".join(lines)
