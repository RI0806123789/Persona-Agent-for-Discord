"""ユーザーごとの親密度・感情（affinity）と会話統計（user_stats）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

from typing import Any

from persona_bot.config.constants import (
    STATS_HOURLY_BUCKETS,
)
from persona_bot.core.db_base import DbMixinBase
from persona_bot.core.db_rows import (
    _insert_sql,
    _json_loads_or,
    _AFFINITY_COLUMNS,
    _USER_STATS_COLUMNS,
    _affinity_row_params,
    _user_stats_row_params,
)


class UsageDbMixin(DbMixinBase):
    """ユーザーごとの親密度・感情（affinity）と会話統計（user_stats）。"""

    def affinity_load_all(self) -> dict[str, dict[str, Any]]:
        """全ユーザーの親密度データを {user_id: {...}} 形式で返す（旧 JSON と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM affinity").fetchall()
        return {
            row["user_id"]: {
                "affinity": row["affinity"],
                "valence": row["valence"],
                "arousal": row["arousal"],
                "last_interaction": row["last_interaction"],
            }
            for row in rows
        }

    def affinity_upsert(self, user_id: str, entry: dict[str, Any]) -> None:
        """対象ユーザー1行だけを保存し、他ユーザーの行やrowidは変更しない。"""
        with self._lock, self._conn:
            self._conn.execute(
                _insert_sql("affinity", _AFFINITY_COLUMNS, upsert_key="user_id"),
                _affinity_row_params(user_id, entry),
            )

    def affinity_save_all(self, cache: dict[str, dict[str, Any]]) -> None:
        """明示的な全件置換用。通常の感情更新には affinity_upsert を使う。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM affinity")
            for user_id, entry in cache.items():
                self._conn.execute(
                    _insert_sql("affinity", _AFFINITY_COLUMNS), _affinity_row_params(user_id, entry),
                )

    def stats_load_all(self) -> dict[str, dict[str, Any]]:
        """全ユーザーの統計を {user_id: {...}} 形式で返す（旧 JSON と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM user_stats").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[row["user_id"]] = {
                "user_name": row["user_name"],
                "total_count": row["total_count"],
                "hourly": _json_loads_or(row["hourly"], [0] * STATS_HOURLY_BUCKETS, list),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "recent_samples": _json_loads_or(row["recent_samples"], [], list),
                "analysis": _json_loads_or(row["analysis"], None, dict),
                # 本人が統計収集を停止しているか（opt-out）。1 = 収集しない。
                "opt_out": bool(row["stats_opt_out"]),
            }
        return result

    def stats_save_all(self, cache: dict[str, dict[str, Any]]) -> None:
        """初回移行など、明示的に全件置換する処理専用。通常更新には使わない。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM user_stats")
            for user_id, entry in cache.items():
                self._stats_upsert_user(user_id, entry)

    def stats_save_user(self, user_id: str, entry: dict[str, Any]) -> None:
        """対象ユーザーの全項目だけを保存し、他ユーザーの行には触れない。"""
        with self._lock, self._conn:
            self._stats_upsert_user(user_id, entry)

    def stats_delete_user(self, user_id: str) -> None:
        """新規行の保存中断を補償する。通常の蓄積データ削除には使わない。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM user_stats WHERE user_id = ?", (str(user_id),))

    def _stats_upsert_user(self, user_id: str, entry: dict[str, Any]) -> None:
        """呼び出し側がDBロックとトランザクションを保持した状態で使う。"""
        self._conn.execute(
            _insert_sql("user_stats", _USER_STATS_COLUMNS, upsert_key="user_id"),
            _user_stats_row_params(user_id, entry),
        )
