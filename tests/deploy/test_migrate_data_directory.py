"""実データを使わず、保存先移行の保全・衝突時の停止を確認する。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deploy"))

from migrate_data_directory import MigrationError, migrate_data_directory


class MigrateDataDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, relative: str, content: bytes = b"fictional") -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def snapshot(self) -> dict[str, bytes]:
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def test_moves_database_and_all_files_with_verified_backup(self) -> None:
        database = self.root / "memory/bot.db"
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE example (value TEXT)")
            connection.execute("INSERT INTO example VALUES ('fictional')")
        self.write("memory/bot.db-wal", b"fictional-wal")
        self.write("memory/nested/legacy.json", b'{"fictional": true}')
        self.write("ng/NG_WORD_private.txt", "架空の語".encode("utf-8"))
        self.write("ng/blacklist.json", b"{}")
        # Git更新で見本の移動先が既に作られていても移行できる。
        self.write("data/memory/.gitkeep", b"")
        self.write("data/ng/NG_WORD.txt", b"fictional-default")
        originals = self.snapshot()

        result = migrate_data_directory(self.root)

        self.assertEqual(result.files, 5)
        self.assertIsNotNone(result.backup)
        for relative, content in originals.items():
            if not relative.startswith("data/"):
                self.assertEqual((self.root / "data" / relative).read_bytes(), content)
                self.assertEqual((result.backup / relative).read_bytes(), content)
        self.assertFalse((self.root / "memory").exists())
        self.assertFalse((self.root / "ng").exists())
        # テスト用のWALを除き、移動後のSQLiteの行も確認する。
        (self.root / "data/memory/bot.db-wal").unlink()
        with closing(sqlite3.connect(self.root / "data/memory/bot.db")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM example").fetchone(), ("fictional",))

    def test_check_does_not_create_or_change_files(self) -> None:
        self.write("memory/projects.json")
        before = self.snapshot()
        result = migrate_data_directory(self.root, check_only=True)
        self.assertEqual(result.files, 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "data").exists())

    def test_conflicting_file_stops_before_either_directory_is_moved(self) -> None:
        self.write("memory/bot.db", b"old-database")
        self.write("ng/blacklist.json", b"old-records")
        self.write("data/ng/blacklist.json", b"other-records")
        before = self.snapshot()
        with self.assertRaisesRegex(MigrationError, "内容が異なる"):
            migrate_data_directory(self.root)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / "data/.migration-backups").exists())

    def test_identical_file_is_backed_up_and_rerun_is_noop(self) -> None:
        self.write("memory/.gitkeep", b"")
        self.write("data/memory/.gitkeep", b"")
        self.write("ng/NG_WORD.txt", b"same")
        self.write("data/ng/NG_WORD.txt", b"same")
        result = migrate_data_directory(self.root)
        self.assertEqual(result.files, 2)
        before = self.snapshot()
        self.assertEqual(migrate_data_directory(self.root).files, 0)
        self.assertEqual(self.snapshot(), before)

    def test_directory_conflict_keeps_originals(self) -> None:
        self.write("memory/nested/entry.json")
        self.write("data/memory/nested")
        before = self.snapshot()
        with self.assertRaises(MigrationError):
            migrate_data_directory(self.root)
        self.assertEqual(self.snapshot(), before)

    def test_backup_failure_keeps_legacy_files(self) -> None:
        original = self.write("memory/projects.json")
        with patch("migrate_data_directory.shutil.copytree", side_effect=OSError("架空の失敗")):
            with self.assertRaises(OSError):
                migrate_data_directory(self.root)
        self.assertEqual(original.read_bytes(), b"fictional")
        self.assertFalse((self.root / "data/memory/projects.json").exists())

    def test_invalid_backup_location_is_rejected_by_check(self) -> None:
        self.write("memory/projects.json")
        self.write("data/.migration-backups", b"occupied")
        before = self.snapshot()
        with self.assertRaises(MigrationError):
            migrate_data_directory(self.root, check_only=True)
        self.assertEqual(self.snapshot(), before)

    def test_read_only_legacy_files_are_preserved_and_moved(self) -> None:
        nested = self.root / "memory/nested"
        nested.mkdir(parents=True)
        original = self.write("memory/nested/legacy.json")
        original.chmod(stat.S_IREAD)
        nested.chmod(stat.S_IREAD | stat.S_IXUSR)
        result = migrate_data_directory(self.root)
        self.assertFalse(original.exists())
        self.assertFalse((self.root / "memory").exists())
        migrated = self.root / "data/memory/nested/legacy.json"
        self.assertEqual(migrated.read_bytes(), b"fictional")
        self.assertTrue(migrated.stat().st_mode & stat.S_IWRITE)
        migrated.write_bytes(b"updated")
        self.assertEqual(migrated.read_bytes(), b"updated")
        self.assertEqual((result.backup / "memory/nested/legacy.json").read_bytes(), b"fictional")

    def test_changed_source_during_backup_is_not_removed(self) -> None:
        original = self.write("memory/projects.json", b"before")
        from shutil import copytree

        def copy_then_change(source, destination):
            copytree(source, destination)
            original.write_bytes(b"after")

        with patch("migrate_data_directory.shutil.copytree", side_effect=copy_then_change):
            with self.assertRaisesRegex(MigrationError, "変化"):
                migrate_data_directory(self.root)
        self.assertEqual(original.read_bytes(), b"after")
        self.assertFalse((self.root / "data/memory/projects.json").exists())

    def test_link_target_is_rejected_without_touching_external_files(self) -> None:
        original = self.write("memory/projects.json")
        with tempfile.TemporaryDirectory() as external:
            target = Path(external)
            try:
                (self.root / "data").symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("この環境ではディレクトリのシンボリックリンクを作成できません")
            with self.assertRaisesRegex(MigrationError, "リンク"):
                migrate_data_directory(self.root)
            self.assertEqual(list(target.iterdir()), [])
            self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main()
