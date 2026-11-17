"""`Database` のテーブル群別ミックスイン（`db_*.py`）が共有する土台（#468）。

接続（`_conn`）とロック（`_lock`）は `Database` が生成時に持つ。ミックスインはそれを `self` 経由で使い、
書き込みは `with self._lock, self._conn:` のトランザクションで行う（`Database` の説明を参照）。
"""

import sqlite3
import threading


class DbMixinBase:
    """ミックスインが前提にする属性と、複数のテーブルで共有する処理。"""

    # `Database.__init__` が設定する。型の宣言だけで、ここでは値を作らない。
    _conn: sqlite3.Connection
    _lock: threading.Lock

    def _prune_keep_newest(self, delete_sql: str, keep_count: int) -> None:
        """「新しい方から keep_count 件だけ残して削除する」DELETE 文を実行する（3つの prune で共有）。

        delete_sql は、残す件数を表す `LIMIT ?` をちょうど1つだけ持つこと。件数は 0 以上へ丸める
        （SQLite の `LIMIT -1` は無制限の意味になり、負数を渡すと全件が残ってしまうため）。
        """
        with self._lock, self._conn:
            self._conn.execute(delete_sql, (max(0, int(keep_count)),))
