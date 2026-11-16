"""ブロック中の利用者がコマンドや開いたままのパネルを操作できないことを確認する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.core import commands
from persona_bot.core.app_state import GuildSettings
from persona_bot.views.functions_view import FunctionsView
from persona_bot.views.settings_view import SettingsView
from persona_bot.views.view_parts import BackButton, swap_panel
from tests.helpers.discord_mocks import make_interaction


class CommandBlacklistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.callbacks = {}
        tree = SimpleNamespace(command=lambda **kwargs: lambda callback: self.callbacks.setdefault(kwargs["name"], callback))
        self.state = SimpleNamespace(get_guild=Mock(return_value=GuildSettings()))
        self.blacklist = SimpleNamespace(is_blocked=Mock(return_value=True))
        self.autonomous = SimpleNamespace(stop_session=AsyncMock())
        commands.register_commands(tree, self.state, None, None, self.blacklist,
                                   None, None, None, None, self.autonomous)

    def interaction(self, guild_id=1, *, admin=False):
        channel = SimpleNamespace(connect=AsyncMock())
        guild = SimpleNamespace(id=guild_id, voice_client=SimpleNamespace(disconnect=AsyncMock()))
        user = Mock(spec=discord.Member, id=11, voice=SimpleNamespace(channel=channel),
                    guild_permissions=SimpleNamespace(administrator=admin))
        return make_interaction(
            user=user, guild=guild if guild_id is not None else None, guild_id=guild_id,
            without=("edit_original_response",),
        )

    async def test_all_commands_reject_blocked_users_before_any_side_effect(self):
        for name, callback in self.callbacks.items():
            for guild_id in (1, None):
                for admin in (False, True):
                    with self.subTest(command=name, guild_id=guild_id, admin=admin):
                        interaction = self.interaction(guild_id, admin=admin)
                        with patch.object(commands, "FunctionsView") as create:
                            await callback(interaction)
                        create.assert_not_called()
                        self.state.get_guild.assert_not_called()
                        self.autonomous.stop_session.assert_not_awaited()
                        interaction.user.voice.channel.connect.assert_not_awaited()
                        if interaction.guild is not None:
                            interaction.guild.voice_client.disconnect.assert_not_awaited()
                        interaction.response.defer.assert_not_awaited()
                        interaction.followup.send.assert_not_awaited()
                        interaction.response.send_message.assert_awaited_once()
                        self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
                        self.assertIn("利用が制限", interaction.response.send_message.call_args.args[0])

    async def test_unblocked_user_can_open_functions_again(self):
        interaction = self.interaction()
        self.blacklist.is_blocked.return_value = False
        view = SimpleNamespace(build_preview=Mock(return_value="架空の機能パネル"))
        with patch.object(commands, "FunctionsView", return_value=view):
            await self.callbacks["functions"](interaction)
        self.blacklist.is_blocked.assert_called_once_with(11)
        self.state.get_guild.assert_called_once_with(1)
        interaction.response.send_message.assert_awaited_once_with("架空の機能パネル", view=view, ephemeral=True)

    async def test_open_panel_and_subpanel_recheck_block_status_after_navigation(self):
        self.blacklist.is_blocked.return_value = False
        hub = FunctionsView(self.state.get_guild(1), None, None, self.blacklist, None, None, None, None)
        panel = SettingsView(self.state.get_guild(1), 0, "未登録", make_hub=hub.clone)
        self.addCleanup(hub.stop)
        self.addCleanup(panel.stop)
        interaction = self.interaction()
        self.assertTrue(await hub.interaction_check(interaction))
        await swap_panel(interaction, hub, panel, "架空の設定パネル")
        self.blacklist.is_blocked.return_value = True
        self.assertFalse(await panel.interaction_check(interaction))
        interaction.response.send_message.assert_awaited_once()
        self.blacklist.is_blocked.return_value = False
        self.assertTrue(await panel.interaction_check(interaction))
        back = next(item for item in panel.children if isinstance(item, BackButton))
        await back.callback(interaction)
        restored = interaction.response.edit_message.call_args.kwargs["view"]
        self.addCleanup(restored.stop)
        self.blacklist.is_blocked.return_value = True
        self.assertFalse(await restored.interaction_check(interaction))
