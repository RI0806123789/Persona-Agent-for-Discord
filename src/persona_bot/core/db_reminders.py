"""リマインダー（reminders）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from persona_bot.config.constants import (
    REMINDER_MAX_PENDING_PER_USER,
)
from persona_bot.core.db_base import DbMixinBase

# 運用ログの出元は分割前と同じ `persona_bot.core.database`（管理コンソールが文言と出元を参照する）。
logger = logging.getLogger("persona_bot.core.database")


class ReminderDbMixin(DbMixinBase):
    """リマインダー（reminders）。"""

    def reminder_add(
        self,
        *,
        user_id: str,
        user_name: str,
        guild_id: int,
        channel_id: int,
        message: str,
        remind_at: str,
        created_at: str,
        recurrence: str = "none",
        recurrence_anchor: str = "",
        max_pending: int = REMINDER_MAX_PENDING_PER_USER,
    ) -> int | None:
        """上限内ならリマインダーを登録して ID を返し、上限到達なら None を返す。

        pending の件数確認と INSERT は1つのSQL文にし、同じロック・
        トランザクションで実行する。ユーザーの枠はサーバー横断で数える。

        remind_at / created_at は UTC の ISO 8601 文字列。同じ形式で揃えることで、
        期限判定を文字列比較のまま行える（時刻順と辞書順が一致する）。

        recurrence は 'none'（単発）/'daily'/'weekly'/'monthly'。既定値を持つため、
        繰り返しを指定しない既存の呼び出しは従来どおり単発として登録される。
        recurrence_anchor は初回の通知予定時刻で、毎月の繰り返しが
        「31日 → 2月は28日 → 3月は31日」と元の日付へ戻るための基準になる
        （丸めた日付に引きずられないよう、以後この値は更新しない）。
        """
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT INTO reminders"
                " (user_id, user_name, guild_id, channel_id, message, remind_at, created_at, status,"
                "  recurrence, recurrence_anchor)"
                " SELECT ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?"
                " WHERE (SELECT COUNT(*) FROM reminders"
                " WHERE user_id = ? AND status = 'pending') < ?",
                (
                    str(user_id),
                    str(user_name),
                    int(guild_id),
                    int(channel_id),
                    str(message),
                    str(remind_at),
                    str(created_at),
                    str(recurrence),
                    str(recurrence_anchor),
                    str(user_id),
                    int(max_pending),
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def reminder_claim_due(
        self, now_iso: str, next_at: Callable[[dict[str, Any]], str | None]
    ) -> list[dict[str, Any]]:
        """期限が来た未通知リマインダーをクレームして返す（無ければ空リスト）。

        単発（recurrence='none'）は done にする。繰り返しは next_at() が返す
        次回時刻へ remind_at を進め、status は pending のまま残す
        （新しい行を作らないため、UI から見た reminder_id が変わらない）。
        SELECT と UPDATE を同一トランザクションで行うため、見回りが重なっても
        同じリマインダーが二重に通知されることはない（queue_tasks と同じ作法）。
        通知の送信自体が失敗した場合は呼び出し側でログに残す — 進めたままにして
        再送しないのは、通知の重複より「時間外れの再通知」の方が困るため。

        Args:
            now_iso: 期限判定に使う現在時刻（UTC の ISO 8601）。
            next_at: 1行分の記録を受け取り、次回の remind_at（UTC の ISO 8601）を
                返す**同期**関数。もう繰り返さないなら None を返す。
                now_iso 以前の時刻を返した場合も None と同じく系列を止める。
                **この関数の中から Database のメソッドを呼ばないこと** —
                self._lock は再入不可の threading.Lock なので、同じスレッドから
                再取得するとデッドロックしてボット全体が固まる。
                また async def にすると await されずコルーチンが返って壊れる。

        Returns:
            クレームした記録のリスト。通知文の組み立てで使えるよう、進めた後の
            時刻を next_remind_at に入れて返す（単発は None）。
        """
        claimed: list[dict[str, Any]] = []
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ?"
                " ORDER BY remind_at, reminder_id",
                (str(now_iso),),
            ).fetchall()
            for row in rows:
                record = dict(row)
                reminder_id = int(record["reminder_id"])
                recurrence = str(record.get("recurrence", "none") or "none")
                # 暦の知識はサービス層に置き、DB 層は「何を書くか」だけを受け取る。
                # 計算が例外で落ちても見回り全体を止めない（その行は次回時刻なしとして扱う）。
                try:
                    next_iso = next_at(record)
                except Exception as error:
                    logger.error('データベース: リマインダーの次回時刻を計算できませんでした。', exc_info=error)
                    next_iso = None

                # 次回時刻が現在以前だと pending のまま残り、上の SELECT と同じ条件で
                # 次の見回りが同じ系列を取り直し続ける（#222）。計算側の不具合として
                # 「作れなかった」扱いにし、下の分岐で系列を止める。
                if next_iso and str(next_iso) <= str(now_iso):
                    logger.warning('データベース: リマインダーの次回時刻が現在以前のため系列を止めます。')
                    next_iso = None

                if next_iso:
                    self._conn.execute(
                        "UPDATE reminders SET remind_at = ? WHERE reminder_id = ?",
                        (str(next_iso), reminder_id),
                    )
                    record["next_remind_at"] = str(next_iso)
                    # status は pending のまま（次回も同じ行が見回りに拾われる）。
                    # 返す記録の remind_at は **進める前の（本来の）通知予定時刻**のまま
                    # にしておく — 呼び出し側の遅延判定（_is_stale）がここを見るため、
                    # 未来の時刻へ書き換えると遅れの抑制が永久に効かなくなる。
                else:
                    # 単発は done（今回の通知で役目を終える）。繰り返しなのに次回時刻を
                    # 作れなかった行（追い付き上限に達した・値が壊れている・計算が例外で
                    # 落ちたなど）は、理由を問わず canceled にして系列を止める —
                    # 毎分再クレームされて通知が溢れるより「止まる」方を選ぶ。
                    # 例外の場合だけ done にすると、正常に完了したように見えて、
                    # 繰り返しが途絶えた原因を DB から追えなくなる。
                    status = "done" if recurrence == "none" else "canceled"
                    self._conn.execute(
                        "UPDATE reminders SET status = ? WHERE reminder_id = ?",
                        (status, reminder_id),
                    )
                    record["status"] = status
                    record["next_remind_at"] = None
                claimed.append(record)
        return claimed

    def reminder_list_pending(self, user_id: str) -> list[dict[str, Any]]:
        """指定ユーザーの未通知リマインダーを、通知が近い順で返す。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reminders WHERE user_id = ? AND status = 'pending'"
                " ORDER BY remind_at, reminder_id",
                (str(user_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def reminder_count_pending(self, user_id: str) -> int:
        """指定ユーザーの未通知リマインダーの件数を、サーバー横断で返す。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS count FROM reminders WHERE user_id = ? AND status = 'pending'",
                (str(user_id),),
            ).fetchone()
        return int(row["count"]) if row else 0

    def reminder_is_pending(self, reminder_id: int) -> bool:
        """繰り返し通知の送信直前に、系列がまだ有効かを確認する。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM reminders WHERE reminder_id = ? AND status = 'pending'",
                (int(reminder_id),),
            ).fetchone()
        return row is not None

    def reminder_cancel(self, reminder_id: int, user_id: str) -> dict[str, Any] | None:
        """リマインダーを取り消す。取り消せた場合はその内容を返す。

        user_id を条件に含めるため、他人のリマインダーは取り消せない（UI 側の
        絞り込みだけに頼らず、SQL でも本人のものに限定する）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE reminder_id = ? AND user_id = ? AND status = 'pending'",
                (int(reminder_id), str(user_id)),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE reminders SET status = 'canceled' WHERE reminder_id = ?",
                (int(reminder_id),),
            )
        record = dict(row)
        record["status"] = "canceled"
        return record

    def reminder_prune_finished(self, max_keep: int) -> None:
        """通知済み・取り消し済みのレコードを新しい方から max_keep 件だけ残して削除する。"""
        self._prune_keep_newest(
            "DELETE FROM reminders WHERE status IN ('done', 'canceled')"
            " AND reminder_id NOT IN ("
            "   SELECT reminder_id FROM reminders WHERE status IN ('done', 'canceled')"
            "   ORDER BY reminder_id DESC LIMIT ?"
            ")",
            max_keep,
        )
