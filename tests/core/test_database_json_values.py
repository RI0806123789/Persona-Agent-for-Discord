"""旧JSONのnullと、通常保存のNoneを同じ既定値で扱えることを検証する。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import STATS_HOURLY_BUCKETS
from persona_bot.core.database import Database


class NullableJsonValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = Database(self.root / "bot.db", legacy_dir=self.root)
        self.addCleanup(self.db.close)

    @staticmethod
    def stats_entry(value):
        return dict.fromkeys((
            "user_name", "total_count", "hourly", "first_seen", "last_seen", "recent_samples", "analysis",
        ), value)

    @staticmethod
    def queue_task(value):
        return {
            "task_id": "fictional-task",
            **dict.fromkeys((
                "status", "channel_id", "message_id", "author_id", "question", "attachment_count",
                "created_at", "updated_at", "finished_at", "error",
            ), value),
        }

    def assert_stats_defaults(self, user_id: str) -> None:
        self.assertEqual(self.db.stats_load_all()[user_id], {
            "user_name": "", "total_count": 0, "hourly": [0] * STATS_HOURLY_BUCKETS,
            "first_seen": "", "last_seen": "", "recent_samples": [], "analysis": None, "opt_out": False,
        })

    def assert_queue_defaults(self) -> None:
        # pendingとして復元されるので、文書サービスが回収して処理を再開できる。
        task = self.db.queue_claim_next_pending("2026-01-01T00:00:00+00:00")
        self.assertIsNotNone(task)
        self.assertEqual(task, {
            "task_id": "fictional-task", "status": "processing", "channel_id": 0, "message_id": 0,
            "author_id": 0, "question": "", "attachment_count": 0, "created_at": "",
            "updated_at": "2026-01-01T00:00:00+00:00", "finished_at": None, "error": None,
        })

    def test_legacy_null_statistics_are_imported_and_backed_up(self) -> None:
        path = self.root / "user_stats.json"
        path.write_text(json.dumps({"11": self.stats_entry(None)}), encoding="utf-8")
        self.db.initialize()
        self.assert_stats_defaults("11")
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("user_stats.json.imported.bak").exists())

    def test_statistics_upsert_and_full_save_accept_null_and_missing_fields(self) -> None:
        self.db.initialize()
        for entry in ({}, self.stats_entry(None)):
            with self.subTest(entry=entry):
                self.db.stats_save_all({"11": entry})
                self.db.stats_save_user("22", entry)
                self.assert_stats_defaults("11")
                self.assert_stats_defaults("22")

    def test_statistics_import_and_save_preserve_all_fields_and_opt_out_aliases(self) -> None:
        cases = ({"opt_out": True}, {"stats_opt_out": True}, {"opt_out": False},
                 {"stats_opt_out": False}, {"opt_out": False, "stats_opt_out": True})
        entries = {
            str(index): {
                "user_name": "架空の利用者", "total_count": "7", "hourly": [1] * STATS_HOURLY_BUCKETS,
                "first_seen": "2026-01-01", "last_seen": "2026-01-02",
                "recent_samples": ["架空の発言"], "analysis": {"topic": "架空の工作"}, **flags,
            } for index, flags in enumerate(cases)
        }
        path = self.root / "user_stats.json"
        path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8-sig")
        self.db.initialize()
        imported = self.db.stats_load_all()
        for index, flags in enumerate(cases):
            expected = {**entries[str(index)], "total_count": 7,
                        "opt_out": flags.get("opt_out", flags.get("stats_opt_out"))}
            expected.pop("stats_opt_out", None)
            self.assertEqual(imported[str(index)], expected)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("user_stats.json.imported.bak").exists())
        # 同じ入力を通常保存・全件置換にも通し、9列すべてが一致することを確かめる。
        for user_id, entry in entries.items():
            self.db.stats_save_user(user_id, entry)
        self.assertEqual(self.db.stats_load_all(), imported)
        self.db.stats_save_all(entries)
        self.assertEqual(self.db.stats_load_all(), imported)
        self.db.close()
        self.db = Database(self.root / "bot.db", legacy_dir=self.root)
        self.addCleanup(self.db.close)
        self.db.initialize()
        self.assertEqual(self.db.stats_load_all(), imported)
        self.db.stats_save_all(imported)
        self.assertEqual(self.db.stats_load_all(), imported)

    def test_statistics_missing_opt_out_uses_same_default_for_import_and_saves(self) -> None:
        for default in (False, True):
            with self.subTest(default=default), patch("persona_bot.core.database.STATS_OPT_OUT_DEFAULT", default):
                path = self.root / "user_stats.json"
                path.write_text(json.dumps({f"legacy-{default}": {}}), encoding="utf-8")
                self.db.initialize()
                expected = self.db.stats_load_all()[f"legacy-{default}"]
                self.assertIs(expected["opt_out"], default)
                self.db.stats_save_user("saved-user", {})
                self.assertEqual(self.db.stats_load_all()["saved-user"], expected)
                self.db.stats_save_all({"replaced-user": {}})
                self.assertEqual(self.db.stats_load_all()["replaced-user"], expected)

    def test_null_opt_out_falls_back_to_legacy_alias_and_default(self) -> None:
        # opt_out が None で入っていても、キー欠落と同じく次の候補へ落とす（#411）。
        from persona_bot.core.database import _affinity_row_params, _user_stats_row_params
        self.assertEqual(_user_stats_row_params("1", {"opt_out": None, "stats_opt_out": True})[-1], 1)
        self.assertEqual(_user_stats_row_params("1", {"opt_out": False, "stats_opt_out": True})[-1], 0)
        for default in (False, True):
            with self.subTest(default=default), patch("persona_bot.core.database.STATS_OPT_OUT_DEFAULT", default):
                self.assertEqual(_user_stats_row_params("1", {"opt_out": None})[-1], int(default))
                self.assertEqual(
                    _user_stats_row_params("1", {"opt_out": None, "stats_opt_out": None})[-1], int(default),
                )
        # 旧JSONの文字列の数値も float に正規化する。
        self.assertEqual(_affinity_row_params("1", {"last_interaction": "12.5"})[-1], 12.5)
        self.assertIsInstance(_affinity_row_params("1", {"last_interaction": 3})[-1], float)
        self.assertEqual(_affinity_row_params("1", {})[-1], 0.0)

    def test_legacy_statistics_do_not_overwrite_existing_opt_out(self) -> None:
        self.db.initialize()
        self.db.stats_save_user("11", {"opt_out": True, "total_count": 5})
        expected = self.db.stats_load_all()["11"]
        path = self.root / "user_stats.json"
        path.write_text(json.dumps({"11": {"opt_out": False}, "22": {"stats_opt_out": True}}), encoding="utf-8")
        self.db.initialize()
        self.assertEqual(self.db.stats_load_all()["11"], expected)
        self.assertIs(self.db.stats_load_all()["22"]["opt_out"], True)
        self.assertFalse(path.exists())

    def test_failed_statistics_import_rolls_back_and_keeps_source_for_retry(self) -> None:
        path = self.root / "user_stats.json"
        path.write_text(json.dumps({"11": {"opt_out": True}, "22": {"total_count": "架空の不正値"}}),
                        encoding="utf-8")
        self.db.initialize()
        self.assertEqual(self.db.stats_load_all(), {})
        self.assertTrue(path.exists())
        self.assertFalse(path.with_name("user_stats.json.imported.bak").exists())

    def test_legacy_null_queue_values_are_imported_and_backed_up(self) -> None:
        path = self.root / "queue.json"
        path.write_text(json.dumps({"tasks": [self.queue_task(None)]}), encoding="utf-8")
        self.db.initialize()
        self.assert_queue_defaults()
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("queue.json.imported.bak").exists())

    def test_queue_upsert_accepts_null_and_missing_fields(self) -> None:
        self.db.initialize()
        for task in ({"task_id": "fictional-task"}, self.queue_task(None)):
            with self.subTest(task=task):
                self.db.queue_upsert_task(task)
                self.assert_queue_defaults()


if __name__ == "__main__":
    unittest.main()
