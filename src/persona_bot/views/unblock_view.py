"""ブロック解除パネル（/functions → 🚫 ブロック解除。ボット所有者専用）。

ハブ（functions_view.py）の UnblockButton から開く。ほかのサブパネル（settings_view.py など）と
同じく、パネル本体は独立したモジュールに置き、ハブはボタンだけを持つ。
所有者の判定はボタンを押したときと、解除を選んだときの両方で行う。
全サーバー共通の違反情報は、サーバー管理者へ開示しない。
"""

import logging

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.utils.discord_helpers import is_bot_owner, is_current_bot_owner
from persona_bot.views.view_parts import (
    BackButton, HubFactory, PanelView, authorize_panel_operation, edit_panel_message, select_option_text,
)


logger = logging.getLogger(__name__)


class UnblockSelect(ui.Select):
    """ブロック中ユーザーから解除対象を選ぶセレクトメニュー。"""

    def __init__(self, blocked_users: list) -> None:
        options = []
        # Discord のセレクトは最大25件まで
        for record in blocked_users[:DISCORD_SELECT_MAX_OPTIONS]:
            user_id = record["user_id"]
            name = record.get("user_name") or str(user_id)
            count = record.get("count", "?")
            options.append(
                discord.SelectOption(
                    label=select_option_text(name, fallback=str(user_id)),
                    value=str(user_id),
                    description=f"違反 {count} 回",
                )
            )
        super().__init__(placeholder="解除するユーザーを選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "UnblockView" = self.view  # type: ignore[assignment]
        if not await authorize_panel_operation(
            view, interaction, allowed=is_bot_owner(interaction), recheck=is_current_bot_owner,
            notice="この機能はボット所有者のみ利用できます。",
        ):
            return
        user_id = int(self.values[0])
        # 選択肢のラベルから表示名を拾う（通知メッセージ用）
        name = next((o.label for o in self.options if o.value == self.values[0]), str(user_id))
        try:
            unblocked = await view.blacklist.unblock(user_id)
        except OSError as error:
            # data/ng/blacklist.json への書き込みに失敗したときも、失敗を黙って捨てない。
            logger.error("ブロック解除パネル: 解除の保存に失敗しました。", exc_info=error)
            await edit_panel_message(interaction, deferred=True,
                content=f"⚠️ {name} のブロック解除を保存できませんでした。時間をおいて再度お試しください。", view=None
            )
            return
        if unblocked:
            await edit_panel_message(interaction, deferred=True, content=f"✅ {name} のブロックを解除しました。", view=None)
        else:
            await edit_panel_message(interaction, deferred=True,
                content=f"{name} はブラックリストに登録されていませんでした。", view=None
            )


class UnblockView(PanelView):
    """ブロック解除用のパネル。ブロック中ユーザーの選択メニューを持つ。"""

    def __init__(
        self,
        blacklist: BlacklistService,
        blocked_users: list,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ ブロック解除パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        self.blacklist = blacklist
        self.blocked_users = blocked_users
        self.blocked_user_check = blacklist.is_blocked
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。"""
        self.add_item(UnblockSelect(self.blocked_users))
        self.add_item(BackButton(row=1))

    def build_preview(self) -> str:
        """選択できる件数の制限を所有者へ知らせる。"""
        text = "🚫 ブロックを解除するユーザーを選択してください。"
        if len(self.blocked_users) > DISCORD_SELECT_MAX_OPTIONS:
            text += f"\n先頭 {DISCORD_SELECT_MAX_OPTIONS} 件を表示しています。"
        return text
