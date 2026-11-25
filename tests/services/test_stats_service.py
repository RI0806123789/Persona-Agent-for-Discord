"""会話統計のユーザー単位保存・並行更新・プライバシー操作を検証する。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import spawn
from persona_bot.config.constants import STATS_PERSONALITY_MIN_SAMPLES, STATS_RECENT_SAMPLES_MAX
from persona_bot.core.database import Database
from persona_bot.services import stats_service
from persona_bot.services.stats_service import StatsService
from persona_bot.utils import timeutil


class StatsCacheTimestampTests(unittest.TestCase):
    def test_cache_freshness_accepts_z_on_python310_and_other_iso_formats(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)

        class Python310DateTime(datetime):
            @classmethod
            def fromisoformat(cls, text):
                # Python3.10の境界を再現し、新しいPythonだけで通る検証にしない。
                if text.endswith("Z"):
                    raise ValueError("Python3.10では末尾Zを直接解釈できません")
                return super().fromisoformat(text)

            @classmethod
            def now(cls, tz=None):
                return now if tz is None else now.astimezone(tz)

        service = StatsService.__new__(StatsService)
        with patch.object(stats_service, "datetime", Python310DateTime), patch.object(timeutil, "datetime", Python310DateTime):
            for text in ("2026-01-01T23:00:00Z", "2026-01-01T23:00:00+00:00",
                         "2026-01-02T08:00:00+09:00", "2026-01-01T23:00:00"):
                with self.subTest(text=text):
                    self.assertTrue(service._is_fresh(text))
            for text in (None, "", "架空の無効な日時", "2020-01-01T00:00:00Z"):
                with self.subTest(text=text):
                    self.assertFalse(service._is_fresh(text))


class UserStatsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # OSのタイムゾーンDBやtzdataの有無に依存させず、日本時間の集計を検証する。
        timezone_patch = patch("persona_bot.services.stats_service.resolve_timezone", return_value=timezone(timedelta(hours=9)))
        timezone_patch.start()
        self.addCleanup(timezone_patch.stop)
        directory = tempfile.TemporaryDirectory(prefix="stats-test-")
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.when = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.generated_analysis = {
            "topics": [{"label": "架空の話題", "weight": 1}],
            "big_five": {"openness": 50}, "personality": "架空の解析結果",
        }
        self.gemini = SimpleNamespace(generate=AsyncMock(
            return_value=(json.dumps(self.generated_analysis), None, [])
        ))
        self.seed(2)

    def seed(self, count: int) -> None:
        entries = {}
        for i in range(count):
            entry = StatsService._new_entry(f"架空の利用者{i}")
            entry.update(total_count=1, recent_samples=["架空の会話"], opt_out=False)
            entry["hourly"][9] = 1
            entries[f"user-{i}"] = entry
        if count > 1:
            entries["user-1"]["opt_out"] = True
        self.db.stats_save_all(entries)
        self.service = StatsService(self.db, self.gemini)

    async def measured_write(self, operation):
        statements = []
        before = self.db._conn.total_changes
        self.db._conn.set_trace_callback(statements.append)
        try:
            result = await operation()
        finally:
            self.db._conn.set_trace_callback(None)
        writes = [sql for sql in statements
                  if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))]
        self.assertEqual(len(writes), 1)
        self.assertFalse(any(sql.lstrip().upper().startswith("DELETE") for sql in writes))
        self.assertEqual(self.db._conn.total_changes - before, 1)
        return result

    async def test_write_count_is_constant_for_one_hundred_and_thousand_users(self) -> None:
        for count in (1, 100, 1000):
            with self.subTest(user_count=count):
                self.seed(count)
                before = self.db.stats_load_all()
                result = await self.measured_write(lambda: self.service.record(
                    "user-0", "変更した架空の名前", "追加の架空発言", self.when
                ))
                self.assertTrue(result)
                after = self.db.stats_load_all()
                self.assertEqual({k: v for k, v in after.items() if k != "user-0"},
                                 {k: v for k, v in before.items() if k != "user-0"})

    async def test_record_round_trip_preserves_all_fields(self) -> None:
        original = deepcopy(self.service.cache["user-0"])
        self.assertTrue(await self.service.record("user-0", "新しい架空の名前", "追加の発言", self.when))
        record = self.db.stats_load_all()["user-0"]
        self.assertEqual(record["total_count"], 2)
        self.assertEqual(record["hourly"][9], 2)
        self.assertEqual(record["recent_samples"], ["架空の会話", "追加の発言"])
        self.assertEqual(record["first_seen"], original["first_seen"])
        self.assertEqual(record["user_name"], "新しい架空の名前")
        self.assertFalse(record["opt_out"])
        self.assertEqual(record, StatsService(self.db, self.gemini).cache["user-0"])

    async def test_missing_timezone_data_records_in_utc(self) -> None:
        from persona_bot.utils.timeutil import resolve_timezone

        with patch("persona_bot.services.stats_service.resolve_timezone", side_effect=resolve_timezone):
            with patch("persona_bot.utils.timeutil.ZoneInfo", side_effect=ZoneInfoNotFoundError("架空の欠落")):
                service = StatsService(self.db, self.gemini)
        self.assertTrue(await service.record("fallback-user", "架空", "UTCで集計", self.when))
        record = self.db.stats_load_all()["fallback-user"]
        self.assertEqual(record["hourly"], [1] + [0] * 23)
        self.assertEqual(service._tz, timezone.utc)

    async def test_analysis_setting_and_clear_each_save_only_the_target_user(self) -> None:
        other = deepcopy(self.db.stats_load_all()["user-1"])
        analysis = await self.measured_write(lambda: self.service.ensure_analysis("user-0", force=True))
        self.assertEqual(analysis["topics"], self.generated_analysis["topics"])
        self.assertEqual(self.db.stats_load_all()["user-0"]["analysis"], analysis)
        self.assertTrue(await self.measured_write(lambda: self.service.set_opt_out("user-0", "架空", True)))
        self.assertTrue(await self.measured_write(lambda: self.service.clear_user_data("user-0")))
        result = self.db.stats_load_all()
        self.assertEqual(result["user-1"], other)
        self.assertTrue(result["user-0"]["opt_out"])
        self.assertEqual(result["user-0"]["total_count"], 0)
        self.assertEqual(result["user-0"]["hourly"], [0] * 24)
        self.assertEqual(result["user-0"]["recent_samples"], [])
        self.assertIsNone(result["user-0"]["analysis"])

    async def test_opted_out_user_is_not_recorded_or_analysed(self) -> None:
        before = self.db.stats_load_all()["user-1"]
        await self.service.record("user-1", "架空", "記録しない発言", self.when)
        self.assertIsNone(await self.service.ensure_analysis("user-1", force=True))
        self.gemini.generate.assert_not_awaited()
        self.assertEqual(self.db.stats_load_all()["user-1"], before)

    async def test_only_opted_out_owner_can_read_saved_dashboard_without_changing_data(self) -> None:
        self.seed(1)
        self.assertTrue(await self.service.set_opt_out("user-0", "架空の本人", True))
        before = deepcopy(self.db.stats_load_all())
        self.assertFalse(self.service.has_data())
        self.assertEqual(self.service.get_user_options("user-0"), [])
        buffer = io.BytesIO(b"fictional-png")
        self.addCleanup(buffer.close)
        with patch.object(self.service, "build_hourly_graph", return_value=buffer):
            caption, images = await self.service.build_dashboard("user-0", "all", viewer_id="user-0")
        self.assertIn("停止前の記録", caption)
        self.assertIn("累計発言回数: 1 回", caption)
        self.assertEqual([name for name, _ in images], ["stats_hourly.png"])
        self.assertEqual(self.db.stats_load_all(), before)
        self.assertTrue(StatsService(self.db, self.gemini).is_opted_out("user-0"))
        self.gemini.generate.assert_not_awaited()

    async def test_other_viewer_cannot_distinguish_opt_out_from_empty_statistics(self) -> None:
        observed = []
        for opted_out in (True, False):
            entries = deepcopy(self.db.stats_load_all())
            hidden = StatsService._new_entry("架空の対象")
            hidden.update(total_count=8 if opted_out else 0, opt_out=opted_out)
            entries["user-1"] = hidden
            self.db.stats_save_all(entries)
            self.service = StatsService(self.db, self.gemini)
            buffer = io.BytesIO(b"fictional-ranking")
            self.addCleanup(buffer.close)
            with patch.object(self.service, "_render_ranking_graph", return_value=buffer) as rendering:
                graph = await self.service.build_ranking_graph()
            self.assertIs(graph, buffer)
            graph.close()
            caption, images = await self.service.build_dashboard("user-1", "all", viewer_id="user-0")
            self.assertEqual(images, [])
            self.assertNotIn("停止", caption)
            self.assertEqual([uid for uid, _, _ in self.service.get_user_options("user-1")], ["user-0"])
            self.assertEqual(rendering.call_args.args[0], [("架空の利用者0", 1)])
            observed.append((self.service.has_data(), self.service.get_user_options("user-1"),
                             caption, rendering.call_args.args))
        self.assertEqual(observed[0], observed[1])
        self.gemini.generate.assert_not_awaited()

    async def test_all_opted_out_users_are_absent_from_candidates_and_ranking(self) -> None:
        self.assertTrue(await self.service.set_opt_out("user-0", "架空の本人", True))
        self.assertFalse(self.service.has_data())
        self.assertEqual(self.service.get_user_options("user-0"), [])
        with patch.object(self.service, "_render_ranking_graph") as rendering:
            self.assertIsNone(await self.service.build_ranking_graph())
        rendering.assert_not_called()
        caption, images = await self.service.build_dashboard("user-1", "all", viewer_id="user-0")
        self.assertEqual(caption, "架空の利用者1 さんの会話データはまだありません。")
        self.assertEqual(images, [])
        self.gemini.generate.assert_not_awaited()

    async def test_concurrent_records_keep_counts_and_recent_samples(self) -> None:
        count = STATS_RECENT_SAMPLES_MAX + 10
        results = await asyncio.gather(*(
            self.service.record("user-0", "架空", f"発言-{i}", self.when) for i in range(count)
        ))
        self.assertTrue(all(results))
        entry = self.db.stats_load_all()["user-0"]
        self.assertEqual(entry["total_count"], count + 1)
        self.assertEqual(entry["hourly"][9], count + 1)
        self.assertEqual(entry["recent_samples"], [f"発言-{i}" for i in range(count - STATS_RECENT_SAMPLES_MAX, count)])
        self.assertEqual(entry, self.service.cache["user-0"])

    async def test_concurrent_users_do_not_overwrite_each_other(self) -> None:
        results = await asyncio.gather(*(
            self.service.record(f"new-{i % 4}", "架空", f"発言-{i}", self.when) for i in range(40)
        ))
        self.assertTrue(all(results))
        data = self.db.stats_load_all()
        for i in range(4):
            self.assertEqual(data[f"new-{i}"]["total_count"], 10)

    async def test_database_failures_are_reported_for_record_and_privacy_operations(self) -> None:
        before = deepcopy(self.db.stats_load_all())
        versions = deepcopy(self.service._analysis_versions)
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "CREATE TRIGGER fail_test_stats BEFORE INSERT ON user_stats "
                "BEGIN SELECT RAISE(ABORT, 'fictional write failure'); END"
            )
        self.assertFalse(await self.service.record("user-0", "架空", "追加", self.when))
        self.assertFalse(await self.service.set_opt_out("user-0", "架空", True))
        self.assertFalse(await self.service.clear_user_data("user-0"))
        self.assertEqual(self.db.stats_load_all(), before)
        self.assertEqual(self.service.cache, before)
        self.assertEqual(self.service._analysis_versions, versions)

    def operation(self, action: str, user_id: str = "user-0"):
        if action == "record":
            return self.service.record(user_id, "変更した架空の名前", "追加の架空の発言", self.when)
        if action in ("opt_out", "resume"):
            return self.service.set_opt_out(user_id, "変更した架空の名前", action == "opt_out")
        if action == "clear":
            return self.service.clear_user_data(user_id)
        return self.service.ensure_analysis(user_id, force=True)

    async def test_failed_candidates_keep_history_for_the_next_successful_record(self) -> None:
        for action in ("record", "opt_out", "clear", "analysis"):
            for failure in ("returned_false", "database_exception"):
                with self.subTest(action=action, failure=failure):
                    self.seed(2)
                    self.service.cache["user-0"]["analysis"] = deepcopy(self.generated_analysis)
                    await self.service.save_background("user-0")
                    self.service._analysis_versions["user-0"] = 7
                    before = deepcopy(self.service.cache)
                    versions = deepcopy(self.service._analysis_versions)
                    failing = (
                        patch.object(self.service, "_save_user_locked", new=AsyncMock(return_value=False))
                        if failure == "returned_false" else
                        patch.object(self.db, "stats_save_user", side_effect=OSError("架空の保存失敗"))
                    )
                    with failing:
                        result = await self.operation(action)
                    self.assertEqual(result, before["user-0"]["analysis"] if action == "analysis" else False)
                    self.assertEqual(self.service.cache, before)
                    self.assertEqual(self.service._analysis_versions, versions)
                    self.assertEqual(self.db.stats_load_all(), before)
                    self.assertTrue(await self.service.record("user-0", "架空", "成功した架空の発言", self.when))
                    saved = self.db.stats_load_all()["user-0"]
                    self.assertEqual(saved["total_count"], before["user-0"]["total_count"] + 1)
                    self.assertEqual(saved["hourly"][9], before["user-0"]["hourly"][9] + 1)
                    self.assertEqual(saved["recent_samples"], before["user-0"]["recent_samples"] + ["成功した架空の発言"])
                    self.assertEqual(saved["analysis"], before["user-0"]["analysis"])
                    self.assertEqual(saved, self.service.cache["user-0"])

    async def test_failed_new_user_candidates_leave_no_cache_or_database_row(self) -> None:
        for action in ("record", "opt_out"):
            for failure in ("returned_false", "database_exception"):
                with self.subTest(action=action, failure=failure):
                    before = deepcopy(self.service.cache)
                    failing = (
                        patch.object(self.service, "_save_user_locked", new=AsyncMock(return_value=False))
                        if failure == "returned_false" else
                        patch.object(self.db, "stats_save_user", side_effect=OSError("架空の保存失敗"))
                    )
                    with failing:
                        self.assertFalse(await self.operation(action, "new-user"))
                    self.assertEqual(self.service.cache, before)
                    self.assertEqual(self.db.stats_load_all(), before)
                    self.assertNotIn("new-user", self.service._analysis_versions)

    async def test_failed_resume_keeps_collection_stopped_and_does_not_invalidate_analysis(self) -> None:
        self.service._analysis_versions["user-1"] = 4
        before = deepcopy(self.service.cache)
        with patch.object(self.db, "stats_save_user", side_effect=OSError("架空の再開保存失敗")):
            self.assertFalse(await self.operation("resume", "user-1"))
        self.assertTrue(self.service.is_opted_out("user-1"))
        self.assertTrue(await self.service.record("user-1", "架空", "記録しない架空の発言", self.when))
        self.assertEqual(self.service.cache, before)
        self.assertEqual(self.db.stats_load_all(), before)
        self.assertEqual(self.service._analysis_versions, {"user-1": 4})

    async def test_candidates_are_not_visible_while_database_save_is_pending(self) -> None:
        for action in ("record", "opt_out", "clear", "analysis"):
            with self.subTest(action=action):
                self.seed(2)
                before = deepcopy(self.service.cache)
                versions = deepcopy(self.service._analysis_versions)
                entered, release = asyncio.Event(), threading.Event()
                loop = asyncio.get_running_loop()
                original_save = self.db.stats_save_user

                def save(user_id, entry):
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise AssertionError("架空の保存待機が解除されませんでした")
                    original_save(user_id, entry)

                with patch.object(self.db, "stats_save_user", side_effect=save):
                    task = spawn(self.operation(action), label="保存待機中の統計公開を検証")
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        self.assertEqual(self.service.cache, before)
                        self.assertEqual(self.service._analysis_versions, versions)
                        self.assertFalse(task.done())
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertFalse(task.cancelled())
                self.assertIsNone(task.exception())
                self.assertEqual(self.service.cache, self.db.stats_load_all())
                self.assertNotEqual(self.service.cache["user-0"], before["user-0"])

    async def test_repeated_cancellation_restores_existing_and_new_rows_before_unlocking(self) -> None:
        for action, user_id in (
            ("record", "user-0"), ("opt_out", "user-0"), ("clear", "user-0"),
            ("analysis", "user-0"), ("resume", "user-1"),
            ("record", "new-user"), ("opt_out", "new-user"),
        ):
            with self.subTest(action=action, user_id=user_id):
                self.seed(2)
                self.service._analysis_versions["user-0"] = 7
                before = deepcopy(self.service.cache)
                versions = deepcopy(self.service._analysis_versions)
                entered, restoring = asyncio.Event(), asyncio.Event()
                release, release_restore = threading.Event(), threading.Event()
                loop = asyncio.get_running_loop()
                original_save, original_delete = self.db.stats_save_user, self.db.stats_delete_user
                writes = 0

                def wait_for_restore():
                    loop.call_soon_threadsafe(restoring.set)
                    if not release_restore.wait(5):
                        raise AssertionError("架空の復元待機が解除されませんでした")

                def save(saved_user_id, entry):
                    nonlocal writes
                    writes += 1
                    if writes == 1:
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(5):
                            raise AssertionError("架空の保存待機が解除されませんでした")
                    else:
                        wait_for_restore()
                    original_save(saved_user_id, entry)

                def delete(saved_user_id):
                    wait_for_restore()
                    original_delete(saved_user_id)

                with patch.object(self.db, "stats_save_user", side_effect=save), patch.object(
                    self.db, "stats_delete_user", side_effect=delete,
                ):
                    task = spawn(self.operation(action, user_id), label="統計保存中の中断と復元を検証")
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertTrue(self.service._save_lock.locked())
                        self.assertEqual(self.service.cache, before)
                        release.set()
                        await asyncio.wait_for(restoring.wait(), 2)
                        task.cancel()
                        await asyncio.sleep(0)
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertTrue(self.service._save_lock.locked())
                        self.assertEqual(self.service.cache, before)
                        self.assertEqual(self.service._analysis_versions, versions)
                    finally:
                        release.set()
                        release_restore.set()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(task.cancelled())
                self.assertEqual(self.service.cache, before)
                self.assertEqual(self.service._analysis_versions, versions)
                self.assertEqual(self.db.stats_load_all(), before)
                self.assertFalse(self.service._save_lock.locked())
                self.assertEqual(self.service._analysis_tasks, {})

    async def test_failed_cancellation_compensation_is_logged_and_keeps_original_cancellation(self) -> None:
        before = deepcopy(self.service.cache)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_save = self.db.stats_save_user
        writes = 0

        def save(user_id, entry):
            nonlocal writes
            writes += 1
            if writes > 1:
                raise OSError("架空の復元失敗")
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("架空の保存待機が解除されませんでした")
            original_save(user_id, entry)

        with patch.object(self.db, "stats_save_user", side_effect=save), \
                self.assertLogs("persona_bot.services.stats_service", level="ERROR") as log:
            task = spawn(self.operation("clear"), label="統計復元失敗時の中断を検証")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertTrue(any("会話統計の中断後の復元に失敗" in record.getMessage() for record in log.records))
        self.assertEqual(self.service.cache, before)
        self.assertEqual(self.service._analysis_versions, {})

    async def check_analysis_overlap(self, action: str) -> None:
        started, resume = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            started.set()
            await resume.wait()
            return json.dumps(self.generated_analysis), None, []

        self.gemini.generate.side_effect = generate
        task = spawn(self.service.ensure_analysis("user-0", force=True), label="統計解析の回帰テスト")
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            if action == "clear":
                await self.service.clear_user_data("user-0")
            elif action == "opt_out":
                await self.service.set_opt_out("user-0", "架空", True)
            else:
                await self.service.record("user-0", "架空", "解析中の追加発言", self.when)
            resume.set()
            await asyncio.wait_for(task, timeout=5)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        entry = self.db.stats_load_all()["user-0"]
        if action in ("clear", "opt_out"):
            self.assertIsNone(entry["analysis"])
            if action == "clear":
                self.assertEqual(entry["recent_samples"], [])
                self.assertEqual(entry["total_count"], 0)
            else:
                self.assertTrue(entry["opt_out"])
        else:
            self.assertEqual(entry["total_count"], 2)
            self.assertIn("解析中の追加発言", entry["recent_samples"])
            self.assertEqual(entry["analysis"]["topics"], self.generated_analysis["topics"])

    async def test_analysis_does_not_restore_cleared_data(self) -> None:
        await self.check_analysis_overlap("clear")

    async def test_analysis_does_not_persist_after_opt_out(self) -> None:
        await self.check_analysis_overlap("opt_out")

    async def test_analysis_keeps_records_added_while_waiting(self) -> None:
        await self.check_analysis_overlap("record")

    async def test_failed_analysis_does_not_return_cache_cleared_while_waiting(self) -> None:
        for fail_with_exception in (False, True):
            with self.subTest(exception=fail_with_exception):
                self.seed(2)
                self.service.cache["user-0"]["analysis"] = deepcopy(self.generated_analysis)
                await self.service.save_background("user-0")

                async def generate(*args, **kwargs):
                    await self.service.clear_user_data("user-0")
                    if fail_with_exception:
                        raise RuntimeError("架空の解析エラー")
                    return "JSONではない架空の応答", None, []

                self.gemini.generate.side_effect = generate
                self.assertIsNone(await self.service.ensure_analysis("user-0", force=True))
                self.assertIsNone(self.db.stats_load_all()["user-0"]["analysis"])


    def start_analysis(self, user_id="user-0", *, force=False):
        task = spawn(self.service.ensure_analysis(user_id, force=force), label="並行統計解析の検証")

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(cleanup)
        return task

    async def test_concurrent_analysis_and_force_requests_share_the_same_work(self) -> None:
        for force in (False, True):
            with self.subTest(force=force):
                self.seed(2)
                self.gemini.generate.reset_mock()
                entered, release = asyncio.Event(), asyncio.Event()

                async def generate(*args, **kwargs):
                    entered.set()
                    await release.wait()
                    return json.dumps(self.generated_analysis), None, []

                self.gemini.generate.side_effect = generate
                first = self.start_analysis(force=force)
                await asyncio.wait_for(entered.wait(), 2)
                others = [self.start_analysis(force=force) for _ in range(4)]
                await asyncio.sleep(0)
                self.gemini.generate.assert_awaited_once()
                release.set()
                results = await asyncio.gather(first, *others)
                self.assertTrue(all(result == results[0] for result in results))
                self.assertIsNot(results[0], results[1])
                self.assertEqual(await self.service.ensure_analysis("user-0"), results[0])
                self.gemini.generate.assert_awaited_once()
                await self.service.ensure_analysis("user-0", force=True)
                self.assertEqual(self.gemini.generate.await_count, 2)
                self.assertEqual(self.service._analysis_tasks, {})

    async def test_older_analysis_cannot_overwrite_newer_completed_samples(self) -> None:
        """交代された古い解析は保存せず、キャッシュとDBは新しい結果を保持する（#451）。

        古い解析の呼び出し元には、計算済みの自分の結果をそのまま返す
        （途中で記録されたサンプルを使った新しい解析の完了を待たせない）。
        """
        self.service.cache["user-0"]["recent_samples"] = [f"架空の発言{i}" for i in range(10)]
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            analysis = deepcopy(self.generated_analysis)
            analysis["personality"] = "古い架空の解析" if index == 0 else "新しい架空の解析"
            return json.dumps(analysis), None, []

        self.gemini.generate.side_effect = generate
        older = self.start_analysis()
        await asyncio.wait_for(entered[0].wait(), 2)
        await self.service.record("user-0", "架空", "追加の架空の発言", self.when)
        newer = self.start_analysis()
        await asyncio.wait_for(entered[1].wait(), 2)
        release[1].set()
        adopted = await newer
        release[0].set()
        outdated = await older
        self.assertEqual(outdated["personality"], "古い架空の解析")
        self.assertEqual(outdated["sample_count"], 10)
        final = await self.service.ensure_analysis("user-0")
        self.assertEqual(final, adopted)
        self.assertEqual(final["sample_count"], 11)
        self.assertEqual(final["personality"], "新しい架空の解析")
        self.assertEqual(self.db.stats_load_all()["user-0"]["analysis"], final)
        self.assertEqual(calls, 2)

    async def test_superseded_analysis_prefers_its_own_result_over_stale_cache(self) -> None:
        """交代された解析は、TTL の切えたキャッシュより自分の計算結果を優先する（#451）。

        保存は交代先の解析が行うため、この返り値はキャッシュに書き込まれない。
        """
        stale = deepcopy(self.generated_analysis)
        stale["personality"] = "古いキャッシュの解析"
        stale["sample_count"] = 5
        stale["computed_at"] = "2020-01-01T00:00:00+00:00"
        self.service.cache["user-0"]["analysis"] = stale
        self.service.cache["user-0"]["recent_samples"] = [f"架空の発言{i}" for i in range(10)]
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            analysis = deepcopy(self.generated_analysis)
            analysis["personality"] = "古い解析の計算結果" if index == 0 else "新しい解析の計算結果"
            return json.dumps(analysis), None, []

        self.gemini.generate.side_effect = generate
        older = self.start_analysis()
        await asyncio.wait_for(entered[0].wait(), 2)
        await self.service.record("user-0", "架空", "追加の架空の発言", self.when)
        newer = self.start_analysis()
        await asyncio.wait_for(entered[1].wait(), 2)
        release[0].set()
        superseded = await older
        self.assertEqual(superseded["personality"], "古い解析の計算結果")
        # この時点ではまだ交代先の解析が完了していないため、キャッシュは古い値のまま。
        self.assertEqual(self.service.cache["user-0"]["analysis"]["personality"], "古いキャッシュの解析")
        release[1].set()
        adopted = await newer
        self.assertEqual(adopted["personality"], "新しい解析の計算結果")
        self.assertEqual(adopted["sample_count"], 11)
        final = await self.service.ensure_analysis("user-0")
        self.assertEqual(final, adopted)
        self.assertEqual(calls, 2)

    async def test_older_waiter_receives_result_when_newer_analysis_is_pending(self):
        entered = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await release[index].wait()
            return json.dumps(self.generated_analysis), None, []

        self.gemini.generate.side_effect = generate
        older = self.start_analysis()
        await entered[0].wait()
        await self.service.record("user-0", "架空", "新しい架空の発言", self.when)
        newer = self.start_analysis()
        await entered[1].wait()
        release[0].set()
        self.assertIsNotNone(await older)
        self.assertIsNone(self.service.cache["user-0"].get("analysis"))
        release[1].set()
        self.assertIsNotNone(await newer)

    async def test_force_request_during_save_shares_the_finishing_analysis(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = self.service._save_user_locked

        async def save(user_id, entry=None):
            entered.set()
            await release.wait()
            return await original_save(user_id, entry)

        with patch.object(self.service, "_save_user_locked", side_effect=save):
            first = self.start_analysis(force=True)
            await asyncio.wait_for(entered.wait(), 2)
            second = self.start_analysis(force=True)
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.gather(first, second)
        self.assertEqual(results[0], results[1])
        self.gemini.generate.assert_awaited_once()

    async def test_other_user_record_analysis_and_privacy_do_not_wait_for_generation(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(prompt, *args, **kwargs):
            if "待機する架空の発言" in prompt:
                entered.set()
                await release.wait()
            return json.dumps(self.generated_analysis), None, []

        self.service.cache["user-0"]["recent_samples"] = ["待機する架空の発言"]
        self.gemini.generate.side_effect = generate
        first = self.start_analysis()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(self.service.record("other", "架空", "別の架空の発言", self.when), 2)
        self.assertIsNotNone(await asyncio.wait_for(self.service.ensure_analysis("other"), 2))
        self.assertTrue(await asyncio.wait_for(self.service.set_opt_out("other", "架空", True), 2))
        self.assertTrue(await asyncio.wait_for(self.service.clear_user_data("other"), 2))
        release.set()
        await first

    async def test_cancelling_one_waiter_keeps_the_shared_analysis(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return json.dumps(self.generated_analysis), None, []

        self.gemini.generate.side_effect = generate
        first = self.start_analysis()
        await asyncio.wait_for(entered.wait(), 2)
        second = self.start_analysis(force=True)
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertIsNotNone(await second)
        self.gemini.generate.assert_awaited_once()
        self.assertEqual(self.service._analysis_tasks, {})

    async def test_last_waiter_cancellation_cleans_up_and_allows_retry(self) -> None:
        entered, canceled = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        self.gemini.generate.side_effect = generate
        first = self.start_analysis()
        await asyncio.wait_for(entered.wait(), 2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(first, 2)
        self.assertTrue(canceled.is_set())
        self.assertEqual(self.service._analysis_tasks, {})
        self.gemini.generate.side_effect = None
        self.assertIsNotNone(await self.service.ensure_analysis("user-0"))

    async def test_shared_failure_is_retired_and_next_request_retries(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("架空の解析失敗")

        self.gemini.generate.side_effect = generate
        first = self.start_analysis()
        await asyncio.wait_for(entered.wait(), 2)
        second = self.start_analysis()
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [None, None])
        self.gemini.generate.assert_awaited_once()
        self.assertEqual(self.service._analysis_tasks, {})
        self.gemini.generate.side_effect = None
        self.assertIsNotNone(await self.service.ensure_analysis("user-0"))

    async def test_shared_task_cancelled_before_start_does_not_remain_registered(self) -> None:
        def cancel_before_start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            task.cancel()
            return task

        with patch.object(stats_service, "spawn", side_effect=cancel_before_start):
            with self.assertRaises(asyncio.CancelledError):
                await self.service.ensure_analysis("user-0")
        self.assertEqual(self.service._analysis_tasks, {})
        self.assertIsNotNone(await self.service.ensure_analysis("user-0"))

    async def test_repeated_cancellation_during_analysis_save_waits_before_clear(self) -> None:
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original_save = self.db.stats_save_user

        def save(user_id, entry):
            if entry.get("analysis") is not None:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("架空の保存待機が解除されませんでした")
            original_save(user_id, entry)

        with patch.object(self.db, "stats_save_user", side_effect=save):
            first = self.start_analysis()
            clear = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                await asyncio.sleep(0)
                clear = spawn(self.service.clear_user_data("user-0"), label="解析中断中の統計削除")
                await asyncio.sleep(0)
                self.assertFalse(first.done())
                self.assertFalse(clear.done())
            finally:
                release.set()
                await asyncio.gather(first, *([clear] if clear else []), return_exceptions=True)
        self.assertTrue(first.cancelled())
        self.assertEqual(self.service._analysis_tasks, {})
        self.assertIsNone(self.db.stats_load_all()["user-0"]["analysis"])

    async def test_stop_and_resume_does_not_adopt_the_pre_stop_analysis(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(*args, **kwargs):
            entered.set()
            await release.wait()
            return json.dumps(self.generated_analysis), None, []

        self.gemini.generate.side_effect = generate
        first = self.start_analysis()
        await asyncio.wait_for(entered.wait(), 2)
        await self.service.set_opt_out("user-0", "架空", True)
        await self.service.set_opt_out("user-0", "架空", False)
        release.set()
        self.assertIsNone(await first)
        self.assertIsNone(self.db.stats_load_all()["user-0"]["analysis"])
        self.assertIsNotNone(await self.service.ensure_analysis("user-0"))


class StatsAnalysisTests(unittest.IsolatedAsyncioTestCase):
    """少数サンプル時の解析の省略と、LLM の型ゆらぎの正規化を確認する（#66）。"""

    async def asyncSetUp(self) -> None:
        timezone_patch = patch("persona_bot.services.stats_service.resolve_timezone", return_value=timezone(timedelta(hours=9)))
        timezone_patch.start()
        self.addCleanup(timezone_patch.stop)
        directory = tempfile.TemporaryDirectory(prefix="stats-analysis-test-")
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.analysis = {
            "topics": [{"label": "架空の話題", "weight": 1}],
            "big_five": {"openness": 60, "extraversion": 40}, "personality": "架空の推定",
        }
        self.gemini = SimpleNamespace(generate=AsyncMock(side_effect=lambda *args, **kwargs: (
            json.dumps(self.analysis), None, []
        )))

    def seed(self, sample_count: int) -> None:
        entry = StatsService._new_entry("架空の利用者")
        entry.update(total_count=sample_count, recent_samples=[f"架空の会話{i}" for i in range(sample_count)])
        entry["hourly"][9] = sample_count
        self.db.stats_save_all({"user-0": entry})
        self.service = StatsService(self.db, self.gemini)

    async def dashboard(self, item: str) -> tuple[str, list[str]]:
        caption, images = await self.service.build_dashboard("user-0", item)
        for _, buffer in images:
            buffer.close()
        return caption, [name for name, _ in images]

    async def test_incomplete_analysis_is_not_cached_and_next_request_retries(self) -> None:
        for defect in ("topics", "big_five"):
            with self.subTest(defect=defect):
                self.seed(STATS_PERSONALITY_MIN_SAMPLES)
                valid = deepcopy(self.analysis)
                self.analysis[defect] = [] if defect == "topics" else {key: 0 for key in stats_service.BIG_FIVE_LABELS}
                self.assertIsNone(await self.service.ensure_analysis("user-0"))
                self.assertIsNone(self.db.stats_load_all()["user-0"]["analysis"])
                self.analysis = valid
                result = await self.service.ensure_analysis("user-0")
                self.assertEqual(result["personality"], valid["personality"])

    async def test_broken_cached_analysis_and_samples_do_not_raise(self) -> None:
        """キャッシュの analysis が dict でない・recent_samples が list でなくても落ちない（#466）。"""
        for analysis, samples in (("壊れた解析", None), (["壊れた"], "壊れたサンプル"),
                                  (12, {"壊れた": 1}), ("壊れた解析", ["架空の会話", 3, None])):
            with self.subTest(analysis=analysis, samples=samples):
                self.seed(STATS_PERSONALITY_MIN_SAMPLES)
                self.gemini.generate.reset_mock()
                entry = self.service.cache["user-0"]
                entry["analysis"] = analysis
                entry["recent_samples"] = samples
                result = await self.service.ensure_analysis("user-0")
                if isinstance(samples, list):
                    # 文字列のサンプルだけで解析する（壊れた解析は使わずに作り直す）。
                    self.gemini.generate.assert_awaited_once()
                    self.assertEqual(result["topics"], self.analysis["topics"])
                else:
                    # サンプルが無いとみなし、壊れた解析も返さない。
                    self.gemini.generate.assert_not_awaited()
                    self.assertIsNone(result)
                caption, names = await self.dashboard("all")
                self.assertIn("累計発言回数", caption)
                self.assertIn("stats_hourly.png", names)

    async def test_recheck_after_rendering_uses_the_normalized_count(self) -> None:
        """描画中に回数が真偽値・不正な文字列へ壊れたら、描画後の確認で送らない（#466）。"""
        for broken in (True, "架空の不正な値", None):
            with self.subTest(broken=broken):
                self.seed(STATS_PERSONALITY_MIN_SAMPLES)
                original = self.service.build_hourly_graph

                def break_count_while_rendering(entry, *, value=broken):
                    self.service.cache["user-0"]["total_count"] = value
                    return original(entry)

                with patch.object(self.service, "build_hourly_graph", side_effect=break_count_while_rendering):
                    caption, images = await self.service.build_dashboard("user-0", "time")
                self.assertEqual(images, [])
                self.assertIn("まだありません", caption)

                self.seed(STATS_PERSONALITY_MIN_SAMPLES)
                original_ranking = self.service._render_ranking_graph

                def break_count_while_ranking(items, top_n, *, value=broken):
                    self.service.cache["user-0"]["total_count"] = value
                    return original_ranking(items, top_n)

                with patch.object(self.service, "_render_ranking_graph", side_effect=break_count_while_ranking):
                    self.assertIsNone(await self.service.build_ranking_graph())

    async def test_previously_cached_empty_analysis_retries_inside_ttl(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        self.service.cache["user-0"]["analysis"] = {
            "topics": [], "big_five": {}, "personality": "",
            "sample_count": STATS_PERSONALITY_MIN_SAMPLES,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        result = await self.service.ensure_analysis("user-0")
        self.gemini.generate.assert_awaited_once()
        self.assertEqual(result["topics"], self.analysis["topics"])

    async def test_deleting_stats_erases_persisted_display_name_and_preserves_opt_out(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        self.service.cache["user-0"]["opt_out"] = True
        self.assertTrue(await self.service.clear_user_data("user-0"))
        self.assertEqual(self.service.display_name("user-0"), "user-0")
        saved = self.db.stats_load_all()["user-0"]
        self.assertEqual(saved["user_name"], "")
        self.assertTrue(saved["opt_out"])

    async def test_analysis_skips_unrelated_json_and_keeps_cache_when_fields_are_missing(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        self.gemini.generate.side_effect = None
        self.gemini.generate.return_value = ('{"dummy": 1}\n' + json.dumps(self.analysis), None, [])
        analysis = await self.service.ensure_analysis("user-0", force=True)
        self.assertEqual(analysis["personality"], "架空の推定")
        self.assertEqual(analysis["topics"], self.analysis["topics"])
        for missing in ("topics", "big_five", "personality"):
            with self.subTest(missing=missing):
                invalid = {key: value for key, value in self.analysis.items() if key != missing}
                self.gemini.generate.return_value = (json.dumps(invalid), None, [])
                self.assertEqual(await self.service.ensure_analysis("user-0", force=True), analysis)
                self.assertEqual(self.db.stats_load_all()["user-0"]["analysis"], analysis)

    async def test_personality_only_with_few_samples_skips_analysis(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES - 1)
        caption, images = await self.dashboard("personality")
        self.gemini.generate.assert_not_awaited()
        self.assertIn("データ不足", caption)
        self.assertEqual(images, [])
        self.assertIsNone(self.service.cache["user-0"]["analysis"])

    async def test_opted_out_personality_without_cache_explains_paused_analysis(self) -> None:
        for item in ("all", "personality"):
            with self.subTest(item=item):
                self.seed(STATS_PERSONALITY_MIN_SAMPLES)
                await self.service.set_opt_out("user-0", "架空の利用者", True)
                caption, images = await self.dashboard(item)
                self.assertIn("統計収集を停止中のため、新しい解析は行っていません", caption)
                self.assertNotIn("解析に失敗", caption)
                self.assertNotIn("stats_personality.png", images)
                self.gemini.generate.assert_not_awaited()

    async def test_opted_out_personality_keeps_existing_cache(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        await self.service.ensure_analysis("user-0")
        self.gemini.generate.reset_mock()
        await self.service.set_opt_out("user-0", "架空の利用者", True)
        caption, images = await self.dashboard("personality")
        self.assertIn("架空の推定", caption)
        self.assertEqual(images, ["stats_personality.png"])
        self.assertNotIn("新しい解析は行っていません", caption)
        self.gemini.generate.assert_not_awaited()

    async def test_collecting_personality_without_result_keeps_failure_notice(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        self.gemini.generate.side_effect = None
        self.gemini.generate.return_value = ("", None, [])
        caption, images = await self.dashboard("personality")
        self.assertIn("解析に失敗", caption)
        self.assertNotIn("停止中", caption)
        self.assertEqual(images, [])

    async def test_few_sample_analysis_keeps_no_personality_and_refreshes_when_enough(self) -> None:
        self.seed(3)
        await self.dashboard("topic")
        self.gemini.generate.assert_awaited_once()
        cached = self.service.cache["user-0"]["analysis"]
        self.assertEqual((cached["big_five"], cached["personality"], cached["sample_count"]), ({}, "", 3))
        self.assertEqual(self.db.stats_load_all()["user-0"]["analysis"]["big_five"], {})
        # TTL 内でも、件数が揃ったら性格推定のために1回だけ解析し直す
        self.service.cache["user-0"]["recent_samples"] = [f"架空の会話{i}" for i in range(STATS_PERSONALITY_MIN_SAMPLES)]
        caption, images = await self.dashboard("personality")
        self.assertEqual(self.gemini.generate.await_count, 2)
        self.assertEqual(images, ["stats_personality.png"])
        self.assertIn("架空の推定", caption)
        await self.dashboard("personality")
        self.assertEqual(self.gemini.generate.await_count, 2)

    async def test_malformed_llm_output_is_normalized_before_caching_and_drawing(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        self.analysis = {
            "topics": ["架空の文字列だけの話題", {"label": "架空の非数値", "weight": "高"},
                       {"label": "", "weight": 1}, 5, {"label": "架空の数字文字列", "weight": "0.5"},
                       {"label": "架空の負の重み", "weight": -3}],
            "big_five": {"openness": "高い", "extraversion": 150, "agreeableness": float("nan"),
                         "neuroticism": float("inf"), "conscientiousness": True, "unknown": 50},
            "personality": 123,
        }
        caption, images = await self.dashboard("all")
        self.assertEqual(images, ["stats_hourly.png", "stats_topics.png", "stats_personality.png"])
        cached = self.service.cache["user-0"]["analysis"]
        self.assertEqual(cached["topics"], [
            {"label": "架空の文字列だけの話題", "weight": 1.0},
            {"label": "架空の数字文字列", "weight": 0.5},
            {"label": "架空の負の重み", "weight": 0.0},
        ])
        self.assertEqual(cached["big_five"], {"extraversion": 100.0})
        self.assertEqual(cached["personality"], "")
        self.assertIn("確定的な性格診断ではありません", caption)

    async def test_previously_saved_bad_cache_and_hourly_do_not_break_drawing(self) -> None:
        self.seed(STATS_PERSONALITY_MIN_SAMPLES)
        entry = self.service.cache["user-0"]
        # 以前の版が型を確かめずに保存したキャッシュ（sample_count なし・TTL 内）
        entry["analysis"] = {
            "topics": ["架空の話題", {"label": "架空", "weight": "高"}],
            "big_five": {"openness": "高い", "agreeableness": 70},
            "personality": "架空の旧推定", "computed_at": datetime.now(timezone.utc).isoformat(),
        }
        entry["hourly"] = [1, "x", None, float("nan"), -2]
        caption, images = await self.dashboard("all")
        self.gemini.generate.assert_not_awaited()
        self.assertEqual(images, ["stats_hourly.png", "stats_topics.png", "stats_personality.png"])
        self.assertIn("架空の旧推定", caption)
        # 時間帯は24個の0以上の有限数に揃え、不足分を0で埋める
        normalized = stats_service.normalize_hourly(entry["hourly"])
        self.assertEqual(normalized, [1.0] + [0.0] * 23)

    async def test_string_false_or_zero_opt_out_is_not_treated_as_opted_out(self) -> None:
        """opt_out の文字列 "false" や "0" を収集停止と誤認しない（#509）。"""
        for raw, expected in (("false", False), ("0", False), (0, False), ("true", True), ("1", True), (1, True)):
            self.assertIs(StatsService._entry_opted_out({"opt_out": raw}), expected)


if __name__ == "__main__":
    unittest.main()
