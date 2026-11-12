"""DB待機・JSON保存・グラフ描画の間もイベントループが進むことを検証する。"""

import asyncio
import io
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.core.background import spawn
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.core.database import Database
from persona_bot.views.functions_view import GraphButton
from persona_bot.views.view_parts import PanelView
from persona_bot.services import memory_service
from persona_bot.services.memory_service import MemoryService, default_projects_memory
from persona_bot.services.stats_service import StatsService
from persona_bot.utils.storage import load_json_file, write_json_file
from persona_bot.utils.usage_graph import UsageTracker


class EventLoopIOTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        paths = patch.multiple(
            memory_service, MEMORY_DIR=self.root,
            PROFILE_MEMORY_PATH=self.root / "profile.json",
            PROJECTS_MEMORY_PATH=self.root / "projects.json",
        )
        paths.start()
        self.addCleanup(paths.stop)
        self.db = Database(self.root / "bot.db")
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.memory = MemoryService(BotState(), None, self.db)

    def blocking_probe(self, result):
        loop = asyncio.get_running_loop()
        released = threading.Event()
        threads = []
        progressed = []

        def heartbeat():
            progressed.append(True)
            released.set()

        def operation(*args, **kwargs):
            threads.append(threading.get_ident())
            loop.call_soon_threadsafe(heartbeat)
            if not released.wait(1):
                raise AssertionError("同期処理の待機中にイベントループが進みません")
            return result

        return operation, threads, progressed

    async def test_profile_db_read_does_not_block_heartbeat(self) -> None:
        operation, threads, progressed = self.blocking_probe({"趣味": "架空の工作"})
        with patch.object(self.db, "profile_load", side_effect=operation):
            text = await self.memory.build_context("profile", "test-user")
        self.assertIn("架空の工作", text)
        self.assertTrue(progressed)
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_project_and_document_contexts_share_one_file_read(self) -> None:
        wrapper = default_projects_memory()
        wrapper["users"] = {"test-user": {"note": "架空の計画", "document_summaries": [
            {"document_name": "fictional.txt", "summary": "架空の要約"},
        ]}, "other-user": {"note": "他人の情報"}}
        write_json_file(self.root / "projects.json", wrapper)
        original = memory_service.load_json_file
        with patch.object(memory_service, "load_json_file", wraps=original) as read:
            context, documents = await self.memory.build_contexts("projects", "test-user")
        self.assertEqual(read.call_count, 1)
        self.assertIn("架空の計画", context)
        self.assertIn("架空の要約", documents)
        self.assertNotIn("他人の情報", context + documents)

    async def test_json_read_does_not_block_heartbeat(self) -> None:
        operation, threads, progressed = self.blocking_probe(default_projects_memory())
        with patch.object(memory_service, "load_json_file", side_effect=operation):
            await self.memory.build_document_memory_context("test-user")
        self.assertTrue(progressed)
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_blacklist_write_does_not_block_heartbeat(self) -> None:
        service = BlacklistService(self.root / "blacklist.json")
        operation, threads, progressed = self.blocking_probe(None)
        with patch("persona_bot.services.blacklist_service.write_json_file", side_effect=operation):
            result = await service.record_violation(11, "架空の利用者")
        self.assertEqual(result, (1, False))
        self.assertTrue(progressed)
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_dashboard_render_does_not_block_heartbeat(self) -> None:
        service = StatsService(self.db, None)
        service.cache["test-user"] = {**service._new_entry("架空"), "total_count": 1}
        operation, threads, progressed = self.blocking_probe(io.BytesIO(b"fictional-png"))
        with patch.object(service, "build_hourly_graph", side_effect=operation):
            _, images = await service.build_dashboard("test-user", "time")
        self.addCleanup(images[0][1].close)
        self.assertTrue(progressed)
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_real_database_lock_wait_keeps_loop_responsive(self) -> None:
        original = self.db.profile_load
        loop = asyncio.get_running_loop()
        progressed = []
        release_threads = []
        self.db._lock.acquire()

        def release():
            if self.db._lock.locked():
                progressed.append(True)
                release_threads.append(threading.get_ident())
                self.db._lock.release()

        def read(user_id):
            loop.call_soon_threadsafe(release)
            return original(user_id)

        watchdog = threading.Timer(2, release)
        watchdog.start()
        try:
            with patch.object(self.db, "profile_load", side_effect=read):
                await self.memory.build_context("profile", "test-user")
        finally:
            watchdog.cancel()
            release()
        self.assertTrue(progressed)
        self.assertEqual(release_threads, [threading.get_ident()])

    async def test_canceled_memory_write_keeps_lock_until_worker_finishes(self) -> None:
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = memory_service.write_json_file
        self.memory.ensure_storage()
        calls = []

        def write(path, data):
            calls.append(path)
            if len(calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("保存の再開を待てませんでした")
            original(path, data)

        with patch.object(memory_service, "write_json_file", side_effect=write):
            first = spawn(self.memory.store_document_summary("test-user", {"summary": "最初"}), label="保存1")
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                first.cancel()
                await asyncio.sleep(0)
                first.cancel()
                second = spawn(self.memory.store_document_summary("other-user", {"summary": "次"}), label="保存2")
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(len(calls), 1)
                self.assertTrue(self.memory.io_lock.locked())
            finally:
                release.set()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        self.assertTrue(first.cancelled())
        data = load_json_file(self.root / "projects.json", {})
        self.assertEqual(set(data["users"]), {"test-user", "other-user"})

    async def test_concurrent_violations_preserve_counts_and_other_users(self) -> None:
        path = self.root / "blacklist.json"
        service = BlacklistService(path)
        results = await asyncio.gather(*(service.record_violation(11, "架空") for _ in range(8)))
        await service.record_violation(22, "別の架空ユーザー")
        self.assertEqual([count for count, _ in results], [1, 2, 3, 3, 3, 3, 3, 3])
        self.assertEqual(sum(blocked for _, blocked in results), 1)
        self.assertTrue(service.is_blocked(11))
        self.assertTrue(await service.unblock(11))
        data = load_json_file(path, {})
        self.assertNotIn("11", data)
        self.assertEqual(data["22"]["count"], 1)

    async def test_failed_violation_save_does_not_change_cached_or_saved_data(self) -> None:
        path = self.root / "blacklist.json"
        service = BlacklistService(path)
        await service.record_violation(11, "架空")
        original = path.read_bytes()
        with patch("persona_bot.services.blacklist_service.write_json_file", side_effect=OSError("架空の障害")):
            with self.assertRaises(OSError):
                await service.record_violation(11, "架空")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(service._data["11"]["count"], 1)

    async def test_canceled_stats_save_cannot_overwrite_a_later_clear(self) -> None:
        service = StatsService(self.db, None)
        # 既存行の更新を中断し、元行への補償が終わるまで後着の削除を待たせる。
        await service.record("test-user", "架空", "変更前の発言", datetime.now(timezone.utc))
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        original = self.db.stats_save_user
        writes = []

        def save(user_id, entry):
            writes.append(entry["total_count"])
            if len(writes) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("保存の再開を待てませんでした")
            original(user_id, entry)

        with patch.object(self.db, "stats_save_user", side_effect=save):
            task = spawn(service.record("test-user", "架空", "保存中の発言", datetime.now(timezone.utc)), label="統計保存")
            clear = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                clear = spawn(service.clear_user_data("test-user"), label="統計削除")
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(writes, [2])
            finally:
                release.set()
                await asyncio.gather(task, *([clear] if clear else []), return_exceptions=True)
        self.assertEqual(writes, [2, 1, 0])
        self.assertEqual(self.db.stats_load_all()["test-user"]["total_count"], 0)

    async def test_usage_graph_uses_snapshot_during_new_records(self) -> None:
        usage = UsageTracker()
        usage.record({"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3})
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        received = []

        def render(*columns):
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("描画の再開を待てませんでした")
            received.extend(columns)
            return io.BytesIO(b"fictional-png")

        with patch.object(usage, "_render_graph_buffer", side_effect=render):
            task = spawn(usage.build_graph_buffer(), label="利用量の描画")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                usage.record({"totalTokenCount": 9})
            finally:
                release.set()
            buffer = await task
        self.addCleanup(buffer.close)
        self.assertEqual(received, [[1], [2], [3]])

    async def test_ranking_render_does_not_block_heartbeat(self) -> None:
        service = StatsService(self.db, None)
        service.cache["test-user"] = {**service._new_entry("架空"), "total_count": 1}
        operation, threads, progressed = self.blocking_probe(io.BytesIO(b"fictional-png"))
        with patch.object(service, "_render_ranking_graph", side_effect=operation):
            buffer = await service.build_ranking_graph()
        self.addCleanup(buffer.close)
        self.assertTrue(progressed)
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_dashboard_drops_graph_if_user_opts_out_during_render(self) -> None:
        service = StatsService(self.db, None)
        service.cache["test-user"] = {**service._new_entry("架空"), "total_count": 1}
        buffer = io.BytesIO(b"fictional-png")
        loop = asyncio.get_running_loop()
        released = threading.Event()

        def stop_collection():
            service.cache["test-user"]["opt_out"] = True
            released.set()

        def render(entry):
            loop.call_soon_threadsafe(stop_collection)
            if not released.wait(3):
                raise AssertionError("収集停止を待てませんでした")
            self.assertFalse(entry["opt_out"])
            return buffer

        with patch.object(service, "build_hourly_graph", side_effect=render):
            _, images = await service.build_dashboard("test-user", "time", viewer_id="admin")
        self.assertEqual(images, [])
        self.assertTrue(buffer.closed)

    async def test_privacy_change_then_new_record_does_not_restore_old_graph(self) -> None:
        for kind in ("dashboard", "ranking"):
            for action in ("clear", "opt_out"):
                with self.subTest(kind=kind, action=action):
                    service = StatsService(self.db, None)
                    service.cache["test-user"] = {**service._new_entry("架空"), "total_count": 20}
                    entered, release = asyncio.Event(), threading.Event()
                    loop = asyncio.get_running_loop()
                    buffer = io.BytesIO(b"old-graph")

                    def render(*args):
                        loop.call_soon_threadsafe(entered.set)
                        if not release.wait(3):
                            raise AssertionError("描画の再開を待てませんでした")
                        return buffer

                    target = "build_hourly_graph" if kind == "dashboard" else "_render_ranking_graph"
                    with patch.object(service, target, side_effect=render):
                        coroutine = (service.build_dashboard("test-user", "time") if kind == "dashboard"
                                     else service.build_ranking_graph())
                        task = spawn(coroutine, label="削除中の描画")
                        try:
                            await asyncio.wait_for(entered.wait(), 2)
                            if action == "clear":
                                await service.clear_user_data("test-user")
                            else:
                                await service.set_opt_out("test-user", "架空", True)
                                await service.set_opt_out("test-user", "架空", False)
                            await service.record("test-user", "架空", "新しい発言", datetime.now(timezone.utc))
                        finally:
                            release.set()
                        result = await task
                    self.assertEqual(result[1] if kind == "dashboard" else result, [] if kind == "dashboard" else None)
                    self.assertTrue(buffer.closed)

    async def test_canceled_dashboard_closes_previously_rendered_images(self) -> None:
        service = StatsService(self.db, None)
        service.cache["test-user"] = {**service._new_entry("架空"), "total_count": 1}
        buffer = io.BytesIO(b"hourly-graph")
        entered = asyncio.Event()

        async def analysis(*args):
            entered.set()
            await asyncio.Future()

        with patch.object(service, "build_hourly_graph", return_value=buffer):
            with patch.object(service, "ensure_analysis", side_effect=analysis):
                task = spawn(service.build_dashboard("test-user", "all"), label="描画キャンセル")
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        self.assertTrue(buffer.closed)

    async def test_graph_button_defers_before_render_and_reports_errors(self) -> None:
        for failed in (False, True):
            with self.subTest(failed=failed):
                interaction = SimpleNamespace(
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    followup=SimpleNamespace(send=AsyncMock()),
                )
                buffer = io.BytesIO(b"fictional-png")

                async def render():
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    if failed:
                        raise OSError("架空の描画失敗")
                    return buffer

                view = PanelView(timeout_notice="架空の時間切れ")  # 実行中の排他は PanelView が持つ（#415）
                self.addCleanup(view.stop)
                view.usage = SimpleNamespace(has_data=Mock(return_value=True), build_graph_buffer=AsyncMock(side_effect=render))
                button = GraphButton()
                view.add_item(button)
                await button.callback(interaction)
                interaction.response.send_message.assert_not_awaited()
                interaction.followup.send.assert_awaited_once()
                if failed:
                    self.assertIn("生成に失敗", interaction.followup.send.await_args.args[0])
                    buffer.close()
                else:
                    self.assertTrue(buffer.closed)

    async def test_actual_graphs_can_render_concurrently(self) -> None:
        service = StatsService(self.db, None)
        service.cache["test-user"] = {**service._new_entry("fictional"), "total_count": 1}
        usage = UsageTracker()
        usage.record({"totalTokenCount": 3})
        dashboard, ranking, tokens = await asyncio.gather(
            service.build_dashboard("test-user", "time"), service.build_ranking_graph(), usage.build_graph_buffer(),
        )
        for buffer in (dashboard[1][0][1], ranking, tokens):
            self.addCleanup(buffer.close)
            self.assertEqual(buffer.read(8), b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
