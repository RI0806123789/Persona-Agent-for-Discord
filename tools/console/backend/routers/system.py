"""接続先・接続・切断・コマンド履歴・サーバーの状態・ボットの中の状態（insights）のルート（#482）。

共有状態（`session`・ロック・インサイトのキャッシュなど）と、それを書き換える補助は `app.py` にあり、
ここでは `state`（呼び出しのたびに `app.py` から読み直す窓口。`routers/context.py`）を通して使う。
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

import remote_commands as rc
from routers.context import state
from service_jobs import last_line
from ssh_errors import NotConnectedError, SshError

router = APIRouter()


class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str


# ---------------------------------------------------------------- 接続


@router.get("/api/profiles")
def get_profiles() -> dict:
    profiles, source = state.load_profiles()
    return {"profiles": [profile.public_dict() for profile in profiles.values()], "source": source}


@router.get("/api/connection")
def get_connection() -> dict:
    return state._connection_snapshot()


@router.post("/api/connect")
async def connect(body: ConnectRequest) -> dict:
    async with state._connection_change_lock:
        state._reject_connection_change_during_deploy()
        return await state._connect(body)


@router.post("/api/disconnect")
async def disconnect() -> dict:
    async with state._connection_change_lock:
        state._reject_connection_change_during_deploy()
        await state.session.disconnect()
        return state._connection_snapshot()


@router.get("/api/history")
def get_history() -> dict:
    return {"commands": state.session.history_list()}


# ---------------------------------------------------------------- 状態


@router.get("/api/status")
async def get_status() -> dict:
    session = state.session
    profile = session.require_profile()
    generation = session.generation
    ping, service, resources, git = await asyncio.gather(
        session.run(rc.PING_COMMAND, label="疎通確認"),
        session.run(rc.service_show_command(profile.service), label="サービス状態"),
        session.run(rc.RESOURCES_COMMAND, label="リソース"),
        session.run(rc.git_command(profile.bot_dir), label="Git"),
        return_exceptions=True,
    )
    if not state._connection_unchanged(generation):
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
        response["git_error"] = last_line(git.stderr) or "Git の情報を取得できませんでした。"
    else:
        try:
            response["git"] = rc.parse_git(git.stdout)
        except ValueError:
            response["git_error"] = "Git の情報を解析できませんでした。"

    return response


@router.get("/api/insights")
async def get_insights(refresh: bool = False) -> dict:
    """Discord API・サーバーごとの設定・直近24時間のログから集めた「ボットの中の状態」。"""
    async with state._insights_lock:
        session = state.session
        profile = session.require_profile()
        generation = session.generation
        epoch = state._insights_epoch
        key = (generation, profile)
        cached = state._insights_cache.get(key)
        if cached and not refresh and time.monotonic() - cached[0] < state.INSIGHTS_CACHE_SECONDS:
            return {"data": cached[2], "fetched_at": cached[1], "cached": True}

        data = await state._read_json_command(
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
        state._insights_cache.clear()
        if epoch == state._insights_epoch:
            state._insights_cache[key] = (time.monotonic(), fetched_at, data)
        return {"data": data, "fetched_at": fetched_at, "cached": False}
