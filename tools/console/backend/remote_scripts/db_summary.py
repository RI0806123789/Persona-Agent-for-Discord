"""読み取り専用でDBの件数と日本時間の日別評価を集計する。"""

import json
import os
import sqlite3
import sys
from pathlib import Path


def collect(db_path, blacklist_path):
    # Path を渡されても動くよう、最初に文字列へそろえる（"-wal" の連結や JSON 出力で TypeError にしない）。
    db_path = str(db_path)
    blacklist_path = str(blacklist_path)
    result = {"path": db_path, "exists": os.path.exists(db_path), "errors": []}

    def count_by(con, sql, label):
        try:
            return {str(key): int(value) for key, value in con.execute(sql)}
        except sqlite3.Error:
            result["errors"].append(label + "を集計できませんでした。旧版のテーブル・列と読み取り権限を確認してください。")
            return None

    if result["exists"]:
        try:
            result["size_bytes"] = os.path.getsize(db_path)
            wal_path = db_path + "-wal"
            result["wal_bytes"] = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
            try:
                result["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
                names = [row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                result["tables"] = {}
                for name in names:
                    try:
                        result["tables"][name] = con.execute(
                            'SELECT COUNT(*) FROM "%s"' % name.replace('"', '""')).fetchone()[0]
                    except sqlite3.Error:
                        result["errors"].append("一部のテーブルの件数を取得できませんでした。")
                result["reminders_by_status"] = count_by(con, "SELECT status, COUNT(*) FROM reminders GROUP BY status", "リマインダー")
                result["queue_by_status"] = count_by(con, "SELECT status, COUNT(*) FROM queue_tasks GROUP BY status", "文書キュー")
                rating = count_by(con, "SELECT rating, COUNT(*) FROM feedback GROUP BY rating", "評価")
                result["feedback"] = None if rating is None else {"good": rating.get("1", 0), "bad": rating.get("-1", 0)}
                # 直近7日の 👍 / 👎（日本時間の日付で区切る。created_at は UTC の ISO 8601）
                try:
                    # 現行の複合索引(guild_id, created_at)をサーバーごとの期間検索に使う。
                    # 古い・独自スキーマにguild_idがなくても日別集計は継続する。
                    # 旧空白区切りとISOのT区切りの両方を拾うため、索引側は日付で広く絞り、
                    # 日本時間の厳密な7日間はdate(created_at, '+9 hours')で判定する。
                    columns = {row[1] for row in con.execute("PRAGMA table_info(feedback)")}
                    guild_filter = "guild_id IN (SELECT DISTINCT guild_id FROM feedback) AND " if "guild_id" in columns else ""
                    result["feedback_by_day"] = [
                        {"day": day, "good": int(good or 0), "bad": int(bad or 0)}
                        for day, good, bad in con.execute(
                            "SELECT date(created_at, '+9 hours') AS day, SUM(rating = 1), SUM(rating = -1) FROM feedback "
                            f"WHERE {guild_filter}"
                            "created_at >= date('now', '+9 hours', 'start of day', '-6 days', '-9 hours') "
                            "AND created_at < date('now', '+9 hours', 'start of day', '+1 day') "
                            "AND date(created_at, '+9 hours') >= date('now', '+9 hours', '-6 days') "
                            "AND date(created_at, '+9 hours') <= date('now', '+9 hours') "
                            "GROUP BY day ORDER BY day")
                    ]
                except sqlite3.Error:
                    result["errors"].append("評価の日別推移を取得できませんでした。旧版のテーブル・列を確認してください。")
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            result["errors"].append("bot.db を読み取れませんでした。ファイルの形式と読み取り権限を確認してください。")

    try:
        with open(blacklist_path, encoding="utf-8-sig") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            raise ValueError("違反記録の形式が不正です。")
        records = [r for r in data.values() if isinstance(r, dict)]
        result["blacklist"] = {"users": len(records), "blocked": sum(1 for r in records if r.get("blocked"))}
    except FileNotFoundError:
        result["blacklist"] = None
    except (OSError, ValueError):
        result["blacklist"] = None
        result["errors"].append("blacklist.json を読み取れませんでした。ファイルの形式と読み取り権限を確認してください。")

    return result


def main(argv=None):
    # insights.py・db_table.py と同じく、引数を渡して呼べる入口にする。
    # 標準入力から `python3 - <bot.db> <blacklist.json>` で実行されるため、引数は argv[1:] から取る。
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) < 2:
        # 引数が足りないときは、生の TypeError ではなく他の結果と同じ形で案内を返す。
        print(json.dumps({
            "path": None, "exists": False,
            "errors": ["引数が足りません。bot.db と blacklist.json のパスを指定してください。"],
        }))
        return
    print(json.dumps(collect(args[0], args[1])))


if __name__ == "__main__":
    main()
