"""database.py から分けた db_migrations.py・db_legacy_importers.py の契約を確かめる（#400）。

`Database` を通した挙動（段階的な移行・途中失敗の取り消し・旧ファイルの取り込みと改名）は
test_database.py などが確かめる。ここでは、分けたモジュール単体の振る舞いと、分けたことで
壊れやすい境界（版の食い違い・管理コンソールが ast で読む SCHEMA_VERSION・パーサーの純粋性）を確かめる。
"""

import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC =Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.core import database, db_legacy_importers, db_migrations
from persona_bot.core.db_legacy_importers import (
    PendingLogFile,
    free_backup_path,
    load_legacy_json_object,
    load_legacy_queue_tasks,
    load_legacy_user_entries,
    load_profile_json,
    parse_pending_log,
    split_profile_by_user,
)
from persona_bot.core.db_migrations import apply_migrations

CORE = SRC / "persona_bot" / "core"


class SchemaVersionContractTests(unittest.TestCase):
    def test_schema_version_matches_the_newest_migration_without_gaps(self) -> None:
        # 番号を飛ばすと、後から埋めた小さい版は user_version が先へ進んでいるため永久に実行されない。
        self.assertEqual(database.SCHEMA_VERSION, max(db_migrations.MIGRATIONS))
        self.assertEqual(set(db_migrations.MIGRATIONS), set(range(2, database.SCHEMA_VERSION + 1)))

    def test_database_module_shares_the_definition_objects(self) -> None:
        # テストが patch.dict で差し替える辞書は、定義側と同じオブジェクトでなければ移行に効かない。
        self.assertIs(database._MIGRATIONS, db_migrations.MIGRATIONS)
        self.assertIs(database._CREATE_TABLE_STATEMENTS, db_migrations.CREATE_TABLE_STATEMENTS)

    def test_console_can_still_read_schema_version_from_database_py_by_ast(self) -> None:
        # tools/console/backend/local_repo.py は database.py の `SCHEMA_VERSION = <整数>` を ast で読む。
        tree = ast.parse((CORE / "database.py").read_text(encoding="utf-8-sig"))
        versions = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SCHEMA_VERSION" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is int
        ]
        self.assertEqual(versions, [database.SCHEMA_VERSION])

    def test_console_reads_the_queue_and_reminder_state_names_from_database_py(self) -> None:
        # 管理コンソールの画面テスト（dbTables.test.mjs）が database.py の本文を正規表現で読む箇所。
        text = (CORE / "database.py").read_text(encoding="utf-8-sig")
        self.assertRegex(text, r"_QUEUE_ACTIVE_STATUSES\s*=\s*\(([^)]+)\)")
        self.assertRegex(text, r"DELETE FROM reminders WHERE status IN \(([^)]+)\)")


class ApplyMigrationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)

    def user_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    def test_applies_only_newer_versions_in_order_and_reports_those_with_statements(self) -> None:
        migrations = {
            1: ["CREATE TABLE fictional (id INTEGER)"],
            2: ["ALTER TABLE fictional ADD COLUMN name TEXT"],
            3: [],
            4: ["ALTER TABLE fictional ADD COLUMN note TEXT"],
        }
        self.assertEqual(apply_migrations(self.connection, 4, migrations), [1, 2, 4])
        self.assertEqual(self.user_version(), 4)
        columns = [row[1] for row in self.connection.execute("PRAGMA table_info(fictional)")]
        self.assertEqual(columns, ["id", "name", "note"])

    def test_resumes_from_the_current_version_and_does_not_repeat(self) -> None:
        migrations = {1: ["CREATE TABLE fictional (id INTEGER)"], 2: ["ALTER TABLE fictional ADD COLUMN name TEXT"]}
        self.assertEqual(apply_migrations(self.connection, 1, migrations), [1])
        self.assertEqual(apply_migrations(self.connection, 2, migrations), [2])
        # 適用済みの版は、もう一度呼んでも実行しない（二重適用で ALTER が失敗しない）。
        self.assertEqual(apply_migrations(self.connection, 2, migrations), [])
        self.assertEqual(self.user_version(), 2)

    def test_versions_without_statements_still_advance_user_version(self) -> None:
        self.assertEqual(apply_migrations(self.connection, 3, {}), [])
        self.assertEqual(self.user_version(), 3)

    def test_failure_propagates_and_leaves_the_rollback_to_the_caller(self) -> None:
        # トランザクションの開始・確定は呼出元の責務。失敗は例外のまま伝え、呼出元が取り消す。
        self.connection.isolation_level = None
        self.connection.execute("BEGIN IMMEDIATE")
        migrations = {1: ["CREATE TABLE fictional (id INTEGER)"], 2: ["SELECT * FROM fictional_missing_table"]}
        with self.assertRaises(sqlite3.OperationalError):
            apply_migrations(self.connection, 2, migrations)
        self.connection.execute("ROLLBACK")
        self.assertEqual(self.user_version(), 0)
        self.assertIsNone(
            self.connection.execute("SELECT name FROM sqlite_master WHERE name = 'fictional'").fetchone()
        )

    def test_real_definitions_build_the_current_schema_on_an_empty_database(self) -> None:
        for statement in db_migrations.CREATE_TABLE_STATEMENTS:
            self.connection.execute(statement)
        applied = apply_migrations(self.connection, database.SCHEMA_VERSION, db_migrations.MIGRATIONS)
        self.assertEqual(applied, sorted(db_migrations.MIGRATIONS))
        self.assertEqual(self.user_version(), database.SCHEMA_VERSION)


class LegacyFileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def write_json(self, name: str, data, encoding: str = "utf-8") -> Path:
        path = self.directory / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding=encoding)
        return path

    def test_json_object_is_read_with_or_without_bom_and_other_shapes_raise(self) -> None:
        self.assertEqual(load_legacy_json_object(self.write_json("a.json", {"1": {}}, "utf-8-sig")), {"1": {}})
        for name, content in (("list.json", []), ("text.json", "文字列"), ("none.json", None)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_legacy_json_object(self.write_json(name, content))
        broken = self.directory / "broken.json"
        broken.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_legacy_json_object(broken)

    def test_backup_path_picks_the_first_free_name_and_raises_when_none_is_left(self) -> None:
        source = self.directory / "affinity.json"
        self.assertEqual(free_backup_path(source, 3).name, "affinity.json.imported.bak")
        (self.directory / "affinity.json.imported.bak").write_text("前回", encoding="utf-8")
        self.assertEqual(free_backup_path(source, 3).name, "affinity.json.imported.1.bak")
        (self.directory / "affinity.json.imported.1.bak").write_text("前々回", encoding="utf-8")
        self.assertEqual(free_backup_path(source, 3).name, "affinity.json.imported.2.bak")
        # 連番の上限を使い切ったら、既存の退避ファイルを置き換えず例外にする（#379）。
        with self.assertRaises(FileExistsError):
            free_backup_path(source, 1)
        with self.assertRaises(FileExistsError):
            free_backup_path(source, 0)

    def test_user_entries_keep_only_dictionary_values_in_file_order(self) -> None:
        path = self.write_json("users.json", {"2": {"a": 1}, "1": "壊れた項目", "3": None, "4": {"b": 2}})
        self.assertEqual(load_legacy_user_entries(path), [("2", {"a": 1}), ("4", {"b": 2})])

    def test_queue_tasks_keep_only_dictionaries_with_a_task_id(self) -> None:
        tasks = [{"task_id": "t1"}, {"task_id": ""}, {"status": "pending"}, "壊れた項目", {"task_id": "t2", "status": "failed"}]
        path = self.write_json("queue.json", {"tasks": tasks})
        self.assertEqual(load_legacy_queue_tasks(path), [{"task_id": "t1"}, {"task_id": "t2", "status": "failed"}])
        self.assertEqual(load_legacy_queue_tasks(self.write_json("empty.json", {})), [])
        with self.assertRaises(ValueError):
            load_legacy_queue_tasks(self.write_json("bad.json", {"tasks": {"task_id": "t1"}}))


class PendingLogParseTests(unittest.TestCase):
    def test_pairs_user_and_bot_lines_and_joins_continuation_lines_to_the_preceding_side(self) -> None:
        source = (
            "[2026-01-01 10:00:00 UTC] User: 架空の質問1\n"
            "続きの質問\n"
            "[2026-01-01 10:00:01 UTC] Bot: 架空の回答1\n"
            "続きの回答\n"
            "[2026-01-01 10:05:00 UTC] User: 架空の質問2\n"
        ).encode("utf-8")
        parsed = parse_pending_log(source)
        self.assertEqual(parsed, PendingLogFile(
            content_hash=hashlib.sha256(source).hexdigest(),
            entries=[
                {"created_at": "2026-01-01 10:00:00 UTC", "question": "架空の質問1\n続きの質問",
                 "response": "架空の回答1\n続きの回答"},
                {"created_at": "2026-01-01 10:05:00 UTC", "question": "架空の質問2", "response": ""},
            ],
        ))

    def test_hash_covers_the_original_bytes_including_a_bom(self) -> None:
        source = "﻿[2026-01-01 10:00:00 UTC] User: 架空の質問\n".encode("utf-8")
        parsed = parse_pending_log(source)
        self.assertEqual(parsed.content_hash, hashlib.sha256(source).hexdigest())
        self.assertEqual(parsed.entries[0]["question"], "架空の質問")

    def test_blank_text_has_nothing_to_import_but_unmatched_text_is_still_a_file(self) -> None:
        for source in (b"", b"  \n\t\n", "﻿\n".encode("utf-8")):
            with self.subTest(source=source):
                self.assertIsNone(parse_pending_log(source))
        # 書式に合う行が無くても、本文があれば取り込み済みの記録を作る対象（件数 0）として返す。
        parsed = parse_pending_log("関係のない行\n".encode("utf-8"))
        self.assertEqual(parsed.entries, [])
        self.assertEqual(len(parsed.content_hash), 64)

    def test_bot_line_before_any_user_line_is_ignored(self) -> None:
        parsed = parse_pending_log("[2026-01-01 10:00:00 UTC] Bot: 先頭の回答\n".encode("utf-8"))
        self.assertEqual(parsed.entries, [])


class ProfileJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "profile.json"

    def test_nothing_to_absorb_is_none_and_broken_json_is_reported_to_the_caller(self) -> None:
        for content in ("", "  \n", "{}", "[]", "\"文字列\"", "﻿{}"):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertIsNone(load_profile_json(self.path))
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            load_profile_json(self.path)

    def test_content_is_returned_as_is(self) -> None:
        self.path.write_text(json.dumps({"1": {"趣味": "架空の読書"}}, ensure_ascii=False), encoding="utf-8-sig")
        self.assertEqual(load_profile_json(self.path), {"1": {"趣味": "架空の読書"}})

    def test_per_user_form_skips_empty_profiles(self) -> None:
        data = {"101": {"趣味": "架空の読書"}, "102": {}, "103": {"好物": "架空のお茶"}}
        self.assertEqual(
            split_profile_by_user(data, ""),
            {"101": {"趣味": "架空の読書"}, "103": {"好物": "架空のお茶"}},
        )
        # ユーザー別形式なら、引き継ぎ先の有無に関係なく同じ（owner へは回さない）。
        self.assertEqual(split_profile_by_user(data, "999"), split_profile_by_user(data, ""))
        # 中身がすべて空なら取り込めるものが無い（旧形式として owner へ回さない）。
        self.assertEqual(split_profile_by_user({"101": {}}, "999"), {})

    def test_old_shared_form_goes_to_the_owner_or_is_dropped_without_one(self) -> None:
        shared = {"趣味": "架空の読書", "好物": "架空のお茶"}
        self.assertEqual(split_profile_by_user(shared, "999"), {"999": shared})
        self.assertEqual(split_profile_by_user(shared, ""), {})

    def test_names_and_non_ascii_digits_are_not_treated_as_user_ids(self) -> None:
        # 全値が辞書でも、キーが名前や全角数字ならユーザー別形式とはみなさない。
        for key in ("太郎", "１２３", "12a"):
            with self.subTest(key=key):
                data = {key: {"趣味": "架空の読書"}}
                self.assertEqual(split_profile_by_user(data, "999"), {"999": data})
                self.assertEqual(split_profile_by_user(data, ""), {})
        # 値が辞書でないキーが1つでも混じれば旧形式。
        mixed = {"101": {"趣味": "架空の読書"}, "メモ": "文字列"}
        self.assertEqual(split_profile_by_user(mixed, "999"), {"999": mixed})


class LegacyDataImporterTests(unittest.TestCase):
    """取り込み（LegacyDataImporter）が、Database の差し替え口を呼出しのたびに読むこと（#414）。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.db = database.Database(self.directory / "bot.db")
        self.addCleanup(self.db.close)
        with patch("persona_bot.core.database.logger"):
            self.db.initialize()

    def test_database_owns_one_importer_sharing_its_connection_and_lock(self) -> None:
        self.assertIsInstance(self.db._legacy, db_legacy_importers.LegacyDataImporter)
        self.assertIs(self.db._legacy._conn, self.db._conn)
        self.assertIs(self.db._legacy._lock, self.db._lock)

    def test_owner_logger_and_backup_limit_follow_the_database_at_call_time(self) -> None:
        (self.directory / "affinity.json").write_text('{"1": {"affinity": 60}}', encoding="utf-8")
        (self.directory / "affinity.json.imported.bak").write_text("前回", encoding="utf-8")
        # 退避先の連番の上限が 0 なら空きが無く、取り込まずに元のファイルを残す。上限は database 側の値を読む。
        with patch("persona_bot.core.database.LEGACY_BACKUP_MAX_SUFFIX", 0), \
                patch("persona_bot.core.database.logger") as quiet:
            self.db._legacy.import_files()
        self.assertTrue((self.directory / "affinity.json").exists())
        self.assertEqual(self.db.affinity_load_all(), {})
        # 差し替えたロガーが使われている（警告・エラーはモジュールのロガーではなく database.logger へ出る）
        quiet.error.assert_called_once()
        # 上限を戻せば、同じファイルを連番の名前へ退避して取り込める
        with patch("persona_bot.core.database.logger"):
            self.db._legacy.import_files()
        self.assertFalse((self.directory / "affinity.json").exists())
        self.assertTrue((self.directory / "affinity.json.imported.1.bak").exists())
        self.assertIn("1", self.db.affinity_load_all())

    def test_profile_json_goes_to_the_owner_set_after_construction(self) -> None:
        (self.directory / "profile.json").write_text('{"趣味": "架空の読書"}', encoding="utf-8")
        self.db._legacy_owner_id = "legacy-user"
        with patch("persona_bot.core.database.logger"):
            self.db._legacy.absorb_profile_json()
        self.assertEqual(self.db.profile_load("legacy-user"), {"趣味": "架空の読書"})
        # 取り込めたので、ファイルは {} に戻る
        self.assertEqual(json.loads((self.directory / "profile.json").read_text(encoding="utf-8")), {})


class ImporterPurityTests(unittest.TestCase):
    def test_importer_module_does_not_import_the_database_lock_or_logging(self) -> None:
        # このモジュールは DB・ロック・ログのライブラリを import しない。取り込み（LegacyDataImporter）が使う
        # 接続・ロック・ロガー・JSON の書き込みは Database が渡す（ログの出元を persona_bot.core.database のまま保つため。#414）。
        tree = ast.parse(Path(db_legacy_importers.__file__).read_text(encoding="utf-8-sig"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"sqlite3", "threading", "logging", "persona_bot"}, imported)

    def test_migration_module_depends_only_on_sqlite_and_the_standard_library(self) -> None:
        tree = ast.parse(Path(db_migrations.__file__).read_text(encoding="utf-8-sig"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"persona_bot", "threading", "logging"}, imported)


if __name__ == "__main__":
    unittest.main()
