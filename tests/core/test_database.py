"""新規DBの初期化と、再起動を挟んだギルド・DM設定の保存を検証する。"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona_bot.core.app_state import BotState
from tests.helpers.persona_helpers import install_persona_catalog
from persona_bot.core import database
from persona_bot.core.database import Database, SCHEMA_VERSION
from persona_bot.core.guild_settings_fields import GUILD_SETTINGS_FIELDS


class DatabaseInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "nested" / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def test_connection_configures_sqlite_busy_timeout(self) -> None:
        self.assertEqual(
            self.db._conn.execute("PRAGMA busy_timeout").fetchone()[0],
            int(database.DATABASE_BUSY_TIMEOUT_SECONDS * 1000),
        )

    def test_write_waits_for_another_connection_and_then_succeeds(self) -> None:
        self.db.close()
        with patch.object(database, "DATABASE_BUSY_TIMEOUT_SECONDS", 1.0):
            self.db = Database(self.path)
        self.db.initialize()
        started, finished = threading.Event(), threading.Event()
        errors = []

        def trace(statement):
            if statement.startswith("INSERT OR REPLACE INTO profile"):
                started.set()

        def save():
            try:
                self.db.profile_merge("fictional-user", {"趣味": "架空の工作"})
            except Exception as error:
                errors.append(error)
            finally:
                finished.set()

        self.db._conn.set_trace_callback(trace)
        with closing(sqlite3.connect(self.path)) as other:
            other.execute("BEGIN IMMEDIATE")
            worker = threading.Thread(target=save)
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.05))
            finally:
                other.rollback()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.db.profile_load("fictional-user"), {"趣味": "架空の工作"})

    def test_busy_wait_remains_bounded_and_does_not_write_on_timeout(self) -> None:
        self.db.close()
        with patch.object(database, "DATABASE_BUSY_TIMEOUT_SECONDS", 0.03):
            self.db = Database(self.path)
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as other:
            other.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                self.db.profile_merge("fictional-user", {"趣味": "架空の工作"})
            elapsed = time.monotonic() - started
            other.rollback()
        self.assertLess(elapsed, 2.0)
        self.assertFalse(self.db._conn.in_transaction)
        self.assertEqual(self.db.profile_load("fictional-user"), {})

    def test_fresh_database_applies_all_migrations(self) -> None:
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertTrue({
            "affinity", "user_stats", "queue_tasks", "pending_log", "profile",
            "guild_settings", "anniversary_posts", "reminders", "feedback",
        }.issubset(tables))
        self.assertEqual(self.db.guild_settings_load_all(), {})
        self.assertEqual(self.db.reminder_list_pending("test-user"), [])

    def test_unknown_settings_column_prevents_replacement(self) -> None:
        self.db.initialize()
        values = {name: getattr(BotState().get_guild(1), attribute) for name, attribute, _, _ in GUILD_SETTINGS_FIELDS}
        self.db.guild_settings_save(1, **values)
        with self.db._conn:
            self.db._conn.execute("ALTER TABLE guild_settings ADD COLUMN future_setting INTEGER DEFAULT 42")
        with self.assertRaises(ValueError):
            self.db.guild_settings_save(1, **values)
        self.assertEqual(self.db._conn.execute("SELECT future_setting FROM guild_settings WHERE guild_id = 1").fetchone()[0], 42)

    def test_repeated_settings_saves_reuse_column_validation(self) -> None:
        self.db.initialize()
        values = {name: getattr(BotState().get_guild(1), attribute) for name, attribute, _, _ in GUILD_SETTINGS_FIELDS}
        statements = []
        self.db._conn.set_trace_callback(statements.append)
        for guild_id in (1, 2, 1):
            self.db.guild_settings_save(guild_id, **values)
        self.assertFalse(any("table_info(guild_settings)" in sql for sql in statements), statements)
        self.assertEqual(len([sql for sql in statements if "PRAGMA schema_version" in sql]), 3)

    def test_unknown_settings_column_rejects_initialization(self) -> None:
        self.db.initialize()
        with self.db._conn:
            self.db._conn.execute("ALTER TABLE guild_settings ADD COLUMN future_setting INTEGER DEFAULT 42")
        with self.assertRaises(ValueError):
            self.db.initialize()
        self.assertFalse(self.db._conn.in_transaction)

    def test_external_settings_schema_change_invalidates_cached_validation(self) -> None:
        self.db.initialize()
        values = {name: getattr(BotState().get_guild(1), attribute) for name, attribute, _, _ in GUILD_SETTINGS_FIELDS}
        self.db.guild_settings_save(1, **values)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE guild_settings ADD COLUMN future_setting INTEGER DEFAULT 42")
        with self.assertRaises(ValueError):
            self.db.guild_settings_save(1, **values)
        self.assertEqual(self.db._conn.execute("SELECT future_setting FROM guild_settings WHERE guild_id = 1").fetchone()[0], 42)

    def test_repeated_initialization_preserves_user_data(self) -> None:
        self.db.initialize()
        self.db.profile_merge("test-user", {"趣味": "架空の工作"})
        self.db.profile_merge("other-user", {"趣味": "架空の読書"})
        self.db.pending_append("test-user", "2026-01-01", "架空の質問", "架空の回答")
        self.db.initialize()
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(self.db.profile_load("other-user"), {"趣味": "架空の読書"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def test_schema_initialization_uses_one_explicit_transaction(self) -> None:
        statements = []
        self.db._conn.set_trace_callback(statements.append)
        self.db.initialize()
        first_commit = statements.index("COMMIT")
        schema_statements = statements[:first_commit + 1]
        self.assertEqual([sql for sql in schema_statements if sql.startswith("BEGIN")], ["BEGIN IMMEDIATE"])
        self.assertTrue(any(sql.startswith("CREATE TABLE") for sql in schema_statements))
        self.assertTrue(any(sql.startswith("PRAGMA user_version =") for sql in schema_statements))
        self.assertFalse(self.db._conn.in_transaction)

    def prepare_version_five(self) -> None:
        # 繰り返し列を追加する前のDBに、架空の既存データを用意する。
        # 旧版の検証対象を作る間だけ、現行版の列照合は行わない。
        with patch.object(database, "SCHEMA_VERSION", 5), patch.object(self.db, "_validate_guild_settings_schema"):
            self.db.initialize()
        self.db.profile_merge("test-user", {"趣味": "架空の工作"})
        self.db.pending_append("test-user", "2026-01-01", "架空の質問", "架空の回答")
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "INSERT INTO reminders"
                " (user_id, channel_id, message, remind_at, created_at)"
                " VALUES ('test-user', 10, '架空の予定', '2026-01-02', '2026-01-01')"
            )

    def assert_version_five_is_intact(self) -> None:
        # 別接続から、失敗したDDLもバージョン番号も永続化されていないことを確認する。
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(reminders)")}
            self.assertNotIn("recurrence", columns)
            self.assertNotIn("recurrence_anchor", columns)
            self.assertEqual(connection.execute(
                "SELECT reminder_id, user_id, message FROM reminders"
            ).fetchall(), [(1, "test-user", "架空の予定")])
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def assert_restart_completes_migration(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        reminders = self.db.reminder_list_pending("test-user")
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["reminder_id"], 1)
        self.assertEqual(reminders[0]["message"], "架空の予定")
        self.assertEqual(reminders[0]["recurrence"], "none")
        self.assertEqual(reminders[0]["recurrence_anchor"], "")
        self.assertEqual(self.db.profile_load("test-user"), {"趣味": "架空の工作"})
        self.assertEqual(len(self.db.pending_fetch_entries("test-user")), 1)

    def check_failed_migration(self, *, completed_statements: int) -> None:
        self.prepare_version_five()
        statements = database._MIGRATIONS[6]
        broken = [
            *statements[:completed_statements],
            "SELECT * FROM fictional_missing_table",
            *statements[completed_statements:],
        ]
        with patch.dict(database._MIGRATIONS, {6: broken}):
            with self.assertRaisesRegex(sqlite3.OperationalError, "fictional_missing_table"):
                self.db.initialize()
        self.assertFalse(self.db._conn.in_transaction)
        self.assert_version_five_is_intact()
        self.assert_restart_completes_migration()

    def test_failure_after_first_ddl_rolls_back_and_retries(self) -> None:
        self.check_failed_migration(completed_statements=1)

    def test_failure_before_version_update_rolls_back_and_retries(self) -> None:
        self.check_failed_migration(completed_statements=len(database._MIGRATIONS[6]))

    def test_later_step_failure_rolls_back_an_already_updated_user_version(self) -> None:
        self.prepare_version_five()
        with patch.dict(database._MIGRATIONS, {7: ["SELECT * FROM fictional_missing_table"]}):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.initialize()
        self.assert_version_five_is_intact()
        self.assert_restart_completes_migration()

    def test_empty_pending_owner_is_rejected_without_saving_any_row(self) -> None:
        self.db.initialize()
        for user_id in (None, "", " ", "\t\n", "　"):
            with self.subTest(user_id=user_id), self.assertRaises(ValueError):
                self.db.pending_append(user_id, "2026-01-01", "架空の質問", "架空の回答")
        self.assertEqual(self.db._conn.execute("SELECT count(*) FROM pending_log").fetchone()[0], 0)

    def test_fresh_database_failure_rolls_back_schema_and_version(self) -> None:
        statements = [*database._MIGRATIONS[6], "SELECT * FROM fictional_missing_table"]
        with patch.dict(database._MIGRATIONS, {6: statements}):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall(), [])
        self.db.initialize()
        self.assertEqual(self.db.reminder_list_pending("test-user"), [])

    def test_process_exit_during_ddl_can_restart_without_data_loss(self) -> None:
        self.prepare_version_five()
        self.db.close()
        script = """
import os
import sys
sys.path.insert(0, sys.argv[2])
from persona_bot.core import database
db = database.Database(sys.argv[1])
db._conn.create_function("stop_migration", 0, lambda: os._exit(17))
statements = database._MIGRATIONS[6]
database._MIGRATIONS[6] = [statements[0], "SELECT stop_migration()", *statements[1:]]
db.initialize()
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script, str(self.path),
             str(Path(__file__).resolve().parents[2] / "src")],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.db = Database(self.path)
        self.assert_version_five_is_intact()
        self.assert_restart_completes_migration()


class GuildSettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()

    async def test_all_persisted_settings_round_trip_for_guilds_and_dm(self) -> None:
        state = BotState()
        state.restore_from_database(self.db)
        from persona_bot.config.choices import AUTO_TOPIC_INTERVAL_CHOICES, MODEL_CHOICES
        personas = ("../prompts/架空のペルソナA.txt", "../prompts/架空のペルソナB.txt", "../prompts/架空のペルソナC.txt")
        install_persona_catalog(self, *personas)
        expected = {}
        for index, guild_id in enumerate((1, 2, None)):
            settings = await state.get_guild_async(guild_id)
            values = {
                "current_prompt_file": personas[index],
                "current_model_name": MODEL_CHOICES[index].value,
                "current_respond_mode": "all" if index == 0 else "mention",
                "grounding_enabled": index != 1,
                "auto_topic_enabled": index == 1,
                "auto_topic_interval_hours": AUTO_TOPIC_INTERVAL_CHOICES[index].value,
                "autonomous_enabled": index != 2,
                # 配信先の指定（#266）。DM は指定なしのまま往復させる
                "autonomous_stage_channel_id": 201 + index if index != 2 else None,
                "autonomous_voice_channel_id": 301 + index if index == 0 else None,
            }
            for name, value in values.items():
                setattr(settings, name, value)
            settings.channel_reset_points[10] = 20
            await settings.persist()
            await settings.record_anniversary_post("test-user", "架空の記念日", "2026-01-01")
            expected[guild_id] = values

        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        restored = BotState()
        restored.restore_from_database(self.db)
        for guild_id, values in expected.items():
            settings = restored.get_guild(guild_id)
            for name, value in values.items():
                self.assertEqual(getattr(settings, name), value, (guild_id, name))
            self.assertEqual(settings.anniversary_posted_dates, {"test-user:架空の記念日": "2026-01-01"})
            self.assertEqual(settings.channel_reset_points, {})

        # 復元済み・新規サーバーのどちらも、その後の変更を同じDBへ保存できる。
        restored.get_guild(1).grounding_enabled = False
        await restored.get_guild(1).persist()
        new_settings = await restored.get_guild_async(3)
        new_settings.autonomous_enabled = True
        await new_settings.persist()
        again = BotState()
        again.restore_from_database(self.db)
        self.assertFalse(again.get_guild(1).grounding_enabled)
        self.assertEqual(again.get_guild(1).current_model_name, expected[1]["current_model_name"])
        self.assertTrue(again.get_guild(3).autonomous_enabled)
        for guild_id in (2, None):
            for name, value in expected[guild_id].items():
                self.assertEqual(getattr(again.get_guild(guild_id), name), value)


class LegacyDataEdgeCaseTests(unittest.TestCase):
    """引き継ぎ先の無い旧データ・壊れた旧 JSON・終了時の close を確かめる（#68）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        quiet = patch("persona_bot.core.database.logger")
        quiet.start()
        self.addCleanup(quiet.stop)

    def open(self, owner: str = "", root: Path | None = None) -> Database:
        db = Database((root or self.root) / "bot.db", legacy_owner_id=owner)
        self.addCleanup(db.close)
        return db

    def pending_users(self, db: Database) -> list[str]:
        return [row[0] for row in db._conn.execute("SELECT user_id FROM pending_log ORDER BY id")]

    def add_pending(self, db: Database, user_id: str) -> None:
        with db._lock, db._conn:
            db._conn.execute(
                "INSERT INTO pending_log (user_id, created_at, question, response) VALUES (?, ?, ?, ?)",
                (user_id, "2026-01-01", "架空の質問", "架空の応答"),
            )

    def test_unassigned_pending_rows_are_dropped_without_owner(self) -> None:
        db = self.open()
        db.initialize()
        self.add_pending(db, "")  # 旧・全体共有の未割当行
        self.add_pending(db, "test-user")
        self.assertEqual(db.pending_distinct_user_ids(), ["test-user"])
        db.initialize()  # 再起動相当。後始末が走る
        self.assertEqual(self.pending_users(db), ["test-user"])

    def test_unassigned_pending_rows_go_to_owner(self) -> None:
        db = self.open(owner="owner-user")
        db.initialize()
        self.add_pending(db, "")
        db.initialize()
        self.assertEqual(self.pending_users(db), ["owner-user"])

    def test_bom_legacy_json_and_first_pending_entry_are_imported(self) -> None:
        (self.root / "affinity.json").write_text('{"test-user": {"affinity": 61}}', encoding="utf-8-sig")
        (self.root / "pending_log.txt").write_text(
            "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の回答\n", encoding="utf-8-sig",
        )
        db = self.open("test-user")
        db.initialize()
        self.assertEqual(db.affinity_load_all()["test-user"]["affinity"], 61)
        self.assertEqual([row["question"] for row in db.pending_fetch_entries("test-user")], ["架空の質問"])
        self.assertTrue((self.root / "affinity.json.imported.bak").exists())
        self.assertTrue((self.root / "pending_log.txt.imported.bak").exists())
        db.initialize()
        self.assertEqual(len(db.pending_fetch_entries("test-user")), 1)

    def test_pending_log_txt_is_kept_as_backup_without_owner(self) -> None:
        text = "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n"
        for owner, expected in (("", []), ("owner-user", ["owner-user"])):
            with self.subTest(owner=owner):
                # ケースごとに別のフォルダを使い、前のケースの失敗が次へ波及しないようにする
                root = self.root / (owner or "no-owner")
                root.mkdir()
                (root / "pending_log.txt").write_text(text, encoding="utf-8")
                db = self.open(owner, root)
                db.initialize()
                self.assertEqual(self.pending_users(db), expected)
                self.assertEqual((root / "pending_log.txt.imported.bak").read_text(encoding="utf-8"), text)

    def test_pending_import_rename_failure_is_idempotent_across_restarts(self) -> None:
        source = self.root / "pending_log.txt"
        text = "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n"
        source.write_text(text, encoding="utf-8-sig")
        for _ in range(3):
            db = self.open("owner-user")
            with patch.object(Path, "replace", side_effect=OSError("架空の改名失敗")):
                db.initialize()
            self.assertEqual(len(db.pending_fetch_entries("owner-user")), 1)
            self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0], 1)
            self.assertEqual(source.read_text(encoding="utf-8-sig"), text)
            db.close()
        restored = self.open("owner-user")
        restored.initialize()
        self.assertEqual(len(restored.pending_fetch_entries("owner-user")), 1)
        self.assertFalse(source.exists())
        self.assertEqual(source.with_suffix(".txt.imported.bak").read_text(encoding="utf-8-sig"), text)

    def test_failed_pending_import_rolls_back_rows_and_marker_and_retries(self) -> None:
        db = self.open("owner-user")
        db.initialize()
        source = self.root / "pending_log.txt"
        source.write_text("[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n", encoding="utf-8")
        # 会話のINSERT成功後、移行記録の書き込みだけを失敗させる。
        with db._conn:
            db._conn.execute(
                "CREATE TRIGGER fail_import BEFORE INSERT ON legacy_imports "
                "BEGIN SELECT RAISE(ABORT, 'fictional import failure'); END"
            )
        db.initialize()
        self.assertTrue(source.exists())
        self.assertEqual(db.pending_fetch_entries("owner-user"), [])
        self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0], 0)
        with db._conn:
            db._conn.execute("DROP TRIGGER fail_import")
        db.close()
        restored = self.open("owner-user")
        restored.initialize()
        self.assertEqual(len(restored.pending_fetch_entries("owner-user")), 1)
        self.assertFalse(source.exists())

    def test_consumed_pending_rows_are_not_reimported_after_rename_failure(self) -> None:
        source = self.root / "pending_log.txt"
        source.write_text("[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n", encoding="utf-8")
        db = self.open("owner-user")
        with patch.object(Path, "replace", side_effect=OSError("架空の改名失敗")):
            db.initialize()
        entries = db.pending_fetch_entries("owner-user")
        db.pending_clear_upto("owner-user", entries[-1]["id"])
        db.close()
        restored = self.open("owner-user")
        restored.initialize()
        self.assertEqual(restored.pending_fetch_entries("owner-user"), [])
        self.assertFalse(source.exists())

    def test_pending_import_keeps_identical_conversations_and_distinct_sources(self) -> None:
        source = self.root / "pending_log.txt"
        text = "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n"
        source.write_text(text * 2, encoding="utf-8")
        db = self.open("owner-user")
        with patch.object(Path, "replace", side_effect=OSError("架空の改名失敗")):
            db.initialize()
        self.add_pending(db, "owner-user")
        source.write_text(text + "[2026-01-02] User: 別の架空の質問\n[2026-01-02] Bot: 別の架空の応答\n", encoding="utf-8")
        db.initialize()
        self.assertEqual(len(db.pending_fetch_entries("owner-user")), 5)
        self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0], 2)

    def test_broken_legacy_json_is_left_in_place_for_retry(self) -> None:
        files = {
            "affinity.json": "{架空の壊れたJSON",
            "user_stats.json": "[1, 2]",
            "queue.json": '{"tasks": "架空の非配列"}',
        }
        for name, text in files.items():
            (self.root / name).write_text(text, encoding="utf-8")
        self.open().initialize()
        for name, text in files.items():
            with self.subTest(file=name):
                self.assertEqual((self.root / name).read_text(encoding="utf-8"), text)
                self.assertFalse((self.root / f"{name}.imported.bak").exists())

    def test_valid_legacy_json_is_still_imported_and_renamed(self) -> None:
        entry = {"affinity": 70, "valence": 60, "arousal": 40, "last_interaction": 1.0}
        (self.root / "affinity.json").write_text(json.dumps({"test-user": entry}), encoding="utf-8")
        db = self.open()
        db.initialize()
        self.assertEqual(db.affinity_load_all()["test-user"]["affinity"], 70)
        self.assertFalse((self.root / "affinity.json").exists())
        self.assertTrue((self.root / "affinity.json.imported.bak").exists())

    def test_existing_backup_is_not_overwritten_by_a_new_legacy_file(self) -> None:
        """同名の退避ファイルがあっても上書きせず、連番の名前へ退避する（#379）。"""
        old_text = "[2026-01-01] User: 以前の架空の質問\n[2026-01-01] Bot: 以前の架空の応答\n"
        new_text = "[2026-02-01] User: 新しい架空の質問\n[2026-02-01] Bot: 新しい架空の応答\n"
        existing = self.root / "pending_log.txt.imported.bak"
        existing.write_text(old_text, encoding="utf-8")
        source = self.root / "pending_log.txt"
        source.write_text(new_text, encoding="utf-8")
        # 引き継ぎ先が無いと旧ログはDBへ入らない。退避ファイルが唯一のコピーになる経路。
        self.open("").initialize()
        self.assertEqual(existing.read_text(encoding="utf-8"), old_text)
        self.assertFalse(source.exists())
        self.assertEqual(
            (self.root / "pending_log.txt.imported.1.bak").read_text(encoding="utf-8"), new_text,
        )

    def test_backup_numbers_skip_every_existing_backup(self) -> None:
        for name in ("affinity.json.imported.bak", "affinity.json.imported.1.bak"):
            (self.root / name).write_text('{"previous": {}}', encoding="utf-8")
        (self.root / "affinity.json").write_text('{"test-user": {"affinity": 61}}', encoding="utf-8")
        db = self.open()
        db.initialize()
        self.assertEqual(db.affinity_load_all()["test-user"]["affinity"], 61)
        self.assertEqual((self.root / "affinity.json.imported.bak").read_text(encoding="utf-8"), '{"previous": {}}')
        self.assertEqual((self.root / "affinity.json.imported.1.bak").read_text(encoding="utf-8"), '{"previous": {}}')
        self.assertIn("test-user", (self.root / "affinity.json.imported.2.bak").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "affinity.json").exists())

    def test_every_legacy_file_keeps_its_existing_backup(self) -> None:
        sources = {
            "affinity.json": '{"test-user": {"affinity": 61}}',
            "user_stats.json": '{"test-user": {"total_count": 3}}',
            "queue.json": '{"tasks": []}',
            "pending_log.txt": "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n",
        }
        for name, text in sources.items():
            (self.root / name).write_text(text, encoding="utf-8")
            (self.root / f"{name}.imported.bak").write_text(f"previous:{name}", encoding="utf-8")
        self.open("owner-user").initialize()
        for name, text in sources.items():
            with self.subTest(file=name):
                self.assertEqual((self.root / f"{name}.imported.bak").read_text(encoding="utf-8"), f"previous:{name}")
                self.assertEqual((self.root / f"{name}.imported.1.bak").read_text(encoding="utf-8"), text)
                self.assertFalse((self.root / name).exists())

    def test_import_is_skipped_and_source_kept_when_no_backup_name_is_free(self) -> None:
        """退避名の空きが無ければ、取り込まずに元のファイルと既存の退避を両方残す。"""
        existing = self.root / "pending_log.txt.imported.bak"
        existing.write_text("以前の退避", encoding="utf-8")
        source = self.root / "pending_log.txt"
        text = "[2026-01-01] User: 架空の質問\n[2026-01-01] Bot: 架空の応答\n"
        source.write_text(text, encoding="utf-8")
        db = self.open("owner-user")
        with patch("persona_bot.core.database.LEGACY_BACKUP_MAX_SUFFIX", 0):
            db.initialize()
        self.assertEqual(existing.read_text(encoding="utf-8"), "以前の退避")
        self.assertEqual(source.read_text(encoding="utf-8"), text)
        self.assertEqual(db.pending_fetch_entries("owner-user"), [])
        self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM legacy_imports").fetchone()[0], 0)
        # 空きができた次回の起動で取り込まれ、重複しない
        db.initialize()
        self.assertEqual(len(db.pending_fetch_entries("owner-user")), 1)
        self.assertFalse(source.exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "以前の退避")

    def test_close_waits_for_a_running_operation_and_is_repeatable(self) -> None:
        db = self.open()
        db.initialize()
        closed = threading.Event()

        def close() -> None:
            db.close()
            closed.set()

        db._lock.acquire()  # 別スレッドの読み書きが実行中の状態
        try:
            worker = threading.Thread(target=close)
            worker.start()
            self.assertFalse(closed.wait(0.2))
            self.assertEqual(db._conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            db._lock.release()
        worker.join(5)
        self.assertTrue(closed.is_set())
        with self.assertRaises(sqlite3.ProgrammingError):
            db._conn.execute("SELECT 1")
        db.close()


class ReminderClaimStatusTests(unittest.TestCase):
    """次回時刻を作れなかった繰り返しの終わり方を確認する（#76）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()

    def add(self, recurrence: str) -> int:
        return self.db.reminder_add(
            user_id="test-user", user_name="架空の利用者", guild_id=1, channel_id=10,
            message=f"架空の予定-{recurrence}", remind_at="2026-01-01T00:00:00+00:00",
            created_at="2025-12-31T00:00:00+00:00", recurrence=recurrence,
            recurrence_anchor="" if recurrence == "none" else "2026-01-01T00:00:00+00:00",
        )

    def stored(self, reminder_id: int) -> dict:
        return dict(self.db._conn.execute(
            "SELECT status, remind_at FROM reminders WHERE reminder_id = ?", (reminder_id,)
        ).fetchone())

    def claim(self, next_at) -> dict[int, dict]:
        with patch("persona_bot.core.database.logger"):
            claimed = self.db.reminder_claim_due("2026-01-01T00:00:00+00:00", next_at)
        return {record["reminder_id"]: record for record in claimed}

    def test_failed_calculation_cancels_recurrences_and_finishes_single(self) -> None:
        def fail(record):
            raise ValueError("架空の計算失敗")

        for next_at in (fail, lambda record: None):
            with self.subTest(next_at="例外" if next_at is fail else "None"):
                single, daily, monthly = self.add("none"), self.add("daily"), self.add("monthly")
                claimed = self.claim(next_at)
                # 単発は通知で役目を終える。繰り返しは理由を問わず系列を止める。
                for reminder_id, status in ((single, "done"), (daily, "canceled"), (monthly, "canceled")):
                    self.assertEqual(claimed[reminder_id]["status"], status)
                    self.assertIsNone(claimed[reminder_id]["next_remind_at"])
                    self.assertEqual(self.stored(reminder_id)["status"], status)

    def test_next_time_not_after_now_stops_series_instead_of_reclaiming(self) -> None:
        # 現在以前の次回時刻を pending で残すと、次の見回りで同じ系列を取り直す（#222）。
        for next_iso in ("2025-12-31T23:00:00+00:00", "2026-01-01T00:00:00+00:00"):
            with self.subTest(next_iso=next_iso):
                daily = self.add("daily")
                claimed = self.claim(lambda record: next_iso)
                self.assertEqual(claimed[daily]["status"], "canceled")
                self.assertIsNone(claimed[daily]["next_remind_at"])
                self.assertEqual(self.stored(daily)["status"], "canceled")
                self.assertEqual(self.claim(lambda record: next_iso), {})

    def test_successful_calculation_keeps_recurrence_pending(self) -> None:
        daily = self.add("daily")
        claimed = self.claim(lambda record: "2026-01-02T00:00:00+00:00")
        self.assertEqual(claimed[daily]["next_remind_at"], "2026-01-02T00:00:00+00:00")
        # 返す記録の remind_at は進める前の予定時刻のまま（遅延判定が使う）。
        self.assertEqual(claimed[daily]["remind_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(self.stored(daily), {"status": "pending", "remind_at": "2026-01-02T00:00:00+00:00"})


class ReminderUserIndexTests(unittest.TestCase):
    """リマインダーのユーザー別の操作が専用の索引を使うことを確認する（#58）。"""

    INDEX = "idx_reminders_user_status"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def add(self, user_id: str, remind_at: str) -> int | None:
        return self.db.reminder_add(
            user_id=user_id, user_name="架空の利用者", guild_id=1, channel_id=10,
            message="架空の予定", remind_at=remind_at, created_at="2026-01-01T00:00:00+00:00",
        )

    def reminder_indexes(self) -> set[str]:
        with closing(sqlite3.connect(self.path)) as connection:
            return {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reminders'"
            )}

    def query_plans(self, operation) -> list[str]:
        """操作が実際に発行した SQL（値は展開済み）を集め、ユーザー別の検索の実行計画を返す。"""
        statements: list[str] = []
        self.db._conn.set_trace_callback(statements.append)
        try:
            operation()
        finally:
            self.db._conn.set_trace_callback(None)
        targets = [statement for statement in statements
                   if "FROM reminders" in statement and "user_id = " in statement]
        self.assertTrue(targets, statements)
        with closing(sqlite3.connect(self.path)) as connection:
            return [" / ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"))
                    for statement in targets]

    def test_user_operations_use_user_index_without_temporary_sort(self) -> None:
        self.db.initialize()
        for user in range(30):
            for hour in range(3):
                self.add(f"user-{user}", f"2026-01-02T{hour:02d}:00:00+00:00")
        target = "user-7"
        operations = {
            "登録時の上限確認": lambda: self.add(target, "2026-01-02T01:00:00+00:00"),
            "一覧": lambda: self.db.reminder_list_pending(target),
            "件数": lambda: self.db.reminder_count_pending(target),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                for plan in self.query_plans(operation):
                    self.assertIn(self.INDEX, plan)
                    self.assertNotIn("TEMP B-TREE", plan)
        # 同じ時刻の予定は登録順（reminder_id）で並ぶ
        listed = [(row["remind_at"][11:13], row["reminder_id"]) for row in self.db.reminder_list_pending(target)]
        self.assertEqual(listed, sorted(listed))
        self.assertEqual([hour for hour, _ in listed], ["00", "01", "01", "02"])
        self.assertEqual(self.db.reminder_count_pending(target), 4)

    def test_version_nine_database_gains_index_and_keeps_reminders(self) -> None:
        with patch.object(database, "SCHEMA_VERSION", 9), patch.object(self.db, "_validate_guild_settings_schema"):
            self.db.initialize()
        reminder_id = self.add("test-user", "2026-01-02T00:00:00+00:00")
        self.assertNotIn(self.INDEX, self.reminder_indexes())
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(self.reminder_indexes(), {"idx_reminders_due", self.INDEX})
        self.assertEqual([row["reminder_id"] for row in self.db.reminder_list_pending("test-user")], [reminder_id])


class FeedbackPruneIndexTests(unittest.TestCase):
    """評価の保持結果を変えず、移行で一時ソートを省けることを確認する（#188）。"""

    INDEX = "idx_feedback_created_message"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        with patch.object(database, "SCHEMA_VERSION", 11), patch.object(self.db, "_validate_guild_settings_schema"):
            self.db.initialize()
        # 日時とIDの大小を逆にし、同じ日時・同じ返信への複数票も用意する。
        for message_id, user_id, day in ((40, "user-a", 1), (10, "user-a", 3),
                                        (20, "user-b", 2), (20, "user-a", 2), (30, "user-a", 2)):
            self.db.feedback_record(
                message_id=message_id, user_id=user_id, guild_id=1, channel_id=10,
                target_user_id="target-user", model_name="架空のモデル", prompt_file="架空のペルソナ",
                rating=1, created_at=f"2026-01-{day:02d}T00:00:00+00:00",
            )

    def rows(self) -> list[tuple]:
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute("SELECT rowid, * FROM feedback ORDER BY rowid").fetchall()

    def prune_plan(self, max_keep: int) -> str:
        # 実際の削除SQLを捕捉し、サブクエリを含めた実行計画を調べる。
        statements: list[str] = []
        self.db._conn.set_trace_callback(statements.append)
        try:
            self.db.feedback_prune(max_keep)
        finally:
            self.db._conn.set_trace_callback(None)
        targets = [statement for statement in statements if statement.startswith("DELETE FROM feedback")]
        self.assertEqual(len(targets), 1)
        with closing(sqlite3.connect(self.path)) as connection:
            return " / ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {targets[0]}"))

    def migrate(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.db.initialize()

    def test_version_eleven_migration_removes_temporary_sort_and_preserves_feedback(self):
        original = self.rows()
        before = self.prune_plan(len(original))
        self.assertIn("USE TEMP B-TREE FOR ORDER BY", before)
        self.assertNotIn(self.INDEX, before)
        self.migrate()
        self.assertEqual(self.rows(), original)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        after = self.prune_plan(len(original))
        self.assertIn(self.INDEX, after)
        self.assertNotIn("TEMP B-TREE", after)
        self.assertEqual(self.rows(), original)

    def test_prune_keeps_same_newest_rows_before_and_after_migration(self):
        original = self.rows()
        for max_keep, expected in ((0, []), (1, [2]), (3, [2, 3, 5]), (4, [2, 3, 4, 5]), (10, [1, 2, 3, 4, 5])):
            for version in (11, 12):
                with self.subTest(max_keep=max_keep, version=version):
                    # 同一の旧DBを複製し、それぞれで移行前後の削除結果を比較する。
                    candidate_path = self.path.with_name(f"feedback-{max_keep}-{version}.db")
                    with closing(sqlite3.connect(candidate_path)) as connection:
                        self.db._conn.backup(connection)
                    candidate = Database(candidate_path)
                    self.addCleanup(candidate.close)
                    with patch.object(database, "SCHEMA_VERSION", version), patch.object(candidate, "_validate_guild_settings_schema"):
                        candidate.initialize()
                    candidate.feedback_prune(max_keep)
                    rows = [tuple(row) for row in candidate._conn.execute(
                        "SELECT rowid, * FROM feedback ORDER BY rowid"
                    )]
                    self.assertEqual(rows, [row for row in original if row[0] in expected])
                    candidate.close()



class PendingLogIndexTests(unittest.TestCase):
    """記憶圧縮の巡回が使う利用者別の検索で、全件走査を避ける索引を確認する（#207）。"""

    INDEX = "idx_pending_log_user_id"
    SELECT = "SELECT * FROM pending_log WHERE user_id = ? ORDER BY id"
    DELETE = "DELETE FROM pending_log WHERE user_id = ? AND id <= ?"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        with patch.object(database, "SCHEMA_VERSION", 12), patch.object(self.db, "_validate_guild_settings_schema"):
            self.db.initialize()
        # 利用者を交互に並べ、追記順（id）と利用者ごとの順が食い違う状態にする。
        for index in range(6):
            self.db.pending_append(f"user-{index % 3}", f"2026-01-01 00:00:0{index} UTC", f"架空の質問{index}", f"架空の回答{index}")

    def plan(self, statement: str, *params) -> str:
        with closing(sqlite3.connect(self.path)) as connection:
            return " / ".join(row[3] for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}", params))

    def migrate(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        self.db.initialize()

    def test_version_twelve_migration_adds_index_and_keeps_user_separation(self) -> None:
        original = {user: self.db.pending_fetch_entries(user) for user in ("user-0", "user-1", "user-2")}
        self.assertIn("SCAN pending_log", self.plan(self.SELECT, "user-1"))
        self.migrate()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        for statement, params in ((self.SELECT, ("user-1",)), (self.DELETE, ("user-1", 4))):
            with self.subTest(statement=statement[:20]):
                plan = self.plan(statement, *params)
                self.assertIn(self.INDEX, plan)
                self.assertNotIn("SCAN pending_log", plan)
                self.assertNotIn("TEMP B-TREE", plan)
        # 取得内容・順序・利用者ごとの分離は索引の有無で変わらない。
        for user, entries in original.items():
            self.assertEqual(self.db.pending_fetch_entries(user), entries)
            self.assertEqual([entry["user_id"] for entry in entries], [user] * 2)
        self.assertEqual(self.db.pending_distinct_user_ids(), ["user-0", "user-1", "user-2"])

    def test_range_delete_only_removes_the_target_users_older_rows(self) -> None:
        self.migrate()
        first, second = (entry["id"] for entry in self.db.pending_fetch_entries("user-1"))
        self.db.pending_clear_upto("user-1", first)
        self.assertEqual([entry["id"] for entry in self.db.pending_fetch_entries("user-1")], [second])
        self.assertEqual(len(self.db.pending_fetch_entries("user-0")), 2)
        self.assertEqual(len(self.db.pending_fetch_entries("user-2")), 2)


class QueueCapacityTests(unittest.TestCase):
    """文書キューの未処理件数を、確認と追加を分けずに制限することを確認する（#202）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()
        self.sequence = 0

    def enqueue(self, author_id: int, *, max_per_user: int = 2, max_total: int = 3) -> str:
        self.sequence += 1
        task = {"task_id": f"fictional-{self.sequence}", "author_id": author_id, "question": "架空の質問",
                "created_at": f"2026-01-01T00:00:{self.sequence:02d}", "updated_at": "2026-01-01T00:00:00"}
        return self.db.queue_try_enqueue(task, max_per_user, max_total)

    def active_count(self) -> int:
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM queue_tasks WHERE status IN ('pending', 'processing', 'sending')"
            ).fetchone()[0]

    def test_user_and_total_limits_are_independent(self) -> None:
        self.assertEqual([self.enqueue(11), self.enqueue(11), self.enqueue(11)], ["accepted", "accepted", "user_limit"])
        # 1人が利用者別の上限に達していても、他の利用者は全体の上限まで受け付ける。
        self.assertEqual(self.enqueue(22), "accepted")
        self.assertEqual(self.enqueue(33), "total_limit")
        self.assertEqual(self.active_count(), 3)

    def test_finished_tasks_free_slots_but_sending_still_counts(self) -> None:
        self.enqueue(11)
        self.enqueue(11)
        self.db.queue_update_task("fictional-1", {"status": "sending"}, "2026-01-01T00:01:00")
        self.assertEqual(self.enqueue(11), "user_limit")
        for status in ("completed", "failed"):
            with self.subTest(status=status):
                self.db.queue_update_task("fictional-1", {"status": status}, "2026-01-01T00:02:00")
                self.assertEqual(self.enqueue(11), "accepted")
                self.db.queue_update_task(f"fictional-{self.sequence}", {"status": "completed"}, "2026-01-01T00:03:00")

    def test_concurrent_enqueue_never_exceeds_limit(self) -> None:
        results: list[str] = []
        barrier = threading.Barrier(12)

        def worker(index: int) -> None:
            barrier.wait()
            task = {"task_id": f"fictional-thread-{index}", "author_id": 11, "question": "架空の質問",
                    "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"}
            results.append(self.db.queue_try_enqueue(task, 3, 20))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("accepted"), 3)
        self.assertEqual(results.count("user_limit"), 9)
        self.assertEqual(self.active_count(), 3)


if __name__ == "__main__":
    unittest.main()
