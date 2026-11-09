"""Persona Bot Console のバックエンド（FastAPI）。

起動:  .venv\\Scripts\\python.exe app.py   →   http://127.0.0.1:8765

役割:
- 画面（React）からの要求を受けて、SSH でボットのサーバーにコマンドを送る
- 送るコマンドは remote_commands.py に書いたものだけ（ホワイトリスト方式）
- frontend/dist（npm run build の成果物）があれば、それも同じポートで配信する
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated
from starlette.websockets import WebSocketDisconnect

import asyncssh
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from service_jobs import last_line as _last_line, wait_for_service_job
import remote_commands as rc
from async_utils import CancellationState, await_shielded
from deploy import DeployManager
from local_repo import local_schema_version
from managed_server import MANAGED_ENV, run_server
from profiles import Profile, ProfileError, load_profiles
from ssh_session import CommandResult, CommandStateUnknownError, NotConnectedError, SshError, SshSession

HOST = "127.0.0.1"
PORT = 8765

# 画面からの要求であることを示すヘッダー。ほかのサイトがブラウザ経由で
# このAPIを勝手に叩く（CSRF）のを防ぐため、全APIでこのヘッダーを必須にする
CLIENT_HEADER = "x-requested-by"
CLIENT_HEADER_VALUE = "persona-bot-console"

# ログのライブ配信（WebSocket）を受け付ける画面のアドレス
ALLOWED_ORIGINS = {f"http://{host}:{port}" for host in ("127.0.0.1", "localhost") for port in (5173, PORT)}

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

ACTION_LABELS = {"start": "起動", "stop": "停止", "restart": "再起動"}
SERVICE_CONTROL_TIMEOUT_SECONDS = 60
SERVICE_JOB_TIMEOUT_SECONDS = 60
SERVICE_JOB_POLL_SECONDS = 0.5


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        try:
            await deploys.wait()
        finally:
            async with _connection_change_lock:
                await session.disconnect()  # 操作の終了を待って接続を閉じる


app = FastAPI(title="Persona Bot Console API", lifespan=lifespan)
# 127.0.0.1 / localhost 以外の Host ヘッダーを拒否する（DNS リバインディング対策）
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

session = SshSession()
deploys = DeployManager()
_connection_change_lock = asyncio.Lock()
operations: deque[dict] = deque(maxlen=100)
_operation_ids = itertools.count(1)
_service_completion_pending = False


@app.middleware("http")
async def require_client_header(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.headers.get(CLIENT_HEADER) != CLIENT_HEADER_VALUE:
        return JSONResponse({"detail": "このAPIはダッシュボードの画面からのみ利用できます。"}, status_code=403)
    return await call_next(request)


@app.exception_handler(NotConnectedError)
async def handle_not_connected(_request: Request, error: NotConnectedError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.exception_handler(SshError)
async def handle_ssh_error(_request: Request, error: SshError) -> JSONResponse:
    detail = {"detail": str(error)}
    if error.safe_message is not None:
        detail["detail_safe"] = error.safe_message
    return JSONResponse(detail, status_code=502)


@app.exception_handler(CommandStateUnknownError)
async def handle_command_state_unknown(_request: Request, error: CommandStateUnknownError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=409)


@app.exception_handler(ProfileError)
async def handle_profile_error(_request: Request, error: ProfileError) -> JSONResponse:
    return JSONResponse({"detail": f"接続先の設定エラー: {error}"}, status_code=500)


# ---------------------------------------------------------------- 接続


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str


@app.get("/api/profiles")
def get_profiles() -> dict:
    profiles, source = load_profiles()
    return {"profiles": [profile.public_dict() for profile in profiles.values()], "source": source}


@app.get("/api/connection")
def get_connection() -> dict:
    return _connection_snapshot()


def _connection_snapshot() -> dict:
    """接続先によらない更新操作の停止状態も、接続・切断の応答に含める。"""
    return {**session.snapshot(), "state_unknown": (
        session.command_state_unknown or deploys.state_unknown or _service_completion_pending
    )}


@app.post("/api/connect")
async def connect(body: ConnectRequest) -> dict:
    async with _connection_change_lock:
        _reject_connection_change_during_deploy()
        return await _connect(body)


def _reject_connection_change_during_deploy() -> None:
    if deploys.active:
        raise HTTPException(status_code=409, detail="デプロイを実行中です。接続先の変更・切断は完了後に行ってください。")


async def _connect(body: ConnectRequest) -> dict:
    profiles, source = load_profiles()
    profile = profiles.get(body.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="指定された接続先が見つかりません。")
    if profile.unset_fields:
        # 見本の仮の値のままでは、名前解決が偶然通った知らないサーバーへつなぎに行きかねないので断る
        raise HTTPException(status_code=400, detail=_unset_profile_message(profile, source))

    await session.connect(profile)
    generation = session.generation
    try:
        result = await session.run(rc.SERVER_INFO_COMMAND, label="サーバー情報")
        if generation == session.generation and session.connected and result.exit_status == 0:
            session.server_info = rc.parse_server_info(result.stdout)
    except SshError:
        pass  # サーバー情報が取れなくても接続自体は成功扱いにする
    return _connection_snapshot()


@app.post("/api/disconnect")
async def disconnect() -> dict:
    async with _connection_change_lock:
        _reject_connection_change_during_deploy()
        await session.disconnect()
        return _connection_snapshot()


@app.get("/api/history")
def get_history() -> dict:
    return {"commands": session.history_list()}


# ---------------------------------------------------------------- 状態


@app.get("/api/status")
async def get_status() -> dict:
    profile = session.require_profile()
    generation = session.generation
    ping, service, resources, git = await asyncio.gather(
        session.run(rc.PING_COMMAND, label="疎通確認"),
        session.run(rc.service_show_command(profile.service), label="サービス状態"),
        session.run(rc.RESOURCES_COMMAND, label="リソース"),
        session.run(rc.git_command(profile.bot_dir), label="Git"),
        return_exceptions=True,
    )
    if generation != session.generation or not session.connected:
        raise NotConnectedError("状態の取得中に SSH 接続が変わりました。接続先を確認して更新してください。")
    if isinstance(ping, BaseException):
        raise ping  # 疎通確認すら失敗 = 接続が使えない
    if ping.exit_status != 0:
        raise SshError("SSH の疎通確認に失敗しました。接続先の状態を確認してください。")

    response: dict = {
        "rtt_ms": ping.duration_ms,
        "fetched_at": time.time(),
        "service": None,
        "service_error": None,
        "resources": None,
        "resources_error": None,
        "git": None,
        "git_error": None,
    }

    host_uptime = None
    if isinstance(resources, BaseException):
        response["resources_error"] = str(resources)
    elif resources.exit_status != 0:
        response["resources_error"] = "リソースを取得できませんでした。サーバーのコマンドと権限を確認してください。"
    else:
        response["resources"] = rc.parse_resources(resources.stdout)
        host_uptime = response["resources"]["uptime_seconds"]

    if isinstance(service, BaseException):
        response["service_error"] = str(service)
    else:
        parsed = rc.parse_service_show(service.stdout, profile.service, host_uptime)
        if parsed["load_state"] == "not-found":
            response["service_error"] = f"サービス {profile.service} がサーバーに見つかりません（systemd に未登録）。"
        elif service.exit_status != 0:
            response["service_error"] = "サービス状態を取得できませんでした。systemd と読み取り権限を確認してください。"
        response["service"] = parsed

    if isinstance(git, BaseException):
        response["git_error"] = str(git)
    elif git.exit_status != 0:
        response["git_error"] = _last_line(git.stderr) or "Git の情報を取得できませんでした。"
    else:
        try:
            response["git"] = rc.parse_git(git.stdout)
        except ValueError:
            response["git_error"] = "Git の情報を解析できませんでした。"

    return response


@app.get("/api/logs")
async def get_logs(lines: Annotated[int, Query(ge=1, le=rc.MAX_LOG_LINES)] = 200) -> dict:
    profile = session.require_profile()
    generation = session.generation
    command = rc.journal_command(profile.service, lines=lines, follow=False, use_sudo=profile.journal_sudo)
    result = await session.run(command, label="ログ取得")
    if generation != session.generation:
        raise NotConnectedError("ログの取得中に SSH 接続が変わりました。接続先を確認して更新してください。")
    entries = [entry for line in result.stdout.splitlines() if (entry := rc.parse_journal_line(line))]
    if result.exit_status != 0 and not entries:
        raise SshError(_journal_error_message(result.stderr))
    return {"entries": entries}


@app.websocket("/api/logs/stream")
async def stream_logs(websocket: WebSocket, lines: Annotated[int, Query(ge=1, le=rc.MAX_LOG_LINES)] = 100) -> None:
    """journalctl -f の出力を、1件ずつ WebSocket で画面へ流す。"""
    origin = websocket.headers.get("origin")
    if origin not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)  # ほかのサイトからの接続は断る
        return
    await websocket.accept()

    try:
        profile = session.require_profile()
        generation = session.generation
        command = rc.journal_command(profile.service, lines=lines, follow=True, use_sudo=profile.journal_sudo)
        process = await session.open_process(command, label="ログ追跡（ライブ）")
        if generation != session.generation:
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
                if generation != session.generation:
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
        message = _journal_error_message(stderr) if stderr.strip() else "ログの追跡が終了しました。"
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
        state = CancellationState()
        await await_shielded(cleanup_task, state)
        if state.canceled:
            raise asyncio.CancelledError



# ---------------------------------------------------------------- 操作


class ServiceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str
    connected_at: float


@app.post("/api/service/{action}")
async def control_service(action: str, body: ServiceTarget | None = None) -> dict:
    if action not in rc.SERVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="許可されていない操作です。")
    async with _connection_change_lock:
        # 送信済みの操作は呼出元がキャンセルされても完了までロックを保つ。
        # ロック待ちの間に変わった接続先とデプロイ状態は、実行直前に取り直す。
        task = asyncio.create_task(_control_service(action, body))
        state = CancellationState()
        try:
            operation = await await_shielded(task, state)
        except Exception as error:
            if state.canceled:
                raise asyncio.CancelledError from error
            raise
        if state.canceled:
            raise asyncio.CancelledError
        return operation


async def _control_service(action: str, body: ServiceTarget | None) -> dict:
    session.require_command_completion()
    if deploys.state_unknown:
        raise HTTPException(status_code=409, detail="デプロイの状態不明のため、サービス操作を停止しています。サーバーの状態を確認してください。")
    if deploys.running:
        raise HTTPException(status_code=409, detail="デプロイを実行中です。終わってから操作してください。")
    profile = session.require_profile()
    if body is None or body.profile_id != profile.id or body.connected_at != session.connected_at:
        raise HTTPException(status_code=409, detail="確認後に接続状態が変わりました。接続先を確認して操作をやり直してください。")
    label = ACTION_LABELS[action]
    result = await _run_service_control(profile.service, action, label)

    ok = result.exit_status == 0
    if ok:
        message = f"{profile.service} を{label}しました。"
    elif "password is required" in result.stderr or "a terminal is required" in result.stderr:
        message = _sudo_password_message(profile.username, profile.service)
    else:
        message = f"{label}に失敗しました: {_last_line(result.stderr) or f'終了コード {result.exit_status}'}"

    operation = {
        "id": next(_operation_ids),
        "at": time.time(),
        "profile_id": profile.id,
        "environment": profile.environment,
        "action": action,
        "ok": ok,
        "message": message,
        "duration_ms": result.duration_ms,
    }
    operations.appendleft(operation)
    return operation


def _mark_service_completion_pending() -> None:
    global _service_completion_pending
    _service_completion_pending = True


async def _run_service_control(service: str, action: str, label: str) -> CommandResult:
    """SSHの終了とsystemdジョブの終了を両方確認するまで、共通ロック内で待つ。"""
    global _service_completion_pending
    # 送信前の名前・操作の検証失敗は、遠隔での実行が不明な状態と区別する。
    command = rc.service_control_command(service, action)
    try:
        try:
            result = await session.run(
                command, label=f"サービス{label}",
                timeout=SERVICE_CONTROL_TIMEOUT_SECONDS, wait_for_exit=True,
                on_waiting=_mark_service_completion_pending,
            )
        except (SshError, asyncio.CancelledError):
            # 通信断・終了通知なしでは追加コマンドを送らない。既知の終了後の
            # 期限超過・中断は、失敗を返す前にもsystemd側の残留ジョブを確かめる。
            if not session.command_state_unknown:
                await _wait_for_service_job(service)
                _invalidate_insights()
            raise
        except Exception as error:
            session.command_state_unknown = True
            raise CommandStateUnknownError(
                "サービス操作中に予期しないエラーが起き、遠隔操作の終了を確認できません。"
                "サーバーで処理の終了とサービスの状態を確認してからバックエンドを起動し直してください。"
            ) from error
        await _wait_for_service_job(service)
        _invalidate_insights()
        return result
    finally:
        _service_completion_pending = False


async def _wait_for_service_job(service: str) -> None:
    """systemctl自体の終了だけでは残り得る、当該unitのジョブを期限付きで確認する。"""
    _mark_service_completion_pending()
    await wait_for_service_job(
        session, service, timeout=SERVICE_JOB_TIMEOUT_SECONDS, poll=SERVICE_JOB_POLL_SECONDS,
        on_wait=_mark_service_completion_pending,
        unknown_message=("サービス操作のsystemdジョブが終了したことを確認できず、状態不明です。"
                         "サーバーで処理の終了とサービスの状態を確認してからバックエンドを起動し直してください。"),
    )


@app.get("/api/operations")
def get_operations() -> dict:
    return {"operations": list(operations)}


# ---------------------------------------------------------------- デプロイ


def _deploy_snapshot() -> dict:
    return {"deploy": deploys.current.to_dict() if deploys.current else None, "running": deploys.running}


@app.get("/api/deploy")
def get_deploy() -> dict:
    """最後のデプロイの記録（実行中なら途中の状態）。画面はこれを取り直してステップごとの進み具合を出す。"""
    return _deploy_snapshot()


@app.post("/api/deploy")
async def start_deploy(body: ServiceTarget | None = None) -> dict:
    """接続中のサーバーへデプロイを始める（git pull → 依存の更新 → 再起動 → ヘルスチェック）。

    画面が確認した接続先と接続時刻を照合する。実行するブランチ等は接続先の設定から決める。
    """
    async with _connection_change_lock:
        profile = session.require_profile()
        if body is None or body.profile_id != profile.id or body.connected_at != session.connected_at:
            raise HTTPException(status_code=409, detail="確認後に接続状態が変わりました。接続先を確認してデプロイをやり直してください。")
        if profile.unset_fields:
            raise HTTPException(status_code=400, detail=_unset_profile_message(profile, load_profiles()[1]))
        if deploys.state_unknown:
            raise HTTPException(status_code=409, detail="デプロイの状態不明のため、更新操作を停止しています。サーバーの状態を確認してください。")
        if deploys.running:
            raise HTTPException(status_code=409, detail="デプロイを実行中です。終わるまで待ってください。")
        _invalidate_insights()
        deploys.start(session, profile, on_finished=_invalidate_insights)
        return _deploy_snapshot()


# Discord API の利用制限に配慮し、ボットの中の状態は接続先ごとに一定時間使い回す
INSIGHTS_CACHE_SECONDS = 60
_insights_cache: dict[tuple[int, Profile], tuple[float, float, dict]] = {}
_insights_lock = asyncio.Lock()
_insights_epoch = 0


def _invalidate_insights() -> None:
    global _insights_epoch
    _insights_epoch += 1
    _insights_cache.clear()


async def _read_json_command(command: str, *, generation: int, changed: str, failed: str,
                             malformed: str, require_connected: bool = True, validate=None, **options) -> dict:
    """接続先の世代を確認し、遠隔スクリプトの最終行を読み取る。"""
    result = await session.run(command, **options)
    if generation != session.generation or (require_connected and not session.connected):
        raise NotConnectedError(changed)
    if result.exit_status != 0:
        raise SshError(failed)
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        if not isinstance(data, dict) or (validate is not None and not validate(data)):
            raise ValueError("結果が想定の形ではありません。")
        return data
    except (ValueError, IndexError) as error:
        raise SshError(malformed) from error


@app.get("/api/insights")
async def get_insights(refresh: bool = False) -> dict:
    """Discord API・サーバーごとの設定・直近24時間のログから集めた「ボットの中の状態」。"""
    async with _insights_lock:
        profile = session.require_profile()
        generation = session.generation
        epoch = _insights_epoch
        key = (generation, profile)
        cached = _insights_cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < INSIGHTS_CACHE_SECONDS:
            return {"data": cached[2], "fetched_at": cached[1], "cached": True}

        data = await _read_json_command(
            rc.insights_command(profile.bot_dir, profile.service, use_sudo=profile.journal_sudo),
            generation=generation, require_connected=True,
            changed="集計中に SSH 接続が変わりました。接続先を確認して更新してください。",
            failed="ボットの状態を集められませんでした。サーバーの Python とログの読み取り権限を確認してください。",
            malformed="ボットの状態の集計結果を読み取れませんでした。",
            label="ボットの中の状態（Discord API・DB・ログ）",
            input=rc.INSIGHTS_SCRIPT,
            timeout=60,
        )
        fetched_at = time.time()
        _insights_cache.clear()
        if epoch == _insights_epoch:
            _insights_cache[key] = (time.monotonic(), fetched_at, data)
        return {"data": data, "fetched_at": fetched_at, "cached": False}


@app.get("/api/db")
async def get_db_summary() -> dict:
    profile = session.require_profile()
    generation = session.generation
    summary = await _read_json_command(
        rc.db_summary_command(profile.bot_dir),
        generation=generation,
        changed="集計中に SSH 接続が変わりました。接続先を確認して更新してください。",
        failed="DB の集計に失敗しました。サーバーの Python と読み取り権限を確認してください。",
        malformed="DB の集計結果を読み取れませんでした。",
        label="DB 集計（読み取り専用）",
        input=rc.DB_SUMMARY_SCRIPT,
    )
    # 手元のリポジトリの最新の版と比べ、新しい版がまだデプロイされていないかを画面に出す
    summary["latest_schema_version"] = local_schema_version()
    return summary


@app.get("/api/db/tables/{table}")
async def get_db_table(
    table: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=rc.DB_TABLE_PAGE_MAX)] = 50,
) -> dict:
    """bot.db の1つのテーブルの行（会話・記憶の本文と ID を含む）を、読み取り専用で取り寄せる。

    範囲外の offset・limit は 422 にする（ログの行数と同じく、API の仕様として拒否する。#399）。
    `db_table_command` の丸めは、直接呼ばれた場合の最後の砦として残す。
    """
    if table not in rc.DB_TABLES:
        raise HTTPException(status_code=404, detail="このテーブルは表示できません。")
    profile = session.require_profile()
    generation = session.generation
    data = await _read_json_command(
        rc.db_table_command(profile.bot_dir, table, offset, limit),
        generation=generation,
        changed="読み取り中に SSH 接続が変わりました。接続先を確認して開き直してください。",
        failed="テーブルの中身を取り寄せられませんでした。サーバーの Python と読み取り権限を確認してください。",
        malformed="テーブルの中身を読み取れませんでした。",
        validate=lambda value: value.get("table") == table,
        label=f"DB の中身（{table}・読み取り専用）",
        input=rc.DB_TABLE_SCRIPT,
    )
    return data


# ---------------------------------------------------------------- 補助


def _unset_profile_message(profile: Profile, source: str) -> str:
    """見本の値のままの接続先へ接続しようとしたときの案内。"""
    fields = "・".join(profile.unset_fields)
    if source == "profiles.example.json":
        how = "backend/profiles.example.json を backend/profiles.json にコピーし、YOUR_ で始まる値を書き換えてください"
    else:
        how = f"backend/{source} の YOUR_ で始まる値を書き換えてください"
    return f"接続先「{profile.label}」は見本の値のままです（{fields}）。{how}（書き換えたら接続し直すだけで反映されます）。"


def _sudo_password_message(username: str | None, service: str) -> str:
    """sudo にパスワードが要る環境向けの案内。許可するのは起動・停止・再起動の3つだけにする例を示す。"""
    user = username or "<ユーザー名>"
    commands = ", ".join(f"/usr/bin/systemctl {action} {service}" for action in rc.SERVICE_ACTIONS)
    return (
        "sudo にパスワードが必要なため実行できませんでした。サーバーで sudo visudo -f /etc/sudoers.d/persona-bot-console を開き、"
        f"「{user} ALL=(root) NOPASSWD: {commands}」の1行を書くと、この3つの操作だけをパスワードなしで許可できます"
        "（systemctl の場所は which systemctl で確かめてください。見本: tools/console/examples/sudoers.example）。"
    )


def _journal_error_message(stderr: str) -> str:
    detail = _last_line(stderr)
    if "permission" in stderr.lower() or "insufficient" in stderr.lower():
        return (
            "ログを読む権限がありません。サーバーのユーザーを adm / systemd-journal グループに入れるか、"
            "profiles.json の journal_sudo を true にしてください。"
        )
    return f"ログを取得できませんでした: {detail or '原因不明'}"


# npm run build 済みなら画面も同じポートで配信する（API のルートより後に登録すること）
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    print(f"Persona Bot Console のバックエンドを起動します: http://{HOST}:{PORT}")
    run_server(app, host=HOST, port=PORT, managed=os.environ.get(MANAGED_ENV) == "1")
