"""bot.db の列定義・INSERT 文の生成・行の変換（`Database` の `db_*` ミックスインが共有する。#468）。

`database.py` から切り出した。列名の一覧（`_AFFINITY_COLUMNS` など）、そこから全列の INSERT 文を作る
`_insert_sql`、保存パラメータの変換（`_*_row_params`）、JSON 列の読み取り（`_json_loads_or`）を持つ。
旧データの取り込み（`db_legacy_importers.py`）も、`Database` が渡す同じ定義を使う。
列を足すときは、ここの列名の一覧と変換関数をそろえて直す。

★ `_`で始まる名前は、`db_*.py` と `database.py` の間でだけ共有する（外へ広げない）。
"""

import json
from typing import Any

from persona_bot.config.constants import (
    AFFINITY_MAX,
    AFFINITY_MIN,
    AFFINITY_NEUTRAL,
    STATS_HOURLY_BUCKETS,
    STATS_OPT_OUT_DEFAULT,
)


# queue_tasks の列名一覧。UPDATE 文へ列名を埋め込む際のホワイトリストに使う。
_QUEUE_COLUMNS = (
    "task_id",
    "status",
    "channel_id",
    "message_id",
    "author_id",
    "question",
    "attachment_count",
    "created_at",
    "updated_at",
    "finished_at",
    "error",
)
# affinity・user_stats の列名一覧。INSERT 文と同順の保存パラメータ（`_*_row_params`）の列順と揃える。
_AFFINITY_COLUMNS = ("user_id", "affinity", "valence", "arousal", "last_interaction")
_USER_STATS_COLUMNS = (
    "user_id",
    "user_name",
    "total_count",
    "hourly",
    "first_seen",
    "last_seen",
    "recent_samples",
    "analysis",
    "stats_opt_out",
)
# まだ終わっていない（受付の上限に数える）文書タスクのステータス。
_QUEUE_ACTIVE_STATUSES = ("pending", "processing", "sending")


def _insert_sql(table: str, columns: tuple[str, ...], *, verb: str = "INSERT", upsert_key: str | None = None) -> str:
    """全列の INSERT 文を列名の一覧から作る（列を足すとき、定義を1か所だけ直せば済むように）。

    verb は "INSERT" / "INSERT OR IGNORE" / "INSERT OR REPLACE"。upsert_key を渡すと、そのキーの衝突時に
    残りの全列を `excluded` の値で更新する。列名・テーブル名はこのモジュールの定数だけを渡すこと
    （SQL 文へ直接埋め込むため）。
    """
    names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"{verb} INTO {table} ({names}) VALUES ({placeholders})"
    if upsert_key is not None:
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != upsert_key)
        sql += f" ON CONFLICT({upsert_key}) DO UPDATE SET {updates}"
    return sql


def _json_loads_or(text: Any, default: Any, expected_type: type | tuple[type, ...] | None = None) -> Any:
    """JSON 文字列を読み、壊れていたら default を返す（フェイルオープン）。

    捕捉するのは JSON として読めない場合（`ValueError`＝`JSONDecodeError` を含む・`TypeError`・深すぎる入れ子の
    `RecursionError`）だけで、想定外の例外（バグ）は隠さない。`expected_type` を渡すと、読めても型が違う値
    （`"null"` や、列の意味と違う型）は default に戻す。プロフィールの値のように、型が決まっていない列は省略する。
    """
    if not text:
        return default
    try:
        value = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return default
    if expected_type is not None and not isinstance(value, expected_type):
        return default
    return value


def _user_stats_row_params(user_id: Any, entry: dict[str, Any]) -> tuple[Any, ...]:
    """user_stats の9列を旧JSON取り込み・通常保存・全件置換で同じ値へ変換する。

    列順は `_USER_STATS_COLUMNS`（user_id, user_name, total_count, hourly, first_seen, last_seen,
    recent_samples, analysis, stats_opt_out）。列追加時は `_USER_STATS_COLUMNS` とこの関数を揃える。
    収集停止は通常の opt_out を優先し、旧JSONの stats_opt_out も引き継ぐ。
    """
    analysis = entry.get("analysis")
    # ★ dict.get の第2引数は「キーが無いとき」にしか効かない。値が None で入っている旧JSON・
    # 破損行でも収集停止の設定が外れないよう、None は次の候補へ明示的に落とす。
    opt_out = entry.get("opt_out")
    if opt_out is None:
        opt_out = entry.get("stats_opt_out")
    if opt_out is None:
        opt_out = STATS_OPT_OUT_DEFAULT
    return (
        str(user_id),
        str(entry.get("user_name") or ""),
        int(entry.get("total_count") or 0),
        json.dumps(entry.get("hourly") or [0] * STATS_HOURLY_BUCKETS, ensure_ascii=False),
        str(entry.get("first_seen") or ""),
        str(entry.get("last_seen") or ""),
        json.dumps(entry.get("recent_samples") or [], ensure_ascii=False),
        json.dumps(analysis, ensure_ascii=False) if analysis else None,
        int(bool(opt_out)),
    )


def _queue_row_params(task: dict[str, Any]) -> tuple[Any, ...]:
    """queue_tasks 1行分の保存パラメータ。通常の追加（_insert_queue_task）と旧 queue.json の取り込みで共有する。

    列順は `_QUEUE_COLUMNS`（task_id, status, channel_id, message_id, author_id, question, attachment_count,
    created_at, updated_at, finished_at, error）。列を足すときは `_QUEUE_COLUMNS` とこの関数を揃える。
    欠けた値・None は既定値（status は pending、数値は 0、文字列は空）にし、finished_at・error は
    そのまま（未終了・成功は None）。
    """
    return (
        str(task.get("task_id") or ""),
        str(task.get("status") or "pending"),
        int(task.get("channel_id") or 0),
        int(task.get("message_id") or 0),
        int(task.get("author_id") or 0),
        str(task.get("question") or ""),
        int(task.get("attachment_count") or 0),
        str(task.get("created_at") or ""),
        str(task.get("updated_at") or ""),
        task.get("finished_at"),
        task.get("error"),
    )


def _affinity_row_params(user_id: Any, entry: dict[str, Any]) -> tuple[str, Any, Any, Any, Any]:
    """affinity テーブル1行分の保存パラメータ（列順は `_AFFINITY_COLUMNS`）。

    旧 JSON の取り込み・1行の保存・全件置換の3か所で同じ既定値を使うため、ここに集約する。
    列を足すときは `_AFFINITY_COLUMNS` とこの関数を揃える（INSERT 文は列名の一覧から作る）。
    None・欠けた値は中立値（最終会話時刻は 0.0）とし、感情値は有効範囲に収める。
    """
    def value_or(key: str, default: Any) -> Any:
        value = entry.get(key)
        return default if value is None else value

    def bounded_value(key: str) -> int:
        return max(AFFINITY_MIN, min(AFFINITY_MAX, int(value_or(key, AFFINITY_NEUTRAL))))

    return (
        str(user_id),
        bounded_value("affinity"),
        bounded_value("valence"),
        bounded_value("arousal"),
        float(value_or("last_interaction", 0.0)),
    )
