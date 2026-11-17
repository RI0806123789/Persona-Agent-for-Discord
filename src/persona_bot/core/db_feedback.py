"""ボット返信への 👍/👎 評価（feedback）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

from typing import Any

from persona_bot.core.db_base import DbMixinBase


class FeedbackDbMixin(DbMixinBase):
    """ボット返信への 👍/👎 評価（feedback）。"""

    def feedback_record(
        self,
        *,
        message_id: int,
        user_id: str,
        guild_id: int,
        channel_id: int,
        target_user_id: str,
        model_name: str,
        prompt_file: str,
        rating: int,
        created_at: str,
    ) -> None:
        """フィードバックを1件記録する（同じ人が同じ返信へ再投票したら上書き）。

        主キーが (message_id, user_id) なので、👍 → 👎 の押し替えでも行は増えず
        rating だけが入れ替わる。model_name / prompt_file を一緒に残すのは、
        「どのモデル・どのペルソナの返信が評価されたか」を後から比べるため。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO feedback"
                " (message_id, user_id, guild_id, channel_id, target_user_id,"
                "  model_name, prompt_file, rating, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(message_id),
                    str(user_id),
                    int(guild_id),
                    int(channel_id),
                    str(target_user_id),
                    str(model_name),
                    str(prompt_file),
                    int(rating),
                    str(created_at),
                ),
            )

    def feedback_delete(self, message_id: int, user_id: str) -> None:
        """その人のその返信への評価を消す（👍👎 を両方とも外したときに呼ぶ）。

        該当行が無くても何も起きない（存在しないことを異常としない）。

        👍 と 👎 の両方を付けた人が片方だけ外した場合に、どちらを残すかは
        FeedbackService が「いま付いている絵文字と付けた順」から決め、残る側を
        feedback_record で書き直す。この行には採用中の1票しか載っていないので、
        外した絵文字と保存済みの rating を照合して消す方式では、採用中の側を
        外したときに残っている側の票まで消えてしまう（ここでは判断しない）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM feedback WHERE message_id = ? AND user_id = ?",
                (int(message_id), str(user_id)),
            )

    def feedback_summary(self, guild_id: int) -> list[dict[str, Any]]:
        """指定サーバーのフィードバックを「モデル×ペルソナ」で集計して返す。

        guild_id で絞るのは、集計の閲覧がそのサーバーの管理者に限られるため
        （DM 分は guild_id=0 として保存されるので、誰の集計にも混ざらない）。
        件数の多い組み合わせが先に来るよう並べ替えて返す（表示は上位数件で打ち切るため）。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT model_name, prompt_file,"
                "       SUM(rating = 1) AS good,"
                "       SUM(rating = -1) AS bad,"
                "       COUNT(*) AS total"
                " FROM feedback WHERE guild_id = ?"
                " GROUP BY model_name, prompt_file"
                " ORDER BY total DESC, model_name, prompt_file",
                (int(guild_id),),
            ).fetchall()
        return [
            {
                "model_name": row["model_name"],
                "prompt_file": row["prompt_file"],
                "good": int(row["good"] or 0),
                "bad": int(row["bad"] or 0),
                "total": int(row["total"] or 0),
            }
            for row in rows
        ]

    def feedback_prune(self, max_keep: int) -> None:
        """フィードバックを新しい方から max_keep 件だけ残して削除する。

        reminder_prune_finished と同じ「古い分から捨てる」方針。主キーが
        (message_id, user_id) の複合キーで NOT IN に使いづらいため、
        SQLite が全行に持つ暗黙の rowid で絞り込む。
        """
        self._prune_keep_newest(
            "DELETE FROM feedback WHERE rowid NOT IN ("
            "   SELECT rowid FROM feedback"
            "   ORDER BY created_at DESC, message_id DESC LIMIT ?"
            ")",
            max_keep,
        )
