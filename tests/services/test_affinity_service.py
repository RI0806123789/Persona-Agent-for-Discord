"""親密度の対象ユーザー保存・保存順序と、既存定数による調整を検証する。"""

import asyncio
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import affinity_service
from persona_bot.services.affinity_service import AffinityService
from persona_bot.core.background import spawn
from persona_bot.core import database
from persona_bot.core.database import Database


class AffinityPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.addCleanup(lambda: self.db.close())
        clock = patch.object(affinity_service.time, "time", return_value=1000.0)
        clock.start()
        self.addCleanup(clock.stop)
        self.seed(3)

    def seed(self, count: int) -> None:
        self.db.affinity_save_all({
            f"user-{i}": {
                "affinity": 50, "valence": 50, "arousal": 50, "last_interaction": 1000.0,
            } for i in range(count)
        })
        self.service = AffinityService(self.db)

    async def persist(self, user_id: str) -> None:
        await self.service.save_background(user_id)

    async def test_write_count_is_constant_for_one_hundred_and_thousand_users(self) -> None:
        for count in (1, 100, 1000):
            with self.subTest(user_count=count):
                self.seed(count)
                before = self.db.affinity_load_all()
                updated = self.service.update_emotion("user-0", 30, -10).copy()
                statements = []
                before_changes = self.db._conn.total_changes
                self.db._conn.set_trace_callback(statements.append)
                try:
                    await self.persist("user-0")
                finally:
                    self.db._conn.set_trace_callback(None)
                writes = [statement for statement in statements if statement.lstrip().upper().startswith(
                    ("INSERT", "UPDATE", "DELETE", "REPLACE")
                )]
                self.assertEqual(len(writes), 1)
                self.assertNotIn("DELETE", writes[0].upper())
                self.assertEqual(self.db._conn.total_changes - before_changes, 1)
                after = self.db.affinity_load_all()
                self.assertEqual(after["user-0"], updated)
                self.assertEqual({key: value for key, value in after.items() if key != "user-0"},
                                 {key: value for key, value in before.items() if key != "user-0"})

    async def test_update_never_deletes_or_reinserts_other_rows(self) -> None:
        with self.db._lock, self.db._conn:
            # 値が同じでも削除・再挿入されていれば、DB自身が保存を拒否する。
            self.db._conn.executescript("""
                CREATE TRIGGER prevent_affinity_delete BEFORE DELETE ON affinity
                BEGIN SELECT RAISE(ABORT, '架空の削除検知'); END;
                CREATE TRIGGER protect_other_affinity_insert BEFORE INSERT ON affinity
                WHEN NEW.user_id != 'user-0'
                BEGIN SELECT RAISE(ABORT, '架空の他ユーザー挿入検知'); END;
                CREATE TRIGGER protect_other_affinity_update BEFORE UPDATE ON affinity
                WHEN OLD.user_id != 'user-0'
                BEGIN SELECT RAISE(ABORT, '架空の他ユーザー更新検知'); END;
            """)
            before_rowids = dict(self.db._conn.execute("SELECT user_id, rowid FROM affinity"))
        updated = self.service.update_emotion("user-0", 10, -5)
        await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all()["user-0"], updated)
        with self.db._lock:
            self.assertEqual(dict(self.db._conn.execute("SELECT user_id, rowid FROM affinity")), before_rowids)

    async def test_saved_values_survive_database_reopening(self) -> None:
        expected = self.service.update_emotion("new-user", 25, -35).copy()
        await self.persist("new-user")
        self.db.close()
        self.db = Database(self.path)
        self.db.initialize()
        restored = AffinityService(self.db)
        self.assertEqual(restored.cache["new-user"], expected)
        self.assertEqual(set(restored.cache["new-user"]),
                         {"affinity", "valence", "arousal", "last_interaction"})

    async def test_database_defaults_match_between_upsert_full_save_and_legacy_import(self) -> None:
        legacy = self.path.parent / "fictional-affinity.json"
        legacy.write_text('{"legacy-user": {}}', encoding="utf-8")
        with patch.object(database, "AFFINITY_NEUTRAL", 40):
            self.db.affinity_save_all({"explicit-user": {}})
            self.db.affinity_upsert("new-user", {})
            self.db._legacy.import_affinity_json(legacy)
        for user_id, entry in self.db.affinity_load_all().items():
            with self.subTest(user_id=user_id):
                self.assertEqual(entry, {
                    "affinity": 40, "valence": 40, "arousal": 40, "last_interaction": 0.0,
                })

    async def test_explicit_none_uses_defaults_and_zero_remains_zero_in_every_save_path(self) -> None:
        import json

        for value, expected in ((None, 40), (0, 0)):
            with self.subTest(value=value):
                entry = dict.fromkeys(("affinity", "valence", "arousal", "last_interaction"), value)
                legacy = self.path.parent / "fictional-affinity.json"
                legacy.write_text(json.dumps({"legacy-user": entry}), encoding="utf-8")
                with patch.object(database, "AFFINITY_NEUTRAL", 40):
                    self.db.affinity_save_all({"explicit-user": entry})
                    self.db.affinity_upsert("new-user", entry)
                    self.db._legacy.import_affinity_json(legacy)
                for data in self.db.affinity_load_all().values():
                    self.assertEqual(data, {
                        "affinity": expected, "valence": expected, "arousal": expected, "last_interaction": 0.0,
                    })

    async def test_missing_user_does_not_rewrite_existing_data(self) -> None:
        before = self.db.affinity_load_all()
        before_changes = self.db._conn.total_changes
        await self.persist("missing-user")
        self.assertEqual(self.db._conn.total_changes, before_changes)
        self.assertEqual(self.db.affinity_load_all(), before)

    async def test_every_save_path_bounds_affinity_and_emotions(self) -> None:
        import json

        for value, expected in ((-9, 0), (109, 100), ("125", 100)):
            with self.subTest(value=value):
                entry = dict.fromkeys(("affinity", "valence", "arousal"), value)
                legacy = self.path.parent / "fictional-affinity.json"
                legacy.write_text(json.dumps({"legacy-user": entry}), encoding="utf-8")
                self.db.affinity_save_all({"explicit-user": entry})
                self.db.affinity_upsert("new-user", entry)
                self.db._legacy.import_affinity_json(legacy)
                for data in self.db.affinity_load_all().values():
                    self.assertEqual((data["affinity"], data["valence"], data["arousal"]), (expected,) * 3)

    async def test_independent_users_keep_both_updates(self) -> None:
        first = self.service.update_emotion("user-0", 15, -5).copy()
        second = self.service.update_emotion("user-1", -20, 30).copy()
        untouched = self.service.cache["user-2"].copy()
        await asyncio.gather(self.persist("user-0"), self.persist("user-1"))
        self.assertEqual(self.db.affinity_load_all(), {
            "user-0": first, "user-1": second, "user-2": untouched,
        })

    async def check_delayed_save(self, *, cancel_first: bool) -> None:
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.db.affinity_upsert
        snapshots = []

        def write(user_id, entry):
            snapshots.append(deepcopy(entry))
            if len(snapshots) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("架空の保存処理を再開できませんでした")
            original(user_id, entry)

        self.service.update_emotion("user-0", 5, 0)
        with patch.object(self.db, "affinity_upsert", side_effect=write):
            first = spawn(self.persist("user-0"), label="親密度の先行保存")
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                if cancel_first:
                    for _ in range(2):
                        first.cancel()
                        await asyncio.sleep(0)
                expected = self.service.update_emotion("user-0", 7, 0).copy()
                second = spawn(self.persist("user-0"), label="親密度の後続保存")
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(len(snapshots), 1)
                self.assertTrue(self.service._save_lock.locked())
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertEqual(first.cancelled(), cancel_first)
        self.assertEqual([entry["valence"] for entry in snapshots], [55, 62])
        self.assertEqual(self.db.affinity_load_all()["user-0"], expected)

    async def test_later_update_is_saved_after_earlier_snapshot(self) -> None:
        await self.check_delayed_save(cancel_first=False)

    async def test_canceled_save_keeps_lock_until_worker_finishes(self) -> None:
        await self.check_delayed_save(cancel_first=True)

    async def test_save_failure_keeps_old_database_and_cached_update_for_retry(self) -> None:
        before = self.db.affinity_load_all()
        expected = self.service.update_emotion("user-0", 8, -6).copy()
        with patch.object(self.db, "affinity_upsert", side_effect=OSError("架空の保存失敗")):
            await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all(), before)
        self.assertEqual(self.service.cache["user-0"], expected)
        await self.persist("user-0")
        self.assertEqual(self.db.affinity_load_all()["user-0"], expected)


class AffinityTuningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AffinityService.__new__(AffinityService)
        self.service.cache = {}
        clock = patch.object(affinity_service.time, "time", return_value=1000.0)
        clock.start()
        self.addCleanup(clock.stop)
        values = patch.multiple(
            affinity_service, AFFINITY_HALF_LIFE_SECONDS=60.0, AFFINITY_NEUTRAL=40,
            AFFINITY_MIN=20, AFFINITY_MAX=80,
            AFFINITY_RAISE_THRESHOLD=45, AFFINITY_LOWER_THRESHOLD=35, create=True,
        )
        values.start()
        self.addCleanup(values.stop)

    def test_clock_rollback_does_not_amplify_emotions(self) -> None:
        data = {"affinity": 60, "valence": 70, "arousal": 25, "last_interaction": 1001.0}
        self.assertEqual(self.service._apply_decay(data.copy()), data)

    def test_new_user_reads_clock_only_once(self) -> None:
        with patch.object(affinity_service.time, "time", side_effect=[1000.0]) as clock:
            self.assertEqual(self.service.get_user_data("new-user")["last_interaction"], 1000.0)
        clock.assert_called_once()

    def test_new_user_uses_configured_neutral_value(self) -> None:
        data = self.service.get_user_data("new-user")
        self.assertEqual((data["affinity"], data["valence"], data["arousal"]), (40, 40, 40))

    def test_half_life_converges_toward_configured_neutral_value(self) -> None:
        data = self.service._apply_decay({
            "affinity": 70, "valence": 80, "arousal": 20, "last_interaction": 940.0,
        })
        self.assertEqual((data["affinity"], data["valence"], data["arousal"]), (70, 60, 30))

    def test_configured_thresholds_keep_strict_boundaries(self) -> None:
        for change, expected_affinity in ((6, 41), (5, 40), (-5, 40), (-6, 39)):
            with self.subTest(change=change):
                self.service.cache.clear()
                data = self.service.update_emotion("new-user", change, 0)
                self.assertEqual(data["affinity"], expected_affinity)

    def test_configured_bounds_apply_to_emotion_and_affinity(self) -> None:
        self.service.cache["upper-user"] = {
            "affinity": 80, "valence": 40, "arousal": 40, "last_interaction": 1000.0,
        }
        self.service.cache["lower-user"] = {
            "affinity": 20, "valence": 40, "arousal": 40, "last_interaction": 1000.0,
        }
        upper = self.service.update_emotion("upper-user", 1000, -1000)
        lower = self.service.update_emotion("lower-user", -1000, 1000)
        self.assertEqual((upper["affinity"], upper["valence"], upper["arousal"]), (80, 80, 20))
        self.assertEqual((lower["affinity"], lower["valence"], lower["arousal"]), (20, 20, 80))


class AffinityTagTests(unittest.TestCase):
    """感情タグの大文字・小文字のゆらぎを抽出・除去できることを確認する（#54）。"""

    BODY = "架空の応答です。"

    def test_lowercase_and_mixed_case_tags_are_extracted_and_removed(self) -> None:
        cases = {
            "[v:+5, a:-10]": (5, -10),
            "[v:0, A:+2]": (0, 2),
            "[V:3, a:4]": (3, 4),
            "[v: +5 , a: -10 ]": (5, -10),
            "[V:+5, A:-10]": (5, -10),
        }
        for tag, changes in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(
                    affinity_service.extract_affinity_changes(self.BODY + tag),
                    (self.BODY, changes),
                )
                self.assertEqual(affinity_service.strip_affinity_tag(tag + self.BODY), self.BODY)

    def test_text_without_tag_is_returned_unchanged(self) -> None:
        text = f" {self.BODY}[x:1, y:2] "
        self.assertEqual(affinity_service.extract_affinity_changes(text), (text, None))

    def test_trailing_metadata_overrides_tags_quoted_in_body(self) -> None:
        text = "タグ[V:+5, A:+5]は表記例です。\n[V:-1, A:0] \n"
        self.assertEqual(affinity_service.extract_affinity_changes(text), ("タグ[V:+5, A:+5]は表記例です。", (-1, 0)))

    def test_quoted_tags_without_trailing_metadata_do_not_change_emotion(self) -> None:
        text = "  `[V:+1, A:0]`という表記を説明します。 \n"
        self.assertEqual(affinity_service.extract_affinity_changes(text), (text, None))

    def test_whitespace_separated_trailing_tags_preserve_quoted_body(self) -> None:
        body = "`[V:+1, A:0]`は表記例です。"
        text = body + "\n[V:1, A:2] \n [v: -3, a: +4] \n"
        self.assertEqual(affinity_service.extract_affinity_changes(text), (body, (-3, 4)))

    def test_last_metadata_tag_wins_when_multiple_tags_are_emitted(self) -> None:
        text = self.BODY + "[V:1, A:2][v: -3, a: +4]"
        self.assertEqual(affinity_service.extract_affinity_changes(text), (self.BODY, (-3, 4)))

    def test_speech_removes_combined_numeric_emotion_tags(self) -> None:
        for tag in (
            "[Valence: +5, Arousal: 0]", "[ arousal:-2, valence:3 ]",
            "[Affinity:50, Valence: +5, Arousal: -1]", "[Valence:0]", "[happy]", "[V:1, A:2]",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(affinity_service.strip_speech_tags(self.BODY + tag), self.BODY)

    def test_speech_preserves_ordinary_bracketed_text(self) -> None:
        text = "[Python]で[Valence: は数値の説明]と[Valence: 5, 本文]を扱います。"
        self.assertEqual(affinity_service.strip_speech_tags(text), text)


if __name__ == "__main__":
    unittest.main()
