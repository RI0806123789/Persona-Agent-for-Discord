"""判断専用モデル（#450）の入口が、判断できないときに例外を出さず None を返すことを確かめる。"""

import asyncio
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import errors
import httpx

from tests.helpers.network_guard import block_network

from persona_bot.services import genai_client
from persona_bot.services.decision_service import BinaryAnswer, BinaryQuestion, GeminiDecider
from persona_bot.services.genai_client import close_genai_client, create_genai_client
from persona_bot.utils.usage_graph import UsageTracker

LOGGER = "persona_bot.services.decision_service"
QUESTION = BinaryQuestion("架空の問い", true_description="架空のはい", false_description="架空のいいえ")


def response(answers, **extra):
    return {"answers": answers, **extra}


class GeminiDeciderTests(unittest.IsolatedAsyncioTestCase):
    async def decide(self, result=None, *, error=None, text="架空の発言", questions=None, decider=None):
        request = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=result)
        with patch("persona_bot.services.decision_service.create_decision", request):
            answers = await (decider or GeminiDecider("架空のクライアント")).decide_binary(
                text, {"q": QUESTION} if questions is None else questions, label="架空の用途",
            )
        return answers, request

    async def test_sends_noul_questions_and_reads_probabilities(self) -> None:
        bare = BinaryQuestion("架空の問い2")
        decider = GeminiDecider("架空のクライアント", model_name="架空のモデル", timeout=1.5)
        answers, request = await self.decide(response({
            "q": {"type": "noul", "noul": 0.25, "confidence": 0.75},
            "bare": {"type": "noul", "noul": 0.9},
        }), questions={"q": QUESTION, "bare": bare}, decider=decider)
        self.assertEqual(answers, {"q": BinaryAnswer(0.25, 0.75), "bare": BinaryAnswer(0.9, 0.9)})
        request.assert_awaited_once_with(
            "架空のクライアント", model="架空のモデル", input="架空の発言", timeout=1.5,
            questions={
                "q": {"type": "noul", "instructions": "架空の問い", "criteria": {
                    "true_description": "架空のはい", "false_description": "架空のいいえ",
                }},
                # 説明が無い問いには criteria を付けない（空の説明を送らない）。
                "bare": {"type": "noul", "instructions": "架空の問い2"},
            },
        )

    async def test_unreadable_answers_return_none(self) -> None:
        cases = [
            None, [], {}, {"other": {"type": "noul", "noul": 0.1}},
            {"q": None}, {"q": {"type": "choice", "noul": 0.1}},
            {"q": {"type": "noul"}}, {"q": {"type": "noul", "noul": True}},
            {"q": {"type": "noul", "noul": "0.1"}}, {"q": {"type": "noul", "noul": math.nan}},
            {"q": {"type": "noul", "noul": 1.5}}, {"q": {"type": "noul", "noul": -0.1}},
        ]
        for answers in cases:
            with self.subTest(answers=answers):
                with self.assertLogs(LOGGER, level="WARNING"):
                    result, _request = await self.decide(response(answers))
                self.assertIsNone(result)

    async def test_out_of_range_confidence_is_recomputed_from_probability(self) -> None:
        result, _request = await self.decide(response({"q": {"type": "noul", "noul": 0.2, "confidence": 3}}))
        self.assertEqual(result, {"q": BinaryAnswer(0.2, 0.8)})

    async def test_blocked_input_returns_none(self) -> None:
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result, _request = await self.decide(response({}, prompt_feedback={"block_reason": "safety"}))
        self.assertIsNone(result)
        self.assertIn("拒否", logs.records[0].getMessage())

    async def test_failures_return_none_without_raising_or_logging_the_error_text(self) -> None:
        secret = "架空の発言を含むエラー本文"
        blocked = errors.ClientError(400, {"error": {"code": "prohibited_content", "message": secret}})
        for error, expected in (
            (asyncio.TimeoutError(), "期限内"),
            (blocked, "prohibited_content"),
            (errors.ClientError(400, {"error": {"code": secret, "message": secret}}), "状態=400"),
            (errors.ServerError(503, {"error": {"code": 503, "message": secret}}), "状態=503"),
            (RuntimeError(secret), "状態=不明"),
            (ValueError(secret), "状態=不明"),
        ):
            with self.subTest(error=type(error).__name__, expected=expected):
                with self.assertLogs(LOGGER, level="WARNING") as logs:
                    result, _request = await self.decide(error=error)
                self.assertIsNone(result)
                message = logs.records[0].getMessage()
                self.assertIn(expected, message)
                self.assertNotIn(secret, message)

    async def test_cancellation_propagates(self) -> None:
        with self.assertRaises(asyncio.CancelledError):
            await self.decide(error=asyncio.CancelledError())

    async def test_empty_text_or_no_questions_skip_the_request(self) -> None:
        for text, questions in (("", {"q": QUESTION}), ("   \n", {"q": QUESTION}), ("架空の発言", {})):
            with self.subTest(text=text, questions=questions):
                result, request = await self.decide(text=text, questions=questions)
                self.assertIsNone(result)
                request.assert_not_awaited()

    async def test_usage_is_recorded_without_counting_a_conversation(self) -> None:
        tracker = UsageTracker()
        decider = GeminiDecider("架空のクライアント", usage=tracker)
        with self.assertLogs("persona_bot.utils.usage_graph", level="INFO") as logs:
            result, _request = await self.decide(response(
                {"q": {"type": "noul", "noul": 0.1, "confidence": 0.9}},
                usage={"total_input_tokens": 12, "total_output_tokens": 0, "total_tokens": 12},
            ), decider=decider)
        self.assertEqual(result, {"q": BinaryAnswer(0.1, 0.9)})
        self.assertEqual((list(tracker.input_tokens), list(tracker.output_tokens), list(tracker.total_tokens)),
                         ([12], [0], [12]))
        messages = "\n".join(record.getMessage() for record in logs.records)
        self.assertIn("Total token count：12", messages)
        self.assertNotIn("User (", messages)

    async def test_usage_recording_failure_does_not_change_the_answer(self) -> None:
        tracker = Mock(spec=UsageTracker)
        tracker.log_tokens.side_effect = RuntimeError("架空の記録の失敗")
        with self.assertLogs("persona_bot.utils.usage_graph", level="WARNING"):
            result, _request = await self.decide(response(
                {"q": {"type": "noul", "noul": 0.6, "confidence": 0.6}}, usage={"total_tokens": 3},
            ), decider=GeminiDecider("架空のクライアント", usage=tracker))
        self.assertEqual(result, {"q": BinaryAnswer(0.6, 0.6)})


class GeminiDeciderRealSdkTests(unittest.IsolatedAsyncioTestCase):
    """本番と同じ factory・SDK を通し、HTTP の境界だけを差し替える。"""

    def setUp(self) -> None:
        block_network(self)

    async def run_with(self, status, body):
        async def handle(request):
            return httpx.Response(status, json=body)

        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
            client = create_genai_client("YOUR_GEMINI_API_KEY")
        try:
            return await GeminiDecider(client).decide_binary("架空の発言", {"q": QUESTION}, label="架空の用途")
        finally:
            await close_genai_client(client)

    async def test_answer_round_trip(self) -> None:
        self.assertEqual(await self.run_with(200, response({"q": {"type": "noul", "noul": 0.03, "confidence": 0.97}})),
                         {"q": BinaryAnswer(0.03, 0.97)})

    async def test_rejected_input_is_reported_by_its_code_only(self) -> None:
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            result = await self.run_with(400, {"error": {"code": "prohibited_content", "message": "架空の拒否理由"}})
        self.assertIsNone(result)
        self.assertIn("prohibited_content", logs.records[0].getMessage())
        self.assertNotIn("架空の拒否理由", logs.records[0].getMessage())


if __name__ == "__main__":
    unittest.main()
