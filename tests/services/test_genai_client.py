"""共通SDK境界のタイムアウトと暗黙リトライ抑止を確認する。"""

import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google import genai
from google.genai import errors, types
import httpx

from tests.helpers.network_guard import block_network

from persona_bot.services import genai_client
from persona_bot.services.genai_client import close_genai_client, create_genai_client, generate_content
from persona_bot.services.gemini_service import GeminiService


class GenaiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_grounding_factory_keeps_key_out_of_urls_and_error_logs(self) -> None:
        # 本番と同じfactory・SDKを使い、HTTP境界だけを差し替える。
        for failure in (None, "connection", "timeout", "http_status", 400, 503):
            with self.subTest(failure=failure):
                requests = []

                async def handle(request):
                    requests.append(request)
                    if failure == "connection":
                        raise httpx.ConnectError(f"架空の接続障害: {request.url}", request=request)
                    if failure == "timeout":
                        raise httpx.ReadTimeout(f"架空のタイムアウト: {request.url}", request=request)
                    if failure == "http_status":
                        response = httpx.Response(502, request=request)
                        response.raise_for_status()
                    if failure is not None:
                        return httpx.Response(failure, json={"error": {"code": failure, "message": "架空のAPI障害"}})
                    return httpx.Response(200, json={"candidates": [{
                        "content": {"parts": [{"text": "架空の検索結果"}]}, "finishReason": "STOP",
                    }]})

                with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
                    client = create_genai_client("YOUR_GEMINI_API_KEY")
                try:
                    with self.assertLogs("persona_bot.services.gemini_service", level="INFO") as logging:
                        result = await GeminiService(client).generate("架空の質問", "selected-model", grounding=True, timeout=2)
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].headers["x-goog-api-key"], "YOUR_GEMINI_API_KEY")
                    self.assertNotIn("key", requests[0].url.params)
                    self.assertNotIn("YOUR_GEMINI_API_KEY", str(requests[0].url))
                    self.assertNotIn("YOUR_GEMINI_API_KEY", "\n".join(record.getMessage() for record in logging.records))
                    self.assertIn(b"googleSearch", requests[0].content)
                    self.assertEqual(result[0], "架空の検索結果" if failure is None else None)
                finally:
                    await close_genai_client(client)

    def setUp(self) -> None:
        block_network(self)
        self.response = object()
        self.request = AsyncMock(return_value=self.response)
        self.client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=self.request)))

    async def test_factory_fixes_backend_and_one_attempt_for_all_operations(self) -> None:
        with patch.object(genai_client.genai, "Client") as factory:
            result = create_genai_client("YOUR_API_KEY")
        self.assertIs(result, factory.return_value)
        self.assertEqual(factory.call_args.kwargs["api_key"], "YOUR_API_KEY")
        self.assertIs(factory.call_args.kwargs["vertexai"], False)
        self.assertNotIn("enterprise", factory.call_args.kwargs)
        self.assertEqual(factory.call_args.kwargs["http_options"].retry_options.attempts, 1)
        await factory.call_args.kwargs["http_options"].httpx_async_client.aclose()

    async def test_factory_accepts_legacy_client_without_enterprise_parameter(self) -> None:
        class LegacyClient:
            def __init__(self, *, api_key, vertexai, http_options):
                self.api_key = api_key
                self.vertexai = vertexai
                self.http_options = http_options
                self.aio = SimpleNamespace(aclose=AsyncMock())
                self.close = Mock()

        with patch.object(genai_client.genai, "Client", LegacyClient):
            client = create_genai_client("YOUR_API_KEY")
        try:
            self.assertEqual(client.api_key, "YOUR_API_KEY")
            self.assertIs(client.vertexai, False)
            self.assertEqual(client.http_options.retry_options.attempts, 1)
        finally:
            await close_genai_client(client)
        self.assertTrue(client.http_options.httpx_async_client.is_closed)

    async def test_factory_uses_developer_api_even_with_cloud_environment_flags(self) -> None:
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"candidates": [{
                "content": {"parts": [{"text": "架空の応答"}]}, "finishReason": "STOP",
            }]})

        with patch.dict(os.environ, {"GOOGLE_GENAI_USE_VERTEXAI": "true", "GOOGLE_GENAI_USE_ENTERPRISE": "true"}), patch.object(
            genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle),
        ):
            client = create_genai_client("YOUR_API_KEY")
            try:
                await generate_content(client, model="selected-model", contents="架空の入力", timeout=2)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].url.host, "generativelanguage.googleapis.com")
                self.assertEqual(requests[0].headers["x-goog-api-key"], "YOUR_API_KEY")
                self.assertNotIn("key", requests[0].url.params)
            finally:
                await close_genai_client(client)

    async def test_timeout_seconds_are_rounded_up_to_milliseconds(self) -> None:
        result = await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=1.2341,
                                        temperature=0, response_mime_type="application/json")
        self.assertIs(result, self.response)
        kwargs = self.request.call_args.kwargs
        self.assertEqual(kwargs["model"], "selected-model")
        self.assertEqual(kwargs["contents"], "架空の入力")
        self.assertIsInstance(kwargs["config"], types.GenerateContentConfig)
        self.assertEqual(kwargs["config"].http_options.timeout, 1235)
        self.assertEqual(kwargs["config"].http_options.retry_options.attempts, 1)
        self.assertEqual(kwargs["config"].temperature, 0)
        self.assertEqual(kwargs["config"].response_mime_type, "application/json")

    async def test_no_timeout_keeps_sdk_default_and_disables_retries(self) -> None:
        await generate_content(self.client, model="selected-model", contents=["架空の入力"])
        config = self.request.call_args.kwargs["config"]
        self.assertIsNone(config.http_options.timeout)
        self.assertEqual(config.http_options.retry_options.attempts, 1)

    async def test_expired_timeout_stops_before_sdk_request(self) -> None:
        for timeout in (0, -0.1):
            with self.subTest(timeout=timeout):
                with self.assertRaises(asyncio.TimeoutError):
                    await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=timeout)
        self.request.assert_not_awaited()

    async def test_asyncio_deadline_cancels_pending_request(self) -> None:
        canceled = asyncio.Event()

        async def request(**kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.request.side_effect = request
        with self.assertRaises(asyncio.TimeoutError):
            await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=0.01)
        self.assertTrue(canceled.is_set())

    async def test_user_cancellation_propagates(self) -> None:
        self.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await generate_content(self.client, model="selected-model", contents="架空の入力", timeout=10)

    async def test_real_sdk_transport_has_no_server_or_quota_retries(self) -> None:
        for status in (429, 503):
            with self.subTest(status=status):
                calls = []

                async def handle(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": {"message": "架空のAPI障害", "code": status}})

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
                    client = genai.Client(api_key="YOUR_API_KEY", vertexai=False,
                                          http_options=types.HttpOptions(httpx_async_client=http_client,
                                                                        async_client_args={"transport": http_client._transport}))
                    try:
                        with self.assertRaises(errors.APIError):
                            await generate_content(client, model="selected-model", contents="架空の入力", timeout=1)
                        self.assertEqual(len(calls), 1)
                    finally:
                        await client.aio.aclose()
                        client.close()

    async def test_factory_transport_does_not_retry_connection_failures_with_aiohttp_installed(self) -> None:
        calls = []

        async def handle(request):
            calls.append(request)
            raise httpx.ConnectError("架空の通信障害", request=request)

        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
            client = create_genai_client("YOUR_API_KEY")
        try:
            with patch("google.genai._api_client.asyncio.sleep", side_effect=AssertionError("SDK内の再送は禁止です。")):
                with self.assertRaises(httpx.ConnectError):
                    await generate_content(client, model="selected-model", contents="架空の入力", timeout=1)
            self.assertEqual(len(calls), 1)
        finally:
            await close_genai_client(client)

    async def test_close_reclaims_owned_httpx_even_when_sdk_async_close_fails(self) -> None:
        for error in (None, RuntimeError("架空の終了エラー")):
            with self.subTest(error=error):
                client = Mock(aio=SimpleNamespace(aclose=AsyncMock(side_effect=error)))
                http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
                with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                        patch.object(genai_client.genai, "Client", return_value=client):
                    self.assertIs(create_genai_client("YOUR_API_KEY"), client)
                if error is not None:
                    with self.assertRaises(RuntimeError):
                        await close_genai_client(client)
                    client.aio.aclose.side_effect = None
                else:
                    await close_genai_client(client)
                await close_genai_client(client)
                http_client.aclose.assert_awaited_once()
                self.assertEqual(client.close.call_count, 2)

    async def test_close_reclaims_owned_httpx_when_sdk_sync_close_fails(self) -> None:
        client = Mock(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock(side_effect=RuntimeError("架空の終了エラー")))
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", return_value=client):
            create_genai_client("YOUR_API_KEY")
        with self.assertRaises(RuntimeError):
            await close_genai_client(client)
        http_client.aclose.assert_awaited_once()

    async def test_real_factory_closes_owned_httpx_and_transport_once(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
        transport.aclose = AsyncMock(wraps=transport.aclose)
        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=transport):
            client = create_genai_client("YOUR_API_KEY")
        owned = genai_client._owned_http_clients[client]
        self.assertFalse(owned.is_closed)
        await close_genai_client(client)
        self.assertTrue(owned.is_closed)
        await close_genai_client(client)
        transport.aclose.assert_awaited_once()

    async def test_failed_factory_reclaims_unused_httpx_on_running_loop(self) -> None:
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", side_effect=ValueError("架空の初期化エラー")):
            with self.assertRaises(ValueError):
                create_genai_client("YOUR_API_KEY")
        await asyncio.sleep(0)
        http_client.aclose.assert_awaited_once()


class GenaiFactoryFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        block_network(self)

    def test_failed_factory_reclaims_unused_httpx_without_running_loop(self) -> None:
        http_client = Mock(spec=httpx.AsyncClient, aclose=AsyncMock())
        with patch.object(genai_client.httpx, "AsyncClient", return_value=http_client), \
                patch.object(genai_client.genai, "Client", side_effect=ValueError("架空の初期化エラー")):
            with self.assertRaises(ValueError):
                create_genai_client("YOUR_API_KEY")
        http_client.aclose.assert_awaited_once()


class DecisionRequestTests(unittest.IsolatedAsyncioTestCase):
    """Decisions API（#450）は固定版 SDK の非公開の送信口を通る。SDK の更新で口が変わったらここで気づく。"""

    QUESTIONS = {"input_unsafe": {"type": "noul", "instructions": "架空の問い"}}

    def setUp(self) -> None:
        block_network(self)

    def make_client(self, handle):
        with patch.object(genai_client.httpx, "AsyncHTTPTransport", return_value=httpx.MockTransport(handle)):
            return create_genai_client("YOUR_GEMINI_API_KEY")

    def test_pinned_sdk_keeps_the_private_send_entrypoint(self) -> None:
        # create_decision が頼る口。引数の名前と順番が変わったら、公式の client.aio.decisions へ移す合図。
        method = genai._api_client.BaseApiClient.async_request
        self.assertTrue(inspect.iscoroutinefunction(method))
        self.assertEqual(
            list(inspect.signature(method).parameters),
            ["self", "http_method", "path", "request_dict", "http_options"],
        )

    async def test_request_goes_to_v1beta_decisions_with_header_key_and_json_body(self) -> None:
        requests = []

        async def handle(request):
            requests.append(request)
            return httpx.Response(200, json={
                "answers": {"input_unsafe": {"type": "noul", "noul": 0.2, "confidence": 0.8}},
                "usage": {"total_input_tokens": 7, "total_output_tokens": 0, "total_tokens": 7},
            })

        client = self.make_client(handle)
        try:
            result = await genai_client.create_decision(
                client, model="gemini-decision-1", input="架空の発言", questions=self.QUESTIONS, timeout=2,
            )
        finally:
            await close_genai_client(client)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/v1beta/decisions")
        self.assertEqual(request.headers["x-goog-api-key"], "YOUR_GEMINI_API_KEY")
        self.assertNotIn("YOUR_GEMINI_API_KEY", str(request.url))
        self.assertEqual(json.loads(request.content), {
            "model": "gemini-decision-1", "input": "架空の発言", "questions": self.QUESTIONS,
        })
        self.assertEqual(result["answers"]["input_unsafe"]["noul"], 0.2)

    async def test_http_errors_are_sdk_errors_without_retries(self) -> None:
        for status, code in ((400, "prohibited_content"), (429, 429), (503, 503)):
            with self.subTest(status=status):
                calls = []

                async def handle(request):
                    calls.append(request)
                    return httpx.Response(status, json={"error": {"code": code, "message": "架空のAPI障害"}})

                client = self.make_client(handle)
                try:
                    with patch("google.genai._api_client.asyncio.sleep", side_effect=AssertionError("SDK内の再送は禁止です。")):
                        with self.assertRaises(errors.APIError) as raised:
                            await genai_client.create_decision(
                                client, model="gemini-decision-1", input="架空の発言",
                                questions=self.QUESTIONS, timeout=1,
                            )
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(raised.exception.code, status)
                finally:
                    await close_genai_client(client)

    async def test_empty_or_non_object_body_is_an_empty_result(self) -> None:
        for body in (b"", b"[]", b'"\xe6\x9e\xb6\xe7\xa9\xba"'):
            with self.subTest(body=body):
                async def handle(request):
                    return httpx.Response(200, content=body)

                client = self.make_client(handle)
                try:
                    self.assertEqual(await genai_client.create_decision(
                        client, model="gemini-decision-1", input="架空の発言", questions=self.QUESTIONS, timeout=1,
                    ), {})
                finally:
                    await close_genai_client(client)

    async def test_expired_timeout_stops_before_request(self) -> None:
        client = SimpleNamespace(aio=SimpleNamespace(_api_client=SimpleNamespace(async_request=AsyncMock())))
        with self.assertRaises(asyncio.TimeoutError):
            await genai_client.create_decision(
                client, model="gemini-decision-1", input="架空の発言", questions=self.QUESTIONS, timeout=0,
            )
        client.aio._api_client.async_request.assert_not_called()

    async def test_deadline_cancels_a_slow_response_and_options_disable_sdk_retries(self) -> None:
        async def slow(*_args):
            await asyncio.sleep(10)

        request = AsyncMock(side_effect=slow)
        client = SimpleNamespace(aio=SimpleNamespace(_api_client=SimpleNamespace(async_request=request)))
        with self.assertRaises(asyncio.TimeoutError):
            await genai_client.create_decision(
                client, model="gemini-decision-1", input="架空の発言", questions=self.QUESTIONS, timeout=0.05,
            )
        http_method, path, _body, http_options = request.call_args.args
        self.assertEqual((http_method, path), ("post", "decisions"))
        self.assertEqual(http_options.api_version, "v1beta")
        self.assertEqual(http_options.timeout, 50)
        self.assertEqual(http_options.retry_options.attempts, 1)


if __name__ == "__main__":
    unittest.main()
