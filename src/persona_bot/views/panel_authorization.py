"""機能パネルの操作の開始と権限確認（#470。`view_parts.py` から分離）。

パネルの操作を始めるときの世代の確保（`begin_current_panel_operation`）と、押下時・待機後の権限確認
（`authorize_panel_operation` / `authorize_administrator_operation` など）を担う。操作世代と画面更新は
`panel_state.py`、`PanelView` 本体は `view_parts.py`。`view_parts` が従来の名前で再公開する。

★ 管理者の判定（`is_administrator` / `is_current_administrator`）は、テストがこのモジュールの名前を差し替える。
ログは分割前と同じロガー名 `persona_bot.views.view_parts` へ出す。
"""

import logging

from persona_bot.utils.discord_helpers import is_administrator, is_current_administrator
from persona_bot.utils.discord_permissions import BLOCKED_USER_NOTICE
from persona_bot.views.panel_state import begin_panel_operation, edit_panel_message, panel_operation_is_current

logger = logging.getLogger("persona_bot.views.view_parts")


async def begin_current_panel_operation(view, interaction, *, defer: bool = False) -> bool:
    """パネルの操作を開始し、そのパネルがまだ有効かを返す（#453）。

    すでに閉じた・別の画面へ移ったパネルの操作なら、応答だけを保留して False を返す
    （呼び出し側は何も実行せずに戻ること）。有効なら True を返し、`defer=True` のときは
    その場で応答（本人だけに見えるエフェメラル）も保留する。
    ★ 世代の確保は最初の `await` より前に行う（後から始まった操作が先に世代を取れるように）。
    """
    operation = begin_panel_operation(view, interaction)
    if operation is not None and not operation.is_current():
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        return False
    if defer:
        await interaction.response.defer(ephemeral=True)
    return True


async def authorize_panel_operation(view, interaction, *, allowed, recheck, notice: str) -> bool:
    """押下時と待機後の権限を確かめ、拒否時は適切な応答を返す。"""
    if not allowed:
        await interaction.response.send_message(notice, ephemeral=True)
        return False
    operation = begin_panel_operation(view, interaction)
    await interaction.response.defer(ephemeral=True)
    if operation is not None and not operation.is_current():
        # すでに閉じた・別の画面へ移ったパネルの操作は、何も実行しない。
        return False
    if not await confirm_panel_permission(interaction, await recheck(interaction), notice=notice):
        return False
    # 権限の再確認を待つ間に「戻る」などで画面が移っていたら、副作用を起こさせない。
    return panel_operation_is_current(view, interaction)


def _is_personal_settings_operation(view, interaction, allow_dm: bool) -> bool:
    return (
        allow_dm and getattr(interaction, "guild", None) is None
        and getattr(getattr(view, "guild_settings", None), "guild_id", None) is None
    )


async def confirm_administrator_operation(view, interaction, *, allow_dm: bool = False, notice: str) -> bool:
    """長い待機の後に管理者を再取得し、失効した操作・設定を保存させない。"""
    allowed = _is_personal_settings_operation(view, interaction, allow_dm) or (
        getattr(interaction, "guild", None) is not None and await is_current_administrator(interaction)
    )
    if not await confirm_panel_permission(interaction, allowed, notice=notice):
        return False
    return panel_operation_is_current(view, interaction) and await view.ensure_settings_current(interaction)


async def authorize_administrator_operation(
    view, interaction, *, allow_dm: bool = False, notice: str, refresh: bool = True,
) -> bool:
    """写しの権限で即時拒否し、保留後の再確認を共通化する。DM許可は明示する。"""
    allowed = _is_personal_settings_operation(view, interaction, allow_dm) or (
        getattr(interaction, "guild", None) is not None and is_administrator(getattr(interaction, "user", None))
    )

    async def recheck(_interaction):
        return not refresh or _is_personal_settings_operation(view, interaction, allow_dm) or await is_current_administrator(interaction)

    if not await authorize_panel_operation(view, interaction, allowed=allowed, recheck=recheck, notice=notice):
        return False
    return panel_operation_is_current(view, interaction) and await view.ensure_settings_current(interaction)


async def confirm_panel_permission(interaction, allowed: bool, *, notice: str, followup: bool = False) -> bool:
    """待機後の判定結果を返し、画面の更新先に応じて拒否を案内する。"""
    if allowed:
        return True
    if followup:
        await interaction.followup.send(notice, ephemeral=True)
    else:
        await edit_panel_message(interaction, deferred=True, content=notice, view=None)
    return False


async def confirm_panel_not_blocked(view, interaction, *, followup: bool = False) -> bool:
    """長い待機の後にブラックリスト登録されていないかを確かめ、登録済みなら拒否を案内する（#509）。"""
    check = getattr(view, "blocked_user_check", None)
    user = getattr(interaction, "user", None)
    user_id = getattr(user, "id", None)
    allowed = check is None or user_id is None or not check(user_id)
    return await confirm_panel_permission(interaction, allowed, notice=BLOCKED_USER_NOTICE, followup=followup)
