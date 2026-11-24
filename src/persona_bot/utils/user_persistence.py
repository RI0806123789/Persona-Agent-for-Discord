"""ユーザー単位の世代合流付き直列化保存ヘルパー（#495）。

`AffinityService` と `StatsService` で共通する「ユーザーごとの更新世代を進め、共有ロックの下で
最新のスナップショットだけを別スレッドで保存する」制御を集約する。

- 先行する保存の待機中に同じユーザーの背景保存が複数回呼ばれても、ロック取得後に最新の版だけを1回書く。
- `run_in_thread` で待つため、呼び出し側がキャンセルされても同期ワーカーの終了までロックを離さない。
- `on_cancel` を渡した呼び出し元（`StatsService`）では、ロックを離す前に中断後の補償保存を行う。
- 保存に失敗した場合は `_saved_versions` を進めず、次の保存要求で再試行できるようにする。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
import logging
from typing import Any

from persona_bot.core.background import run_in_thread, wait_for_cleanup


async def write_user_snapshot_locked(
    user_id: str,
    snapshot: dict[str, Any],
    *,
    write_fn: Callable[[str, dict[str, Any]], Any],
    logger: logging.Logger,
    error_message: str,
    previous: dict[str, Any] | None = None,
    on_cancel: Callable[[str, dict[str, Any] | None], Awaitable[None]] | None = None,
    cancel_label: str = "ユーザーデータの中断後の復元",
) -> bool:
    """ロック保持中に1ユーザー分のスナップショットを保存し、成否を返す。"""
    try:
        await run_in_thread(write_fn, user_id, snapshot)
    except asyncio.CancelledError:
        if on_cancel is not None:
            await wait_for_cleanup(on_cancel(user_id, previous), label=cancel_label)
        raise
    except Exception as error:
        logger.error(error_message, exc_info=error)
        return False
    return True


async def save_user_background(
    user_id: str,
    *,
    lock: asyncio.Lock,
    save_versions: dict[str, int],
    saved_versions: dict[str, int],
    cache: Mapping[str, dict[str, Any]],
    write_fn: Callable[[str, dict[str, Any]], Any],
    logger: logging.Logger,
    error_message: str,
    on_cancel: Callable[[str, dict[str, Any] | None], Awaitable[None]] | None = None,
    cancel_label: str = "ユーザーデータの中断後の復元",
) -> bool:
    """更新世代を進めてロックを取り、未反映の最新スナップショットを保存する。"""
    save_versions[user_id] = save_versions.get(user_id, 0) + 1
    requested_version = save_versions[user_id]
    async with lock:
        if saved_versions.get(user_id, 0) >= requested_version:
            return True
        latest_version = save_versions.get(user_id, requested_version)
        current = cache.get(user_id)
        if current is None:
            saved_versions[user_id] = latest_version
            return True
        previous = deepcopy(current)
        saved = await write_user_snapshot_locked(
            user_id,
            deepcopy(previous),
            write_fn=write_fn,
            logger=logger,
            error_message=error_message,
            previous=previous,
            on_cancel=on_cancel,
            cancel_label=cancel_label,
        )
        if saved:
            saved_versions[user_id] = latest_version
        return saved
