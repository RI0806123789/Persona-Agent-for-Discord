"""SQLite データベース層（data/memory/bot.db）。

以下の10テーブルを SQLite で管理する。SQL 文はすべてこのモジュールに集約する。
旧 JSON / テキストファイルから移したデータと、その後追加した機能のデータを持つ。

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
SCHEMA_VERSION を 1 つ上げ、_MIGRATIONS にそのバージョン番号で変更用の SQL 文を
追記する。対応する読み込み・保存処理も更新する。
適用済みバージョンは DB 内の PRAGMA user_version に記録され、
起動時に未適用の分だけが順番に実行される（二重適用されない）。
_CREATE_TABLE_STATEMENTS は初版（バージョン1）の形のまま変更しないこと —
列の追加・変更は必ず _MIGRATIONS 側で行う（新規 DB にも同じ移行が走るため、
両方を書き換えると二重適用でエラーになる）。
"""

import hashlib
import json
import logging
import re
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
from persona_bot.core.guild_settings_fields import GUILD_SETTINGS_FIELDS
from persona_bot.utils.storage import write_json_file


logger = logging.getLogger(__name__)

# 現在のスキーマバージョン。スキーマを変更したら 1 つ上げる。
SCHEMA_VERSION = 15

# バージョン番号 → そのバージョンへ上げるための SQL 文リスト。
# 例) user_stats に列を追加したくなったら:
#   SCHEMA_VERSION を現在値から 1 つ上げ、その新しい番号をキーとして
#   ["ALTER TABLE user_stats ADD COLUMN favorite_emoji TEXT NOT NULL DEFAULT ''"]
#   を追記する。既存 DB・新規 DB の両方に自動適用される。
_MIGRATIONS: dict[int, list[str]] = {
    # バージョン2: プロフィール記憶を profile.json から DB へ移行
    2: [
        """CREATE TABLE IF NOT EXISTS profile (
            key   TEXT PRIMARY KEY,                    -- プロフィールのトップレベルキー
            value TEXT NOT NULL                        -- 値（JSON文字列）
        )"""
    ],
    # バージョン3: 記憶をユーザー別に分離する。
    #   - profile: 主キーを (user_id, key) の複合主キーに変更（テーブルを作り直す）。
    #     旧・全体共有の行は移行後に _migrate_legacy_profile_rows で owner へ割り当てる。
    #   - pending_log: user_id 列を追加（旧・全体共有の行は owner へ振り直す）。
    3: [
        "ALTER TABLE pending_log ADD COLUMN user_id TEXT NOT NULL DEFAULT ''",
        # profile は主キーを変えるため、旧テーブルを退避 → 新テーブルを作成する。
        # 退避テーブルの行は _migrate_legacy_profile_rows() が owner へ移して破棄する。
        "ALTER TABLE profile RENAME TO profile_legacy_v2",
        """CREATE TABLE profile (
            user_id TEXT NOT NULL,                     -- Discord ユーザーID（文字列）
            key     TEXT NOT NULL,                     -- プロフィールのトップレベルキー
            value   TEXT NOT NULL,                     -- 値（JSON文字列）
            PRIMARY KEY (user_id, key)
        )""",
    ],
    # バージョン4: サーバー（ギルド）ごとの設定と記念日の投稿記録を永続化する。
    #   これらは従来インメモリ（GuildSettings）だけに持っていたため再起動で失われていた。
    #   なお会話リセット地点・自発会話の監視チャンネルは「実行時に自然に再構築される
    #   一時状態」のため永続化しない（インメモリのまま）。
    4: [
        # DEFAULT 句の値は「列を追加した時点の埋め草」でしかない（保存時は常に全列を
        # 書き込む）。実際の既定値は constants.py の DEFAULT_* が唯一の情報源。
        """CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id                  INTEGER PRIMARY KEY,        -- Discord サーバーID（DM は 0）
            prompt_file               TEXT    NOT NULL DEFAULT '',-- ペルソナのプロンプトファイル
            model_name                TEXT    NOT NULL DEFAULT '',-- Gemini のモデル名
            respond_mode              TEXT    NOT NULL DEFAULT '',-- 応答モード（全発言/メンションのみ）
            grounding_enabled         INTEGER NOT NULL DEFAULT 1, -- Google検索グラウンディング（0/1）
            auto_topic_enabled        INTEGER NOT NULL DEFAULT 0, -- 自発的な会話の有効/無効（0/1）
            auto_topic_interval_hours INTEGER NOT NULL DEFAULT 6  -- 自発的な会話の無会話しきい値（時間）
        )""",
        """CREATE TABLE IF NOT EXISTS anniversary_posts (
            guild_id    INTEGER NOT NULL,              -- お祝いを投稿したサーバーID
            user_id     TEXT    NOT NULL,              -- お祝い対象の Discord ユーザーID
            label       TEXT    NOT NULL,              -- 記念日の呼び名（例: 誕生日）
            posted_date TEXT    NOT NULL,              -- 最後に投稿した日付（YYYY-MM-DD）
            PRIMARY KEY (guild_id, user_id, label)
        )""",
    ],
    # バージョン5: リマインダー（会話から自動登録し、時刻が来たら通知する）を追加。
    5: [
        """CREATE TABLE IF NOT EXISTS reminders (
            reminder_id INTEGER PRIMARY KEY AUTOINCREMENT, -- 登録順を保つ連番
            user_id     TEXT    NOT NULL,                  -- 依頼した Discord ユーザーID
            user_name   TEXT    NOT NULL DEFAULT '',       -- 依頼時の表示名（一覧表示用）
            guild_id    INTEGER NOT NULL DEFAULT 0,        -- 依頼されたサーバーID（DM は 0）
            channel_id  INTEGER NOT NULL,                  -- 通知を投稿するチャンネルID
            message     TEXT    NOT NULL,                  -- 通知する本文
            remind_at   TEXT    NOT NULL,                  -- 通知予定時刻（UTC の ISO 8601）
            created_at  TEXT    NOT NULL,                  -- 登録時刻（UTC の ISO 8601）
            status      TEXT    NOT NULL DEFAULT 'pending' -- pending/done/canceled
        )""",
        # 見回り（status と remind_at での絞り込み）が主なアクセスパターンのため索引を張る。
        "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, remind_at)",
    ],
    # バージョン6: リマインダーの繰り返し（毎日/毎週/毎月）に対応する。
    #   - recurrence        … 繰り返し種別。'none' は従来どおりの単発。
    #   - recurrence_anchor … 初回の通知予定時刻（UTC の ISO 8601）。毎月の繰り返しで
    #                         「31日 → 2月は28日 → 3月は31日」と元の日付へ戻すための基準。
    #                         既存行は空文字のままでよい（recurrence='none' のため使わない）。
    # DEFAULT 句により既存のリマインダーはすべて 'none' になり、挙動は完全に後方互換。
    # 索引 idx_reminders_due は変更不要（繰り返し行も pending のまま同じ索引に乗る）。
    6: [
        "ALTER TABLE reminders ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none'",
        "ALTER TABLE reminders ADD COLUMN recurrence_anchor TEXT NOT NULL DEFAULT ''",
    ],
    # バージョン7: ボット返信への 👍/👎 フィードバックを記録する。
    #   応答品質のチューニングとモデル選定の判断材料にするため、評価そのものに加えて
    #   「どのモデル・どのペルソナの返信だったか」も一緒に残す。
    #   同じ人が同じ返信へ何度も投票できないよう (message_id, user_id) を主キーにする
    #   （押し替えは INSERT OR REPLACE で上書きされる）。
    7: [
        """CREATE TABLE IF NOT EXISTS feedback (
            message_id     INTEGER NOT NULL,              -- 評価対象のボット返信メッセージID
            user_id        TEXT    NOT NULL,              -- 評価した人の Discord ユーザーID
            guild_id       INTEGER NOT NULL DEFAULT 0,    -- 返信が投稿されたサーバーID（DM は 0）
            channel_id     INTEGER NOT NULL DEFAULT 0,    -- 返信が投稿されたチャンネルID
            target_user_id TEXT    NOT NULL DEFAULT '',   -- その返信の宛先（会話相手）のユーザーID
            model_name     TEXT    NOT NULL DEFAULT '',   -- 返信の生成に使った Gemini モデル
            prompt_file    TEXT    NOT NULL DEFAULT '',   -- 返信時のペルソナ（プロンプトファイル）
            rating         INTEGER NOT NULL,              -- 1 = 👍 / -1 = 👎
            created_at     TEXT    NOT NULL,              -- 記録時刻（UTC の ISO 8601）
            PRIMARY KEY (message_id, user_id)
        )""",
        # 集計はサーバー単位・新しい順で行うため索引を張る。
        "CREATE INDEX IF NOT EXISTS idx_feedback_guild ON feedback (guild_id, created_at)",
    ],
    # バージョン8: 会話統計の収集を本人が停止できるようにする（opt-out）。
    #   0 = 収集する（従来どおり）/ 1 = 収集しない。
    #   既存行はすべて 0 になるため、これまでの利用者の挙動は変わらない。
    8: [
        "ALTER TABLE user_stats ADD COLUMN stats_opt_out INTEGER NOT NULL DEFAULT 0",
    ],
    # バージョン9: 自律行動（ボットが自分からボイス/ステージへ出て配信する）の有効/無効。
    #   0 = 無効（既定）/ 1 = 有効。既存行はすべて 0 になるため挙動は後方互換。
    #   「話したいゲージ」が満タンになるまでの時間は auto_topic_interval_hours を
    #   共用するため、この機能で増える列はこの1つだけ。
    #   配信中かどうか・ゲージの現在値は実行時状態なので永続化しない
    #   （ボイス接続自体が再起動をまたげないため、復元しても意味がない）。
    9: [
        "ALTER TABLE guild_settings ADD COLUMN autonomous_enabled INTEGER NOT NULL DEFAULT 0",
    ],
    # バージョン10: リマインダーのユーザー別の操作（登録時の上限確認・一覧・件数）用の索引。
    #   これらは WHERE user_id = ? AND status = 'pending'（一覧は ORDER BY remind_at, reminder_id）
    #   で引くが、既存の idx_reminders_due は status が先頭のため、全ユーザーの pending 行を
    #   走査していた。索引の末尾には rowid（= reminder_id）が暗黙に並ぶため、一覧の並び替えも
    #   索引の順序だけで済む。見回り（reminder_claim_due）は引き続き idx_reminders_due を使う。
    10: [
        "CREATE INDEX IF NOT EXISTS idx_reminders_user_status ON reminders (user_id, status, remind_at)",
    ],
    # バージョン11: 改名失敗後も旧ログの取り込みを繰り返さないための記録。
    11: [
        """CREATE TABLE legacy_imports (
            source_name  TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            PRIMARY KEY (source_name, content_hash)
        )""",
    ],
    # バージョン12: 全サーバーの古い評価を削除する際の一時ソートを避ける索引。
    #   ORDER BY created_at DESC, message_id DESC と同じ降順で前向きに走査する。
    #   両列が同じ票は末尾の暗黙のrowidが昇順となり、従来の同順位の順序も保つ。
    12: [
        "CREATE INDEX IF NOT EXISTS idx_feedback_created_message ON feedback (created_at DESC, message_id DESC)",
    ],
    # バージョン13: 記憶圧縮の巡回が利用者ごとに行う pending_log の取得・削除で、全件走査を避ける索引。
    #   WHERE user_id = ? ORDER BY id / WHERE user_id = ? AND id <= ? を、並べ替えなしの索引検索にする。
    13: [
        "CREATE INDEX IF NOT EXISTS idx_pending_log_user_id ON pending_log (user_id, id)",
    ],
    # バージョン14: 記念日巡回のキー別取得で、無関係なプロフィールの全件走査を避ける。
    #   ユーザー別の取得・更新には従来の (user_id, key) 主キーを引き続き使う。
    14: [
        "CREATE INDEX IF NOT EXISTS idx_profile_key_user ON profile (key, user_id)",
    ],
    # バージョン15: 自律行動の配信先を管理者が指定する（#266）。
    #   ステージ・ボイスそれぞれのチャンネルID。0 = 指定なし（従来どおり自動で選ぶ）。
    #   既存行はすべて 0 になるため挙動は後方互換。
    15: [
        "ALTER TABLE guild_settings ADD COLUMN autonomous_stage_channel_id INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE guild_settings ADD COLUMN autonomous_voice_channel_id INTEGER NOT NULL DEFAULT 0",
    ],
}

# 初版（バージョン1）のテーブル定義。ここは変更禁止（変更は _MIGRATIONS で）。
_CREATE_TABLE_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS affinity (
        user_id          TEXT PRIMARY KEY,               -- Discord ユーザーID（文字列）
        affinity         INTEGER NOT NULL DEFAULT 50,    -- 親密度 0〜100
        valence          INTEGER NOT NULL DEFAULT 50,    -- 機嫌 0〜100
        arousal          INTEGER NOT NULL DEFAULT 50,    -- テンション 0〜100
        last_interaction REAL    NOT NULL DEFAULT 0      -- 最終対話時刻（UNIX秒）
    )""",
    """CREATE TABLE IF NOT EXISTS user_stats (
        user_id        TEXT PRIMARY KEY,                 -- Discord ユーザーID（文字列）
        user_name      TEXT NOT NULL DEFAULT '',
        total_count    INTEGER NOT NULL DEFAULT 0,       -- 累計発言回数
        hourly         TEXT NOT NULL DEFAULT '[]',       -- 時間帯別回数（24要素のJSON配列）
        first_seen     TEXT NOT NULL DEFAULT '',         -- ISO 8601
        last_seen      TEXT NOT NULL DEFAULT '',         -- ISO 8601
        recent_samples TEXT NOT NULL DEFAULT '[]',       -- 直近発言サンプル（JSON配列）
        analysis       TEXT                              -- LLM解析結果のJSON（未解析はNULL）
    )""",
    """CREATE TABLE IF NOT EXISTS queue_tasks (
        task_id          TEXT PRIMARY KEY,
        status           TEXT NOT NULL DEFAULT 'pending', -- pending/processing/completed/failed
        channel_id       INTEGER NOT NULL DEFAULT 0,
        message_id       INTEGER NOT NULL DEFAULT 0,
        author_id        INTEGER NOT NULL DEFAULT 0,
        question         TEXT NOT NULL DEFAULT '',
        attachment_count INTEGER NOT NULL DEFAULT 0,
        created_at       TEXT NOT NULL DEFAULT '',        -- ISO 8601
        updated_at       TEXT NOT NULL DEFAULT '',        -- ISO 8601
        finished_at      TEXT,                            -- 終了時刻（未終了はNULL）
        error            TEXT                             -- 失敗理由（成功時はNULL）
    )""",
    """CREATE TABLE IF NOT EXISTS pending_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,     -- 追記順を保つ連番
        created_at TEXT NOT NULL,                         -- 記録時刻（表示用文字列）
        question   TEXT NOT NULL,                         -- ユーザーの発言
        response   TEXT NOT NULL                          -- ボットの応答
    )""",
]
# 注: pending_log.user_id 列はバージョン3の移行(_MIGRATIONS[3])で追加される。
# 初版テーブル定義（上）は変更禁止のため、ここには含めない。

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
    return (
        str(user_id),
        str(entry.get("user_name") or ""),
        int(entry.get("total_count") or 0),
        json.dumps(entry.get("hourly") or [0] * STATS_HOURLY_BUCKETS, ensure_ascii=False),
        str(entry.get("first_seen") or ""),
        str(entry.get("last_seen") or ""),
        json.dumps(entry.get("recent_samples") or [], ensure_ascii=False),
        json.dumps(analysis, ensure_ascii=False) if analysis else None,
        int(bool(entry.get("opt_out", entry.get("stats_opt_out", STATS_OPT_OUT_DEFAULT)))),
    )


def _load_legacy_json_object(path: Path) -> dict[str, Any]:
    """旧 JSON ファイルを読み、最上位が object でなければ例外にする。

    _json_loads_or のように壊れたファイルを {} として扱うと、取り込みが「成功」して
    .imported.bak へ改名され、データを取り込めなかったことに誰も気づけない。
    例外にすれば _import_legacy_files が元のファイルを残して警告し、次回起動時に再試行する。
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"最上位が object ではありません（{type(data).__name__}）")
    return data


def _free_backup_path(path: Path) -> Path:
    """旧ファイルの退避先を、既存のファイルと衝突しない名前で返す（#379）。

    通常は「<元の名前>.imported.bak」。すでに同名のファイルがあるときは、
    「<元の名前>.imported.<連番>.bak」の空いている最小の番号にする。
    Path.replace は既存のファイルを黙って置き換えるため、名前を決めずに改名すると、
    前回の退避ファイルが消える。特に引き継ぎ先が無いときの pending_log.txt は
    DB へ取り込まれず、退避ファイルが唯一のコピーになる。
    空きが見つからなければ FileExistsError にする（呼び出し側が取り込みごと見送り、
    元のファイルを残して警告する）。
    """
    candidates = [path.with_name(path.name + ".imported.bak")]
    candidates += [
        path.with_name(f"{path.name}.imported.{number}.bak")
        for number in range(1, LEGACY_BACKUP_MAX_SUFFIX + 1)
    ]
    for candidate in candidates:
        # 壊れたシンボリックリンクは exists() が偽になるため、is_symlink() も見る
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise FileExistsError(f"退避先の名前が空いていません（{path.name}）")


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
        value_or("last_interaction", 0.0),
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
        self._migrate_legacy_profile_rows()
        self._import_legacy_files()
        self._absorb_profile_json()
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
        """呼出元の明示トランザクション内で未適用の移行を実行し、変更した版を返す。"""
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        applied_versions = []
        for version in range(current + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, []):
                self._conn.execute(statement)
            # user_version はプレースホルダ(?)が使えないため f-string で埋める。
            # version は range() 由来の int なのでインジェクションの心配はない。
            self._conn.execute(f"PRAGMA user_version = {version}")
            if _MIGRATIONS.get(version):
                applied_versions.append(version)
        return applied_versions

    def _migrate_legacy_profile_rows(self) -> None:
        """ユーザー別記憶への移行の後始末（冪等）。

        バージョン3の移行で退避した profile_legacy_v2（旧・全体共有プロフィール）の
        行を owner の user_id に割り当てて新 profile へ移し、退避テーブルを削除する。
        あわせて pending_log の user_id 未設定行（旧・全体共有）も owner へ振り直す。

        スキーマ移行本体（_apply_migrations）とは別トランザクションで走るため、
        途中でクラッシュしても次回起動時にこのメソッドが再実行されて完了する
        （テーブルの有無・行の user_id で判定するため二重実行されても安全）。
        owner が空文字の場合は引き継ぎ元を破棄する（旧データを捨てる）。
        """
        owner = self._legacy_owner_id
        with self._lock, self._conn:
            legacy_exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='profile_legacy_v2'"
            ).fetchone()
            if legacy_exists is not None:
                if owner:
                    # 既に owner に同名キーがあれば旧値で上書きしない（新 DB 側を正とする）
                    self._conn.execute(
                        "INSERT OR IGNORE INTO profile (user_id, key, value)"
                        " SELECT ?, key, value FROM profile_legacy_v2",
                        (owner,),
                    )
                    logger.info('データベース: 旧・全体共有プロフィールを user_id=<ID> へ移行しました。')
                self._conn.execute("DROP TABLE profile_legacy_v2")
            if owner:
                cursor = self._conn.execute(
                    "UPDATE pending_log SET user_id = ? WHERE user_id = ''", (owner,)
                )
                if cursor.rowcount:
                    logger.info(f'データベース: pending_log の未割当 {cursor.rowcount} 件を user_id=<ID> へ振り直しました。')
            else:
                # 引き継ぎ先が無いときは、旧・全体共有の会話ログも破棄する（profile と同じ方針）。
                # 残すと user_id='' の「誰でもない利用者」として記憶圧縮の対象に数えられ、
                # 誰も読まない記憶を作るために Gemini を呼び続ける。
                cursor = self._conn.execute("DELETE FROM pending_log WHERE user_id = ''")
                if cursor.rowcount:
                    logger.warning(f'データベース: 引き継ぎ先が無いため、pending_log の未割当 {cursor.rowcount} 件を破棄しました。')

    # ---- 旧 JSON / テキストファイルの取り込み --------------------------------

    def _import_legacy_files(self) -> None:
        importers = [
            ("affinity.json", self._import_affinity_json),
            ("user_stats.json", self._import_user_stats_json),
            ("queue.json", self._import_queue_json),
            ("pending_log.txt", self._import_pending_log_txt),
        ]
        for filename, importer in importers:
            path = self._legacy_dir / filename
            if not path.exists():
                continue
            try:
                # 退避先は取り込みの前に決める。空きが無ければ取り込まずに元のファイルを残す。
                # 起動処理は単一のスレッドで動くため、決めてから改名するまでに同名は作られない。
                backup_path = _free_backup_path(path)
                importer(path)
                path.replace(backup_path)
                logger.info(f'データベース: {filename} を取り込み、{backup_path.name} に改名しました。')
            except Exception as error:
                # 取り込み失敗でも起動は続ける（ファイルが残るので次回起動時に再試行される）
                logger.error(f'データベース: {filename} の取り込みに失敗しました: {type(error).__name__}', exc_info=error)

    def _import_affinity_json(self, path: Path) -> None:
        data = _load_legacy_json_object(path)
        with self._lock, self._conn:
            for user_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                # 既に DB にあるユーザーは上書きしない（DB 側を正とする）
                self._conn.execute(
                    "INSERT OR IGNORE INTO affinity"
                    " (user_id, affinity, valence, arousal, last_interaction)"
                    " VALUES (?, ?, ?, ?, ?)",
                    _affinity_row_params(user_id, entry),
                )

    def _import_user_stats_json(self, path: Path) -> None:
        data = _load_legacy_json_object(path)
        with self._lock, self._conn:
            for user_id, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_stats"
                    " (user_id, user_name, total_count, hourly, first_seen, last_seen,"
                    "  recent_samples, analysis, stats_opt_out)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _user_stats_row_params(user_id, entry),
                )

    def _import_queue_json(self, path: Path) -> None:
        tasks = _load_legacy_json_object(path).get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError("tasks が配列ではありません")
        with self._lock, self._conn:
            for task in tasks:
                if not isinstance(task, dict) or not task.get("task_id"):
                    continue
                self._conn.execute(
                    "INSERT OR IGNORE INTO queue_tasks"
                    " (task_id, status, channel_id, message_id, author_id, question,"
                    "  attachment_count, created_at, updated_at, finished_at, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(task.get("task_id")),
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
                    ),
                )

    def _import_pending_log_txt(self, path: Path) -> None:
        """旧 pending_log.txt（[時刻] User: … / [時刻] Bot: … 形式）を行単位で復元する。"""
        # 読み取った同じバイト列からハッシュと会話を作り、別内容の取り違えを防ぐ。
        source = path.read_bytes()
        content_hash = hashlib.sha256(source).hexdigest()
        text = source.decode("utf-8-sig")
        if not text.strip():
            return
        entries: list[dict[str, str]] = []
        current: dict[str, str] | None = None
        bot_seen = False
        for line in text.splitlines():
            user_match = re.match(r"^\[(.+?)\] User: (.*)$", line)
            bot_match = re.match(r"^\[(.+?)\] Bot: (.*)$", line)
            if user_match:
                if current is not None:
                    entries.append(current)
                current = {"created_at": user_match[1], "question": user_match[2], "response": ""}
                bot_seen = False
            elif bot_match and current is not None:
                current["response"] = bot_match[2]
                bot_seen = True
            elif current is not None:
                # 発言が複数行だった場合の続きの行。直前に書いていた側へ連結する。
                key = "response" if bot_seen else "question"
                current[key] += "\n" + line
        if current is not None:
            entries.append(current)

        # 旧 pending_log.txt はユーザー別記憶の導入前のもの＝全体共有。owner に紐付ける。
        owner = self._legacy_owner_id
        if not owner:
            # 引き継ぎ先が無ければ取り込まない（_migrate_legacy_profile_rows と同じく破棄する）。
            # ファイルは呼び出し元が .imported.bak へ改名して残すため、内容は失われない。
            if entries:
                logger.info(f'データベース: 引き継ぎ先が無いため、pending_log.txt の {len(entries)} 件は取り込みません。')
            return
        with self._lock, self._conn:
            imported = self._conn.execute(
                "SELECT 1 FROM legacy_imports WHERE source_name = ? AND content_hash = ?",
                (path.name, content_hash),
            ).fetchone()
            if imported:
                return
            for entry in entries:
                self._conn.execute(
                    "INSERT INTO pending_log (user_id, created_at, question, response) VALUES (?, ?, ?, ?)",
                    (owner, entry["created_at"], entry["question"], entry["response"]),
                )
            # INSERTと同じトランザクションで確定する。失敗時は両方とも再試行できる。
            self._conn.execute(
                "INSERT INTO legacy_imports (source_name, content_hash) VALUES (?, ?)",
                (path.name, content_hash),
            )

    def _absorb_profile_json(self) -> None:
        """profile.json に書き足された内容を profile テーブルへ吸収する（毎起動時）。

        ユーザー別記憶に対応するため、形式は {ユーザーID: {キー: 値, ...}, ...}。
        各ユーザーごとにトップレベルキー単位でマージ（同名キーはファイル側が勝つ）し、
        ファイルを {} に戻す。数字のユーザーIDと dict の組でないもの（旧・全体共有形式の
        手書き）は owner のプロフィールとして取り込む（後方互換）。
        JSON として読めない場合はファイルを壊さず残し、警告だけ出してスキップする
        （手書きミスでデータを失わないため）。
        """
        path = self._legacy_dir / "profile.json"
        if not path.exists():
            return
        try:
            raw_text = path.read_text(encoding="utf-8-sig").strip()
            if not raw_text or raw_text == "{}":
                return
            data = json.loads(raw_text)
            if not isinstance(data, dict) or not data:
                return

            # 全キーがASCII数字のID、全値がdictのときだけユーザー別形式とみなす。
            # 旧形式では値がすべてdictのこともあるため、名前や全角数字をIDにしない。
            looks_per_user = all(
                user_id.isascii() and user_id.isdigit() and isinstance(value, dict)
                for user_id, value in data.items()
            )
            absorbed: dict[str, list[str]] = {}
            if looks_per_user:
                for user_id, profile in data.items():
                    if isinstance(profile, dict) and profile:
                        self.profile_merge(str(user_id), profile)
                        absorbed[str(user_id)] = list(profile.keys())
            elif self._legacy_owner_id:
                self.profile_merge(self._legacy_owner_id, data)
                absorbed[self._legacy_owner_id] = list(data.keys())

            if not absorbed:
                logger.warning('データベース: profile.json に取り込めるプロフィールがありません。ユーザー別形式と旧形式の引継ぎ先を確認してください。ファイルを保持します。')
                return

            write_json_file(path, {})
            logger.info(f'データベース: profile.json から {len(absorbed)} 人分・{sum((len(keys) for keys in absorbed.values()))} 項目を profile テーブルへ追加しました。')
        except json.JSONDecodeError as error:
            logger.error(f'データベース: profile.json が JSON として読めないため取り込みをスキップします: {type(error).__name__}', exc_info=error)
        except Exception as error:
            logger.error(f'データベース: profile.json の取り込みに失敗しました: {type(error).__name__}', exc_info=error)

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
        return {row["guild_id"]: self._guild_settings_values(row) for row in rows}

    @staticmethod
    def _guild_settings_values(row: sqlite3.Row) -> dict[str, Any]:
        """全件復元と再参加時の復元で、保存列の変換を揃える。"""
        return {
            name: bool(row[name]) if kind == "bool" else row[name]
            for name, _, kind, _ in GUILD_SETTINGS_FIELDS
        }

    def guild_state_load_all(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, str]]]:
        """起動時の設定と投稿記録を同じロック内で読み出す。"""
        with self._lock:
            settings = self._conn.execute("SELECT * FROM guild_settings").fetchall()
            posts = self._conn.execute("SELECT * FROM anniversary_posts").fetchall()
        values = {row["guild_id"]: self._guild_settings_values(row) for row in settings}
        posted: dict[int, dict[str, str]] = {}
        for row in posts:
            posted.setdefault(row["guild_id"], {})[f"{row['user_id']}:{row['label']}"] = row["posted_date"]
        return values, posted

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
        posted = {f"{post['user_id']}:{post['label']}": post["posted_date"] for post in posts}
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
        result: dict[int, dict[str, str]] = {}
        for row in rows:
            result.setdefault(row["guild_id"], {})[f"{row['user_id']}:{row['label']}"] = row["posted_date"]
        return result

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
            (
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
            ),
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
