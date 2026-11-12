"""DBが待機しても先に応答を保留し、選択肢と統計収集停止の秘匿を維持する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import GuildSettings
from persona_bot.services.stats_service import StatsService
from persona_bot.views.auto_topic_view import AutoTopicView, AutoTopicConfirmButton
from persona_bot.views.functions_view import GroundingToggleButton, ReminderButton
from persona_bot.views.unblock_view import UnblockSelect
from persona_bot.views.reminder_view import ReminderCancelSelect, ReminderView
from persona_bot.views.stats_privacy_view import StatsPrivacyView, StatsPrivacyConfirmButton, StatsDataDeleteButton
from persona_bot.views.stats_view import StatsUserSelect, StatsView
from persona_bot.views.view_parts import PanelView, swap_panel
from tests.helpers.discord_mocks import make_interaction, make_response


class DatabaseInteractionTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        response = make_response()

        async def defer(**kwargs):
            response.is_done.return_value = True

        response.defer.side_effect = defer
        return make_interaction(response=response, user_id=11, without=("followup",))

    async def test_every_database_callback_defers_before_waiting(self):
        for kind in ("grounding", "auto_topic", "reminder_list", "reminder_cancel", "privacy_save", "privacy_delete"):
            with self.subTest(kind=kind):
                interaction = self.interaction()
                entered, release = asyncio.Event(), asyncio.Event()

                async def database(*args):
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                    entered.set()
                    await release.wait()
                    return [] if kind == "reminder_list" else True if kind != "reminder_cancel" else None

                settings = GuildSettings()
                settings.persist = AsyncMock(side_effect=database)
                if kind == "auto_topic":
                    view = AutoTopicView(settings)
                    button = next(item for item in view.children if isinstance(item, AutoTopicConfirmButton))
                elif kind.startswith("privacy"):
                    stats = SimpleNamespace(is_opted_out=Mock(return_value=False),
                                            set_opt_out=AsyncMock(side_effect=database), clear_user_data=AsyncMock(side_effect=database))
                    view = StatsPrivacyView(stats, viewer_id="11", viewer_name="架空の本人")
                    view.delete_confirmed = True
                    cls = StatsPrivacyConfirmButton if kind == "privacy_save" else StatsDataDeleteButton
                    button = next(item for item in view.children if isinstance(item, cls))
                elif kind == "reminder_cancel":
                    service = SimpleNamespace(cancel=AsyncMock(side_effect=database), describe=Mock(return_value="架空の予定"))
                    view = ReminderView(service, [{"reminder_id": 1, "message": "架空の予定"}], "11")
                    button = next(item for item in view.children if isinstance(item, ReminderCancelSelect))
                    button._values = ["1"]
                else:
                    view = PanelView(timeout_notice="架空の時間切れ")
                    view.guild_settings = settings
                    view.refresh_items = Mock()
                    view.build_preview = Mock(return_value="架空のパネル")
                    view.clone = Mock()
                    view.reminders = SimpleNamespace(list_pending=AsyncMock(side_effect=database))
                    button = GroundingToggleButton(False) if kind == "grounding" else ReminderButton()
                    view.add_item(button)
                self.addCleanup(view.stop)
                task = asyncio.create_task(button.callback(interaction))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    self.assertFalse(task.done())
                    interaction.response.edit_message.assert_not_awaited()
                    release.set()
                    await task
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                interaction.edit_original_response.assert_awaited_once()
                if kind == "reminder_list":
                    new_panel = interaction.edit_original_response.await_args.kwargs["view"]
                    self.addCleanup(new_panel.stop)
                    self.assertIs(new_panel.message, interaction.original_response.return_value)
                    self.assertTrue(view.is_finished())

    async def test_stale_panel_operations_do_not_run_side_effects(self):
        """閉じた・別の画面へ移ったパネルの確定・取消では、DBを更新しない（#432）。"""
        for kind in ("privacy_save", "reminder_cancel"):
            with self.subTest(kind=kind):
                interaction = self.interaction()
                if kind == "privacy_save":
                    stats = SimpleNamespace(is_opted_out=Mock(return_value=False), set_opt_out=AsyncMock(return_value=True))
                    view = StatsPrivacyView(stats, viewer_id="11", viewer_name="架空の本人")
                    button = next(item for item in view.children if isinstance(item, StatsPrivacyConfirmButton))
                    side_effect = stats.set_opt_out
                else:
                    service = SimpleNamespace(cancel=AsyncMock(return_value=None), describe=Mock(return_value="架空の予定"))
                    view = ReminderView(service, [{"reminder_id": 1, "message": "架空の予定"}], "11")
                    button = next(item for item in view.children if isinstance(item, ReminderCancelSelect))
                    button._values = ["1"]
                    side_effect = service.cancel
                view.stop()  # 「戻る」などで画面が移り、停止済みになった
                await button.callback(interaction)
                side_effect.assert_not_awaited()
                interaction.response.edit_message.assert_not_awaited()
                interaction.edit_original_response.assert_not_awaited()

    async def test_authorize_panel_operation_rejects_stale_views_and_screens_changed_while_waiting(self):
        from persona_bot.views.view_parts import authorize_panel_operation, begin_panel_operation

        stale = PanelView(timeout_notice="架空")
        stale.stop()
        recheck = AsyncMock(return_value=True)
        interaction = self.interaction()
        self.assertFalse(await authorize_panel_operation(
            stale, interaction, allowed=True, recheck=recheck, notice="架空の拒否"))
        recheck.assert_not_awaited()

        view = PanelView(timeout_notice="架空")
        self.addCleanup(view.stop)
        interaction = self.interaction()

        async def move_to_another_screen(_interaction):
            # 権限の再確認を待つ間に、別の操作（戻るなど）が新しい世代を確保した
            begin_panel_operation(view, self.interaction())
            return True

        self.assertFalse(await authorize_panel_operation(
            view, interaction, allowed=True, recheck=move_to_another_screen, notice="架空の拒否"))

        fresh = PanelView(timeout_notice="架空")
        self.addCleanup(fresh.stop)
        self.assertTrue(await authorize_panel_operation(
            fresh, self.interaction(), allowed=True, recheck=AsyncMock(return_value=True), notice="架空の拒否"))

    async def test_deferred_swap_keeps_timeout_and_access_checks(self):
        hub = PanelView(timeout_notice="元の時間切れ")
        panel = PanelView(timeout_notice="新しい時間切れ")
        self.addCleanup(hub.stop)
        self.addCleanup(panel.stop)
        hub.blocked_user_check = Mock(return_value=False)
        interaction = self.interaction()
        await interaction.response.defer(ephemeral=True)
        await swap_panel(interaction, hub, panel, "新しいパネル")
        self.assertIs(panel.blocked_user_check, hub.blocked_user_check)
        self.assertTrue(hub.is_finished())
        self.assertIs(panel.message, interaction.original_response.return_value)
        interaction.response.edit_message.assert_not_awaited()

    async def test_current_user_below_top_25_is_selected_and_labels_are_single_line(self):
        service = StatsService.__new__(StatsService)
        service.cache = {str(i): {"total_count": 100 - i, "user_name": " \n " if i == 30 else f"架空の\n利用者 {i}",
                                 "opt_out": False} for i in range(31)}
        options = service.get_user_options("30")
        self.assertEqual(len(options), 25)
        self.assertIn("30", [row[0] for row in options])
        view = StatsView(service, viewer_id="30", is_admin=True)
        self.addCleanup(view.stop)
        for select in (StatsUserSelect(service.get_user_options("30"), "30"),
                       StatsUserSelect([(str(i), " \n ", i) for i in range(31)], "30"),
                       next(item for item in view.children if isinstance(item, StatsUserSelect))):
            self.assertEqual(len(select.options), 25)
            self.assertEqual([option.value for option in select.options if option.default], ["30"])
            self.assertTrue(all(option.label.strip() and "\n" not in option.label for option in select.options))
        unblock = UnblockSelect([{"user_id": "11", "user_name": " \n "}, {"user_id": "22", "user_name": "架空の\n名前"}])
        self.assertEqual([option.label for option in unblock.options], ["11", "架空の 名前"])

    async def test_all_opted_out_statistics_match_no_data_and_remain_unselectable(self):
        service = StatsService.__new__(StatsService)
        service.cache = {"11": {"total_count": 3, "opt_out": True}, "22": {"total_count": 0, "opt_out": False}}
        self.assertFalse(service.has_data())
        self.assertEqual(service.get_user_options("11"), [])
        service.cache["22"]["total_count"] = 1
        self.assertTrue(service.has_data())
        self.assertEqual([row[0] for row in service.get_user_options("11")], ["22"])
