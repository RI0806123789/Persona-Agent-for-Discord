"""DB と音声の同期処理が完了するまで、繰り返しのキャンセルでも待つことを検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio import speech_text, tts_synthesis, voice
from persona_bot.core.app_state import BotState
from persona_bot.core.database import Database
from persona_bot.services import feedback_service, reminder_service
from persona_bot.services.auto_topic_service import AutoTopicService
from persona_bot.services.document_service import DocumentService
from persona_bot.services.feedback_service import FeedbackService
from persona_bot.services.reminder_service import ReminderService


class ServiceThreadCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Mock()
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.message = SimpleNamespace(author=SimpleNamespace(id=11, display_name="架空の利用者"),
                                       channel=SimpleNamespace(id=10), guild=SimpleNamespace(id=1))
        self.request = {"at": (self.now + timedelta(days=1)).isoformat(), "message": "架空の予定"}
        clock = patch.object(reminder_service, "_utc_now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    async def assert_waits(self, target, name, invoke, result=None, operation=None):
        loop = asyncio.get_running_loop()
        entered, released, finished = asyncio.Event(), threading.Event(), threading.Event()
        threads = []

        def blocked(*args, **kwargs):
            threads.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not released.wait(5):
                raise AssertionError("同期処理を再開できませんでした。")
            try:
                return operation(*args, **kwargs) if operation else result
            finally:
                finished.set()

        with patch.object(target, name, side_effect=blocked):
            task = asyncio.create_task(invoke())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.01)
                ended_early = task.done()
            finally:
                released.set()
                outcome = await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(ended_early, name)
        self.assertTrue(finished.is_set(), name)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertIsInstance(outcome[0], asyncio.CancelledError)

    async def test_reminder_db_calls_wait_on_cancellation(self):
        service = ReminderService(None, self.db, None)
        self.db.reminder_claim_due.return_value = [{"reminder_id": 1, "remind_at": self.now.isoformat()}]
        service._notify = AsyncMock()
        cases = [
            ("reminder_add", lambda: service.create_from_request(self.message, self.request)),
            ("reminder_list_pending", lambda: service.list_pending("11")),
            ("reminder_cancel", lambda: service.cancel(1, "11")),
            ("reminder_claim_due", service.run_due_checks),
            ("reminder_prune_finished", service.run_due_checks),
        ]
        for name, invoke in cases:
            with self.subTest(name=name):
                await self.assert_waits(self.db, name, invoke)

    async def test_document_db_calls_wait_on_cancellation(self):
        service = DocumentService(None, BotState(), None, None, None, self.db)
        cases = [
            ("queue_recover_stuck", service._recover_stuck_tasks),
            ("queue_try_enqueue", lambda: service._enqueue({"task_id": "fictional"})),
            ("queue_update_task", lambda: service._update_task("fictional", status="completed")),
            ("queue_prune_finished", lambda: service._update_task("fictional", status="completed")),
            ("queue_claim_next_pending", service._claim_next_task),
        ]
        for name, invoke in cases:
            with self.subTest(name=name):
                await self.assert_waits(self.db, name, invoke)

    async def test_anniversary_db_read_waits_on_cancellation(self):
        service = AutoTopicService(None, BotState(), None, None, None, None, None, self.db)
        await self.assert_waits(self.db, "profile_load_all_for_key", service.run_anniversary_checks)

    async def test_feedback_db_calls_wait_on_cancellation(self):
        service = FeedbackService(SimpleNamespace(user=SimpleNamespace(id=99)), self.db)
        service._resolve_vote_target = Mock(return_value=SimpleNamespace(
            guild_id=1, channel_id=10, target_user_id="11", model_name="架空のモデル", prompt_file="persona.txt"))
        payload = SimpleNamespace(message_id=20, user_id=11, emoji="👍", member=SimpleNamespace(bot=False))
        with patch.object(feedback_service, "FEEDBACK_ENABLED", True):
            cases = [
                ("feedback_record", lambda: service.handle_reaction_add(payload)),
                ("feedback_delete", lambda: service.handle_reaction_remove(payload)),
                ("feedback_prune", service.prune_stored),
                ("feedback_summary", lambda: service.build_summary_text(1)),
            ]
            for name, invoke in cases:
                with self.subTest(name=name):
                    await self.assert_waits(self.db, name, invoke)

    async def test_audio_synthesis_and_decode_wait_on_cancellation(self):
        with patch.object(tts_synthesis, "_edge_tts_failing", False), patch.object(
            tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=OSError("架空の障害"))), patch("builtins.print"):
            await self.assert_waits(tts_synthesis, "_fetch_gtts_mp3", lambda: tts_synthesis._fetch_mp3("架空の音声", {}), b"mp3")
        with patch.object(tts_synthesis, "_fetch_mp3", new=AsyncMock(return_value=b"mp3")):
            await self.assert_waits(tts_synthesis, "_decode_to_pcm", lambda: voice._synthesize_pcm("架空の音声", {}), b"pcm")

    async def test_cancelled_registration_finishes_transaction_before_database_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "bot.db")
            db.initialize()
            try:
                service = ReminderService(None, db, None)
                original = db.reminder_add
                await self.assert_waits(db, "reminder_add", lambda: service.create_from_request(self.message, self.request),
                                       operation=original)
                self.assertEqual(db.reminder_count_pending("11"), 1)
            finally:
                db.close()
