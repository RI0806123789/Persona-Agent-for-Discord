"""他サーバーの管理者には全体の違反一覧・解除・人数を公開しない（#279）。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import GuildSettings
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.views.functions_view import FunctionsView, OpenSettingsButton, UnblockButton
from persona_bot.views.unblock_view import UnblockSelect
from tests.helpers.discord_mocks import make_interaction, make_response
from tests.helpers.member_helpers import make_member, make_owner_client


class BlacklistPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "blacklist.json"
        # サーバーIDが無い旧形式を使う。全体ブロックの効力は変えない。
        path.write_text(json.dumps({
            "22": {"blocked": True, "count": 3, "user_name": "架空のサーバーA利用者"},
            "33": {"blocked": True, "count": 4, "user_name": "架空のサーバーB利用者"},
            "44": "架空の壊れた記録",
        }, ensure_ascii=False), encoding="utf-8")
        self.blacklist = BlacklistService(path)
        self.blacklist.get_all_blocked_users = Mock(wraps=self.blacklist.get_all_blocked_users)
        self.blacklist.unblock = AsyncMock(wraps=self.blacklist.unblock)
        self.hub = self.track(FunctionsView(
            GuildSettings(), Mock(), Mock(), self.blacklist, Mock(), Mock(), Mock(), Mock(),
            is_admin=True,
        ))

    def track(self, view):
        self.addCleanup(view.stop)
        return view

    def interaction(self, *, guild_id=1, owner=False, admin=True):
        response = make_response()

        async def respond(**kwargs):
            response.is_done.return_value = True

        response.defer.side_effect = respond
        response.edit_message.side_effect = respond
        return make_interaction(
            user=make_member(admin=admin), client=make_owner_client(owner_id=11 if owner else 99),
            guild_id=guild_id, extras={}, response=response, without=("followup",),
        )

    async def test_admins_of_two_guilds_cannot_read_or_unblock_global_records(self):
        self.assertFalse(any(isinstance(item, UnblockButton) for item in self.hub.children))
        for guild_id in (1, 2):
            with self.subTest(guild_id=guild_id):
                interaction = self.interaction(guild_id=guild_id)
                # 古いパネルのボタンを直接実行しても一覧を読ませない。
                button = UnblockButton()
                self.hub.add_item(button)
                await button.callback(interaction)
                self.hub.remove_item(button)
                self.assertIn("ボット所有者のみ", interaction.response.send_message.await_args.args[0])
        self.blacklist.get_all_blocked_users.assert_not_called()
        self.blacklist.unblock.assert_not_awaited()
        self.assertTrue(self.blacklist.is_blocked(22))
        self.assertTrue(self.blacklist.is_blocked(33))
        self.assertFalse(self.blacklist.is_blocked(44))

    async def test_settings_hide_count_without_enumerating_other_users(self):
        for owner, expected in ((False, "非公開（ボット所有者のみ）"), (True, "2人")):
            with self.subTest(owner=owner):
                hub = self.track(self.hub.clone(self.interaction(owner=owner)))
                button = next(item for item in hub.children if isinstance(item, OpenSettingsButton))
                interaction = self.interaction(owner=owner)
                with patch("persona_bot.views.functions_view.refresh_persona_catalog", new=AsyncMock()):
                    await button.callback(interaction)
                content = interaction.edit_original_response.await_args.kwargs["content"]
                self.assertIn(expected, content)
                self.assertNotIn("サーバーA利用者", content)
                if not owner:
                    self.blacklist.get_all_blocked_users.assert_not_called()
                self.track(interaction.edit_original_response.await_args.kwargs["view"])

    async def test_owner_without_server_admin_can_open_and_unblock_old_record(self):
        interaction = self.interaction(owner=True, admin=False)
        hub = self.track(self.hub.clone(interaction))
        self.assertFalse(hub.is_admin)
        self.assertTrue(hub.is_owner)
        button = next(item for item in hub.children if isinstance(item, UnblockButton))
        await button.callback(interaction)
        view = self.track(interaction.edit_original_response.await_args.kwargs["view"])
        select = next(item for item in view.children if isinstance(item, UnblockSelect))
        select._values = ["22"]
        await select.callback(self.interaction(owner=True, admin=False))
        self.blacklist.unblock.assert_awaited_once_with(22)
        self.assertFalse(self.blacklist.is_blocked(22))
        self.assertTrue(self.blacklist.is_blocked(33))

    async def test_owner_change_or_lookup_failure_denies_before_reading_list(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                interaction = self.interaction(owner=True)
                if failure:
                    interaction.client.application_info.side_effect = RuntimeError("架空の取得失敗")
                else:
                    interaction.client.application_info.return_value = make_owner_client(owner_id=99).application
                hub = self.track(self.hub.clone(interaction))
                button = next(item for item in hub.children if isinstance(item, UnblockButton))
                with patch("builtins.print"):
                    await button.callback(interaction)
                self.blacklist.get_all_blocked_users.assert_not_called()
                edit = interaction.edit_original_response.await_args.kwargs
                self.assertIsNone(edit["view"])
                self.assertNotIn("サーバーA利用者", edit["content"])
