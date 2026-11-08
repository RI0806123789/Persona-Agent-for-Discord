"""DB待機・画面遷移・時間切れの完了順が逆転しても、最新パネルを維持する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core.app_state import GuildSettings
from persona_bot.core.background import spawn
from persona_bot.views import functions_view
from persona_bot.views.auto_topic_view import AutoTopicView, AutoTopicConfirmButton, AutoTopicToggleButton
from persona_bot.views.functions_view import FunctionsView, GraphButton, GroundingToggleButton, HelpButton, ReminderButton, StatsButton
from persona_bot.views.help_view import HelpView
from persona_bot.views.reminder_view import ReminderView
from persona_bot.views.stats_privacy_view import StatsPrivacyView, StatsPrivacyConfirmButton, StatsDataDeleteButton, StatsCollectToggleButton
from persona_bot.views.stats_view import StatsView, RunStatsButton, StatsItemSelect
from persona_bot.views.summary_view import SummaryView, RunSummaryButton, PrivateToggleButton
from persona_bot.views.view_parts import BackButton, PanelView, swap_panel, begin_panel_operation, edit_panel_message
from tests.helpers.discord_mocks import make_interaction, make_response


class PanelConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = GuildSettings(grounding_enabled=False)
        self.reminders = SimpleNamespace(list_pending=AsyncMock(return_value=[]))
        self.blacklist = SimpleNamespace(is_blocked=Mock(return_value=False))
        self.views = []
        self.tasks = []
        self.releases = []
        self.edits = []
        self.current = None

        async def edit(**kwargs):
            self.edits.append(kwargs)
            self.current = kwargs["view"]

        self.webhook = SimpleNamespace(edit=AsyncMock(side_effect=edit))
        self.hub = self.make_hub()
        self.current = self.hub

    def make_hub(self, _interaction=None):
        hub = FunctionsView(self.settings, Mock(), Mock(), self.blacklist, Mock(), Mock(), Mock(), self.reminders)
        return self.track(hub)

    def track(self, view):
        self.views.append(view)
        view.message = self.webhook
        return view

    def interaction(self, *, edit=None):
        response = make_response()

        async def defer(**kwargs):
            response.is_done.return_value = True

        async def editing(**kwargs):
            response.is_done.return_value = True
            if edit is not None:
                await edit(**kwargs)
            await self.webhook.edit(**kwargs)

        response.defer.side_effect = defer
        response.edit_message.side_effect = editing
        return make_interaction(
            response=response, edit_original_response=AsyncMock(side_effect=editing),
            original_response=AsyncMock(return_value=self.webhook), extras={},
            user=SimpleNamespace(id=11, display_name="架空の利用者"),
            client=SimpleNamespace(user=SimpleNamespace(name="架空のボット")),
        )

    async def dispatch(self, view, button_type, interaction=None):
        interaction = interaction or self.interaction()
        self.assertTrue(await view.interaction_check(interaction))
        button = next(item for item in view.children if isinstance(item, button_type))
        await button.callback(interaction)
        return interaction

    def start(self, coroutine):
        task = spawn(coroutine, label="パネル操作の競合の確認")
        self.tasks.append(task)
        return task

    def waiting(self, result):
        entered, release = asyncio.Event(), asyncio.Event()
        self.releases.append(release)

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return result

        return entered, release, AsyncMock(side_effect=wait)

    async def asyncTearDown(self):
        for release in self.releases:
            release.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for view in self.views:
            view.stop()
        if isinstance(self.current, PanelView):
            self.current.stop()

    async def test_grounding_save_cannot_restore_hub_after_help_and_back(self):
        entered, release, self.settings.persist = self.waiting(True)
        pending = self.start(self.dispatch(self.hub, GroundingToggleButton))
        await asyncio.wait_for(entered.wait(), 2)
        await self.dispatch(self.hub, HelpButton)
        help_view = self.track(self.current)
        self.assertIsInstance(help_view, HelpView)
        await self.dispatch(help_view, BackButton)
        newest = self.track(self.current)
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertIs(self.current, newest)
        self.assertFalse(newest.is_finished())
        self.assertTrue(self.settings.grounding_enabled)
        self.settings.persist.assert_awaited_once()
        count = len(self.edits)
        await self.hub.on_timeout()
        await help_view.on_timeout()
        self.assertEqual(len(self.edits), count)
        self.assertFalse(await self.hub.interaction_check(self.interaction()))

    async def test_stats_start_edit_rejects_late_select_without_losing_result(self):
        stats = SimpleNamespace(
            build_dashboard=AsyncMock(return_value=("架空の統計結果", [])),
            get_user_options=Mock(return_value=[]), display_name=Mock(return_value="架空の本人"),
        )
        view = self.track(StatsView(stats, viewer_id="11", is_admin=False))
        select = next(item for item in view.children if isinstance(item, StatsItemSelect))
        select._values = ["personality"]
        entered, release, waiting = self.waiting(None)
        running = self.start(self.dispatch(view, RunStatsButton, self.interaction(edit=waiting)))
        await asyncio.wait_for(entered.wait(), 2)
        self.assertFalse(await view.interaction_check(self.interaction()))
        # interaction_checkを先に通過済みだったコールバックも世代を進めさせない。
        late_interaction = self.interaction()
        late = self.start(select.callback(late_interaction))
        while not late_interaction.response.defer.await_count:
            await asyncio.sleep(0)
        await view.on_timeout()
        release.set()
        await asyncio.wait_for(asyncio.gather(running, late), 2)
        stats.build_dashboard.assert_awaited_once_with("11", "all", viewer_id="11")
        self.assertEqual(self.edits[-1]["content"], "架空の統計結果")
        self.assertEqual(len(self.edits), 2)

    async def test_summary_start_edit_keeps_private_choice_and_completes_after_late_toggle(self):
        view = self.track(SummaryView(self.settings, Mock(), Mock(), Mock(), Mock()))
        view.temp_private = True
        toggle = next(item for item in view.children if isinstance(item, PrivateToggleButton))
        entered, release, waiting = self.waiting(None)
        interaction = self.interaction(edit=waiting)
        interaction.channel = Mock()
        with patch("persona_bot.views.summary_view.is_supported_message_channel", return_value=True), \
             patch("persona_bot.views.summary_view.generate_channel_summary", new=AsyncMock(return_value=(True, "架空の要約結果"))), \
             patch("persona_bot.views.summary_view.send_long_message", new=AsyncMock()) as public:
            running = self.start(self.dispatch(view, RunSummaryButton, interaction))
            await asyncio.wait_for(entered.wait(), 2)
            self.assertFalse(await view.interaction_check(self.interaction()))
            late_interaction = self.interaction()
            late = self.start(toggle.callback(late_interaction))
            while not late_interaction.response.defer.await_count:
                await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(running, late), 2)
        public.assert_not_awaited()
        self.assertEqual(self.edits[-1]["content"], "架空の要約結果")
        self.assertEqual(len(self.edits), 2)

    async def test_failed_close_edit_restores_live_view(self):
        for error in (RuntimeError("架空の編集失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                interaction = self.interaction()
                interaction.response.edit_message.side_effect = error
                begin_panel_operation(self.hub, interaction)
                with self.assertRaises(type(error)):
                    await edit_panel_message(interaction, content="実行中", view=None)
                self.assertTrue(await self.hub.interaction_check(self.interaction()))
                self.assertFalse(self.hub.is_finished())

    async def test_old_reminder_query_discards_and_stops_its_unused_panel(self):
        entered, release, self.reminders.list_pending = self.waiting([])
        created = []

        def make_panel(*args, **kwargs):
            panel = self.track(ReminderView(*args, **kwargs))
            created.append(panel)
            return panel

        with patch.object(functions_view, "ReminderView", side_effect=make_panel):
            pending = self.start(self.dispatch(self.hub, ReminderButton))
            await asyncio.wait_for(entered.wait(), 2)
            await self.dispatch(self.hub, HelpButton)
            newest = self.track(self.current)
            release.set()
            await asyncio.wait_for(pending, 2)
        self.assertIs(self.current, newest)
        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].is_finished())
        await created[0].on_timeout()
        self.assertIs(self.current, newest)

    async def test_separate_graph_message_does_not_invalidate_pending_hub_refresh(self):
        entered, release, self.settings.persist = self.waiting(True)
        buffer = io.BytesIO(b"fictional-png")
        self.addCleanup(buffer.close)
        self.hub.usage = SimpleNamespace(has_data=Mock(return_value=True), build_graph_buffer=AsyncMock(return_value=buffer))
        pending = self.start(self.dispatch(self.hub, GroundingToggleButton))
        await asyncio.wait_for(entered.wait(), 2)
        await self.dispatch(self.hub, GraphButton)
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertIs(self.current, self.hub)
        self.assertEqual(len(self.edits), 1)
        self.assertIn("Google検索: ✅ 有効", self.edits[0]["content"])

    async def test_stats_entry_with_no_public_data_invalidates_pending_hub_refresh(self):
        hub = self.make_hub()
        entered, release, self.settings.persist = self.waiting(True)
        hub.stats.has_data.return_value = False
        pending = self.start(self.dispatch(hub, GroundingToggleButton))
        await asyncio.wait_for(entered.wait(), 2)
        await self.dispatch(hub, StatsButton)
        panel = self.track(self.current)
        self.assertIsInstance(panel, StatsView)
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertIs(self.current, panel)
        self.assertEqual(len(self.edits), 1)
        self.assertTrue(hub.is_finished())
        self.assertFalse(panel.is_finished())
        self.assertTrue(self.settings.grounding_enabled)
        self.settings.persist.assert_awaited_once()
        await hub.on_timeout()
        self.assertEqual(len(self.edits), 1)

    async def test_latest_query_wins_when_database_results_finish_in_reverse_order(self):
        first_entered, first_release, first = self.waiting([])
        second_entered, second_release, second = self.waiting([])
        results = iter((first, second))

        async def query(*args):
            return await next(results)(*args)

        self.reminders.list_pending.side_effect = query
        old = self.start(self.dispatch(self.hub, ReminderButton))
        await asyncio.wait_for(first_entered.wait(), 2)
        recent = self.start(self.dispatch(self.hub, ReminderButton))
        await asyncio.wait_for(second_entered.wait(), 2)
        second_release.set()
        await asyncio.wait_for(recent, 2)
        newest = self.track(self.current)
        first_release.set()
        await asyncio.wait_for(old, 2)
        self.assertIs(self.current, newest)
        self.assertIsInstance(newest, ReminderView)
        self.assertEqual(len(self.edits), 1)

    async def test_edits_in_progress_are_serialized_and_replaced_panels_stop(self):
        entered, release, waiting = self.waiting(None)
        first = self.start(self.dispatch(self.hub, HelpButton, self.interaction(edit=waiting)))
        await asyncio.wait_for(entered.wait(), 2)
        second = self.start(self.dispatch(self.hub, ReminderButton))
        # 一覧取得まで進んだことを待ち、保存やネットワークの速度には依存させない。
        while not self.reminders.list_pending.await_count:
            await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        old_help = self.track(self.edits[0]["view"])
        self.assertTrue(old_help.is_finished())
        self.assertIsInstance(self.current, ReminderView)
        self.assertFalse(self.current.is_finished())
        count = len(self.edits)
        await old_help.on_timeout()
        self.assertEqual(len(self.edits), count)

    async def test_timeout_invalidates_pending_save_even_after_discord_marks_view_finished(self):
        entered, release, self.settings.persist = self.waiting(True)
        pending = self.start(self.dispatch(self.hub, GroundingToggleButton))
        await asyncio.wait_for(entered.wait(), 2)
        # discord.pyはon_timeoutを呼ぶ前にViewを終了済みにする。
        discord.ui.View.stop(self.hub)
        await self.hub.on_timeout()
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertIsNone(self.current)
        self.assertEqual(len(self.edits), 1)
        self.assertTrue(self.settings.grounding_enabled)

    async def test_failed_or_cancelled_edit_stops_the_unadopted_view(self):
        for error in (RuntimeError("架空の編集失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                panel = self.track(PanelView(timeout_notice="架空の時間切れ"))
                interaction = self.interaction()
                interaction.response.edit_message.side_effect = error
                with self.assertRaises(type(error)):
                    await swap_panel(interaction, self.hub, panel, "架空の画面")
                self.assertTrue(panel.is_finished())
                self.assertIs(self.current, self.hub)

    async def test_cancelled_lock_wait_acknowledges_and_stops_the_unadopted_view(self):
        entered, release, waiting = self.waiting(None)
        first = self.start(self.dispatch(self.hub, HelpButton, self.interaction(edit=waiting)))
        await asyncio.wait_for(entered.wait(), 2)
        panel = self.track(PanelView(timeout_notice="架空の時間切れ"))
        interaction = self.interaction()
        acknowledged = asyncio.Event()
        original_defer = interaction.response.defer.side_effect

        async def defer(**kwargs):
            await original_defer(**kwargs)
            acknowledged.set()

        interaction.response.defer.side_effect = defer
        queued = self.start(swap_panel(interaction, self.hub, panel, "架空の画面"))
        await asyncio.wait_for(acknowledged.wait(), 2)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.assertTrue(panel.is_finished())
        interaction.response.edit_message.assert_not_awaited()
        release.set()
        await asyncio.wait_for(first, 2)
        self.assertIsInstance(self.current, HelpView)

    async def test_privacy_save_failure_after_back_is_reported_without_overwriting_hub(self):
        entered, release, database = self.waiting(False)
        service = SimpleNamespace(is_opted_out=Mock(return_value=False), set_opt_out=database)
        view = self.track(StatsPrivacyView(service, viewer_id="11", viewer_name="架空の利用者", make_hub=self.make_hub))
        interaction = self.interaction()
        pending = self.start(self.dispatch(view, StatsPrivacyConfirmButton, interaction))
        await asyncio.wait_for(entered.wait(), 2)
        await self.dispatch(view, BackButton)
        newest = self.track(self.current)
        release.set()
        await asyncio.wait_for(pending, 2)
        self.assertIs(self.current, newest)
        interaction.followup.send.assert_awaited_once()
        self.assertIn("保存できませんでした", interaction.followup.send.await_args.args[0])
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_confirmed_database_changes_finish_after_back_without_closing_new_hub(self):
        for kind in ("auto_topic", "privacy_save", "privacy_delete"):
            with self.subTest(kind=kind):
                entered, release, database = self.waiting(True)
                if kind == "auto_topic":
                    self.settings.persist = database
                    view = AutoTopicView(self.settings, make_hub=self.make_hub)
                    button_type = AutoTopicConfirmButton
                else:
                    service = SimpleNamespace(is_opted_out=Mock(return_value=False),
                                              set_opt_out=database, clear_user_data=database)
                    view = StatsPrivacyView(service, viewer_id="11", viewer_name="架空の利用者", make_hub=self.make_hub)
                    view.delete_confirmed = kind == "privacy_delete"
                    view.refresh_items()
                    button_type = StatsDataDeleteButton if kind == "privacy_delete" else StatsPrivacyConfirmButton
                self.track(view)
                self.current = view
                pending = self.start(self.dispatch(view, button_type))
                await asyncio.wait_for(entered.wait(), 2)
                await self.dispatch(view, BackButton)
                newest = self.track(self.current)
                release.set()
                await asyncio.wait_for(pending, 2)
                self.assertIs(self.current, newest)
                self.assertFalse(newest.is_finished())
                database.assert_awaited_once()

    async def test_old_confirmation_does_not_stop_the_same_panel_after_newer_selection(self):
        for kind in ("auto_topic", "privacy_save", "privacy_delete"):
            with self.subTest(kind=kind):
                entered, release, database = self.waiting(True)
                if kind == "auto_topic":
                    self.settings.persist = database
                    view = AutoTopicView(self.settings)
                    confirm_type, toggle_type = AutoTopicConfirmButton, AutoTopicToggleButton
                else:
                    service = SimpleNamespace(is_opted_out=Mock(return_value=False),
                                              set_opt_out=database, clear_user_data=database)
                    view = StatsPrivacyView(service, viewer_id="11", viewer_name="架空の利用者")
                    view.delete_confirmed = kind == "privacy_delete"
                    view.refresh_items()
                    confirm_type = StatsDataDeleteButton if kind == "privacy_delete" else StatsPrivacyConfirmButton
                    toggle_type = StatsCollectToggleButton
                self.track(view)
                self.current = view
                pending = self.start(self.dispatch(view, confirm_type))
                await asyncio.wait_for(entered.wait(), 2)
                await self.dispatch(view, toggle_type)
                count = len(self.edits)
                release.set()
                await asyncio.wait_for(pending, 2)
                self.assertEqual(len(self.edits), count)
                self.assertIs(self.current, view)
                self.assertFalse(view.is_finished())
                self.assertTrue(await view.interaction_check(self.interaction()))
                database.assert_awaited_once()
