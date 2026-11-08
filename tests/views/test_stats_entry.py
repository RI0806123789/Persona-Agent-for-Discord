"""実統計サービスと機能ハブで、収集停止後の本人閲覧と他人への秘匿を確認する。"""

from copy import deepcopy
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import GuildSettings
from persona_bot.core.database import Database
from persona_bot.services.stats_service import StatsService
from persona_bot.views.functions_view import FunctionsView, StatsButton
from persona_bot.views.stats_view import RankingButton, RunStatsButton, StatsUserSelect, StatsView
from tests.helpers.discord_mocks import make_interaction, make_response
from tests.helpers.member_helpers import make_member


class StatsEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="stats-entry-test-")
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.gemini = SimpleNamespace(generate=AsyncMock())
        self.blacklist = SimpleNamespace(is_blocked=Mock(return_value=False))
        self.seed({})

    def seed(self, counts: dict[str, int]) -> None:
        entries = {}
        for user_id, count in counts.items():
            entry = StatsService._new_entry(f"架空の利用者{user_id}")
            entry.update(total_count=count, recent_samples=["架空の会話"], opt_out=False)
            entry["hourly"][9] = count
            entries[user_id] = entry
        self.db.stats_save_all(entries)
        self.stats = StatsService(self.db, self.gemini)

    def make_hub(self) -> FunctionsView:
        hub = FunctionsView(
            GuildSettings(), Mock(), Mock(), self.blacklist, self.gemini,
            Mock(), self.stats, Mock(),
        )
        self.addCleanup(hub.stop)
        return hub

    def interaction(self, *, admin: bool = False) -> SimpleNamespace:
        response = make_response("is_done", "send_message", "edit_message")

        async def edit(**kwargs):
            response.is_done.return_value = True

        response.edit_message.side_effect = edit
        message = SimpleNamespace(edit=AsyncMock())
        return make_interaction(
            user=make_member(admin=admin), guild_id=1, extras={}, response=response,
            original_response=AsyncMock(return_value=message),
        )

    async def open_stats(self, *, admin: bool = False) -> StatsView:
        hub = self.make_hub()
        interaction = self.interaction(admin=admin)
        self.assertTrue(await hub.interaction_check(interaction))
        await next(item for item in hub.children if isinstance(item, StatsButton)).callback(interaction)
        interaction.response.edit_message.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        panel = interaction.response.edit_message.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        self.assertIsInstance(panel, StatsView)
        self.assertIs(panel.stats, self.stats)
        self.assertEqual((panel.viewer_id, panel.temp_target_id), ("11", "11"))
        self.assertEqual(panel.is_admin, admin)
        self.assertEqual(any(isinstance(item, RankingButton) for item in panel.children), admin)
        self.assertTrue(hub.is_finished())
        self.assertIs(panel.message, interaction.original_response.return_value)
        self.assertIs(panel.blocked_user_check, self.blacklist.is_blocked)
        return panel

    async def assert_owner_can_read_saved_stats(self, *, admin: bool = False) -> None:
        before = deepcopy(self.db.stats_load_all())
        panel = await self.open_stats(admin=admin)
        interaction = self.interaction(admin=admin)
        buffer = io.BytesIO(b"fictional-png")
        self.addCleanup(buffer.close)
        with patch.object(self.stats, "build_hourly_graph", return_value=buffer) as rendering:
            with patch.object(self.stats, "build_dashboard", wraps=self.stats.build_dashboard) as dashboard:
                self.assertTrue(await panel.interaction_check(interaction))
                await next(item for item in panel.children if isinstance(item, RunStatsButton)).callback(interaction)
        dashboard.assert_awaited_once_with("11", "all", viewer_id="11")
        rendering.assert_called_once()
        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("停止前の記録", content)
        self.assertIn("累計発言回数: 3 回", content)
        interaction.followup.send.assert_awaited_once()
        delivery = interaction.followup.send.await_args.kwargs
        self.assertTrue(delivery["ephemeral"])
        self.assertEqual([file.filename for file in delivery["files"]], ["stats_hourly.png"])
        self.assertTrue(buffer.closed)
        self.assertEqual(self.db.stats_load_all(), before)
        self.assertTrue(StatsService(self.db, self.gemini).is_opted_out("11"))
        self.gemini.generate.assert_not_awaited()

    async def test_only_data_owner_can_read_after_stopping_collection_without_resuming(self) -> None:
        self.seed({"11": 3})
        self.assertTrue(await self.stats.set_opt_out("11", "架空の本人", True))
        self.assertFalse(self.stats.has_data())
        await self.assert_owner_can_read_saved_stats()

    async def test_admin_can_read_own_history_when_every_user_has_stopped_collection(self) -> None:
        self.seed({"11": 3, "22": 8})
        for user_id in ("11", "22"):
            self.assertTrue(await self.stats.set_opt_out(user_id, f"架空の利用者{user_id}", True))
        self.assertFalse(self.stats.has_data())
        self.assertEqual(self.stats.get_user_options("11"), [])
        await self.assert_owner_can_read_saved_stats(admin=True)

    async def test_own_history_remains_the_initial_target_when_another_user_has_data(self) -> None:
        self.seed({"11": 3, "22": 8})
        self.assertTrue(await self.stats.set_opt_out("11", "架空の本人", True))
        self.assertTrue(self.stats.has_data())
        await self.assert_owner_can_read_saved_stats(admin=True)

    async def test_empty_owner_gets_existing_dashboard_notice_after_opening_panel(self) -> None:
        for admin in (False, True):
            with self.subTest(admin=admin):
                panel = await self.open_stats(admin=admin)
                interaction = self.interaction(admin=admin)
                await next(item for item in panel.children if isinstance(item, RunStatsButton)).callback(interaction)
                self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"],
                                 "11 さんの会話データはまだありません。")
                interaction.followup.send.assert_not_awaited()
        self.assertEqual(self.db.stats_load_all(), {})
        self.gemini.generate.assert_not_awaited()

    async def test_empty_owner_does_not_fall_back_to_another_users_statistics(self) -> None:
        self.seed({"22": 8})
        self.assertTrue(self.stats.has_data())
        for admin in (False, True):
            with self.subTest(admin=admin):
                panel = await self.open_stats(admin=admin)
                interaction = self.interaction(admin=admin)
                await next(item for item in panel.children if isinstance(item, RunStatsButton)).callback(interaction)
                self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"],
                                 "11 さんの会話データはまだありません。")
                interaction.followup.send.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()

    async def test_admin_cannot_distinguish_hidden_users_from_absent_users_at_entry(self) -> None:
        observed = []
        for hidden_user in (False, True):
            self.seed({"22": 8} if hidden_user else {})
            if hidden_user:
                self.assertTrue(await self.stats.set_opt_out("22", "架空の非表示対象", True))
            self.assertFalse(self.stats.has_data())
            panel = await self.open_stats(admin=True)
            self.assertFalse(any(isinstance(item, StatsUserSelect) for item in panel.children))
            interaction = self.interaction(admin=True)
            await next(item for item in panel.children if isinstance(item, RunStatsButton)).callback(interaction)
            observed.append((panel.build_preview(),
                             interaction.edit_original_response.await_args.kwargs["content"]))
            interaction.followup.send.assert_not_awaited()
        self.assertEqual(observed[0], observed[1])

    async def test_blocked_user_is_rejected_before_entry_and_after_panel_transition(self) -> None:
        self.seed({"11": 3})
        self.assertTrue(await self.stats.set_opt_out("11", "架空の本人", True))
        self.blacklist.is_blocked.return_value = True
        hub = self.make_hub()
        interaction = self.interaction()
        self.assertFalse(await hub.interaction_check(interaction))
        interaction.response.edit_message.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.blacklist.is_blocked.return_value = False
        panel = await self.open_stats()
        self.blacklist.is_blocked.return_value = True
        interaction = self.interaction()
        self.assertFalse(await panel.interaction_check(interaction))
        interaction.response.edit_message.assert_not_awaited()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.gemini.generate.assert_not_awaited()
