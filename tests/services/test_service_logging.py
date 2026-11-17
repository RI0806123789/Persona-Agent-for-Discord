"""サービスの運用ログへ本文を渡さず、診断用の例外情報と既存の復帰動作を保つ。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import (
    affinity_service, autonomous_stage, content_moderator, memory_service,
    ng_word_service, reminder_service, stats_service, voice_guard,
)


class ServiceLoggingTests(unittest.IsolatedAsyncioTestCase):
    # すべて架空。本文・入力値・識別子・パスをそれぞれ区別して漏洩を確認する。
    PRIVATE_TEXT = "fictional-private-body"
    PRIVATE_ID = "fictional-private-user-id"
    PRIVATE_PATH = "/fictional-private-root/private-file.txt"

    def assert_private_message(self, records) -> None:
        self.assertTrue(records)
        for record in records:
            for private in (self.PRIVATE_TEXT, self.PRIVATE_ID, self.PRIVATE_PATH):
                self.assertNotIn(private, record.getMessage())

    def failure(self) -> OSError:
        return OSError(f"{self.PRIVATE_TEXT} {self.PRIVATE_ID} {self.PRIVATE_PATH}")

    def test_failed_initial_load_keeps_empty_cache_and_exception_information(self) -> None:
        for module, method, create in (
            (affinity_service, "affinity_load_all", lambda db: affinity_service.AffinityService(db)),
            (stats_service, "stats_load_all", lambda db: stats_service.StatsService(db, None)),
        ):
            with self.subTest(module=module.__name__):
                error = self.failure()
                database = Mock()
                getattr(database, method).side_effect = error
                with self.assertLogs(module.logger, level="ERROR") as captured:
                    service = create(database)
                self.assertEqual(service.cache, {})
                self.assert_private_message(captured.records)
                self.assertIs(captured.records[0].exc_info[1], error)
                self.assertIsNotNone(captured.records[0].exc_info[2])

    async def test_compaction_failure_passes_exception_without_preformatted_private_text(self) -> None:
        database = Mock()
        service = memory_service.MemoryService(None, None, database)
        error = self.failure()
        try:
            raise error
        except OSError:
            with self.assertLogs(memory_service.logger, level="ERROR") as captured:
                await service._register_compact_failure(self.PRIVATE_ID, error, 7)
        self.assert_private_message(captured.records)
        self.assertIs(captured.records[0].exc_info[1], error)
        self.assertIsNotNone(captured.records[0].exc_info[2])
        self.assertEqual(service._compact_fail_count[self.PRIVATE_ID], 1)
        database.pending_clear_upto.assert_not_called()

    async def test_moderation_preserves_decision_and_fail_open_without_logging_inputs(self) -> None:
        service = content_moderator.ContentModerator(None, SimpleNamespace(words=[]))
        for text, expected in ((self.PRIVATE_TEXT, True),
                               ('{"safe":false,"reason":"' + self.PRIVATE_TEXT + '"}', False)):
            with self.subTest(text=text), patch.object(
                content_moderator, "generate_content", AsyncMock(return_value=SimpleNamespace(text=text)),
            ), self.assertLogs(content_moderator.logger, level="WARNING") as captured:
                safe, reason, _ = await service._judge(("架空の指示", self.PRIVATE_TEXT))
                self.assertEqual(safe, expected)
                self.assertEqual(reason, "" if expected else self.PRIVATE_TEXT)
            self.assert_private_message(captured.records)
        error = self.failure()
        with patch.object(content_moderator, "generate_content", AsyncMock(side_effect=error)), \
                self.assertLogs(content_moderator.logger, level="ERROR") as captured:
            self.assertEqual(await service._judge(("架空の指示", self.PRIVATE_TEXT)), (True, "", None))
        self.assert_private_message(captured.records)
        self.assertIs(captured.records[0].exc_info[1], error)

    def test_ng_detection_logs_count_without_words_or_context(self) -> None:
        with patch.object(ng_word_service, "load_ng_words", return_value=[self.PRIVATE_TEXT]):
            service = ng_word_service.NgWordFilter()
        with self.assertLogs(ng_word_service.logger, level="WARNING") as captured:
            masked = service.mask_with_log(self.PRIVATE_TEXT, label=self.PRIVATE_ID)
        self.assertEqual(masked, "●" * len(self.PRIVATE_TEXT))
        self.assertEqual(captured.records[0].getMessage(), "NGワード検出: 1件をマスクしました。")
        self.assert_private_message(captured.records)

    def test_ng_read_failure_omits_path_and_keeps_original_exception(self) -> None:
        error = self.failure()
        with patch.object(Path, "read_text", side_effect=error), \
                self.assertLogs(ng_word_service.logger, level="ERROR") as captured:
            with self.assertRaises(OSError) as raised:
                ng_word_service._load_words_from_file(Path(self.PRIVATE_PATH))
        self.assertIs(raised.exception, error)
        self.assertIs(captured.records[0].exc_info[1], error)
        self.assert_private_message(captured.records)

    def test_invalid_reminder_datetime_is_rejected_without_logging_input(self) -> None:
        service = reminder_service.ReminderService(None, None, None)
        with self.assertLogs(reminder_service.logger, level="WARNING") as captured:
            self.assertIsNone(service._parse_at(self.PRIVATE_TEXT))
        self.assert_private_message(captured.records)
        self.assertIsNotNone(captured.records[0].exc_info)

    async def test_stage_cleanup_and_playback_failures_omit_channel_data(self) -> None:
        error = self.failure()
        instance = SimpleNamespace(id=1, delete=AsyncMock(side_effect=error))
        channel = SimpleNamespace(instance=instance, name=self.PRIVATE_TEXT, id=self.PRIVATE_ID)
        with self.assertLogs(autonomous_stage.logger, level="ERROR") as captured:
            await autonomous_stage._rollback_created_stage(channel, instance)
        self.assert_private_message(captured.records)
        self.assertIs(captured.records[0].exc_info[1], error)
        voice = Mock(spec=voice_guard.discord.VoiceClient)
        voice.is_playing.return_value = True
        voice.stop.side_effect = error
        with self.assertLogs(voice_guard.logger, level="ERROR") as captured:
            voice_guard._stop_playback(voice)
        self.assert_private_message(captured.records)
        self.assertIs(captured.records[0].exc_info[1], error)


if __name__ == "__main__":
    unittest.main()
