"""SQLite のスキーマ移行の定義（data/memory/bot.db）。

`Database`（`database.py`）から、スキーマの定義と移行の適用だけを切り出したモジュール。
ここには SQL 文のうち「テーブルの形」を決めるもの（初版のテーブル作成と、バージョンごとの変更）と、
それを未適用の分だけ実行する `apply_migrations` を置く。データの読み書きの SQL は `database.py` に残る。

■ スキーマを後から変更したくなったら（列の追加など）
1. `MIGRATIONS` にそのバージョン番号で変更用の SQL 文を追記する。
2. `database.py` の `SCHEMA_VERSION` を 1 つ上げる（`MIGRATIONS` の最大のキーと同じ値にする）。
3. 対応する読み込み・保存処理も更新する。
`CREATE_TABLE_STATEMENTS` は初版（バージョン1）の形のまま変更しないこと — 列の追加・変更は
必ず `MIGRATIONS` 側で行う（新規 DB にも同じ移行が走るため、両方を書き換えると二重適用でエラーになる）。

★ `SCHEMA_VERSION` を `database.py` に置いたままにしている理由: 管理コンソールが
  `tools/console/backend/local_repo.py` で `database.py` の `SCHEMA_VERSION = <整数>` を ast で読み、
  本番の bot.db の版と比べる。このモジュールへ移すと、画面の「新しい版が未デプロイ」の判定が
  読めなくなる。2か所の食い違いは `tests/core/test_db_modules.py` が検出する。
★ 適用は `Database` が呼び出し元で `BEGIN IMMEDIATE` を開いた明示トランザクションの中で行う
  （DDL は接続のコンテキストマネージャーだけではトランザクションが始まらないため）。
  `apply_migrations` 自身はトランザクションを開始・確定しない。
"""

import sqlite3
from collections.abc import Mapping, Sequence


# バージョン番号 → そのバージョンへ上げるための SQL 文リスト。
# 例) user_stats に列を追加したくなったら:
#   database.py の SCHEMA_VERSION を現在値から 1 つ上げ、その新しい番号をキーとして
#   ["ALTER TABLE user_stats ADD COLUMN favorite_emoji TEXT NOT NULL DEFAULT ''"]
#   を追記する。既存 DB・新規 DB の両方に自動適用される。
MIGRATIONS: dict[int, list[str]] = {
    # バージョン2: プロフィール記憶を profile.json から DB へ移行
    2: [
        """CREATE TABLE IF NOT EXISTS profile (
            key   TEXT PRIMARY KEY,                    -- プロフィールのトップレベルキー
            value TEXT NOT NULL                        -- 値（JSON文字列）
        )"""
    ],
    # バージョン3: 記憶をユーザー別に分離する。
    #   - profile: 主キーを (user_id, key) の複合主キーに変更（テーブルを作り直す）。
    #     旧・全体共有の行は移行後に Database._migrate_legacy_profile_rows で owner へ割り当てる。
    #   - pending_log: user_id 列を追加（旧・全体共有の行は owner へ振り直す）。
    3: [
        "ALTER TABLE pending_log ADD COLUMN user_id TEXT NOT NULL DEFAULT ''",
        # profile は主キーを変えるため、旧テーブルを退避 → 新テーブルを作成する。
        # 退避テーブルの行は Database._migrate_legacy_profile_rows() が owner へ移して破棄する。
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

# 初版（バージョン1）のテーブル定義。ここは変更禁止（変更は MIGRATIONS で）。
CREATE_TABLE_STATEMENTS = [
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


def apply_migrations(
    connection: sqlite3.Connection,
    target_version: int,
    migrations: Mapping[int, Sequence[str]],
) -> list[int]:
    """呼出元の明示トランザクション内で、`user_version` の次から target_version までの移行を実行する。

    SQL を変更した版（SQL 文が1つ以上ある版）だけを、適用順のリストで返す。
    SQL 文の無い版も `PRAGMA user_version` は進める。呼出元はコミット後に、返った版だけ「移行しました」
    とログに出す（途中失敗で取り消された移行を、成功したように記録しないため）。
    target_version と migrations は呼出元から受け取る — `Database` は自分のモジュールの
    `SCHEMA_VERSION`・`_MIGRATIONS` を渡す（テストがそれらを差し替えて、段階的な移行や
    途中失敗を再現するため）。
    """
    current = int(connection.execute("PRAGMA user_version").fetchone()[0])
    applied_versions: list[int] = []
    for version in range(current + 1, target_version + 1):
        statements = migrations.get(version, [])
        for statement in statements:
            connection.execute(statement)
        # user_version はプレースホルダ(?)が使えないため f-string で埋める。
        # version は range() 由来の int なのでインジェクションの心配はない。
        connection.execute(f"PRAGMA user_version = {version}")
        if statements:
            applied_versions.append(version)
    return applied_versions
