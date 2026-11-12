import discord
from discord import ui

from persona_bot.config.constants import STATS_RECENT_SAMPLES_MAX
from persona_bot.services.stats_service import StatsService
from persona_bot.views.view_parts import (
    begin_panel_operation, edit_panel_message, BackButton,
    HubFactory, PanelView,
)


def _build_status_text(
    collect_enabled: bool,
    *,
    delete_confirmed: bool = False,
) -> str:
    """統計プライバシーパネルの表示テキストを組み立てる。"""
    state_display = "✅ ON（記録します）" if collect_enabled else "🔕 OFF（記録しません）"
    lines = [
        "🔒 **統計プライバシーパネル** — 会話統計・性格推定の収集を、あなた自身で止められます。",
        f"・**📊 統計収集**: {state_display}",
        "・**集めている内容**: 累計発言回数 / 発言した時間帯 / "
        f"直近の発言サンプル（最大{STATS_RECENT_SAMPLES_MAX}件） / "
        "そのサンプルから AI が推定する話題・性格（ビッグファイブ）",
        "・OFF にすると、以降あなたの発言は一切記録されず、話題・性格の AI 推定も行いません。"
        "管理者のユーザー選択・発言回数ランキングにも表示されなくなります。",
        "・すでに溜まっているデータは OFF にしても残ります（あとで見返せます）。"
        "消したい場合は 🗑️ 蓄積データを削除 を押してください。",
        "※ ここで扱うのは**会話統計だけ**です。会話の記憶（プロフィール記憶・"
        "プロジェクト記憶）は別機能のため、この設定では消えません。",
        "※ この設定はあなた専用で、他の人（管理者を含む）からは見えません。",
    ]
    if delete_confirmed:
        lines.append(
            "⚠️ **もう一度 🗑️ を押すと、あなたの統計データ（発言回数・時間帯・"
            "発言サンプル・AI 推定結果）を削除します。元には戻せません。**"
        )
    else:
        lines.append("※ ON/OFF の変更は「確定」を押すまで反映されません。")
    return "\n".join(lines)


class StatsCollectToggleButton(ui.Button):
    """統計収集の ON/OFF トグルボタン（押しただけでは反映しない）。"""

    def __init__(self, collect_enabled: bool) -> None:
        label = "📊 統計収集: ON" if collect_enabled else "📊 統計収集: OFF"
        style = discord.ButtonStyle.success if collect_enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsPrivacyView" = self.view  # type: ignore[assignment]
        def update() -> None:
            view.temp_collect_enabled = not view.temp_collect_enabled
            view.delete_confirmed = False
        await view.update_preview(interaction, update)


class StatsPrivacyConfirmButton(ui.Button):
    """確定ボタン。ここで初めて収集停止（opt-out）の設定を保存する。"""

    def __init__(self) -> None:
        super().__init__(label="確定", style=discord.ButtonStyle.success, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsPrivacyView" = self.view  # type: ignore[assignment]
        operation = begin_panel_operation(view, interaction)
        if operation is not None and not operation.is_current():
            # 閉じた・別の画面へ移ったパネルの確定では、設定を保存しない。
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        opted_out = not view.temp_collect_enabled
        saved = await view.stats.set_opt_out(view.viewer_id, view.viewer_name, opted_out)

        if not saved:
            result = (
                "⚠️ 統計収集の設定を保存できませんでした。設定は変更されていません。\n"
                "時間をおいて /functions → 🔒 統計プライバシー から再度お試しください。"
            )
        elif opted_out:
            result = (
                "🔕 統計収集を停止しました。以降のあなたの発言は記録されません。\n"
                "※ すでに溜まっているデータは残っています（消したい場合は"
                "再度 /functions → 🔒 統計プライバシー → 🗑️ 蓄積データを削除）。"
            )
        else:
            result = "✅ 統計収集を再開しました。以降のあなたの発言が記録されます。"
        shown = await edit_panel_message(interaction, deferred=True, content=result, view=None)
        if not shown and not saved:
            await interaction.followup.send(result, ephemeral=True)


class StatsDataDeleteButton(ui.Button):
    """蓄積データ削除ボタン。1回目は確認、2回目で実行する（2段階）。"""

    def __init__(self, delete_confirmed: bool) -> None:
        label = "本当に削除する" if delete_confirmed else "蓄積データを削除"
        super().__init__(label=label, style=discord.ButtonStyle.danger, emoji="🗑️", row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "StatsPrivacyView" = self.view  # type: ignore[assignment]
        operation = begin_panel_operation(view, interaction)
        if operation is not None and not operation.is_current():
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        if not view.delete_confirmed:
            # 1回目は確認を促すだけ。DB には触らない。
            await view.update_preview(interaction, lambda: setattr(view, "delete_confirmed", True))
            return

        # 削除するのは実行者本人の分だけ（他人のIDを指定できる経路は作らない）。
        await interaction.response.defer(ephemeral=True)
        deleted = await view.stats.clear_user_data(view.viewer_id)
        if deleted:
            result = (
                "🗑️ あなたの会話統計データ（発言回数・時間帯・発言サンプル・AI 推定結果）"
                "を削除しました。\n※ 会話の記憶は別機能のため、ここでは消えていません。"
            )
        else:
            # プライバシー操作の失敗は隠さない（黙って成功したように見せない）。
            result = "⚠️ データを削除できませんでした。時間をおいて再度お試しください。"

        shown = await edit_panel_message(interaction, deferred=True, content=result, view=None)
        if not shown and not deleted:
            await interaction.followup.send(result, ephemeral=True)


class StatsPrivacyView(PanelView):
    """会話統計・性格推定の収集停止（opt-out）とデータ削除のパネル。

    設定は「実行者本人の分だけ」を対象にする。ON/OFF は確定を押すまで
    反映しない（AutoTopicView と同じ一時選択値方式）。データ削除は opt-out とは
    独立した操作で、2回押しの確認を挟む。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        stats: StatsService,
        *,
        viewer_id: str,
        viewer_name: str,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 統計プライバシーパネルは時間切れになりました。設定は変更されていません。"
                "再度 /functions を実行してください。"
            )
        )
        self.stats = stats
        self.viewer_id = viewer_id
        self.viewer_name = viewer_name
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        # 一時選択値（現在の設定を初期値にする）。UI は「収集する/しない」で
        # 見せるため、内部の opt_out（停止するか）とは真偽が反転している点に注意。
        self.temp_collect_enabled: bool = not stats.is_opted_out(viewer_id)
        # 削除ボタンの確認状態（View の一時状態。DB には触らない）
        self.delete_confirmed: bool = False

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。トグル・確認状態の更新時にも呼ばれる。"""
        self.add_item(StatsCollectToggleButton(self.temp_collect_enabled))
        self.add_item(StatsPrivacyConfirmButton())
        self.add_item(StatsDataDeleteButton(self.delete_confirmed))
        self.add_item(BackButton(row=1))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        return _build_status_text(
            self.temp_collect_enabled,
            delete_confirmed=self.delete_confirmed,
        )
