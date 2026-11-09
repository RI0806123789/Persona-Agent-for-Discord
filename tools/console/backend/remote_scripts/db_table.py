"""ボットのサーバー上で実行する、bot.db の1つのテーブルの行を取り出すスクリプト
（ダッシュボードが SSH の標準入力で送り込む）。標準ライブラリだけで動く。

データページで「テーブルの中身」を開いたときに使う。利用者の判断で、会話・記憶・リマインダーの
本文や Discord の ID も含めて持ち帰る（画面の伏せ字モードで、本文と ID を隠してから写せる）。

★ DB は mode=ro（読み取り専用）で開く。書き込みは一切しない。
★ テーブル名はこのファイルの TABLES にあるものだけを受け付ける（バックエンド側でも同じ確認をする）。
  並び順の列もここに書いたものだけを使い、引数の文字列を SQL に埋め込まない。
★ 会話統計の収集を止めた人（user_stats.stats_opt_out = 1）の発言サンプルと解析結果は持ち帰らない
  （ボットは停止中の人の統計を管理者にも見せないため、ダッシュボードも同じ扱いにする）。
★ 17 桁以上の Discord の ID は JavaScript の数値に収まらない（2^53 を超えると下の桁が丸まる）ため、
  `_id` で終わる列と大きな整数は文字列にして返す。

使い方: python3 - --db-table <bot.db の場所> <テーブル名> <開始位置> <件数>   （スクリプト本体は標準入力）
"""

import json
import math
import os
import sqlite3
import sys
from pathlib import Path

# テーブル名 → 並び順（新しいものを先に）と、中身が JSON 文字列の列。
# 並び順の列が古い版の DB に1つでも無ければ、rowid（追加した順）の降順にする。
TABLES = {
    "affinity": {"order": ["last_interaction DESC", "user_id"], "json": []},
    "user_stats": {"order": ["last_seen DESC", "user_id"], "json": ["hourly", "recent_samples", "analysis"]},
    "queue_tasks": {"order": ["created_at DESC", "task_id"], "json": []},
    "pending_log": {"order": ["id DESC"], "json": []},
    "legacy_imports": {"order": ["source_name", "content_hash"], "json": []},
    "profile": {"order": ["user_id", "key"], "json": ["value"]},
    "guild_settings": {"order": ["guild_id"], "json": []},
    "anniversary_posts": {"order": ["posted_date DESC", "guild_id", "user_id", "label"], "json": []},
    "reminders": {"order": ["reminder_id DESC"], "json": []},
    "feedback": {"order": ["created_at DESC", "message_id", "user_id"], "json": []},
}
# 統計の収集を止めた人の行で、持ち帰らない列
OPT_OUT_HIDDEN = ("recent_samples", "analysis")
# 利用者の ID が入る列（表示名を引くため）
USER_COLUMNS = ("user_id", "target_user_id", "author_id")
MAX_LIMIT = 200
# 表示名を引く1回の問い合わせに渡す ID の数の上限（古い SQLite の変数の上限 999 より小さく保つ）
NAME_LOOKUP_CHUNK = 500
# JavaScript の Number で正確に表せる整数の上限
MAX_SAFE_INTEGER = 2 ** 53 - 1

def quote(name):
    return '"' + name.replace('"', '""') + '"'


def sanitize(value):
    """JSON の中の値を、画面へ渡せる形にそろえる（NaN・無限大は JSON にできないので null にする）。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_SAFE_INTEGER:
        return str(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {str(key): sanitize(item) for key, item in value.items()}
    return value


def to_json_value(column, value):
    """画面へ渡す値にする。ID は文字列に、bytes は読めない値として長さだけにする。"""
    if isinstance(value, bytes):
        return "<バイナリ %d バイト>" % len(value)
    if isinstance(value, int) and not isinstance(value, bool) and column.endswith("_id"):
        return str(value)
    return sanitize(value)


def parse_json_column(value):
    """JSON 文字列の列を読む。読めなければ元の文字列のまま返す（手で直した値でも画面を止めない）。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, RecursionError):
        return value


def user_names(con, user_ids):
    """利用者の ID → 表示名。会話統計とリマインダーに残っている、依頼時・発言時の表示名を使う。

    表示中の行に出てくる ID（多くても件数の上限 × 利用者の列の数）だけを WHERE user_id IN (...) で
    引く。テーブルを全件読むと、記録が増えるほど1ページの表示が遅くなり、メモリも使うため。
    ID は文字列のまま渡す（どちらのテーブルも user_id は TEXT 型）。
    """
    names = {}
    ids = sorted(user_ids)
    if not ids:
        return names
    for sql in (
        # リマインダーは古いものから読み、新しい表示名で上書きする。会話統計の名前を最優先にする
        "SELECT user_id, user_name FROM reminders WHERE user_name != '' AND user_id IN (%s) ORDER BY reminder_id",
        "SELECT user_id, user_name FROM user_stats WHERE user_name != '' AND user_id IN (%s)",
    ):
        try:
            # 区切りごとに ID が重ならないので、区切っても上書きの順序は変わらない
            for start in range(0, len(ids), NAME_LOOKUP_CHUNK):
                chunk = ids[start:start + NAME_LOOKUP_CHUNK]
                for user_id, name in con.execute(sql % ", ".join("?" * len(chunk)), chunk):
                    key = str(user_id)
                    if key in user_ids and isinstance(name, str) and name:
                        names[key] = name
        except sqlite3.Error:
            pass  # 古い版の DB にテーブル・列が無いだけなら、表示名なしで続ける
    return names


def read_table(con, table, offset, limit, result):
    """呼出元が所有する結果へ追記する。途中の失敗でも取得済みの列・件数を残す。"""
    spec = TABLES[table]
    columns = [row[1] for row in con.execute("PRAGMA table_info(%s)" % quote(table))]
    if not columns:
        result["errors"].append("この DB には %s テーブルがありません（スキーマが古い可能性があります）。" % table)
        return result
    result["columns"] = columns
    result["total"] = con.execute("SELECT COUNT(*) FROM %s" % quote(table)).fetchone()[0]

    # 並び順の列が1つでも欠けていたら（古い版の DB）、追加した順の新しいものから並べる
    order = []
    for item in spec["order"]:
        name, _, direction = item.partition(" ")
        if name not in columns:
            order = []
            break
        order.append(quote(name) + (" " + direction if direction else ""))
    order_sql = ", ".join(order) if order else "rowid DESC"
    rows = con.execute(
        "SELECT %s FROM %s ORDER BY %s LIMIT ? OFFSET ?" % (", ".join(quote(c) for c in columns), quote(table), order_sql),
        (limit, offset),
    ).fetchall()

    opt_out_index = columns.index("stats_opt_out") if table == "user_stats" and "stats_opt_out" in columns else None
    output = []
    seen_users = set()
    for row in rows:
        cells = {}
        hidden = []
        opted_out = opt_out_index is not None and bool(row[opt_out_index])
        for column, value in zip(columns, row):
            if opted_out and column in OPT_OUT_HIDDEN:
                cells[column] = None
                hidden.append(column)
                continue
            if column in spec["json"]:
                try:
                    converted = to_json_value(column, parse_json_column(value))
                    # 最終出力で行やセルの外側に付く階層も含めて書き出せるか確かめる。
                    probe = converted
                    for _ in range(10):
                        probe = [probe]
                    json.dumps(probe)
                    cells[column] = converted
                except RecursionError:
                    # 解析できても表示用変換や JSON 出力が深さの上限に達する場合がある。
                    cells[column] = value
            else:
                cells[column] = to_json_value(column, value)
            if column in USER_COLUMNS and value not in (None, ""):
                seen_users.add(str(value))
        item = {"cells": cells}
        if hidden:
            item["hidden"] = hidden
        output.append(item)
    result["rows"] = output
    result["users"] = user_names(con, seen_users)
    return result


def collect(db_path, table, offset=0, limit=50):
    """1ページ分を読み取り専用で収集し、実行ごとに独立した結果を返す。"""
    try:
        # bool は int のサブクラスで、True / False が 1 / 0 として通ってしまうため不正値にする。
        if isinstance(offset, bool) or isinstance(limit, bool):
            raise ValueError("開始位置と件数は整数で指定します。")
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError, OverflowError):
        # None・数値でない型・無限大なども、開始位置と件数を一緒に既定値へ戻す。
        offset, limit = 0, 50
    result = {"table": table, "offset": offset, "limit": limit, "columns": [], "rows": [], "total": 0, "users": {}, "errors": []}
    if table not in TABLES:
        result["errors"].append("このテーブルは表示できません。")
        return result
    if not os.path.exists(db_path):
        result["errors"].append("bot.db が見つかりません。")
        return result
    try:
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            result["schema_version"] = con.execute("PRAGMA user_version").fetchone()[0]
            read_table(con, table, offset, limit, result)
        finally:
            con.close()
    except (OSError, sqlite3.Error):
        # 例外の文面にはサーバー上のパスが入りうるので、決まった案内だけを返す
        result["errors"].append("bot.db を読み取れませんでした。ファイルの形式と読み取り権限を確認してください。")
    return result


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    # 省略時・不正値のときは、従来どおり開始位置と件数を一緒に既定値へ戻す。
    offset, limit = args[3:5] if len(args) >= 5 else (0, 50)
    result = collect(args[1], args[2], offset, limit)
    # 日本語は \uXXXX にして ASCII だけで出す（サーバーのロケールが UTF-8 でなくても書き出せるように）
    print(json.dumps(result))


if __name__ == "__main__":
    main()
