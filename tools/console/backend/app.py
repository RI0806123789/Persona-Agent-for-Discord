"""Persona Bot Console のバックエンド（FastAPI）。

起動:  .venv\\Scripts\\python.exe app.py   →   http://127.0.0.1:8765

役割:
- 画面（React）からの要求を受けて、SSH でボットのサーバーにコマンドを送る
- 送るコマンドは remote_commands.py に書いたものだけ（ホワイトリスト方式）
- frontend/dist（npm run build の成果物）があれば、それも同じポートで配信する

構成（#482）:
- このファイル: アプリの組み立て・ミドルウェア・例外ハンドラ・**共有状態**（接続・デプロイ・ロック・インサイトの
  キャッシュ・操作の記録）と、その状態を読み書きする補助。
- routers/: ルート（エンドポイント）をドメインごとの APIRouter に分けたもの。ルートは共有状態を
  `routers.context.state` 越しに、呼び出しのたびにこのファイルのモジュールから読み直す。
- console_messages.py: 画面へ返す案内文のうち、状態を持たないもの。

★ 共有状態をコンテナへまとめない（#461 の判断）。テストは `patch.object(app, "session", fake)` のように
  このモジュールのグローバルを差し替えるので、ルートへ名前を取り込まず（`from app import session` をせず）、
  `state` を通して読む。ルートから `import app` もしない（`python app.py` ではこのモジュールの名前が `__main__`
  なので、別のコピーが読み込まれて状態が二重になる）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect  # noqa: F401  （テストが app.WebSocketDisconnect で参照する）

import remote_commands as rc
from console_messages import (  # noqa: F401  （従来の名前で再公開する。テストが app._sudo_password_message などで参照する）
    journal_error_message as _journal_error_message,
    sudo_password_message as _sudo_password_message,
    unset_profile_message as _unset_profile_message,
)
from deploy import DeployManager
from managed_server import MANAGED_ENV, run_server
from profiles import Profile, ProfileError, load_profiles
from routers import database as database_routes, logs as logs_routes, operations as operations_routes, system as system_routes
from routers.context import state
from routers.database import get_db_summary, get_db_table  # noqa: F401  （従来の名前で再公開する）
from routers.logs import get_logs, stream_logs  # noqa: F401
from routers.operations import ServiceTarget, control_service, get_deploy, get_operations, start_deploy  # noqa: F401
from routers.system import (  # noqa: F401
    ConnectRequest, connect, disconnect, get_connection, get_history, get_insights, get_profiles, get_status,
)
from service_jobs import wait_for_service_job
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

# ルートが、このモジュールのグローバルを呼び出しのたびに読み直せるようにする。
# `__main__`（python app.py）でも `app`（テスト）でも、自分自身を指す。
state.bind(sys.modules[__name__])


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
    detail = {"detail": str(error)}
    if error.safe_message is not None:
        detail["detail_safe"] = error.safe_message
    return JSONResponse(detail, status_code=409)


@app.exception_handler(ProfileError)
async def handle_profile_error(_request: Request, error: ProfileError) -> JSONResponse:
    return JSONResponse({"detail": f"接続先の設定エラー: {error}"}, status_code=500)


# ---------------------------------------------------------------- 接続の補助


def _connection_snapshot() -> dict:
    """接続先によらない更新操作の停止状態も、接続・切断の応答に含める。"""
    return {**session.snapshot(), "state_unknown": (
        session.command_state_unknown or deploys.state_unknown or _service_completion_pending
    )}


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
        if _connection_unchanged(generation) and result.exit_status == 0:
            session.server_info = rc.parse_server_info(result.stdout)
    except SshError:
        pass  # サーバー情報が取れなくても接続自体は成功扱いにする
    return _connection_snapshot()


def _connection_unchanged(generation: int, *, require_connected: bool = True) -> bool:
    """コマンドを送った時点の接続のまま（世代が同じで、必要なら接続中）か（#461）。

    世代は接続し直し・切断のたびに進む。切断だけで世代が進まない経路を見逃さないため、
    結果を使う API は `require_connected` を既定のまま（接続中の確認つき）にする。
    ログの取得・追跡は、接続の有無ではなく「取り始めた接続のままか」だけを見る。
    """
    return generation == session.generation and (not require_connected or session.connected)


async def _read_json_command(command: str, *, generation: int, changed: str, failed: str,
                             malformed: str, require_connected: bool = True, validate=None, **options) -> dict:
    """接続先の世代を確認し、遠隔スクリプトの最終行を読み取る。"""
    result = await session.run(command, **options)
    if not _connection_unchanged(generation, require_connected=require_connected):
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


# ---------------------------------------------------------------- サービス操作の終了確認


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


# ---------------------------------------------------------------- ボットの中の状態（インサイト）のキャッシュ

# Discord API の利用制限に配慮し、ボットの中の状態は接続先ごとに一定時間使い回す
INSIGHTS_CACHE_SECONDS = 60
_insights_cache: dict[tuple[int, Profile], tuple[float, float, dict]] = {}
_insights_lock = asyncio.Lock()
_insights_epoch = 0


def _invalidate_insights() -> None:
    global _insights_epoch
    _insights_epoch += 1
    _insights_cache.clear()


# ---------------------------------------------------------------- ルートの登録

# 元の並び（接続 → ログ → 操作・デプロイ → DB）に合わせる。パスは重ならないので順序で挙動は変わらないが、
# 静的ファイルの配信（下の mount）は必ず最後に置く。
for _routes in (system_routes, logs_routes, operations_routes, database_routes):
    app.include_router(_routes.router)

# npm run build 済みなら画面も同じポートで配信する（API のルートより後に登録すること）
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")


if __name__ == "__main__":
    print(f"Persona Bot Console のバックエンドを起動します: http://{HOST}:{PORT}")
    run_server(app, host=HOST, port=PORT, managed=os.environ.get(MANAGED_ENV) == "1")
