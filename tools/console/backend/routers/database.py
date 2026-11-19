"""bot.db の集計と、テーブルの中身（読み取り専用）のルート（#482）。

共有状態（`session` など）は `state`（`routers/context.py`）を通して、呼び出しのたびに `app.py` から読み直す。
遠隔スクリプトの結果の読み取り（`_read_json_command`）は、接続の世代の確認を伴うため `app.py` に残している。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

import remote_commands as rc
from local_repo import local_schema_version
from routers.context import state

router = APIRouter()


@router.get("/api/db")
async def get_db_summary() -> dict:
    session = state.session
    profile = session.require_profile()
    generation = session.generation
    summary = await state._read_json_command(
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


@router.get("/api/db/tables/{table}")
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
    session = state.session
    profile = session.require_profile()
    generation = session.generation
    data = await state._read_json_command(
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
