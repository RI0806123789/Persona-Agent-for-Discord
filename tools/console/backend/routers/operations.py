"""サービス操作・操作の記録・デプロイのルート（#482）。

共有状態（`session`・`deploys`・共通ロック・操作の記録など）と、それを書き換える補助は `app.py` にあり、
ここでは `state`（呼び出しのたびに `app.py` から読み直す窓口。`routers/context.py`）を通して使う。
サービス操作の終了確認（`_run_service_control`）は、完了待ちのフラグを書き換えるため `app.py` に残している。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

import remote_commands as rc
from async_utils import CancellationState, await_shielded
from console_messages import sudo_password_message, unset_profile_message
from routers.context import state
from service_jobs import last_line

router = APIRouter()


class ServiceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str
    connected_at: float


# ---------------------------------------------------------------- 操作


@router.post("/api/service/{action}")
async def control_service(action: str, body: ServiceTarget | None = None) -> dict:
    if action not in rc.SERVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="許可されていない操作です。")
    async with state._connection_change_lock:
        # 送信済みの操作は呼出元がキャンセルされても完了までロックを保つ。
        # ロック待ちの間に変わった接続先とデプロイ状態は、実行直前に取り直す。
        task = asyncio.create_task(_control_service(action, body))
        cancellation = CancellationState()
        try:
            operation = await await_shielded(task, cancellation)
        except Exception as error:
            if cancellation.canceled:
                raise asyncio.CancelledError from error
            raise
        if cancellation.canceled:
            raise asyncio.CancelledError
        return operation


async def _control_service(action: str, body: ServiceTarget | None) -> dict:
    session, deploys = state.session, state.deploys
    session.require_command_completion()
    if deploys.state_unknown:
        raise HTTPException(status_code=409, detail="デプロイの状態不明のため、サービス操作を停止しています。サーバーの状態を確認してください。")
    if deploys.running:
        raise HTTPException(status_code=409, detail="デプロイを実行中です。終わってから操作してください。")
    profile = session.require_profile()
    if body is None or body.profile_id != profile.id or body.connected_at != session.connected_at:
        raise HTTPException(status_code=409, detail="確認後に接続状態が変わりました。接続先を確認して操作をやり直してください。")
    label = state.ACTION_LABELS[action]
    result = await state._run_service_control(profile.service, action, label)

    ok = result.exit_status == 0
    if ok:
        message = f"{profile.service} を{label}しました。"
    elif rc.needs_sudo_password(result.stderr):
        message = sudo_password_message(profile.username, profile.service)
    else:
        message = f"{label}に失敗しました: {last_line(result.stderr) or f'終了コード {result.exit_status}'}"

    operation = {
        "id": next(state._operation_ids),
        "at": time.time(),
        "profile_id": profile.id,
        "environment": profile.environment,
        "action": action,
        "ok": ok,
        "message": message,
        "duration_ms": result.duration_ms,
    }
    state.operations.appendleft(operation)
    return operation


@router.get("/api/operations")
def get_operations() -> dict:
    return {"operations": list(state.operations)}


# ---------------------------------------------------------------- デプロイ


def _deploy_snapshot() -> dict:
    deploys = state.deploys
    return {"deploy": deploys.current.to_dict() if deploys.current else None, "running": deploys.running}


@router.get("/api/deploy")
def get_deploy() -> dict:
    """最後のデプロイの記録（実行中なら途中の状態）。画面はこれを取り直してステップごとの進み具合を出す。"""
    return _deploy_snapshot()


@router.post("/api/deploy")
async def start_deploy(body: ServiceTarget | None = None) -> dict:
    """接続中のサーバーへデプロイを始める（git pull → 依存の更新 → 再起動 → ヘルスチェック）。

    画面が確認した接続先と接続時刻を照合する。実行するブランチ等は接続先の設定から決める。
    """
    async with state._connection_change_lock:
        session, deploys = state.session, state.deploys
        profile = session.require_profile()
        if body is None or body.profile_id != profile.id or body.connected_at != session.connected_at:
            raise HTTPException(status_code=409, detail="確認後に接続状態が変わりました。接続先を確認してデプロイをやり直してください。")
        if profile.unset_fields:
            raise HTTPException(status_code=400, detail=unset_profile_message(profile, state.load_profiles()[1]))
        if deploys.state_unknown:
            raise HTTPException(status_code=409, detail="デプロイの状態不明のため、更新操作を停止しています。サーバーの状態を確認してください。")
        if deploys.running:
            raise HTTPException(status_code=409, detail="デプロイを実行中です。終わるまで待ってください。")
        state._invalidate_insights()
        deploys.start(session, profile, on_finished=state._invalidate_insights)
        return _deploy_snapshot()
