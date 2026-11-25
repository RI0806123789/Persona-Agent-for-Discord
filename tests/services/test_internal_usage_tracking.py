"""内部の Gemini 呼び出し（モデレーション・記憶・統計）のトークン記録（#480）。

会話ではない呼び出しは UsageTracker.log_tokens で数え、「User (」行（会話件数）には混ぜない。
記録は付随的な処理なので、失敗しても本処理（特にフェイルオープンのモデレーション判定）を変えない。
"""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from datetime import timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import types
from persona_bot.core.database import Database
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentService
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.services.memory_service import MemoryService, _PromptBlockedError
from persona_bot.services.stats_service import StatsService
from persona_bot.utils.usage_graph import (
    UsageTracker, log_conversation_safely, log_snapshot_safely, log_tokens_safely,
)

USAGE = {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12}
USAGE_LOGGER = "persona_bot.utils.usage_graph"


def failing_tracker() -> SimpleNamespace:
    """記録に必ず失敗する UsageTracker の代役。例外の本文に非公開の値を載せる。"""
    return SimpleNamespace(log_tokens=Mock(side_effect=ValueError("架空の非公開の値")))


class LogTokensSafelyTests(unittest.TestCase):
    def test_records_to_the_tracker_without_a_conversation_line(self):
        tracker = UsageTracker()
        with self.assertLogs(USAGE_LOGGER, "INFO") as captured:
            log_tokens_safely(tracker, "架空の内部処理", USAGE)
        self.assertEqual(list(tracker.total_tokens), [12])
        self.assertIn("Total token count：12", captured.output[-1])
        # 管理コンソールは「User (」行を会話件数に数える。内部処理では出さない。
        self.assertFalse([line for line in captured.output if "User (" in line])

    def test_missing_tracker_or_empty_usage_does_nothing(self):
        tracker = UsageTracker()
        log_tokens_safely(None, "架空の内部処理", USAGE)
        for usage_info in (None, {}):
            log_tokens_safely(tracker, "架空の内部処理", usage_info)
        self.assertFalse(tracker.has_data())

    def test_recording_failure_is_swallowed_and_logs_only_the_error_type(self):
        with self.assertLogs(USAGE_LOGGER, "WARNING") as captured:
            log_tokens_safely(failing_tracker(), "架空の内部処理", USAGE)
        self.assertIn("ValueError", captured.output[0])
        self.assertNotIn("架空の非公開の値", captured.output[0])

    def test_log_conversation_safely_emits_user_line_and_swallows_errors(self):
        """音声会話のターン完了などを会話件数行へ安全に記録する（#496）。"""
        tracker = UsageTracker()
        with self.assertLogs(USAGE_LOGGER, "INFO") as captured:
            log_conversation_safely(tracker, "音声会話")
        self.assertTrue(any("User (音声会話)：会話を処理しました。" in line for line in captured.output))

        log_conversation_safely(None, "音声会話")
        broken = SimpleNamespace(log_conversation=Mock(side_effect=RuntimeError("架空の非公開の値")))
        with self.assertLogs(USAGE_LOGGER, "WARNING") as warn_logs:
            log_conversation_safely(broken, "音声会話")
        self.assertIn("RuntimeError", warn_logs.output[0])
        self.assertNotIn("架空の非公開の値", warn_logs.output[0])

    def test_log_snapshot_safely_emits_conversation_and_tokens_and_swallows_errors(self):
        """会話応答のトークン＋会話件数記録も同じ安全ラッパーを通す（#497）。"""
        tracker = UsageTracker()
        with self.assertLogs(USAGE_LOGGER, "INFO") as captured:
            log_snapshot_safely(tracker, "会話", "架空の質問", None, "架空のプロンプト", "架空の応答", USAGE)
        self.assertEqual(list(tracker.total_tokens), [12])
        self.assertTrue(any("User (会話)：会話を処理しました。" in line for line in captured.output))
        self.assertTrue(any("Total token count：12" in line for line in captured.output))
        for secret in ("架空の質問", "架空のプロンプト", "架空の応答"):
            self.assertFalse([line for line in captured.output if secret in line])

        log_snapshot_safely(None, "会話", "質問", None, "プロンプト", "応答", USAGE)
        broken = SimpleNamespace(log_snapshot=Mock(side_effect=ValueError("架空の非公開の値")))
        with self.assertLogs(USAGE_LOGGER, "WARNING") as warn_logs:
            log_snapshot_safely(broken, "会話", "質問", None, "プロンプト", "応答", USAGE)
        self.assertIn("ValueError", warn_logs.output[0])
        self.assertNotIn("架空の非公開の値", warn_logs.output[0])


class OutputTokenFormatTests(unittest.TestCase):
    """生成 API は candidates_token_count、Gemini Live は response_token_count に出力トークン数を入れる（#496）。"""

    def test_live_usage_metadata_output_tokens_are_recorded(self):
        tracker = UsageTracker()
        live = types.UsageMetadata(prompt_token_count=120, response_token_count=80, total_token_count=200)
        with self.assertLogs(USAGE_LOGGER, "INFO"):
            tracker.log_tokens("音声会話", live)
        self.assertEqual(
            (list(tracker.input_tokens), list(tracker.output_tokens), list(tracker.total_tokens)), ([120], [80], [200]),
        )

    def test_generate_content_usage_and_rest_dicts_are_still_read(self):
        tracker = UsageTracker()
        generated = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=10, candidates_token_count=7, total_token_count=17,
        )
        with self.assertLogs(USAGE_LOGGER, "INFO"):
            tracker.log_tokens("架空", generated)
            tracker.log_tokens("架空", USAGE)
            tracker.log_tokens("架空", {"promptTokenCount": 5, "responseTokenCount": 3, "totalTokenCount": 8})
        self.assertEqual(list(tracker.output_tokens), [7, 2, 3])

    def test_missing_output_tokens_stay_zero(self):
        tracker = UsageTracker()
        with self.assertLogs(USAGE_LOGGER, "INFO"):
            tracker.log_tokens("架空", types.UsageMetadata(prompt_token_count=4, total_token_count=4))
        self.assertEqual(list(tracker.output_tokens), [0])


class ModeratorUsageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def safe_response():
        return SimpleNamespace(text='{"safe": true}', usage_metadata=USAGE)

    async def test_each_judgement_is_recorded_once(self):
        tracker = UsageTracker()
        moderator = ContentModerator(None, SimpleNamespace(words=[]), usage=tracker)
        request = AsyncMock(return_value=self.safe_response())
        with patch("persona_bot.services.content_moderator.generate_content", request):
            await moderator.check("架空の入力")
            await moderator.check_and_route("架空の入力")
        self.assertEqual(list(tracker.total_tokens), [12, 12])
        self.assertEqual(request.await_count, 2)

    async def test_blocked_prompt_still_counts_the_consumed_tokens(self):
        tracker = UsageTracker()
        moderator = ContentModerator(None, SimpleNamespace(words=[]), usage=tracker)
        response = types.GenerateContentResponse(
            prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, total_token_count=5),
        )
        with patch("persona_bot.services.content_moderator.generate_content", AsyncMock(return_value=response)), \
                patch("builtins.print"):
            is_safe, _reason = await moderator.check("架空の入力")
        self.assertFalse(is_safe)
        self.assertEqual(list(tracker.total_tokens), [5])

    async def test_failed_request_records_nothing(self):
        tracker = UsageTracker()
        moderator = ContentModerator(None, SimpleNamespace(words=[]), usage=tracker)
        for error in (asyncio.TimeoutError(), RuntimeError("架空の通信障害")):
            with self.subTest(error=type(error).__name__), patch(
                "persona_bot.services.content_moderator.generate_content", AsyncMock(side_effect=error),
            ), patch("builtins.print"):
                self.assertEqual(await moderator.check("架空の入力"), (True, ""))
        self.assertFalse(tracker.has_data())

    async def test_recording_failure_does_not_change_the_verdict(self):
        # _judge_verdict は全体を except Exception で包む。記録の失敗が「不適切」を「判定不能」へ
        # 変えて通過させてはならない（不適切の判定を取りこぼさない）。
        moderator = ContentModerator(None, SimpleNamespace(words=[]), usage=failing_tracker())
        unsafe = SimpleNamespace(text='{"safe": false, "reason": "架空の理由"}', usage_metadata=USAGE)
        with patch("persona_bot.services.content_moderator.generate_content", AsyncMock(return_value=unsafe)), \
                patch("builtins.print"):
            is_safe, reason = await moderator.check("架空の入力")
        self.assertFalse(is_safe)
        self.assertEqual(reason, "架空の理由")

    async def test_works_without_a_tracker(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch(
            "persona_bot.services.content_moderator.generate_content",
            AsyncMock(return_value=self.safe_response()),
        ):
            self.assertEqual(await moderator.check("架空の入力"), (True, ""))


class MemoryUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_routing_is_recorded_but_short_circuited_greeting_is_not(self):
        tracker = UsageTracker()
        service = MemoryService(None, Mock(), usage=tracker)
        response = SimpleNamespace(text='{"category": "profile"}', usage_metadata=USAGE)
        request = AsyncMock(return_value=response)
        with patch("persona_bot.services.memory_service.generate_content", request):
            self.assertEqual(await service.route_category("私の好きな食べ物を覚えていますか？"), "profile")
            self.assertEqual(await service.route_category("こんにちは"), "none")
        # 定型のあいさつは LLM を呼ばないので、消費も数えない。
        self.assertEqual(request.await_count, 1)
        self.assertEqual(list(tracker.total_tokens), [12])

    async def test_compaction_is_recorded_even_when_the_prompt_is_blocked(self):
        tracker = UsageTracker()
        service = MemoryService(None, Mock(), usage=tracker)
        ok = SimpleNamespace(
            text=json.dumps({"profile": {"note": "架空"}, "projects": {"note": "架空"}}), usage_metadata=USAGE,
        )
        with patch("persona_bot.services.memory_service.generate_content", AsyncMock(return_value=ok)):
            self.assertEqual(
                await service._compact_memory("架空のログ", "{}", "{}"), ({"note": "架空"}, {"note": "架空"}),
            )
        blocked = types.GenerateContentResponse(
            prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"),
            usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=3, total_token_count=3),
        )
        with patch("persona_bot.services.memory_service.generate_content", AsyncMock(return_value=blocked)):
            with self.assertRaises(_PromptBlockedError):
                await service._compact_memory("架空のログ", "{}", "{}")
        self.assertEqual(list(tracker.total_tokens), [12, 3])

    async def test_routing_failure_in_recording_still_returns_the_category(self):
        service = MemoryService(None, Mock(), usage=failing_tracker())
        response = SimpleNamespace(text='{"category": "projects"}', usage_metadata=USAGE)
        with patch("persona_bot.services.memory_service.generate_content", AsyncMock(return_value=response)):
            self.assertEqual(await service.route_category("架空の計画の進み具合を教えてください"), "projects")


class StatsUsageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # OSのタイムゾーンDBの有無に依存させない。
        zone = patch("persona_bot.services.stats_service.resolve_timezone", return_value=timezone(timedelta(hours=9)))
        zone.start()
        self.addCleanup(zone.stop)
        directory = tempfile.TemporaryDirectory(prefix="usage-stats-")
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        entry = StatsService._new_entry("架空の利用者")
        entry.update(total_count=1, recent_samples=["架空の会話"], opt_out=False)
        self.db.stats_save_all({"user-0": entry})
        self.analysis = {
            "topics": [{"label": "架空の話題", "weight": 1}],
            "big_five": {"openness": 50}, "personality": "架空の解析結果",
        }

    def service(self, result, usage):
        gemini = SimpleNamespace(generate=AsyncMock(return_value=result))
        return StatsService(self.db, gemini, usage=usage)

    async def test_analysis_tokens_are_recorded(self):
        tracker = UsageTracker()
        service = self.service((json.dumps(self.analysis), USAGE, []), tracker)
        self.assertIsNotNone(await service.ensure_analysis("user-0", force=True))
        self.assertEqual(list(tracker.total_tokens), [12])

    async def test_failed_generation_records_nothing(self):
        tracker = UsageTracker()
        service = self.service((None, None, []), tracker)
        await service.ensure_analysis("user-0", force=True)
        self.assertFalse(tracker.has_data())

    async def test_recording_failure_does_not_discard_the_analysis(self):
        service = self.service((json.dumps(self.analysis), USAGE, []), failing_tracker())
        analysis = await service.ensure_analysis("user-0", force=True)
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis["personality"], "")  # サンプル不足のため性格推定は保存されない（従来どおり）


class AgentUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_recording_failure_does_not_abort_subagent_steps(self):
        from persona_bot.services.gemini_service import GenerationResult

        zone = patch("persona_bot.services.agent_service.resolve_timezone", return_value=timezone(timedelta(hours=9)))
        zone.start()
        self.addCleanup(zone.stop)
        gemini = SimpleNamespace(
            generate_with_metadata=AsyncMock(return_value=GenerationResult("架空の最終報告", USAGE, [], "selected-model")),
        )
        service = AgentService(gemini, failing_tracker())
        plan = AgentPlan(
            delegate=True, role="調査担当", task="架空の調査", expected_output="箇条書き", steps=("調査する",),
        )
        with self.assertLogs(USAGE_LOGGER, "WARNING") as warn_logs:
            report = await service.run(plan, "selected-model")
        self.assertIsNotNone(report)
        self.assertEqual(report.text, "架空の最終報告")
        self.assertIn("ValueError", warn_logs.output[0])
        self.assertNotIn("架空の非公開の値", warn_logs.output[0])


if __name__ == "__main__":
    unittest.main()
