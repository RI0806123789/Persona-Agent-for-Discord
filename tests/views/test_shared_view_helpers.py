"""画面層の共通化（#415）: SettingsView の共通ファクトリと、followup ボタンの排他を確かめる。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import GuildSettings
from persona_bot.views import functions_view, settings_view
from persona_bot.views.settings_view import BLACKLIST_PRIVATE_STATUS, build_settings_view
from persona_bot.views.view_parts import PANEL_BUSY_NOTICE, PanelView
from tests.helpers.discord_mocks import make_interaction, make_response
from tests.helpers.persona_helpers import install_persona_catalog


class BuildSettingsViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_persona_catalog(self)

    async def build(self, *, owner: bool, admin: bool, provider=lambda: [1, 2], previous=None, fallback=0, ng=lambda: 4):
        refresh = AsyncMock()
        with patch.object(settings_view, "refresh_persona_catalog", refresh), \
                patch.object(settings_view, "is_bot_owner", Mock(return_value=owner)), \
                patch.object(settings_view, "is_current_bot_owner", AsyncMock(return_value=owner)), \
                patch.object(settings_view, "is_current_administrator", AsyncMock(return_value=admin)) as latest_admin:
            view = await build_settings_view(
                make_interaction(), GuildSettings(), ng_count_provider=ng, blacklist_provider=provider,
                make_hub=None, fallback_ng_count=fallback, previous=previous,
            )
        self.addCleanup(view.stop)
        refresh.assert_awaited_once()
        latest_admin.assert_awaited_once()
        return view

    async def test_blacklist_count_is_shown_only_to_the_current_bot_owner(self):
        self.assertEqual((await self.build(owner=True, admin=False)).blacklist_status, "2人")
        self.assertEqual((await self.build(owner=True, admin=False, provider=lambda: [])).blacklist_status, "なし")
        self.assertEqual((await self.build(owner=False, admin=True)).blacklist_status, BLACKLIST_PRIVATE_STATUS)

    async def test_admin_flag_comes_from_the_latest_member_and_counts_from_providers(self):
        view = await self.build(owner=False, admin=False)
        self.assertFalse(view.is_admin)
        self.assertEqual(view.ng_count, 4)
        self.assertTrue((await self.build(owner=False, admin=True)).is_admin)
        view = await self.build(owner=False, admin=True, ng=None, fallback=9)
        self.assertEqual(view.ng_count, 9)

    async def test_previous_unconfirmed_choices_are_carried_over(self):
        previous = await self.build(owner=False, admin=True)
        previous.temp_persona, previous.temp_model, previous.temp_respond_mode = "../prompts/a.txt", "m", "all"
        view = await self.build(owner=False, admin=True, previous=previous)
        self.assertEqual((view.temp_persona, view.temp_model, view.temp_respond_mode), ("../prompts/a.txt", "m", "all"))

    async def test_hub_button_uses_the_shared_factory_after_deferring(self):
        hub = PanelView(timeout_notice="架空")
        self.addCleanup(hub.stop)
        hub.guild_settings = GuildSettings()
        hub.ng_filter = SimpleNamespace(word_count=3)
        hub.blacklist = SimpleNamespace(get_all_blocked_users=Mock(return_value=[]))
        hub.clone = Mock()
        button = functions_view.OpenSettingsButton()
        hub.add_item(button)
        built = PanelView(timeout_notice="架空")
        self.addCleanup(built.stop)
        built.build_preview = Mock(return_value="架空のパネル")
        interaction = make_interaction(response=make_response("defer"))
        order = []
        interaction.response.defer.side_effect = lambda **kwargs: order.append("defer")

        async def factory(*args, **kwargs):
            order.append("factory")
            return built

        with patch.object(functions_view, "build_settings_view", side_effect=factory) as build, \
                patch.object(functions_view, "swap_panel", new=AsyncMock()) as swap:
            await button.callback(interaction)
        self.assertEqual(order, ["defer", "factory"])
        self.assertIs(build.call_args.kwargs["make_hub"], hub.clone)
        self.assertEqual(build.call_args.kwargs["ng_count_provider"](), 3)
        swap.assert_awaited_once()


class FollowupButtonExclusionTests(unittest.IsolatedAsyncioTestCase):
    def make_hub(self) -> PanelView:
        hub = PanelView(timeout_notice="架空")
        self.addCleanup(hub.stop)
        return hub

    def test_aggregation_guard_lives_on_the_panel_base_class(self):
        hub = self.make_hub()
        self.assertTrue(hub.try_begin_aggregation("graph"))
        self.assertFalse(hub.try_begin_aggregation("graph"))
        self.assertTrue(hub.try_begin_aggregation("ng_reload"))  # 名前ごとに独立
        hub.end_aggregation("graph")
        self.assertTrue(hub.try_begin_aggregation("graph"))

    async def test_graph_button_refuses_a_second_press_while_rendering(self):
        hub = self.make_hub()
        entered, release = asyncio.Event(), asyncio.Event()

        async def render():
            entered.set()
            await release.wait()
            return object()

        hub.usage = SimpleNamespace(has_data=Mock(return_value=True), build_graph_buffer=AsyncMock(side_effect=render))
        button = functions_view.GraphButton()
        hub.add_item(button)
        first = make_interaction(response=make_response("defer"))
        second = make_interaction(response=make_response("send_message"))
        with patch.object(functions_view, "send_graph_ephemeral", new=AsyncMock()) as send:
            running = asyncio.create_task(button.callback(first))
            await asyncio.wait_for(entered.wait(), 2)
            await button.callback(second)
            second.response.send_message.assert_awaited_once_with(PANEL_BUSY_NOTICE, ephemeral=True)
            hub.usage.build_graph_buffer.assert_awaited_once()
            release.set()
            await asyncio.wait_for(running, 2)
            send.assert_awaited_once()
        # 終わった後は、もう一度押せる
        self.assertTrue(hub.try_begin_aggregation("graph"))

    async def test_exclusion_is_released_when_the_work_fails(self):
        hub = self.make_hub()
        hub.ng_filter = SimpleNamespace(reload=Mock(side_effect=PermissionError("架空")), word_count=1)
        button = functions_view.NgReloadButton()
        hub.add_item(button)
        await button.callback(make_interaction(response=make_response("defer")))
        self.assertTrue(hub.try_begin_aggregation("ng_reload"))


if __name__ == "__main__":
    unittest.main()
