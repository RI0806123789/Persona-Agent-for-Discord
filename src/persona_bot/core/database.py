"""SQLite データベース層（data/memory/bot.db）。

以下の10テーブルを SQLite で管理する。データの読み書きの SQL 文は `db_*.py` に集約している（下記）。
旧 JSON / テキストファイルから移したデータと、その後追加した機能のデータを持つ。

`Database` は窓口（ファサード）で、接続・ロック・初期化・終了だけを持つ。責務は次のモジュールへ分けている。
  - db_migrations.py        … スキーマの定義（初版のテーブル作成・バージョンごとの変更）と、その適用
  - db_legacy_importers.py  … 旧ファイルの読み取り・解釈と、旧データの DB への取り込み（#414）
  - db_rows.py              … 列名の一覧・INSERT 文の生成・行の変換・JSON 列の読み取り（#468）
  - db_memory.py / db_guild.py / db_reminders.py / db_feedback.py / db_usage.py / db_queue.py
                            … テーブル群ごとの読み書き（`Database` が継承するミックスイン。#468）
`db_base.py` のミックスインの土台が、接続（`_conn`）・ロック（`_lock`）と prune の共通処理を持つ。
外から見たメソッド名・引数・挙動は分割前と同じ。SQL の実体は `db_*.py` にあり、新しい列・テーブルを
足すときは、対応する `db_*.py` と `db_rows.py` の列定義・変換をそろえて直す。
旧データの取り込み（`LegacyDataImporter`）は、このモジュールの接続・ロックを共有し、ログの出力元
（`persona_bot.core.database`）・退避先の連番の上限（`LEGACY_BACKUP_MAX_SUFFIX`）・取り込みの値の変換
（`_affinity_row_params` など。通常の保存と共有する）を使う。

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

import logging
import sqlite3
import threading
from pathlib import Path

from persona_bot.config.constants import (
    DATABASE_BUSY_TIMEOUT_SECONDS,
    LEGACY_BACKUP_MAX_SUFFIX,
)
from persona_bot.core.db_feedback import FeedbackDbMixin
from persona_bot.core.db_guild import GuildDbMixin
from persona_bot.core.db_legacy_importers import LegacyDataImporter, LegacyRowParams
from persona_bot.core.db_memory import MemoryDbMixin
# テストと既存の呼び出しが `database._MIGRATIONS` / `database._CREATE_TABLE_STATEMENTS` で参照・差し替えるため、
# 先頭にアンダースコアを付けた別名で持つ（同じオブジェクトなので、patch.dict の変更は定義側にも届く）。
from persona_bot.core.db_migrations import (
    CREATE_TABLE_STATEMENTS as _CREATE_TABLE_STATEMENTS,
    MIGRATIONS as _MIGRATIONS,
    apply_migrations,
)
from persona_bot.core.db_queue import QueueDbMixin
from persona_bot.core.db_reminders import ReminderDbMixin
# 列定義と行の変換は db_rows.py が持つ。旧データの取り込みへ渡す・テストが database.py から読むため、同じ名前で持つ。
from persona_bot.core.db_rows import (  # noqa: F401
    _AFFINITY_COLUMNS,
    _QUEUE_ACTIVE_STATUSES,
    _QUEUE_COLUMNS,
    _USER_STATS_COLUMNS,
    _affinity_row_params,
    _insert_sql,
    _json_loads_or,
    _queue_row_params,
    _user_stats_row_params,
)
from persona_bot.core.db_usage import UsageDbMixin
from persona_bot.utils.storage import write_json_file


logger = logging.getLogger(__name__)

# 現在のスキーマバージョン。db_migrations.MIGRATIONS に版を足したら 1 つ上げ、最大のキーと同じ値にする。
# ★ 管理コンソール（tools/console/backend/local_repo.py）がこの行を ast で読む。`SCHEMA_VERSION = <整数>` の
#   形のまま、このモジュールに置くこと（他のモジュールへ移したり式にしたりすると画面の版比較が読めなくなる）。
SCHEMA_VERSION = 15


class Database(
    MemoryDbMixin, GuildDbMixin, ReminderDbMixin, FeedbackDbMixin, UsageDbMixin, QueueDbMixin,
):
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
                affinity_sql=_insert_sql("affinity", _AFFINITY_COLUMNS, verb="INSERT OR IGNORE"),
                user_stats_sql=_insert_sql("user_stats", _USER_STATS_COLUMNS, verb="INSERT OR IGNORE"),
                queue_sql=_insert_sql("queue_tasks", _QUEUE_COLUMNS, verb="INSERT OR IGNORE"),
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

    def _apply_migrations(self) -> list[int]:
        """呼出元の明示トランザクション内で未適用の移行を実行し、変更した版を返す。

        定義と適用の手順は db_migrations にある。ここでは、このモジュールの SCHEMA_VERSION・_MIGRATIONS を
        渡すだけにしている（テストがこれらを差し替えて、段階的な移行や途中失敗を再現するため）。
        """
        return apply_migrations(self._conn, SCHEMA_VERSION, _MIGRATIONS)
