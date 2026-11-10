"""SQLite データベース層（data/memory/bot.db）。

以下の10テーブルを SQLite で管理する。データの読み書きの SQL 文はすべてこのモジュールに集約する。
旧 JSON / テキストファイルから移したデータと、その後追加した機能のデータを持つ。

`Database` は窓口（ファサード）で、次の2つの責務を別のモジュールへ分けている。
  - db_migrations.py        … スキーマの定義（初版のテーブル作成・バージョンごとの変更）と、その適用
  - db_legacy_importers.py  … 旧ファイルの読み取り・解釈と、旧データの DB への取り込み（#414）
どちらも `Database` を通して使い、外から見たメソッド名・引数・挙動は分離前と同じ。
旧データの取り込み（`LegacyDataImporter`）は、このモジュールの接続・ロックを共有し、ログの出力元
（`persona_bot.core.database`）・退避先の連番の上限（`LEGACY_BACKUP_MAX_SUFFIX`）・取り込みの値の変換
（`_affinity_row_params` など。通常の保存と共有する）はこのモジュールの定義を使う。

  - affinity     … ユーザーごとの親密度・感情（旧 affinity.json）
  - user_stats   … ユーザー別会話統計（旧 user_stats.json）
  - queue_tasks  … 文書処理タスクキュー（旧 queue.json）
  - pending_log  … 記憶圧縮前の一時会話ログ（旧 pending_log.txt）。user_id 列でユーザー別。
  - profile      … プロフィール記憶。(user_id, key) の複合主キーでユーザー別。値はJSON文字列。
  - guild_settings … サーバーごとの設定。DM は共通の設定枠。
  - anniversary_posts … サーバー・ユーザー・記念日ごとの最終お祝い投稿日。
  - reminders    … ユーザーごとの通知予定・繰り返し・通知/取り消し状態。
  - feedback     … ボット返信への評価。(message_id, user_id) ごとに記録する。
  - legacy_imports … 旧ファイルの取り込み済み記録。本文は保存せず内容のハッシュで照合する。

projects.json は LLM が構造を自由に決める JSON のため、
テーブル化せず従来どおりファイルのまま扱う。

■ 旧データの取り込み
初回起動時、旧ファイルが残っていれば内容を DB へ取り込み、二重取り込みを
防ぐためファイルを「<元の名前>.imported.bak」に改名して残す（バックアップ兼用）。
同名の退避ファイルが既にあるときは上書きせず、「<元の名前>.imported.<連番>.bak」にする。
JSON として読めない・最上位が object でないファイルは取り込まず、改名もせずに
元の名前のまま残して警告する（次回起動時に再試行される）。

■ profile.json は「手書き追加用の受け皿」
profile.json は毎回の起動時にチェックされ、中身があれば profile テーブルへ
マージ（同名キーはファイル側で上書き）した後、ファイルを {} に戻す。
ユーザー別記憶に対応するため形式は {ユーザーID: {キー: 値, ...}, ...}。
つまり profile.json に対象ユーザーIDごとの事実を書き足して再起動するだけで、
その内容が自動的にデータベースの記憶へ追加される。

■ スキーマを後から変更したくなったら（列の追加など）
db_migrations.MIGRATIONS にそのバージョン番号で変更用の SQL 文を追記し、このモジュールの
SCHEMA_VERSION を 1 つ上げる（MIGRATIONS の最大のキーと同じ値）。対応する読み込み・保存処理も更新する。
適用済みバージョンは DB 内の PRAGMA user_version に記録され、
起動時に未適用の分だけが順番に実行される（二重適用されない）。
db_migrations.CREATE_TABLE_STATEMENTS は初版（バージョン1）の形のまま変更しないこと —
列の追加・変更は必ず MIGRATIONS 側で行う（新規 DB にも同じ移行が走るため、
両方を書き換えると二重適用でエラーになる）。
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from persona_bot.config.constants import (
    AFFINITY_MAX,
    AFFINITY_MIN,
    AFFINITY_NEUTRAL,
    DATABASE_BUSY_TIMEOUT_SECONDS,
    LEGACY_BACKUP_MAX_SUFFIX,
    REMINDER_MAX_PENDING_PER_USER,
    STATS_HOURLY_BUCKETS,
    STATS_OPT_OUT_DEFAULT,
)
from persona_bot.core.db_legacy_importers import LegacyDataImporter, LegacyRowParams
# テストと既存の呼び出しが `database._MIGRATIONS` / `database._CREATE_TABLE_STATEMENTS` で参照・差し替えるため、
# 先頭にアンダースコアを付けた別名で持つ（同じオブジェクトなので、patch.dict の変更は定義側にも届く）。
from persona_bot.core.db_migrations import (
    CREATE_TABLE_STATEMENTS as _CREATE_TABLE_STATEMENTS,
    MIGRATIONS as _MIGRATIONS,
    apply_migrations,
)
from persona_bot.core.guild_settings_fields import GUILD_SETTINGS_FIELDS
from persona_bot.utils.storage import write_json_file


logger = logging.getLogger(__name__)

# 現在のスキーマバージョン。db_migrations.MIGRATIONS に版を足したら 1 つ上げ、最大のキーと同じ値にする。
# ★ 管理コンソール（tools/console/backend/local_repo.py）がこの行を ast で読む。`SCHEMA_VERSION = <整数>` の
#   形のまま、このモジュールに置くこと（他のモジュールへ移したり式にしたりすると画面の版比較が読めなくなる）。
SCHEMA_VERSION = 15

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
# まだ終わっていない（受付の上限に数える）文書タスクのステータス。
_QUEUE_ACTIVE_STATUSES = ("pending", "processing", "sending")


def _json_loads_or(text: Any, default: Any) -> Any:
    """JSON 文字列を読み、壊れていたら default を返す（フェイルオープン）。"""
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _user_stats_row_params(user_id: Any, entry: dict[str, Any]) -> tuple[Any, ...]:
    """user_stats の9列を旧JSON取り込み・通常保存・全件置換で同じ値へ変換する。

    列順は user_id, user_name, total_count, hourly, first_seen, last_seen,
    recent_samples, analysis, stats_opt_out。列追加時はこの関数と両方のINSERT文を揃える。
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

    列順は task_id, status, channel_id, message_id, author_id, question, attachment_count,
    created_at, updated_at, finished_at, error。列を足すときはこの関数と2つの INSERT 文を揃える。
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
    """affinity テーブル1行分の保存パラメータ（列順は user_id, affinity, valence, arousal, last_interaction）。

    旧 JSON の取り込み・1行の保存・全件置換の3か所で同じ既定値を使うため、ここに集約する。
    列を足すときはこの関数と3か所の INSERT 文の列を合わせて直すこと。
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


class Database:
    """data/memory/bot.db への読み書きを一手に引き受けるクラス。

    サービス各所が run_in_thread 経由（＝別スレッド）で呼ぶため、
    check_same_thread=False で接続し、自前の threading.Lock で直列化する。
    書き込みは `with self._conn:` のトランザクションで原子的に行う
    （途中でクラッシュしてもファイルが壊れない — 旧 JSON 方式にはなかった利点）。
    """

    def __init__(
        self,
        db_path: str | Path,
        legacy_dir: str | Path | None = None,
        legacy_owner_id: str = "",
    ) -> None:
        self._db_path = Path(db_path)
        # 旧 JSON ファイルの置き場所。省略時は DB と同じディレクトリ（data/memory/）。
        self._legacy_dir = Path(legacy_dir) if legacy_dir is not None else self._db_path.parent
        # ユーザー別記憶への移行時、旧・全体共有データ（profile / pending_log）を
        # 引き継ぐ先の Discord ユーザーID。空文字なら引き継がず破棄する。
        self._legacy_owner_id = str(legacy_owner_id or "")
        self._lock = threading.Lock()
        self._guild_settings_schema_version: int | None = None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # connectのtimeoutがSQLiteのbusy_timeout（ミリ秒）も設定する。
        self._conn = sqlite3.connect(
            self._db_path, timeout=DATABASE_BUSY_TIMEOUT_SECONDS, check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row  # 行に列名でアクセスできるようにする
        # WAL モード: ボット稼働中でも sqlite3 CLI 等から安全に読み取れる
        self._conn.execute("PRAGMA journal_mode=WAL")
        # 旧データの取り込み（#414）。値の変換・ロガー・退避先の連番の上限・オーナーIDは、
        # このモジュールの定義を呼出しのたびに読む（テストが差し替えるため、ここで固定しない）。
        self._legacy = LegacyDataImporter(
            self._conn, self._lock, self._legacy_dir,
            owner_id=lambda: self._legacy_owner_id,
            row_params=LegacyRowParams(
                affinity=_affinity_row_params, user_stats=_user_stats_row_params, queue=_queue_row_params,
            ),
            profile_merge=lambda user_id, profile: self.profile_merge(user_id, profile),
            write_json=lambda path, data: write_json_file(path, data),
            backup_max_suffix=lambda: LEGACY_BACKUP_MAX_SUFFIX,
            get_logger=lambda: logger,
        )

    def initialize(self) -> None:
        """テーブル作成 → スキーマ移行 → 旧行の owner 移行 → 旧ファイル取り込み → profile.json 吸収。起動時に1回呼ぶ。"""
        with self._lock, self._conn:
            # 接続のcontext managerはBEGINを発行しない（commit/rollbackだけを担う）。
            # DDL と PRAGMA は自動ではトランザクションを開始しないため明示する。
            # スキーマと user_version をまとめて確定し、途中失敗時はすべて元へ戻す。
            self._conn.execute("BEGIN IMMEDIATE")
            for statement in _CREATE_TABLE_STATEMENTS:
                self._conn.execute(statement)
            applied_versions = self._apply_migrations()
            self._validate_guild_settings_schema(force=True)
        # 途中失敗で取り消された移行を、成功したように記録しない。
        for version in applied_versions:
            logger.info(f'データベース: スキーマをバージョン {version} へ移行しました。')
        self._legacy.migrate_profile_rows()
        self._legacy.import_files()
        self._legacy.absorb_profile_json()
        with self._lock:
            # 旧テーブルの回収で schema_version が変わった場合も、初期化中に検査を終える。
            self._validate_guild_settings_schema()

    def close(self) -> None:
        """接続を閉じる（終了時に PersonaClient.close から呼ばれる）。

        各サービスは別スレッドから DB を使うため、実行中の読み書きが終わるまで
        ロックで待ってから閉じる。何度呼んでもよい（sqlite3 の close は冪等）。
        """
        with self._lock:
            self._conn.close()

    # ---- スキーマ移行 -------------------------------------------------------

    def _apply_migrations(self) -> list[int]:
        """呼出元の明示トランザクション内で未適用の移行を実行し、変更した版を返す。

        定義と適用の手順は db_migrations にある。ここでは、このモジュールの SCHEMA_VERSION・_MIGRATIONS を
        渡すだけにしている（テストがこれらを差し替えて、段階的な移行や途中失敗を再現するため）。
        """
        return apply_migrations(self._conn, SCHEMA_VERSION, _MIGRATIONS)

    # ---- profile（プロフィール記憶） ------------------------------------------

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

    # ---- guild_settings（サーバーごとの設定） ---------------------------------

    def guild_settings_load_all(self) -> dict[int, dict[str, Any]]:
        """保存済みのギルド設定を {guild_id: {列名: 値}} 形式で全件返す（起動時の復元用）。

        DM の設定は guild_id=0 の行として保存されている（DM にはギルドIDが無いため）。
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM guild_settings").fetchall()
        return self._settings_by_guild(rows)

    @staticmethod
    def _guild_settings_values(row: sqlite3.Row) -> dict[str, Any]:
        """全件復元と再参加時の復元で、保存列の変換を揃える。"""
        return {
            name: bool(row[name]) if kind == "bool" else row[name]
            for name, _, kind, _ in GUILD_SETTINGS_FIELDS
        }

    @classmethod
    def _settings_by_guild(cls, rows: list[sqlite3.Row]) -> dict[int, dict[str, Any]]:
        """guild_settings の全行を {guild_id: {列名: 値}} に変換する（全件読込の共通処理）。"""
        return {row["guild_id"]: cls._guild_settings_values(row) for row in rows}

    @staticmethod
    def _post_key(row: sqlite3.Row) -> str:
        """anniversary_posts の1行を GuildSettings.anniversary_posted_dates のキー（"ユーザーID:呼び名"）にする。"""
        return f"{row['user_id']}:{row['label']}"

    @classmethod
    def _posts_by_guild(cls, rows: list[sqlite3.Row]) -> dict[int, dict[str, str]]:
        """anniversary_posts の全行を {guild_id: {"ユーザーID:呼び名": 投稿日}} に変換する（全件読込の共通処理）。"""
        posted: dict[int, dict[str, str]] = {}
        for row in rows:
            posted.setdefault(row["guild_id"], {})[cls._post_key(row)] = row["posted_date"]
        return posted

    def guild_state_load_all(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, str]]]:
        """起動時の設定と投稿記録を同じロック内で読み出す。"""
        with self._lock:
            settings = self._conn.execute("SELECT * FROM guild_settings").fetchall()
            posts = self._conn.execute("SELECT * FROM anniversary_posts").fetchall()
        return self._settings_by_guild(settings), self._posts_by_guild(posts)

    def guild_state_load(self, guild_id: int) -> tuple[dict[str, Any], dict[str, str]]:
        """1サーバー分の設定と記念日の投稿記録を、同じロック内で読み出す。

        再参加時に全件を走査せず、そのサーバーの主キーで取得する。
        どちらかの読込に失敗した場合は例外を伝え、未復元の設定を使わせない。
        """
        guild_key = int(guild_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_key,)
            ).fetchone()
            posts = self._conn.execute(
                "SELECT user_id, label, posted_date FROM anniversary_posts WHERE guild_id = ?",
                (guild_key,),
            ).fetchall()
        values = self._guild_settings_values(row) if row is not None else {}
        posted = {self._post_key(post): post["posted_date"] for post in posts}
        return values, posted

    def guild_settings_save(self, guild_id: int, **values: Any) -> None:
        """共通定義とスキーマが一致するときだけ設定を丸ごと保存する。"""
        names = [name for name, _, _, _ in GUILD_SETTINGS_FIELDS]
        if set(values) != set(names):
            raise ValueError("guild_settings の保存値が定義と一致しません。")
        converted = []
        for name, _, kind, _ in GUILD_SETTINGS_FIELDS:
            value = values[name]
            if kind == "text":
                value = str(value)
            elif kind == "bool":
                value = int(bool(value))
            elif kind == "channel":
                value = int(value or 0)
            else:
                value = int(value)
            converted.append(value)
        columns = ", ".join(["guild_id", *names])
        placeholders = ", ".join("?" for _ in range(len(names) + 1))
        with self._lock, self._conn:
            # 外部接続のスキーマ変更が検査と置換の間へ割り込まないようにする。
            self._conn.execute("BEGIN IMMEDIATE")
            self._validate_guild_settings_schema()
            self._conn.execute(
                f"INSERT OR REPLACE INTO guild_settings ({columns}) VALUES ({placeholders})",
                (int(guild_id), *converted),
            )

    def _validate_guild_settings_schema(self, *, force: bool = False) -> None:
        """初期化時とスキーマ変更時だけ列を照合する。呼出元でロックを保持する。"""
        schema_version = int(self._conn.execute("PRAGMA schema_version").fetchone()[0])
        if not force and schema_version == self._guild_settings_schema_version:
            return
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(guild_settings)")}
        expected = {"guild_id", *(name for name, _, _, _ in GUILD_SETTINGS_FIELDS)}
        if columns != expected:
            raise ValueError("guild_settings の保存列と DB の列が一致しません。")
        self._guild_settings_schema_version = schema_version

    # ---- anniversary_posts（記念日のお祝い投稿記録） --------------------------

    def anniversary_posts_load_all(self) -> dict[int, dict[str, str]]:
        """記念日の投稿記録を {guild_id: {"ユーザーID:呼び名": "YYYY-MM-DD"}} 形式で返す。

        内側の辞書は GuildSettings.anniversary_posted_dates と同じ形にしてあるため、
        起動時にそのまま代入して復元できる。
        """
        with self._lock:
            rows = self._conn.execute("SELECT * FROM anniversary_posts").fetchall()
        return self._posts_by_guild(rows)

    def anniversary_post_record(self, guild_id: int, user_id: str, label: str, posted_date: str) -> None:
        """お祝いを投稿したことを記録する（同じサーバー・同じ記念日の行は上書き）。

        主キーが (guild_id, user_id, label) なので、毎年お祝いしても行は増えない
        （その記念日の最終投稿日だけが残る）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO anniversary_posts (guild_id, user_id, label, posted_date)"
                " VALUES (?, ?, ?, ?)",
                (int(guild_id), str(user_id), str(label), str(posted_date)),
            )

    # ---- reminders（リマインダー） --------------------------------------------

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
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM reminders WHERE status IN ('done', 'canceled')"
                " AND reminder_id NOT IN ("
                "   SELECT reminder_id FROM reminders WHERE status IN ('done', 'canceled')"
                "   ORDER BY reminder_id DESC LIMIT ?"
                ")",
                (int(max_keep),),
            )

    # ---- feedback（ボット返信への 👍/👎 評価） --------------------------------

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
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM feedback WHERE rowid NOT IN ("
                "   SELECT rowid FROM feedback"
                "   ORDER BY created_at DESC, message_id DESC LIMIT ?"
                ")",
                (int(max_keep),),
            )

    # ---- affinity（親密度・感情） --------------------------------------------

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
                "INSERT INTO affinity"
                " (user_id, affinity, valence, arousal, last_interaction)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " affinity=excluded.affinity, valence=excluded.valence,"
                " arousal=excluded.arousal, last_interaction=excluded.last_interaction",
                _affinity_row_params(user_id, entry),
            )

    def affinity_save_all(self, cache: dict[str, dict[str, Any]]) -> None:
        """明示的な全件置換用。通常の感情更新には affinity_upsert を使う。"""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM affinity")
            for user_id, entry in cache.items():
                self._conn.execute(
                    "INSERT INTO affinity"
                    " (user_id, affinity, valence, arousal, last_interaction)"
                    " VALUES (?, ?, ?, ?, ?)",
                    _affinity_row_params(user_id, entry),
                )

    # ---- user_stats（会話統計） ----------------------------------------------

    def stats_load_all(self) -> dict[str, dict[str, Any]]:
        """全ユーザーの統計を {user_id: {...}} 形式で返す（旧 JSON と同じ形）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM user_stats").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            result[row["user_id"]] = {
                "user_name": row["user_name"],
                "total_count": row["total_count"],
                "hourly": _json_loads_or(row["hourly"], [0] * STATS_HOURLY_BUCKETS),
                "first_seen": row["first_seen"],
                "last_seen": row["last_seen"],
                "recent_samples": _json_loads_or(row["recent_samples"], []),
                "analysis": _json_loads_or(row["analysis"], None),
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
            "INSERT INTO user_stats"
            " (user_id, user_name, total_count, hourly, first_seen, last_seen,"
            "  recent_samples, analysis, stats_opt_out)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET"
            " user_name = excluded.user_name, total_count = excluded.total_count,"
            " hourly = excluded.hourly, first_seen = excluded.first_seen,"
            " last_seen = excluded.last_seen, recent_samples = excluded.recent_samples,"
            " analysis = excluded.analysis, stats_opt_out = excluded.stats_opt_out",
            _user_stats_row_params(user_id, entry),
        )

    # ---- queue_tasks（文書処理キュー） ----------------------------------------

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
            "INSERT OR REPLACE INTO queue_tasks"
            " (task_id, status, channel_id, message_id, author_id, question,"
            "  attachment_count, created_at, updated_at, finished_at, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _queue_row_params(task),
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
        with self._lock, self._conn:
            self._conn.execute(
                """DELETE FROM queue_tasks
                   WHERE status IN ('completed', 'failed')
                     AND task_id NOT IN (
                         SELECT task_id FROM queue_tasks
                         WHERE status IN ('completed', 'failed')
                         ORDER BY created_at DESC, task_id DESC
                         LIMIT ?
                     )""",
                (int(keep_count),),
            )

    # ---- pending_log（記憶圧縮前の一時ログ） ----------------------------------

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
