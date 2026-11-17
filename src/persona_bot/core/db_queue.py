"""文書処理キュー（queue_tasks）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

from typing import Any

from persona_bot.core.db_base import DbMixinBase
from persona_bot.core.db_rows import (
    _insert_sql,
    _QUEUE_COLUMNS,
    _QUEUE_ACTIVE_STATUSES,
    _queue_row_params,
)


class QueueDbMixin(DbMixinBase):
    """文書処理キュー（queue_tasks）。"""

    def queue_upsert_task(self, task: dict[str, Any]) -> None:
        """タスクを追加する（同じ task_id があれば丸ごと置き換える）。"""
        with self._lock, self._conn:
            self._insert_queue_task(task)

    def queue_try_enqueue(self, task: dict[str, Any], max_per_user: int, max_total: int) -> str:
        """未処理件数の上限内なら追加する。結果は "accepted" / "user_limit" / "total_limit"。

        件数の確認と追加を同じロック・トランザクションで行い、並行した受付が
        確認と追加の間へ割り込んで上限を超えることを防ぐ。上限に数えるのは
        終わっていないタスク（_QUEUE_ACTIVE_STATUSES）だけなので、完了・失敗へ
        進めば枠が空く。
        """
        placeholders = ", ".join("?" for _ in _QUEUE_ACTIVE_STATUSES)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(author_id = ?), 0) AS mine"
                f" FROM queue_tasks WHERE status IN ({placeholders})",
                (int(task.get("author_id") or 0), *_QUEUE_ACTIVE_STATUSES),
            ).fetchone()
            if row["mine"] >= max_per_user:
                return "user_limit"
            if row["total"] >= max_total:
                return "total_limit"
            self._insert_queue_task(task)
            return "accepted"

    def _insert_queue_task(self, task: dict[str, Any]) -> None:
        """タスクを1行書く。呼び出し側がロックとトランザクションを持つこと。"""
        self._conn.execute(
            _insert_sql("queue_tasks", _QUEUE_COLUMNS, verb="INSERT OR REPLACE"), _queue_row_params(task),
        )

    def queue_update_task(self, task_id: str, updates: dict[str, Any], updated_at: str) -> None:
        """指定タスクの列を部分更新する（updated_at は常に更新される）。"""
        # 列名は SQL 文へ直接埋め込むため、既知の列だけを許可する（インジェクション対策）
        safe_updates = {
            key: value
            for key, value in updates.items()
            if key in _QUEUE_COLUMNS and key != "task_id"
        }
        safe_updates["updated_at"] = updated_at
        set_clause = ", ".join(f"{key} = ?" for key in safe_updates)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE queue_tasks SET {set_clause} WHERE task_id = ?",
                (*safe_updates.values(), str(task_id)),
            )

    def queue_claim_next_pending(self, now_iso: str) -> dict[str, Any] | None:
        """最も古い pending タスクを processing に変えて返す。無ければ None。

        SELECT と UPDATE を同一トランザクション内で行うため、
        同じタスクが二重に取得されることはない。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM queue_tasks WHERE status = 'pending'"
                " ORDER BY created_at, task_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE queue_tasks SET status = 'processing', updated_at = ? WHERE task_id = ?",
                (now_iso, row["task_id"]),
            )
            task = dict(row)
            task["status"] = "processing"
            task["updated_at"] = now_iso
            return task

    def queue_recover_stuck(self, now_iso: str) -> int:
        """処理中は再開し、送信中断で配送結果が不明なタスクは再送せず失敗として残す。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE queue_tasks SET status = 'failed', updated_at = ?, finished_at = ?, error = ?"
                " WHERE status = 'sending'",
                (now_iso, now_iso, "返信の送信中に中断されました。重複を防ぐため自動再送しません。"),
            )
            cursor = self._conn.execute(
                "UPDATE queue_tasks SET status = 'pending', updated_at = ?"
                " WHERE status = 'processing'",
                (now_iso,),
            )
            return cursor.rowcount

    def queue_prune_finished(self, keep_count: int) -> None:
        """完了/失敗タスクを新しい順に keep_count 件だけ残し、それ以前を削除する。"""
        self._prune_keep_newest(
            """DELETE FROM queue_tasks
               WHERE status IN ('completed', 'failed')
                 AND task_id NOT IN (
                     SELECT task_id FROM queue_tasks
                     WHERE status IN ('completed', 'failed')
                     ORDER BY created_at DESC, task_id DESC
                     LIMIT ?
                 )""",
            keep_count,
        )
