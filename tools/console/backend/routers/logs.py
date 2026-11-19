"""ログの取得と、ライブ配信（WebSocket）のルート（#482）。

共有状態（`session` など）は `state`（`routers/context.py`）を通して、呼び出しのたびに `app.py` から読み直す。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Annotated

import asyncssh
from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

import remote_commands as rc
from async_utils import CancellationState, await_shielded
from console_messages import journal_error_message
from routers.context import state
from ssh_errors import NotConnectedError, SshError

router = APIRouter()


@router.get("/api/logs")
async def get_logs(lines: Annotated[int, Query(ge=1, le=rc.MAX_LOG_LINES)] = 200) -> dict:
    session = state.session
    profile = session.require_profile()
    generation = session.generation
    command = rc.journal_command(profile.service, lines=lines, follow=False, use_sudo=profile.journal_sudo)
    result = await session.run(command, label="ログ取得")
    if not state._connection_unchanged(generation, require_connected=False):
        raise NotConnectedError("ログの取得中に SSH 接続が変わりました。接続先を確認して更新してください。")
    entries = [entry for line in result.stdout.splitlines() if (entry := rc.parse_journal_line(line))]
    if result.exit_status != 0 and not entries:
        raise SshError(journal_error_message(result.stderr))
    return {"entries": entries}


@router.websocket("/api/logs/stream")
async def stream_logs(websocket: WebSocket, lines: Annotated[int, Query(ge=1, le=rc.MAX_LOG_LINES)] = 100) -> None:
    """journalctl -f の出力を、1件ずつ WebSocket で画面へ流す。"""
    origin = websocket.headers.get("origin")
    if origin not in state.ALLOWED_ORIGINS:
        await websocket.close(code=1008)  # ほかのサイトからの接続は断る
        return
    await websocket.accept()

    session = state.session
    try:
        profile = session.require_profile()
        generation = session.generation
        command = rc.journal_command(profile.service, lines=lines, follow=True, use_sudo=profile.journal_sudo)
        process = await session.open_process(command, label="ログ追跡（ライブ）")
        if not state._connection_unchanged(generation, require_connected=False):
            process.close()
            with suppress(asyncio.TimeoutError, asyncssh.Error, OSError):
                await asyncio.wait_for(process.wait_closed(), timeout=5)
            raise NotConnectedError("ログの取得中に SSH 接続が変わりました。接続先を確認して更新してください。")
    except SshError as error:
        with suppress(WebSocketDisconnect, RuntimeError):
            # 接続処理の生エラーには、画面が未収集の解決済みホスト名なども入りうる。
            safe_message = error.safe_message or (
                "ログの接続を開始できませんでした。SSHの接続設定と接続状態を確認してから再試行してください。"
            )
            await websocket.send_json({"type": "error", "message": str(error), "message_safe": safe_message})
            await websocket.close()
        return

    async def pump() -> None:
        """SSH → 画面 へ流し続ける。"""
        try:
            async for line in process.stdout:
                if not state._connection_unchanged(generation, require_connected=False):
                    return
                entry = rc.parse_journal_line(line)
                if entry:
                    await websocket.send_json({"type": "entry", "entry": entry})
        except WebSocketDisconnect:
            return
        except (asyncssh.Error, OSError):
            pass
        stderr = ""
        try:
            stderr = await asyncio.wait_for(process.stderr.read(), timeout=1)
        except (asyncio.TimeoutError, asyncssh.Error, OSError):
            pass
        message = journal_error_message(stderr) if stderr.strip() else "ログの追跡が終了しました。"
        with suppress(WebSocketDisconnect, RuntimeError):
            await websocket.send_json({"type": "end", "message": message})

    async def wait_for_client_close() -> None:
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            if message["type"] == "websocket.disconnect":
                return

    pump_task = asyncio.create_task(pump())
    client_task = asyncio.create_task(wait_for_client_close())
    try:
        await asyncio.wait({pump_task, client_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        async def cleanup() -> None:
            # 転送の終了待ちが再キャンセルされても、SSH側のjournalctlを残さない。
            process.close()
            for task in (pump_task, client_task):
                task.cancel()
            await asyncio.gather(pump_task, client_task, return_exceptions=True)
            try:
                await asyncio.wait_for(process.wait_closed(), timeout=5)
            except (asyncio.TimeoutError, asyncssh.Error, OSError):
                pass
            with suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close()

        cleanup_task = asyncio.create_task(cleanup())
        cancellation = CancellationState()
        await await_shielded(cleanup_task, cancellation)
        if cancellation.canceled:
            raise asyncio.CancelledError
