"""退出前のパネルが再参加後の設定を上書きしないことを、一時DBで検証する。"""

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.choices import MODEL_CHOICES
from persona_bot.core.app_state import BotState
from persona_bot.core.database import Database
from persona_bot.views.auto_topic_view import AutoTopicConfirmButton, AutoTopicView
from persona_bot.views.autonomous_channel_view import AutonomousChannelConfirmButton, AutonomousChannelView
from persona_bot.views.functions_view import FunctionsView, GroundingToggleButton
from persona_bot.views.settings_view import ConfirmButton, SettingsView
from tests.helpers.discord_mocks import make_guild, make_interaction, make_response
from tests.helpers.member_helpers import make_member


class SettingsRejoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.state = BotState()
        self.state.restore_from_database(self.db)

    def panel(self, kind, settings):
        if kind == "auto":
            view, button = AutoTopicView(settings), AutoTopicConfirmButton
        elif kind == "settings":
            view, button = SettingsView(settings, 0, "なし"), ConfirmButton
        elif kind == "channels":
            guild = make_guild(stage_channels=[], voice_channels=[], me=None)
            view, button = AutonomousChannelView(settings, guild), AutonomousChannelConfirmButton
        else:
            view = FunctionsView(settings, Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock())
            view.blocked_user_check = None
            button = GroundingToggleButton
        self.addCleanup(view.stop)
        return view, next(item for item in view.children if isinstance(item, button))

    def interaction(self):
        response = make_response()

        async def defer(**kwargs):
            response.is_done.return_value = True

        response.defer.side_effect = defer
        return make_interaction(
            response=response, extras={}, guild=SimpleNamespace(id=1), user=make_member(admin=True),
            without=("followup", "original_response"),
        )

    async def rejoin(self):
        self.state.forget_guild(1)
        current = await self.state.get_guild_async(1)
        current.current_model_name = MODEL_CHOICES[-1].value
        await current.persist()
        return current

    async def test_old_panels_are_rejected_and_new_other_and_dm_panels_remain_usable(self):
        old = await self.state.get_guild_async(1)
        old.current_model_name = MODEL_CHOICES[0].value
        await old.persist()
        other = await self.state.get_guild_async(2)
        dm = await self.state.get_guild_async(None)
        panels = [self.panel(kind, old)[0] for kind in ("auto", "settings", "channels", "hub")]
        current = await self.rejoin()
        before = self.db.guild_settings_load_all()
        for view in panels:
            with self.subTest(view=type(view).__name__):
                interaction = self.interaction()
                self.assertFalse(await view.interaction_check(interaction))
                edit = interaction.response.edit_message.call_args.kwargs
                self.assertIn("開き直してください", edit["content"])
                self.assertIsNone(edit["view"])
                self.assertTrue(view.is_finished())
        for settings in (current, other, dm):
            for kind in ("auto", "settings", "channels", "hub"):
                with self.subTest(guild=settings.guild_id, kind=kind):
                    view, _ = self.panel(kind, settings)
                    self.assertTrue(await view.interaction_check(self.interaction()))
        self.assertEqual(self.db.guild_settings_load_all(), before)

    async def test_rejoin_during_defer_rejects_all_setting_writes(self):
        for kind in ("auto", "settings", "channels", "hub"):
            with self.subTest(kind=kind):
                old = await self.state.get_guild_async(1)
                old.current_model_name = MODEL_CHOICES[0].value
                await old.persist()
                view, button = self.panel(kind, old)
                interaction = self.interaction()
                self.assertTrue(await view.interaction_check(interaction))
                original_defer = interaction.response.defer.side_effect

                async def rejoin_on_defer(**kwargs):
                    await original_defer(**kwargs)
                    await self.rejoin()

                interaction.response.defer.side_effect = rejoin_on_defer
                with patch("persona_bot.views.view_parts.is_current_administrator",
                           new=AsyncMock(return_value=True)):
                    await button.callback(interaction)
                self.assertIn("開き直してください", interaction.edit_original_response.call_args.kwargs["content"])
                current = self.state.get_guild(1)
                self.assertEqual(self.db.guild_settings_load_all()[1]["model_name"], current.current_model_name)
                self.assertEqual(current.current_model_name, MODEL_CHOICES[-1].value)

    async def test_old_direct_persist_and_lock_waiter_cannot_overwrite_rejoined_settings(self):
        old = await self.state.get_guild_async(1)
        await old.persist()
        await old._persist_lock.acquire()
        waiter = asyncio.create_task(old.persist())
        try:
            await asyncio.sleep(0)
            self.state.forget_guild(1)
        finally:
            old._persist_lock.release()
        await waiter
        current = await self.rejoin()
        before = self.db.guild_settings_load_all()
        old.current_model_name = MODEL_CHOICES[0].value
        await old.persist()
        self.assertFalse(old.is_active)
        self.assertTrue(current.is_active)
        self.assertEqual(self.db.guild_settings_load_all(), before)

    async def test_current_panel_still_saves_after_rejoin(self):
        await self.state.get_guild_async(1)
        current = await self.rejoin()
        view, button = self.panel("auto", current)
        view.temp_auto_topic_enabled = True
        interaction = self.interaction()
        self.assertTrue(await view.interaction_check(interaction))
        await button.callback(interaction)
        saved = self.db.guild_settings_load_all()[1]
        self.assertTrue(saved["auto_topic_enabled"])
        self.assertEqual(saved["model_name"], MODEL_CHOICES[-1].value)
        self.assertIn("更新しました", interaction.edit_original_response.call_args.kwargs["content"])

    async def test_rejoin_waits_for_started_write_and_old_panel_does_not_report_success(self):
        old = await self.state.get_guild_async(1)
        old.current_model_name = MODEL_CHOICES[0].value
        view, button = self.panel("auto", old)
        interaction = self.interaction()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_save = self.db.guild_settings_save

        def save(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("保存待機が解除されませんでした。")
            original_save(*args, **kwargs)

        tasks = []
        with patch.object(self.db, "guild_settings_save", side_effect=save):
            try:
                tasks.append(asyncio.create_task(button.callback(interaction)))
                await asyncio.wait_for(entered.wait(), 2)
                tasks.append(asyncio.create_task(self.rejoin()))
                await asyncio.sleep(0.02)
                self.assertFalse(tasks[1].done())
                self.assertIsNotNone(await self.state.get_guild_async(2))
                self.assertIs(await self.state.get_guild_async(None), self.state.dm_settings)
            finally:
                release.set()
                await asyncio.gather(*tasks)
        self.assertIn("開き直してください", interaction.edit_original_response.call_args.kwargs["content"])
        self.assertEqual(self.db.guild_settings_load_all()[1]["model_name"], MODEL_CHOICES[-1].value)

    async def test_rejoin_while_confirm_waits_for_save_lock_does_not_report_success(self):
        old = await self.state.get_guild_async(1)
        await old.persist()
        view, button = self.panel("auto", old)
        interaction = self.interaction()
        await old._persist_lock.acquire()
        task = asyncio.create_task(button.callback(interaction))
        try:
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.state.forget_guild(1)
        finally:
            old._persist_lock.release()
            await task
        current = await self.rejoin()
        self.assertIn("開き直してください", interaction.edit_original_response.call_args.kwargs["content"])
        self.assertEqual(self.db.guild_settings_load_all()[1]["model_name"], current.current_model_name)
