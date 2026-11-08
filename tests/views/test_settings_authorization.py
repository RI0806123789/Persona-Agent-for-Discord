"""キャラ設定の確定を、最新の管理者権限とパネル世代で保護する。"""

import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.views.settings_view import ConfirmButton, SettingsView
from persona_bot.views.view_parts import begin_panel_operation
from tests.helpers.discord_mocks import make_interaction, make_response
from tests.helpers.member_helpers import make_member
from tests.helpers.persona_helpers import install_persona_catalog


class SettingsAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_persona_catalog(self, "../prompts/架空のペルソナ.txt")
        self.settings = BotState().get_guild(1)
        self.settings.persist = AsyncMock()
        self.view = SettingsView(self.settings, 0, "なし")
        self.view.temp_model = "fictional-model"
        self.addCleanup(self.view.stop)
        self.button = next(item for item in self.view.children if isinstance(item, ConfirmButton))

    def interaction(self, *, admin=True, fresh_admin=None, dm=False):
        response = make_response("defer", "send_message", "is_done")

        async def defer(**kwargs):
            response.is_done.return_value = True

        response.defer.side_effect = defer
        return make_interaction(
            response=response, extras={},
            guild=None if dm else SimpleNamespace(id=1),
            user=SimpleNamespace(id=11) if dm else make_member(admin=admin, fresh_admin=fresh_admin),
            without=("followup", "original_response"),
        )

    async def test_nonadministrator_is_denied_without_waiting_or_fetch(self):
        interaction = self.interaction(admin=False)
        await self.button.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_awaited()
        interaction.user.guild.fetch_member.assert_not_awaited()
        self.settings.persist.assert_not_awaited()
        self.assertNotEqual(self.settings.current_model_name, self.view.temp_model)

    async def test_administrator_is_rechecked_and_can_save(self):
        interaction = self.interaction()
        await self.button.callback(interaction)
        interaction.user.guild.fetch_member.assert_awaited_once()
        self.settings.persist.assert_awaited_once()
        self.assertEqual(self.settings.current_model_name, self.view.temp_model)

    async def test_revoked_or_unavailable_permission_does_not_save(self):
        for failure in (False, True):
            with self.subTest(fetch_failure=failure):
                interaction = self.interaction(fresh_admin=False)
                if failure:
                    interaction.user.guild.fetch_member.side_effect = OSError("架空の取得失敗")
                await self.button.callback(interaction)
                self.settings.persist.assert_not_awaited()
                self.assertNotEqual(self.settings.current_model_name, self.view.temp_model)
                self.assertIn("管理者のみ", interaction.edit_original_response.await_args.kwargs["content"])
                # 拒否で閉じたパネルを、次の独立したケース用に作り直す。
                self.view = SettingsView(self.settings, 0, "なし")
                self.view.temp_model = "fictional-model"
                self.addCleanup(self.view.stop)
                self.button = next(item for item in self.view.children if isinstance(item, ConfirmButton))

    async def test_permission_is_fetched_after_persona_validation(self):
        interaction = self.interaction()
        self.view.temp_persona = "../prompts/changed-persona.txt"

        def validate(_value):
            interaction.user.fresh.guild_permissions.administrator = False
            return True

        with patch("persona_bot.views.settings_view.is_selectable_persona", side_effect=validate):
            await self.button.callback(interaction)
        self.settings.persist.assert_not_awaited()
        self.assertNotEqual(self.settings.current_model_name, self.view.temp_model)

    async def test_newer_panel_operation_during_permission_fetch_prevents_save(self):
        interaction = self.interaction()

        async def fetch(_user_id):
            begin_panel_operation(self.view, self.interaction())
            return interaction.user.fresh

        interaction.user.guild.fetch_member.side_effect = fetch
        await self.button.callback(interaction)
        self.settings.persist.assert_not_awaited()
        self.assertNotEqual(self.settings.current_model_name, self.view.temp_model)

    async def test_dm_user_can_confirm_without_guild_permissions(self):
        self.settings.guild_id = None
        interaction = self.interaction(dm=True)
        await self.button.callback(interaction)
        self.settings.persist.assert_awaited_once()
        self.assertEqual(self.settings.current_model_name, self.view.temp_model)
