"""Gemini Developer API のクライアントと非同期生成の共通境界。"""

import asyncio
import re
from math import ceil
from typing import Any
from weakref import WeakKeyDictionary

from google import genai
from google.genai import types
import httpx

from persona_bot.core.background import spawn


# 利用枠の超過を示す HTTP ステータス。
QUOTA_EXHAUSTED_STATUS = 429


def error_status_code(error: BaseException, *, max_code: int = 599) -> int | None:
    """SDK と HTTP クライアントの例外からステータスを読み取る（#434）。

    `code`・`status_code`・`response.status_code` を順に見て、最初に 100〜`max_code` の整数
    （3桁の数字文字列を含む。bool は除く）として読めたものを返す。Live の WebSocket の終了コード
    （1007・1011 など）も扱う呼び出し側は `max_code` を広げる。
    """
    for code in (
        getattr(error, "code", None), getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(code, str) and re.fullmatch(r"[0-9]{3}", code.strip()):
            code = int(code.strip())
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= max_code:
            return code
    return None


def is_quota_exhausted(error: BaseException, status_code: int | None = None) -> bool:
    """利用枠の超過（429 / RESOURCE_EXHAUSTED）か。

    型付きのステータスがあればそれだけで決める（認証・サーバー障害のコードを文面で誤って
    利用枠と読まない）。無い旧形式の例外だけ、文面の `RESOURCE_EXHAUSTED` と 429 で補う。
    """
    if status_code is None:
        status_code = error_status_code(error)
    if status_code is not None:
        return status_code == QUOTA_EXHAUSTED_STATUS
    message = " ".join(str(part) for part in (
        getattr(error, "status", ""), getattr(error, "code", ""), error,
    )).lower()
    return (
        "resource_exhausted" in message or "resource exhausted" in message
        or re.search(r"(?<!\d)429(?!\d)", message) is not None
    )


class _UploadStatusTransport(httpx.AsyncBaseTransport):
    """不完全な転送応答を明示的に失敗させ、SDK内の同期sleepを避ける。"""

    def __init__(self, transport: httpx.AsyncBaseTransport) -> None:
        self._transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._transport.handle_async_request(request)
        commands = request.headers.get("x-goog-upload-command", "").split(",")
        if "upload" in [command.strip() for command in commands] and not response.headers.get("x-goog-upload-status"):
            await response.aclose()
            raise httpx.RemoteProtocolError("Geminiファイル転送の状態ヘッダーを取得できませんでした。", request=request)
        return response

    async def aclose(self) -> None:
        await self._transport.aclose()


_owned_http_clients: WeakKeyDictionary[genai.Client, httpx.AsyncClient] = WeakKeyDictionary()


def create_genai_client(api_key: str) -> genai.Client:
    """Developer APIとHTTPXを明示し、生成・転送の暗黙リトライを防ぐ。"""
    transport = _UploadStatusTransport(httpx.AsyncHTTPTransport(retries=0))
    http_client = httpx.AsyncClient(transport=transport)
    try:
        # vertexaiは新旧SDKで共通。新SDKだけの別名enterpriseを使わずDeveloper APIへ固定する。
        client = genai.Client(
            api_key=api_key,
            vertexai=False,
            http_options=types.HttpOptions(
                httpx_async_client=http_client,
                # 旧SDKもHTTPXを選ぶよう、同じtransportを設定に明示する。
                async_client_args={"transport": transport},
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    except BaseException:
        # 初期化時には未通信だが、作成済みのトランスポートも回収する。
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(http_client.aclose())
        else:
            spawn(http_client.aclose(), label="Geminiクライアント初期化失敗時の回収")
        raise
    _owned_http_clients[client] = http_client
    return client


async def close_genai_client(client: Any) -> None:
    """SDKが所有しないHTTPXも、SDK終了が失敗した場合を含め一度だけ回収する。"""
    http_client = _owned_http_clients.pop(client, None)
    try:
        try:
            await client.aio.aclose()
        finally:
            client.close()
    finally:
        if http_client is not None:
            await http_client.aclose()


async def generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    timeout: float | None = None,
    **options: Any,
) -> Any:
    """残り秒数をSDKのミリ秒へ変換し、通信全体にも同じ期限を適用する。"""
    if timeout is not None and timeout <= 0:
        raise asyncio.TimeoutError()
    config = types.GenerateContentConfig(
        http_options=types.HttpOptions(
            timeout=ceil(timeout * 1000) if timeout is not None else None,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
        **options,
    )
    request = client.aio.models.generate_content(model=model, contents=contents, config=config)
    return await asyncio.wait_for(request, timeout=timeout) if timeout is not None else await request


async def generate_json_content(sender, client: Any, **options: Any) -> Any:
    """JSON 生成設定を共通化し、呼出元の期限とスキーマはそのまま渡す。"""
    return await sender(client, temperature=0, response_mime_type="application/json", **options)
