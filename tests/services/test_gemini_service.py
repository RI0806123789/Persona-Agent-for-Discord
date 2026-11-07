"""新SDKで生成期限・検索・画像・文書の既存動作を検証する。"""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google import genai
from google.genai import errors, types
import httpx
from PIL import Image

from tests.helpers.network_guard import block_network

from persona_bot.services import gemini_service
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.genai_client import close_genai_client, create_genai_client


def api_error(code: int, message: str) -> errors.APIError:
    error_type = errors.ServerError if code >= 500 else errors.ClientError
    return error_type(code, {"error": {"message": message}})


def unsupported_search() -> errors.APIError:
    return api_error(400, "google_search is not supported for this model")


class GeminiRequestFixture(unittest.IsolatedAsyncioTestCase):
    """SDKの生成要求だけを差し替え、模擬時計で生成全体の期限を確かめる。"""

    def setUp(self) -> None:
        block_network(self)
        self.now = 100.0
        # 旧REST経路を含め、移行前の失敗確認でも外部通信を発生させない。
        rest_guard = patch("httpx.AsyncClient.post", new_callable=AsyncMock,
                           side_effect=AssertionError("外部通信は禁止です。"))
        rest_guard.start()
        self.addCleanup(rest_guard.stop)
        clock = patch.object(gemini_service, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.response = types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text="架空の応答")]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=7),
        )
        self.sdk_request = AsyncMock(return_value=self.response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.sdk_request)))
        self.service = GeminiService(self.client)

    def timeouts_ms(self) -> list[int | None]:
        return [call.kwargs["config"].http_options.timeout for call in self.sdk_request.call_args_list]

    def models(self) -> list[str]:
        return [call.kwargs["model"] for call in self.sdk_request.call_args_list]

    def search_flags(self) -> list[bool]:
        return [bool(call.kwargs["config"].tools) for call in self.sdk_request.call_args_list]


class GeminiGenerationTests(GeminiRequestFixture):
    async def test_metadata_has_no_generator_when_generation_fails(self) -> None:
        self.sdk_request.side_effect = RuntimeError("架空の生成失敗")
        result = await self.service.generate_with_metadata("架空の質問", "selected-model")
        self.assertIsNone(result.text)
        self.assertIsNone(result.model_name)

    async def test_concurrent_results_keep_their_own_generator(self) -> None:
        first_started, release = asyncio.Event(), asyncio.Event()

        async def generate(**kwargs):
            if kwargs["model"] == "first-model":
                first_started.set()
                await release.wait()
                raise unsupported_search()
            return self.response

        self.sdk_request.side_effect = generate
        first = asyncio.create_task(self.service.generate_with_metadata("架空の質問1", "first-model", grounding=True))
        try:
            await asyncio.wait_for(first_started.wait(), 2)
            second = await self.service.generate_with_metadata("架空の質問2", "second-model")
            release.set()
            first_result = await asyncio.wait_for(first, 2)
            self.assertEqual(first_result.model_name, gemini_service.GROUNDING_MODEL_NAME)
            self.assertEqual(second.model_name, "second-model")
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)

    async def test_search_preserves_selected_model_sources_and_usage(self) -> None:
        self.response.candidates[0].grounding_metadata = types.GroundingMetadata(
            grounding_chunks=[types.GroundingChunk(web=types.GroundingChunkWeb(
                title=" 架空の資料 ", uri=" https://example.com/source ",
            ))],
        )
        result = await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result, ("架空の応答", self.response.usage_metadata,
                                  [{"title": "架空の資料", "uri": "https://example.com/source"}]))
        self.assertEqual(self.models(), ["selected-model"])
        self.assertIsInstance(self.sdk_request.call_args.kwargs["config"].tools[0].google_search, types.GoogleSearch)

    async def test_search_fallbacks_share_budget_and_restore_selected_model(self) -> None:
        async def generate(**kwargs):
            self.now += 8
            if self.sdk_request.await_count < 3:
                raise unsupported_search()
            return self.response

        self.sdk_request.side_effect = generate
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME, "selected-model"])
        self.assertEqual(self.timeouts_ms(), [30000, 22000, 14000])
        self.assertEqual(self.search_flags(), [True, True, False])
        self.assertTrue(all(call.kwargs["config"].http_options.retry_options.attempts == 1
                            for call in self.sdk_request.call_args_list))

    async def test_submodel_success_finishes_without_plain_generation(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME])
        self.assertEqual(self.search_flags(), [True, True])

    async def test_expired_budget_does_not_start_another_attempt(self) -> None:
        async def generate(**kwargs):
            self.now += 30
            raise unsupported_search()

        self.sdk_request.side_effect = generate
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_zero_and_negative_timeout_do_not_send_requests(self) -> None:
        for timeout in (0, -1):
            with self.subTest(timeout=timeout):
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=timeout, grounding=True),
                                 (None, None, []))
        self.sdk_request.assert_not_awaited()

    async def test_default_grounding_budget_uses_configured_constant(self) -> None:
        with patch.object(gemini_service, "GROUNDING_REQUEST_TIMEOUT_SECONDS", 17):
            await self.service.generate("質問", "selected-model", grounding=True)
        self.assertEqual(self.timeouts_ms(), [17000])

    async def test_timeouts_and_connection_errors_do_not_retry(self) -> None:
        for error in (asyncio.TimeoutError(), httpx.ReadTimeout("架空の期限切れ"), httpx.ConnectError("架空の通信障害")):
            with self.subTest(error=type(error).__name__):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = error
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.sdk_request.assert_awaited_once()

    async def test_server_auth_and_unrelated_bad_requests_do_not_retry(self) -> None:
        for code, message in ((500, "障害"), (503, "障害"), (401, "認証失敗"), (403, "権限なし"),
                              (400, "Invalid input text"), (404, "model not found")):
            with self.subTest(code=code):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = api_error(code, message)
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                                 (None, None, []))
                self.sdk_request.assert_awaited_once()

    async def test_search_quota_uses_grounding_submodel(self) -> None:
        # 無料枠の Gemini 3 以降は検索が 429 になるため、検索用のサブモデルの枠で検索する。
        self.sdk_request.side_effect = [api_error(429, "架空の利用枠超過"), self.response]
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME])
        self.assertEqual(self.search_flags(), [True, True])

    async def test_search_quota_recognizes_alternate_status_formats(self) -> None:
        status_error = RuntimeError("架空の利用枠超過")
        status_error.status_code = "429"
        response_error = httpx.HTTPStatusError(
            "架空の利用枠超過", request=httpx.Request("POST", "https://example.invalid/generate"),
            response=httpx.Response(429),
        )
        symbolic_error = RuntimeError("架空の利用枠超過")
        symbolic_error.code = "RESOURCE_EXHAUSTED"
        for error in (status_error, response_error, symbolic_error, RuntimeError("resource_exhausted"),
                      RuntimeError("429 Too Many Requests")):
            with self.subTest(error=type(error).__name__, message=str(error)):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = [error, self.response]
                result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
                self.assertEqual(result[0], "架空の応答")
                self.assertEqual(self.search_flags(), [True, True])

    async def test_structured_status_prevents_text_only_quota_false_positive(self) -> None:
        for code in (401, 403, 500, 503):
            with self.subTest(code=code):
                error = RuntimeError("架空の診断: resource_exhausted 429")
                error.status_code = code
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = error
                result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
                self.assertIsNone(result[0])
                self.sdk_request.assert_awaited_once()

    async def test_text_only_quota_without_search_is_not_retried(self) -> None:
        self.sdk_request.side_effect = RuntimeError("RESOURCE_EXHAUSTED")
        self.assertEqual(await self.service.generate("質問", "selected-model"), (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_invalid_status_numbers_do_not_break_error_handling(self) -> None:
        for code in ("²", "9" * 5000):
            with self.subTest(length=len(code)):
                error = RuntimeError("架空の失敗")
                error.status_code = code
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = error
                result = await self.service.generate("質問", "selected-model", grounding=True)
                self.assertIsNone(result[0])
                self.sdk_request.assert_awaited_once()

    async def test_search_quota_on_both_models_answers_without_search(self) -> None:
        self.sdk_request.side_effect = [api_error(429, "架空の利用枠超過"), api_error(429, "架空の利用枠超過"),
                                        self.response]
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result, ("架空の応答", self.response.usage_metadata, []))
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME, "selected-model"])
        self.assertEqual(self.search_flags(), [True, True, False])

    async def test_quota_without_search_is_not_retried(self) -> None:
        self.sdk_request.side_effect = api_error(429, "架空の利用枠超過")
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.assertEqual(self.search_flags(), [True, True, False])
        self.sdk_request.reset_mock()
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30), (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_search_quota_without_submodel_answers_without_search(self) -> None:
        self.sdk_request.side_effect = [api_error(429, "架空の利用枠超過"), self.response]
        with patch.object(gemini_service, "GROUNDING_SUBMODEL_RETRY_ENABLED", False):
            result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", "selected-model"])
        self.assertEqual(self.search_flags(), [True, False])

    async def test_submodel_failure_does_not_start_plain_fallback(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), api_error(503, "架空の障害")]
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.assertEqual(self.sdk_request.await_count, 2)

    async def test_retry_setting_disables_submodel_and_keeps_selected_model(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        with patch.object(gemini_service, "GROUNDING_SUBMODEL_RETRY_ENABLED", False):
            result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", "selected-model"])
        self.assertEqual(self.search_flags(), [True, False])

    async def test_grounding_submodel_is_not_requested_twice(self) -> None:
        self.sdk_request.side_effect = [unsupported_search(), self.response]
        await self.service.generate("質問", gemini_service.GROUNDING_MODEL_NAME, timeout=30, grounding=True)
        self.assertEqual(self.models(), [gemini_service.GROUNDING_MODEL_NAME] * 2)
        self.assertEqual(self.search_flags(), [True, False])

    async def test_image_with_search_tool_rejection_falls_back_without_search(self) -> None:
        """画像と検索ツールの併用を断られても、検索なしで画像ごと答える（#398）。"""
        image = object()
        refusal = api_error(400, "Tools are not supported with multimodal inputs")
        self.sdk_request.side_effect = [refusal, refusal, self.response]
        result = await self.service.generate("質問", "selected-model", images=[image], timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.search_flags(), [True, True, False])
        self.assertTrue(all(call.kwargs["contents"] == ["質問", image] for call in self.sdk_request.call_args_list))

    async def test_multimodal_refusal_falls_back_only_when_images_were_sent(self) -> None:
        refusal = api_error(400, "Multimodal input is not supported in this configuration")
        with patch.object(gemini_service, "GROUNDING_SUBMODEL_RETRY_ENABLED", False):
            self.sdk_request.side_effect = [refusal, self.response]
            result = await self.service.generate("質問", "selected-model", images=[object()], timeout=30, grounding=True)
            self.assertEqual(result[0], "架空の応答")
            self.assertEqual(self.search_flags(), [True, False])

            self.sdk_request.reset_mock()
            self.sdk_request.side_effect = [refusal, self.response]
            self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                             (None, None, []))
            self.sdk_request.assert_awaited_once()

    async def test_image_format_errors_do_not_start_search_fallbacks(self) -> None:
        """画像そのものの失敗は検索を外しても通らないので、追加の生成をしない。"""
        for message in ("Unsupported media type", "The image is not supported"):
            with self.subTest(message=message):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = [api_error(400, message), self.response]
                self.assertEqual(
                    await self.service.generate("質問", "selected-model", images=[object()], timeout=30, grounding=True),
                    (None, None, []),
                )
                self.sdk_request.assert_awaited_once()

    def test_search_unsupported_detection_keeps_other_failures_apart(self) -> None:
        detect = gemini_service._grounding_is_unsupported
        self.assertTrue(detect(400, "Tool use is not supported for this model"))
        self.assertTrue(detect(400, "google_search is not supported for this model"))
        self.assertTrue(detect(400, "Multimodal inputs are not allowed here", multimodal=True))
        self.assertFalse(detect(400, "Multimodal inputs are not allowed here"))
        for code in (401, 403, 429, 500, 503):
            with self.subTest(code=code):
                self.assertFalse(detect(code, "Tools are not supported with multimodal inputs", multimodal=True))
        self.assertFalse(detect(400, "Request contains an invalid argument", multimodal=True))

    async def test_images_are_preserved_in_all_fallbacks(self) -> None:
        image = object()
        self.sdk_request.side_effect = [unsupported_search(), unsupported_search(), self.response]
        result = await self.service.generate("質問", "selected-model", images=[image], timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertTrue(all(call.kwargs["contents"] == ["質問", image] for call in self.sdk_request.call_args_list))

    async def test_plain_generation_keeps_response_shape_and_no_tools(self) -> None:
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30),
                         ("架空の応答", self.response.usage_metadata, []))
        self.assertEqual(self.models(), ["selected-model"])
        self.assertEqual(self.search_flags(), [False])
        self.assertEqual(self.timeouts_ms(), [30000])

    async def test_plain_generation_does_not_retry_search_errors(self) -> None:
        self.sdk_request.side_effect = unsupported_search()
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30), (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_without_explicit_timeout_keeps_sdk_default_timeout(self) -> None:
        result = await self.service.generate("質問", "selected-model")
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.timeouts_ms(), [None])

    async def test_response_arriving_after_deadline_is_discarded(self) -> None:
        async def generate(**kwargs):
            self.now += 31
            return self.response

        self.sdk_request.side_effect = generate
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_cancellation_propagates_without_fallback(self) -> None:
        for images in (None, ["架空の画像"]):
            with self.subTest(images=images):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = asyncio.CancelledError()
                with self.assertRaises(asyncio.CancelledError):
                    await self.service.generate("質問", "selected-model", images=images, timeout=30, grounding=True)
                self.sdk_request.assert_awaited_once()

    async def test_total_timeout_cancels_pending_request(self) -> None:
        canceled = asyncio.Event()

        async def generate(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.sdk_request.side_effect = generate
        result = await asyncio.wait_for(self.service.generate("質問", "selected-model", timeout=0.01, grounding=True),
                                        timeout=0.5)
        self.assertEqual(result, (None, None, []))
        self.assertTrue(canceled.is_set())
        self.sdk_request.assert_awaited_once()

    async def test_empty_grounding_metadata_and_source_limit_are_safe(self) -> None:
        for metadata in (None, types.GroundingMetadata(), types.GroundingMetadata(grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title=None, uri="  ")),
            *[types.GroundingChunk(web=types.GroundingChunkWeb(uri=f"https://example.com/{index}"))
              for index in range(10)],
        ])):
            with self.subTest(metadata=metadata):
                self.response.candidates[0].grounding_metadata = metadata
                result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
                self.assertTrue(all(source["uri"] for source in result[2]))
                self.assertLessEqual(len(result[2]), gemini_service.MAX_GROUNDING_SOURCES)

    async def test_grounding_skips_invalid_and_duplicate_chunks_before_applying_limit(self) -> None:
        chunks = [SimpleNamespace(web=None), SimpleNamespace(web=SimpleNamespace(uri="  "))]
        for index in range(7):
            web = SimpleNamespace(uri=f" https://example.com/{index} ", title=f" 資料{index} ")
            chunks.extend([SimpleNamespace(web=web), SimpleNamespace(web=web)])
        self.response.candidates[0].grounding_metadata = SimpleNamespace(grounding_chunks=chunks)
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[2], [
            {"title": f"資料{index}", "uri": f"https://example.com/{index}"}
            for index in range(gemini_service.MAX_GROUNDING_SOURCES)
        ])


# 検索の呼び出し・思考・最終回答が通常の text に連続した、架空の異常応答（Issue #167）。
LEAKED_TEXT = (
    'tool_code\nprint(google_search.search(queries=["架空の天気"]))\n\n'
    "thought\n\nI should answer the fictional question in character.\n\n"
    "架空の最終回答です。"
)


def leaked_response(text: str = LEAKED_TEXT) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            finish_reason=types.FinishReason.STOP,
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            grounding_metadata=types.GroundingMetadata(grounding_chunks=[types.GroundingChunk(
                web=types.GroundingChunkWeb(title="破棄される架空の資料", uri="https://example.com/discarded"),
            )]),
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=99),
    )


class GeminiInternalTextTests(GeminiRequestFixture):
    """通常の text へ混入した内部形式を、回数と期限を制限して再生成する。"""

    async def test_internal_text_is_regenerated_with_same_model_and_search(self) -> None:
        self.response.candidates[0].grounding_metadata = types.GroundingMetadata(grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title="架空の資料", uri="https://example.com/source")),
        ])

        async def generate(**kwargs):
            self.now += 8
            return leaked_response() if self.sdk_request.await_count == 1 else self.response

        self.sdk_request.side_effect = generate
        result = await self.service.generate("架空の質問", "selected-model", timeout=30, grounding=True)
        # 破棄した応答の本文・参考リンク・利用量は返さない。
        self.assertEqual(result, ("架空の応答", self.response.usage_metadata,
                                  [{"title": "架空の資料", "uri": "https://example.com/source"}]))
        self.assertEqual(self.models(), ["selected-model", "selected-model"])
        self.assertEqual(self.search_flags(), [True, True])
        # 再生成も生成全体の期限を共有する。
        self.assertEqual(self.timeouts_ms(), [30000, 22000])

    async def test_internal_text_from_search_submodel_after_quota_is_regenerated(self) -> None:
        self.sdk_request.side_effect = [api_error(429, "架空の利用枠超過"), leaked_response(), self.response]
        result = await self.service.generate("質問", "selected-model", timeout=30, grounding=True)
        self.assertEqual(result[0], "架空の応答")
        self.assertEqual(self.models(), ["selected-model", gemini_service.GROUNDING_MODEL_NAME,
                                         gemini_service.GROUNDING_MODEL_NAME])
        self.assertEqual(self.search_flags(), [True, True, True])

    async def test_internal_text_without_search_is_regenerated(self) -> None:
        self.sdk_request.side_effect = [leaked_response(), self.response]
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30),
                         ("架空の応答", self.response.usage_metadata, []))
        self.assertEqual(self.models(), ["selected-model", "selected-model"])
        self.assertEqual(self.search_flags(), [False, False])

    async def test_repeated_internal_text_returns_failure_without_more_requests(self) -> None:
        only_internal = 'tool_code\nprint(google_search.search(queries=["架空の語"]))'
        for grounding, first in ((True, LEAKED_TEXT), (False, only_internal)):
            with self.subTest(grounding=grounding):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = [leaked_response(first), leaked_response(), self.response]
                self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=grounding),
                                 (None, None, []))
                # 初回＋再生成の上限（既定1回）だけで打ち切り、検索なしへの退避も行わない。
                self.assertEqual(self.sdk_request.await_count, 1 + gemini_service.GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS)
                self.assertEqual(self.search_flags(), [grounding] * 2)

    async def test_regeneration_limit_follows_constant(self) -> None:
        for limit, expected in ((0, (None, None, [])), (2, ("架空の応答", self.response.usage_metadata, []))):
            with self.subTest(limit=limit):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = [leaked_response(), leaked_response(), self.response]
                with patch.object(gemini_service, "GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS", limit):
                    self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30), expected)
                self.assertEqual(self.sdk_request.await_count, limit + 1)

    async def test_regeneration_does_not_start_after_budget_is_spent(self) -> None:
        async def generate(**kwargs):
            self.now += 30
            return leaked_response()

        self.sdk_request.side_effect = generate
        self.assertEqual(await self.service.generate("質問", "selected-model", timeout=30, grounding=True),
                         (None, None, []))
        self.sdk_request.assert_awaited_once()

    async def test_diagnostic_log_omits_reply_prompt_and_search_terms(self) -> None:
        self.sdk_request.side_effect = [leaked_response(), leaked_response()]
        with self.assertLogs(gemini_service.logger, level="INFO") as recorded:
            await self.service.generate("架空の秘密の質問", "selected-model", timeout=30, grounding=True)
        logs = "\n".join(record.getMessage() for record in recorded.records)
        self.assertIn("内部形式が混入した応答を破棄しました（検索: 有効）", logs)
        self.assertIn("再生成の上限に達したため", logs)
        for secret in ("架空の秘密の質問", "架空の天気", "I should answer", "架空の最終回答", "example.com", "selected-model"):
            self.assertNotIn(secret, logs)

    async def test_normal_words_code_examples_and_structured_thoughts_are_not_regenerated(self) -> None:
        normal = "I thought about it.\n```python\nprint('架空の出力')\n```\n`tool_code` は架空の語だよ。"
        structured = types.GenerateContentResponse(candidates=[types.Candidate(
            finish_reason=types.FinishReason.STOP,
            content=types.Content(role="model", parts=[
                types.Part(text=LEAKED_TEXT, thought=True), types.Part(text="架空の公開回答"),
            ]),
        )])
        for response, expected in ((leaked_response(normal), normal), (structured, "架空の公開回答")):
            with self.subTest(expected=expected):
                self.sdk_request.reset_mock()
                self.sdk_request.side_effect = [response]
                result = await self.service.generate("質問", "selected-model", timeout=30)
                self.assertEqual(result[0], expected)
                self.sdk_request.assert_awaited_once()

    async def test_affinity_tag_survives_regeneration(self) -> None:
        tagged = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[
            types.Part(text="架空の応答です。[V:+5, A:-3]"),
        ]))])
        self.sdk_request.side_effect = [leaked_response(LEAKED_TEXT + "[V:+1, A:+1]"), tagged]
        result = await self.service.generate("質問", "selected-model", timeout=30)
        self.assertEqual(result[0], "架空の応答です。[V:+5, A:-3]")


class GeminiDocumentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        block_network(self)
        self.uploaded = types.File(name="files/example", uri="https://example.com/file", mime_type="application/pdf")
        self.files = SimpleNamespace(upload=AsyncMock(return_value=self.uploaded), delete=AsyncMock())
        response = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[
            types.Part(text='{"summary":" 架空の要約 ","key_points":["要点"],"warnings":"不正な型"}')
        ]))])
        self.generate = AsyncMock(return_value=response)
        self.client = SimpleNamespace(aio=SimpleNamespace(files=self.files, models=SimpleNamespace(generate_content=self.generate)))
        self.service = GeminiService(self.client)

    async def test_document_upload_uses_async_files_and_explicit_mime(self) -> None:
        for extension, mime in ((".pdf", "application/pdf"), (".PDF", "application/pdf"),
                                (".txt", "text/plain"), (".md", "text/plain"), (".csv", "text/plain")):
            with self.subTest(extension=extension):
                self.files.upload.reset_mock()
                path = Path("example" + extension)
                self.assertIs(await self.service.upload_file(path), self.uploaded)
                self.assertEqual(self.files.upload.call_args.kwargs["file"], path)
                config = self.files.upload.call_args.kwargs["config"]
                self.assertEqual(config.mime_type, mime)
                self.assertEqual(config.http_options.timeout, int(gemini_service.DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000))
                self.assertEqual(config.http_options.retry_options.attempts, 1)

    async def test_upload_timeout_cancels_request(self) -> None:
        canceled = asyncio.Event()

        async def upload(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.files.upload.side_effect = upload
        with patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(asyncio.TimeoutError):
                await self.service.upload_file(Path("example.pdf"))
        self.assertTrue(canceled.is_set())

    async def test_document_summary_passes_file_and_json_config(self) -> None:
        result = await self.service.summarize_document(self.uploaded, "example.pdf", "selected-model")
        self.assertEqual(result, {"document_name": "example.pdf", "summary": "架空の要約", "key_points": ["要点"],
                                  "warnings": [], "usage_notes": []})
        kwargs = self.generate.call_args.kwargs
        self.assertEqual(kwargs["model"], "selected-model")
        self.assertIs(kwargs["contents"][1], self.uploaded)
        self.assertEqual(kwargs["config"].temperature, 0)
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")
        self.assertEqual(kwargs["config"].http_options.timeout, int(gemini_service.DOCUMENT_SUMMARY_TIMEOUT_SECONDS * 1000))

    async def test_document_summary_reports_usage_even_if_json_is_invalid(self) -> None:
        usage = SimpleNamespace(total_token_count=17)
        self.generate.return_value = SimpleNamespace(text="invalid", candidates=[], usage_metadata=usage)
        callback = Mock()
        with self.assertRaises(ValueError):
            await self.service.summarize_document(self.uploaded, "example.pdf", "selected-model", on_usage=callback)
        callback.assert_called_once_with(usage)

    async def test_invalid_or_empty_summary_raises(self) -> None:
        for text in ("JSONではない", "{}", '{"summary":" "}', '{"summary":null}',
                     '{"summary":42}', '{"summary":true}', '{"summary":[]}', '{"summary":{}}'):
            with self.subTest(text=text):
                self.generate.return_value = SimpleNamespace(text=text, candidates=[])
                with self.assertRaises(ValueError):
                    await self.service.summarize_document(self.uploaded, "example.pdf", "selected-model")

    async def test_delete_uses_async_file_name_and_skips_nameless_files(self) -> None:
        await self.service.delete_file(SimpleNamespace(name=None))
        self.files.delete.assert_not_awaited()
        await self.service.delete_file(self.uploaded)
        self.files.delete.assert_awaited_once()
        self.assertEqual(self.files.delete.call_args.kwargs["name"], "files/example")

    async def test_delete_errors_are_logged_and_cancellation_propagates(self) -> None:
        self.files.delete.side_effect = api_error(503, "架空の削除エラー")
        await self.service.delete_file(self.uploaded)
        self.files.delete.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.delete_file(self.uploaded)


class GeminiSdkTransportTests(unittest.IsolatedAsyncioTestCase):
    """実SDKのJSON変換とFilesの転送を、ネットワークを使わず確認する。"""

    def setUp(self) -> None:
        block_network(self)

    def sdk_client(self, http_client: httpx.AsyncClient) -> genai.Client:
        return genai.Client(api_key="YOUR_API_KEY", vertexai=False,
                            http_options=types.HttpOptions(httpx_async_client=http_client,
                                                          async_client_args={"transport": http_client._transport},
                                                          retry_options=types.HttpRetryOptions(attempts=1)))

    async def test_pil_image_and_google_search_reach_sdk_request(self) -> None:
        requests = []

        async def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "candidates": [{"content": {"parts": [{"text": "画像の応答"}]},
                                "groundingMetadata": {"groundingChunks": [{"web": {
                                    "title": "資料", "uri": "https://example.com/source",
                                }}]}}],
                "usageMetadata": {"totalTokenCount": 9},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = self.sdk_client(http_client)
            try:
                with Image.new("RGB", (2, 2), "white") as image:
                    result = await GeminiService(client).generate("架空の画像", "selected-model", images=[image],
                                                                  timeout=10, grounding=True)
                self.assertEqual(result[0], "画像の応答")
                self.assertEqual(result[1].total_token_count, 9)
                self.assertEqual(result[2], [{"title": "資料", "uri": "https://example.com/source"}])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["tools"], [{"googleSearch": {}}])
                parts = requests[0]["contents"][0]["parts"]
                self.assertEqual(parts[0], {"text": "架空の画像"})
                # 旧SDKはJPEG、現行SDKはPNGへ変換する。どちらもAPIの対応形式として受け付ける。
                inline = parts[1]["inlineData"]
                self.assertIn(types.Blob.model_validate(inline).mime_type, ("image/png", "image/jpeg"))
                self.assertTrue(inline["data"])
            finally:
                await client.aio.aclose()
                client.close()

    async def test_real_sdk_search_errors_follow_only_supported_fallback_path(self) -> None:
        requests = []

        async def handle(request):
            requests.append((request.url.path, json.loads(request.content)))
            if len(requests) < 3:
                return httpx.Response(400, json={"error": {"code": 400,
                                                           "message": "google_search is not supported"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "架空の応答"}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = self.sdk_client(http_client)
            try:
                result = await GeminiService(client).generate("架空の質問", "selected-model", timeout=10, grounding=True)
                self.assertEqual(result[0], "架空の応答")
                self.assertEqual([path.split("/")[-1] for path, _ in requests], [
                    "selected-model:generateContent", f"{gemini_service.GROUNDING_MODEL_NAME}:generateContent",
                    "selected-model:generateContent",
                ])
                self.assertEqual([bool(body.get("tools")) for _, body in requests], [True, True, False])
            finally:
                await client.aio.aclose()
                client.close()

    async def test_real_sdk_search_quota_error_uses_grounding_submodel(self) -> None:
        requests = []

        async def handle(request):
            requests.append(request.url.path.split("/")[-1])
            if len(requests) == 1:
                # 無料枠の Gemini 3 以降で検索を使ったときと同じ形の応答（文言は架空）。
                return httpx.Response(429, json={"error": {"code": 429, "message": "架空の利用枠超過",
                                                           "status": "RESOURCE_EXHAUSTED"}})
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "架空の応答"}]}}]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = self.sdk_client(http_client)
            try:
                result = await GeminiService(client).generate("架空の質問", "selected-model", timeout=10, grounding=True)
                self.assertEqual(result[0], "架空の応答")
                self.assertEqual(requests, ["selected-model:generateContent",
                                            f"{gemini_service.GROUNDING_MODEL_NAME}:generateContent"])
            finally:
                await client.aio.aclose()
                client.close()

    async def test_files_upload_summary_delete_are_serialized_by_real_sdk(self) -> None:
        for extension, mime in ((".pdf", "application/pdf"), (".txt", "text/plain"),
                                (".md", "text/plain"), (".csv", "text/plain")):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / ("example" + extension)
                payload = b"%PDF-1.4\n" if extension == ".pdf" else "架空の文書".encode("utf-8")
                path.write_bytes(payload)
                requests = []

                async def handle(request):
                    requests.append(request)
                    if request.url.path == "/upload/v1beta/files":
                        return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
                    if request.url.path == "/upload":
                        return httpx.Response(200, headers={"x-goog-upload-status": "final"}, json={"file": {
                            "name": "files/example", "uri": "https://example.com/file", "mimeType": mime,
                            "state": "ACTIVE",
                        }})
                    if request.url.path.endswith(":generateContent"):
                        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{
                            "text": '{"summary":"架空の要約"}',
                        }]}}]})
                    if request.method == "DELETE" and request.url.path == "/v1beta/files/example":
                        return httpx.Response(200, json={})
                    raise AssertionError(f"想定しない模擬リクエストです: {request.method} {request.url.path}")

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
                    client = self.sdk_client(http_client)
                    service = GeminiService(client)
                    try:
                        uploaded = await service.upload_file(path)
                        summary = await service.summarize_document(uploaded, path.name, "selected-model")
                        await service.delete_file(uploaded)
                        self.assertEqual(summary["summary"], "架空の要約")
                        self.assertEqual(len(requests), 4)
                        self.assertEqual(requests[0].headers["X-Goog-Upload-Header-Content-Type"], mime)
                        self.assertEqual(types.File.model_validate(json.loads(requests[0].content)["file"]).mime_type, mime)
                        self.assertEqual(requests[1].content, payload)
                        self.assertEqual(requests[1].headers["X-Goog-Upload-Command"], "upload, finalize")
                        body = json.loads(requests[2].content)
                        file_data = types.FileData.model_validate(body["contents"][0]["parts"][1]["fileData"])
                        self.assertEqual(file_data.file_uri, "https://example.com/file")
                        self.assertEqual(file_data.mime_type, mime)
                        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
                        self.assertEqual(body["generationConfig"]["temperature"], 0)
                    finally:
                        await client.aio.aclose()
                        client.close()

    async def test_missing_upload_status_fails_once_without_sdk_sleep_and_closes_response(self) -> None:
        class TrackedStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"error":{"code":503,"message":"example"}}'

            async def aclose(self):
                self.closed = True

        stream = TrackedStream()
        calls = []

        async def handle(request):
            calls.append(request)
            if request.headers.get("x-goog-upload-command") == "start":
                return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
            return httpx.Response(503, stream=stream)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "example.txt"
            path.write_text("架空の文書", encoding="utf-8")
            with patch("persona_bot.services.genai_client.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                client = create_genai_client("YOUR_API_KEY")
            try:
                with patch("google.genai._api_client.time.sleep", side_effect=AssertionError("同期sleepは禁止です。")), \
                        patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 0.5):
                    with self.assertRaises(httpx.RemoteProtocolError):
                        await asyncio.wait_for(GeminiService(client).upload_file(path), timeout=1)
                self.assertEqual(len(calls), 2)
                self.assertTrue(stream.closed)
            finally:
                await close_genai_client(client)

    async def test_factory_upload_timeout_cancels_real_sdk_transfer(self) -> None:
        calls = []
        canceled = asyncio.Event()

        async def handle(request):
            calls.append(request)
            if request.headers.get("x-goog-upload-command") == "start":
                return httpx.Response(200, json={}, headers={"x-goog-upload-url": "https://example.com/upload"})
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "example.txt"
            path.write_text("架空の文書", encoding="utf-8")
            with patch("persona_bot.services.genai_client.httpx.AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                client = create_genai_client("YOUR_API_KEY")
            try:
                # SDKのローカル初期化・ファイル準備が終わり、転送待機へ到達できる余裕を持つ。
                with patch.object(gemini_service, "DOCUMENT_UPLOAD_TIMEOUT_SECONDS", 2.0):
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(GeminiService(client).upload_file(path), timeout=4)
                self.assertEqual(len(calls), 2)
                self.assertTrue(canceled.is_set())
            finally:
                await close_genai_client(client)


if __name__ == "__main__":
    unittest.main()
