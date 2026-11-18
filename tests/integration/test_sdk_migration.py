"""実SDKの変換と模擬HTTPを使い、移行した各処理を外部通信なしで確認する。"""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from google import genai
from google.genai import types
import httpx

try:
    from pypdf import PdfWriter
    from pypdf.errors import PdfReadError
except ImportError:
    # pypdf の無い環境では PDF の検証だけを省略し、他のテストの検出を止めない
    PdfWriter = PdfReadError = None

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.bot_client import PersonaClient
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.services.document_service import validate_pdf_file
from persona_bot.services.memory_service import MemoryService, _PromptBlockedError
from persona_bot.utils.storage import get_prompt_block_reason, safe_response_text
from persona_bot.utils.usage_graph import UsageTracker


def json_response(text: str) -> dict:
    return {
        "candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 4, "totalTokenCount": 7},
    }


class SdkResponseTests(unittest.TestCase):
    def test_string_enum_block_reason_and_unspecified_reason_are_distinguished(self) -> None:
        for reason in types.BlockedReason:
            with self.subTest(reason=reason.name):
                response = types.GenerateContentResponse(prompt_feedback=types.GenerateContentResponsePromptFeedback(
                    block_reason=reason,
                ))
                expected = None if reason is types.BlockedReason.BLOCKED_REASON_UNSPECIFIED else reason.name
                self.assertEqual(get_prompt_block_reason(response), expected)
                self.assertIsNone(safe_response_text(response))
        for reason, expected in ((0, None), (1, "1")):
            response = SimpleNamespace(prompt_feedback=SimpleNamespace(block_reason=reason))
            self.assertEqual(get_prompt_block_reason(response), expected)

    def test_safe_text_excludes_thoughts_and_blocked_candidate_text(self) -> None:
        response = types.GenerateContentResponse(candidates=[types.Candidate(
            content=types.Content(role="model", parts=[
                types.Part(text="架空の内部思考", thought=True), types.Part(text="公開する架空の応答"),
            ]), finish_reason=types.FinishReason.STOP,
        )])
        self.assertEqual(safe_response_text(response), "公開する架空の応答")
        for reason in ("SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "RECITATION",
                       "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION"):
            with self.subTest(reason=reason):
                response.candidates[0].finish_reason = types.FinishReason(reason)
                self.assertIsNone(safe_response_text(response))
        self.assertIsNone(safe_response_text(types.GenerateContentResponse()))

    def test_new_usage_metadata_keeps_token_counters(self) -> None:
        response = types.GenerateContentResponse.model_validate(json_response("架空の応答"))
        usage = UsageTracker()
        usage.record(response.usage_metadata)
        self.assertEqual(list(usage.input_tokens), [3])
        self.assertEqual(list(usage.output_tokens), [4])
        self.assertEqual(list(usage.total_tokens), [7])


class SdkWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests = []
        self.responses = []

        async def respond(request):
            self.requests.append(request)
            value = self.responses.pop(0)
            if isinstance(value, Exception):
                raise value
            status, body = value if isinstance(value, tuple) else (200, value)
            return httpx.Response(status, json=body)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        self.sdk = genai.Client(
            api_key="YOUR_GEMINI_API_KEY", vertexai=False, enterprise=False,
            http_options=types.HttpOptions(httpx_async_client=self.http),
        )
        self.addAsyncCleanup(self.http.aclose)
        self.addCleanup(self.sdk.close)
        self.addAsyncCleanup(self.sdk.aio.aclose)
        self.moderator = ContentModerator(self.sdk, SimpleNamespace(words=[]), timeout=2)
        self.memory = MemoryService(self.sdk, None)

    async def test_moderation_routes_and_extracts_reminder_in_one_request(self) -> None:
        self.responses.append(json_response(json.dumps({
            "safe": True, "category": "profile",
            "reminder": {"at": "2026-12-01 12:00", "message": "架空の予定", "repeat": "weekly"},
        })))
        safe, reason, category, reminder = await self.moderator.check_and_route("架空の予定を来週も教えて")
        self.assertTrue(safe)
        self.assertEqual(reason, "")
        self.assertEqual(category, "profile")
        self.assertEqual(reminder["repeat"], "weekly")
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.headers["x-goog-api-key"], "YOUR_GEMINI_API_KEY")
        self.assertNotIn("key", request.url.params)
        payload = json.loads(request.content)
        config = payload["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["temperature"], 0)
        self.assertEqual(config["responseSchema"]["required"], ["safe", "category", "reminder"])
        self.assertNotIn("agent", config["responseSchema"]["properties"])
        system = payload["systemInstruction"]["parts"][0]["text"]
        contents = payload["contents"][0]["parts"][0]["text"]
        self.assertIn("追加タスク2", system)
        self.assertNotIn("架空の予定を来週も教えて", system)
        self.assertIn("架空の予定を来週も教えて", contents)
        self.assertNotIn("追加タスク2", contents)

    async def test_prompt_block_is_unsafe_but_server_error_is_fail_open_without_retry(self) -> None:
        self.responses.extend([
            {"promptFeedback": {"blockReason": "SAFETY"}},
            (503, {"error": {"code": 503, "message": "架空の一時障害", "status": "UNAVAILABLE"}}),
        ])
        safe, _ = await self.moderator.check("架空の入力")
        self.assertFalse(safe)
        self.assertEqual(await self.moderator.check("別の架空の入力"), (True, ""))
        self.assertEqual(len(self.requests), 2)

    async def test_memory_routing_and_compaction_use_json_and_keep_block_semantics(self) -> None:
        self.responses.extend([
            json_response('{"category":"projects"}'),
            json_response('{"profile":{"趣味":"架空の趣味"},"projects":{"計画":"架空の計画"}}'),
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}},
        ])
        self.assertEqual(await self.memory.route_category("先週作った架空の計画について詳しく教えて"), "projects")
        profile, projects = await self.memory._compact_memory("架空の会話", "{}", "{}")
        self.assertEqual(profile, {"趣味": "架空の趣味"})
        self.assertEqual(projects, {"計画": "架空の計画"})
        with self.assertRaises(_PromptBlockedError):
            await self.memory._compact_memory("別の架空の会話", "{}", "{}")
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(json.loads(request.content)["generationConfig"]["responseMimeType"] == "application/json"
                            for request in self.requests))

    async def test_malformed_routing_result_is_ignored(self) -> None:
        self.responses.append(json_response("JSONではない架空の応答"))
        self.assertEqual(await self.memory.route_category("架空の設計について詳細に確認したい"), "none")
        self.assertEqual(len(self.requests), 1)

    async def test_user_cancellation_propagates_out_of_moderation(self) -> None:
        with patch("persona_bot.services.content_moderator.generate_content", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.moderator.check("架空の入力")


@unittest.skipIf(PdfWriter is None, "pypdf が未インストールのため PDF の検証を省略します")
class PdfMigrationTests(unittest.TestCase):
    def test_plain_pdf_is_accepted_and_encrypted_or_broken_pdf_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=72, height=72)
            writer.write(path)
            validate_pdf_file(path)
            writer.encrypt("fictional-password")
            writer.write(path)
            with self.assertRaisesRegex(ValueError, "暗号化"):
                validate_pdf_file(path)
            path.write_bytes(b"not-a-pdf")
            with self.assertRaises(PdfReadError):
                validate_pdf_file(path)


class ClientShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_discord_or_async_sdk_close_failure_still_closes_sync_sdk(self) -> None:
        for discord_error, sdk_error in ((None, None), (OSError("架空のDiscord終了失敗"), None),
                                         (None, OSError("架空のSDK終了失敗"))):
            with self.subTest(discord_error=discord_error, sdk_error=sdk_error):
                sdk = Mock(aio=SimpleNamespace(aclose=AsyncMock(side_effect=sdk_error)), close=Mock())
                client = PersonaClient(genai_client=sdk, intents=discord.Intents.none())
                with patch.object(discord.Client, "close", new=AsyncMock(side_effect=discord_error)) as closing:
                    if discord_error or sdk_error:
                        with self.assertRaises(OSError):
                            await client.close()
                    else:
                        await client.close()
                closing.assert_awaited_once()
                sdk.aio.aclose.assert_awaited_once()
                sdk.close.assert_called_once()
