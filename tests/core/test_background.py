"""同期ワーカーの完了待ちとキャンセル時の結果回収を確かめる（#88）。"""

import asyncio
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.background import TaskOwner, run_in_thread, spawn


class RunInThreadTests(unittest.IsolatedAsyncioTestCase):
    async def assert_canceled_after_worker(self, function):
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def work():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise AssertionError("テストのワーカーが解放されませんでした。")
            return function()

        task = asyncio.create_task(run_in_thread(work))
        try:
            await asyncio.wait_for(started.wait(), 2)
            # 繰り返し中断されても、同期処理が終わるまで呼出元へ戻さない。
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertTrue(task.cancelled())
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_normal_return_preserves_result_without_closing(self):
        result = SimpleNamespace(close=Mock())
        self.assertIs(await run_in_thread(lambda: result), result)
        result.close.assert_not_called()

    async def test_worker_error_propagates_without_cancellation(self):
        error = OSError("架空の同期処理エラー")
        with self.assertRaises(OSError) as raised:
            await run_in_thread(Mock(side_effect=error))
        self.assertIs(raised.exception, error)

    async def test_cancellation_closes_buffer(self):
        result = io.BytesIO(b"fictional")
        await self.assert_canceled_after_worker(lambda: result)
        self.assertTrue(result.closed)

    async def test_cancellation_ignores_results_without_close(self):
        for result in (None, object(), {"close": True}):
            with self.subTest(result=type(result).__name__):
                await self.assert_canceled_after_worker(lambda: result)

    async def test_cancellation_ignores_noncallable_close(self):
        for close in (True, "closed", 1, {}, None):
            with self.subTest(close=close):
                await self.assert_canceled_after_worker(lambda: SimpleNamespace(close=close))

    async def test_close_error_does_not_replace_cancellation(self):
        close = Mock(side_effect=OSError("架空の回収エラー"))
        with self.assertLogs("persona_bot.core.background", level="ERROR"):
            await self.assert_canceled_after_worker(lambda: SimpleNamespace(close=close))
        close.assert_called_once_with()

    async def test_close_property_error_does_not_replace_cancellation(self):
        class Result:
            @property
            def close(self):
                raise OSError("架空の属性取得エラー")

        with self.assertLogs("persona_bot.core.background", level="ERROR"):
            await self.assert_canceled_after_worker(Result)

    async def test_worker_error_does_not_replace_cancellation(self):
        await self.assert_canceled_after_worker(Mock(side_effect=OSError("架空の同期処理エラー")))


class TaskOwnerShutdownTests(unittest.IsolatedAsyncioTestCase):
    def run_without_task(self, coro):
        """イベントループのコールバックから、Taskで包まずにコルーチンを駆動する。"""
        loop = asyncio.get_running_loop()
        result = loop.create_future()

        def advance(_=None):
            try:
                awaited = coro.send(None)
            except StopIteration as finished:
                result.set_result(finished.value)
            except BaseException as error:
                result.set_exception(error)
            else:
                awaited.add_done_callback(advance)

        loop.call_soon(advance)
        return result

    async def test_run_without_current_task_tracks_body_and_children(self):
        owner = TaskOwner()
        child = None

        async def work():
            nonlocal child
            self.assertIn(asyncio.current_task(), owner._tasks)
            child = spawn(asyncio.sleep(0), label="架空の子処理")
            self.assertIn(child, owner._tasks)
            await child
            return "架空の結果"

        self.assertEqual(await self.run_without_task(owner.run(work())), "架空の結果")
        self.assertTrue(child.done())
        await owner.shutdown()
        self.assertEqual(owner._tasks, {})

    async def test_run_without_current_task_is_canceled_by_owner(self):
        owner = TaskOwner()
        started = asyncio.Event()
        cleaned = Mock()

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned()

        result = self.run_without_task(owner.run(work()))
        await asyncio.wait_for(started.wait(), 1)
        await owner.shutdown(timeout=1)
        with self.assertRaises(asyncio.CancelledError):
            await result
        cleaned.assert_called_once_with()
        self.assertEqual(owner._tasks, {})

    async def test_run_without_current_task_rejects_work_after_stop(self):
        owner = TaskOwner()
        owner.stop_accepting()
        body = Mock()

        async def work():
            body()

        coro = work()
        with self.assertRaises(asyncio.CancelledError):
            await self.run_without_task(owner.run(coro))
        body.assert_not_called()
        self.assertIsNone(coro.cr_frame)
        self.assertEqual(owner._tasks, {})

    async def test_rejected_spawn_is_tracked_until_cancellation_finishes(self):
        owner = TaskOwner()
        owner.stop_accepting()
        body = Mock()

        async def late_work():
            body()

        task = spawn(late_work(), label="停止後の架空の要求", owner=owner)
        self.assertIn(task, owner._tasks)
        await owner.shutdown()
        self.assertTrue(task.cancelled())
        self.assertEqual(owner._tasks, {})
        body.assert_not_called()

    async def test_timeout_preserves_protected_work_and_allows_later_drain(self):
        owner = TaskOwner()
        release = asyncio.Event()
        task = spawn(release.wait(), label="架空の終了待機", owner=owner, _protected=True)
        try:
            with self.assertLogs("persona_bot.core.background", level="ERROR"):
                with self.assertRaises(TimeoutError):
                    await owner.shutdown(timeout=0.01)
            self.assertFalse(task.done())
            self.assertIn(task, owner._tasks)
        finally:
            release.set()
            await owner.shutdown(timeout=1)
        self.assertEqual(owner._tasks, {})


if __name__ == "__main__":
    unittest.main()
