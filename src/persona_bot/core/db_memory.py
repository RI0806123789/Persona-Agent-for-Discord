"""プロフィール記憶（profile）と記憶圧縮前の一時ログ（pending_log）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from persona_bot.core.db_base import DbMixinBase
from persona_bot.core.db_rows import (
    _json_loads_or,
)


class MemoryDbMixin(DbMixinBase):
    """プロフィール記憶（profile）と記憶圧縮前の一時ログ（pending_log）。"""

    def profile_load(self, user_id: str) -> dict[str, Any]:
        """指定ユーザーのプロフィール記憶を {キー: 値} 形式で返す（旧 profile.json と同じ形）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM profile WHERE user_id = ? ORDER BY key",
                (str(user_id),),
            ).fetchall()
        return {row["key"]: _json_loads_or(row["value"], None) for row in rows}

    def profile_merge(self, user_id: str, updates: dict[str, Any]) -> None:
        """指定ユーザーのトップレベルキー単位でマージする（同名キーは updates 側で上書き）。"""
        with self._lock, self._conn:
            for key, value in updates.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO profile (user_id, key, value) VALUES (?, ?, ?)",
                    (str(user_id), str(key), json.dumps(value, ensure_ascii=False)),
                )

    def profile_load_all_for_key(self, key: str) -> dict[str, Any]:
        """全ユーザーの指定キー1件分の値を {user_id: 値} 形式で返す。

        記念日チェックのように「全ユーザーを横断してある1項目だけ見たい」場合に使う
        （profile_load はユーザー1人分の全キーを返すため、全ユーザー分の走査には向かない）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id, value FROM profile WHERE key = ?", (str(key),)
            ).fetchall()
        return {row["user_id"]: _json_loads_or(row["value"], None) for row in rows}

    def profile_replace_all(self, user_id: str, profile: dict[str, Any]) -> None:
        """指定ユーザーのプロフィール全体を置き換える（記憶圧縮の結果を保存する際に使う）。

        他ユーザーの行には一切触れない（WHERE user_id で対象を限定する）。
        """
        with self._lock, self._conn:
            self._profile_replace_all(user_id, profile)

    def _profile_replace_all(self, user_id: str, profile: dict[str, Any]) -> None:
        """呼出元がDBロックとトランザクションを保持した状態で置き換える。"""
        self._conn.execute("DELETE FROM profile WHERE user_id = ?", (str(user_id),))
        for key, value in profile.items():
            self._conn.execute(
                "INSERT INTO profile (user_id, key, value) VALUES (?, ?, ?)",
                (str(user_id), str(key), json.dumps(value, ensure_ascii=False)),
            )

    def save_compacted_memory(
        self, user_id: str, profile: dict[str, Any], max_id: int, save_projects: Callable[[], None],
    ) -> None:
        """圧縮結果と対象ログの削除をまとめ、JSON保存の失敗時もDBを戻す。

        save_projects は同期のファイル保存だけを行い、DBメソッドを呼ばないこと。
        """
        with self._lock, self._conn:
            self._profile_replace_all(user_id, profile)
            self._conn.execute(
                "DELETE FROM pending_log WHERE user_id = ? AND id <= ?",
                (str(user_id), int(max_id)),
            )
            save_projects()

    def pending_append(self, user_id: str, created_at: str, question: str, response: str) -> None:
        if user_id is None or not str(user_id).strip():
            raise ValueError("会話ログの保存には空でないユーザーIDが必要です。")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO pending_log (user_id, created_at, question, response) VALUES (?, ?, ?, ?)",
                (str(user_id), created_at, question, response),
            )

    def pending_distinct_user_ids(self) -> list[str]:
        """一時ログを持つユーザーIDの一覧を返す（記憶圧縮の対象ユーザー列挙に使う）。"""
        with self._lock:
            rows = self._conn.execute(
                # user_id='' は旧・全体共有の未割当行。起動時に owner へ振り直すか破棄するが、
                # 万一残っていても「誰でもない利用者」として圧縮しない。
                "SELECT DISTINCT user_id FROM pending_log WHERE user_id != '' ORDER BY user_id"
            ).fetchall()
        return [row["user_id"] for row in rows]

    def pending_fetch_entries(self, user_id: str) -> list[dict[str, Any]]:
        """指定ユーザーの一時ログを追記順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_log WHERE user_id = ? ORDER BY id",
                (str(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_clear_upto(self, user_id: str, max_id: int) -> None:
        """指定ユーザーの一時ログのうち、id が max_id 以下の行だけを削除する。

        記憶圧縮は Gemini 呼び出しの間 io_lock を手放すため、その数十秒の間に
        新しい会話ログが追記されうる。圧縮対象に含まれていないその分まで
        消さないよう、削除範囲を「圧縮に使った行の最大 id」で区切る。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM pending_log WHERE user_id = ? AND id <= ?",
                (str(user_id), int(max_id)),
            )
