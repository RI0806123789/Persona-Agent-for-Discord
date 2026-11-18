"""機能パネルの操作世代と、同じメッセージを共有するパネルの編集の直列化（#470。`view_parts.py` から分離）。

同じメッセージを差し替えながら使う `PanelView` どうしは、1つの `_PanelState`（表示中のパネル・操作世代・
編集ロック）を引き継ぐ。DB 等の待機の前に `begin_panel_operation` で世代を確保し、画面更新は
`edit_panel_message` を通す。後から始まった操作がある旧い更新は破棄され、受け付け済みの保存は最後まで進む。

`PanelView` 本体は `view_parts.py` にある。ここから参照するだけなので、循環を避けるため呼び出し時に取得する。
`view_parts` が従来の名前で再公開する。ログは分割前と同じロガー名 `persona_bot.views.view_parts` へ出す。
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord
from discord import ui

if TYPE_CHECKING:
    from persona_bot.views.view_parts import PanelView

logger = logging.getLogger("persona_bot.views.view_parts")


def _panel_view_class() -> type:
    """`PanelView` を呼び出し時に取得する（view_parts がこのモジュールを import するため、先頭では取れない）。"""
    from persona_bot.views.view_parts import PanelView

    return PanelView


class _PanelState:
    def __init__(self, view: "PanelView") -> None:
        self.current: "PanelView | None" = view
        self.generation = 0
        self.edit_lock = asyncio.Lock()


@dataclass(frozen=True)
class _PanelOperation:
    source: "PanelView"
    state: _PanelState
    generation: int

    def is_current(self) -> bool:
        # 元Viewが遷移で停止しても、受付済みの最新操作は次の遷移を完了できる。
        # 停止済みViewそのものの再表示は、編集時に別途拒否する。
        return self.generation == self.state.generation


def begin_panel_operation(view: ui.View | None, interaction: discord.Interaction) -> _PanelOperation | None:
    """DB待機より前に操作の世代を確保する。同じInteractionでは二重に進めない。"""
    if not isinstance(view, _panel_view_class()):
        return None
    extras = getattr(interaction, "extras", None)
    if not isinstance(extras, dict):
        extras = interaction.extras = {}
    previous = extras.get("persona_panel_operation")
    if isinstance(previous, _PanelOperation) and previous.source is view:
        return previous
    state = view._panel_state
    if state.current is not view or view.is_finished():
        operation = _PanelOperation(view, state, -1)
    else:
        state.generation += 1
        operation = _PanelOperation(view, state, state.generation)
    extras["persona_panel_operation"] = operation
    return operation


def panel_operation_is_current(view: ui.View | None, interaction: discord.Interaction) -> bool:
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation):
        operation = begin_panel_operation(view, interaction)
    return operation is None or operation.is_current()


async def _edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None, deferred: bool,
) -> None:
    if deferred or getattr(interaction.response, "is_done", lambda: not hasattr(interaction.response, "edit_message"))():
        await interaction.edit_original_response(content=content, view=view)
    else:
        await interaction.response.edit_message(content=content, view=view)


async def edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None = None,
    source: ui.View | None = None, deferred: bool = False,
) -> bool:
    """同じパネルの編集を直列化し、後から始まった操作がある旧更新を破棄する。"""
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation) or (source is not None and operation.source is not source):
        operation = begin_panel_operation(source if source is not None else view, interaction)
    if operation is None:
        await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
        return True
    state = operation.state
    try:
        # 先行するHTTP編集が遅くても、ロック待ちでInteractionを失効させない。
        if state.edit_lock.locked() and not getattr(interaction.response, "is_done", lambda: False)():
            await interaction.response.defer(ephemeral=True)
        async with state.edit_lock:
            if not operation.is_current() or (view is not None and view.is_finished()):
                if view is not None and view is not state.current:
                    view.stop()
                if not getattr(interaction.response, "is_done", lambda: False)():
                    await interaction.response.defer(ephemeral=True)
                return False
            previous = state.current
            # 閉じる編集の通信中は新しい操作を受け付けない。後発のセレクトが
            # 実行中の世代を無効にし、両方の結果が破棄されることを防ぐ。
            if view is None:
                state.current = None
            try:
                await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
            except BaseException:
                if view is None and operation.is_current() and previous is not None and not previous.is_finished():
                    state.current = previous
                raise
            if previous is not view:
                state.current = view
                if isinstance(view, _panel_view_class()):
                    view._panel_state = state
                if previous is not None:
                    previous.stop()
            return True
    except BaseException:
        # ロック待ちの取り消しでも、不採用の遷移先を残さない。
        if view is not None and view is not state.current:
            view.stop()
        raise


async def bind_panel_message(
    interaction: discord.Interaction, panel: ui.View, *, previous_message: Any = None,
) -> None:
    """編集後の参照取得に失敗しても、直前のWebhook参照で時間切れ処理を保つ。"""
    panel.message = previous_message
    try:
        panel.message = await interaction.original_response()
    except Exception as error:
        logger.warning("パネルの応答参照の取得に失敗しました。", exc_info=error)
