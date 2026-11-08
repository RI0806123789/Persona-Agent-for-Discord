"""サブエージェントの進捗メッセージ・中止ボタン・全体の期限を検証する（#170）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import discord

from tests.helpers.discord_mocks import ONLY_RESPONSE, make_http_response, make_interaction, make_response
from persona_bot.config.constants import AGENT_DEFAULT_LINES
from persona_bot.core.background import TaskOwner, spawn
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport
from persona_bot.views import agent_view
from persona_bot.views.agent_view import AgentProgress

PLAN = AgentPlan(True, "架空の調査担当", "架空の依頼", "箇条書き", ("候補の検索", "特徴の比較"))
LINES = dict(AGENT_DEFAULT_LINES, progress="{step}中（{current}/{total}）", done="架空の完了", failed="架空の失敗")


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # View の停止状態はイベントループの中で作られるため、ループ内で組み立てる
        self.sent = SimpleNamespace(edit=AsyncMock())
        self.source = SimpleNamespace(author=SimpleNamespace(id=11), reply=AsyncMock(return_value=self.sent))
        self.progress = AgentProgress(self.source, LINES, PLAN)
        self.addCleanup(self.progress._view.stop)

    async def test_start_update_and_finish_use_template_lines(self) -> None:
        await self.progress.start()
        content = self.source.reply.await_args.args[0]
        self.assertIn("架空の調査担当", content)
        self.assertIn("候補の検索中（1/2）", content)
        # 進捗は誰にも通知を飛ばさず、中止ボタンを付ける
        self.assertEqual(self.source.reply.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertIs(self.source.reply.await_args.kwargs["view"], self.progress._view)

        with patch.object(agent_view, "monotonic", return_value=self.progress._last_edit + 1):
            await self.progress.update(1, "特徴の比較")
        # 間隔が短い書き換えは見送る（Discord のレート制限）
        self.sent.edit.assert_not_awaited()
        with patch.object(agent_view, "monotonic", return_value=self.progress._last_edit + 60):
            await self.progress.update(1, "特徴の比較")
        self.assertIn("特徴の比較中（2/2）", self.sent.edit.await_args.kwargs["content"])

        await self.progress.finish("done")
        # 終了の書き換えは間隔に関わらず行い、中止ボタンを外す
        self.assertIn("架空の完了", self.sent.edit.await_args.kwargs["content"])
        self.assertIsNone(self.sent.edit.await_args.kwargs["view"])
        self.assertTrue(self.progress._view.is_finished())

    async def test_progress_failures_do_not_raise(self) -> None:
        self.source.reply.side_effect = [discord.DiscordException("架空の送信失敗"), None]
        with patch("persona_bot.views.agent_view.logger"):
            await self.progress.start()
            await self.progress.update(1, "特徴の比較")
            await self.progress.finish("failed")
        # 進捗を出せなかったときも、失敗は定型文で伝える
        self.assertEqual(self.source.reply.await_args.args[0], "架空の失敗")

    async def test_run_returns_report(self) -> None:
        async def work():
            return AgentReport("架空の報告", [])

        self.assertEqual(await self.progress.run(work()), ("done", AgentReport("架空の報告", [])))

    async def test_run_reports_failure_for_none_or_exception(self) -> None:
        async def empty():
            return None

        async def broken():
            raise RuntimeError("架空の障害")

        with patch("persona_bot.views.agent_view.logger"):
            self.assertEqual(await self.progress.run(empty()), ("failed", None))
            self.assertEqual(await self.progress.run(broken()), ("failed", None))

    async def test_cancel_button_stops_the_worker(self) -> None:
        started = asyncio.Event()
        worker_cancelled = asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        run = asyncio.create_task(self.progress.run(slow()))
        await started.wait()
        self.progress._cancel_event.set()
        with patch("persona_bot.views.agent_view.logger"):
            self.assertEqual(await run, ("canceled", None))
        await asyncio.wait_for(worker_cancelled.wait(), 1)

    async def test_total_timeout_stops_the_worker(self) -> None:
        async def slow():
            await asyncio.sleep(3600)

        with patch.object(agent_view, "AGENT_TOTAL_TIMEOUT_SECONDS", 0.01), patch("persona_bot.views.agent_view.logger"):
            self.assertEqual(await self.progress.run(slow()), ("failed", None))

    async def test_caller_cancellation_propagates_and_stops_worker(self) -> None:
        started = asyncio.Event()
        worker_cancelled = asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                worker_cancelled.set()
                raise

        run = asyncio.create_task(self.progress.run(slow()))
        await started.wait()
        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run
        await asyncio.wait_for(worker_cancelled.wait(), 1)
        self.assertTrue(self.progress._view.is_finished())


    async def wait_for_worker_cleanup(self, trigger, *, repeat=False, broken=False, report=False, late_button=False):
        started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        children = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            children.append(task)
            return task

        async def work():
            try:
                started.set()
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # 中止を握り潰す子でも、依頼の終了理由は変えない。
                if report:
                    return AgentReport("中止後の架空の報告", [])
                raise
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
                if broken:
                    raise RuntimeError("架空の子の回収失敗")

        with patch.object(agent_view, "spawn", side_effect=start), patch.object(
            agent_view, "AGENT_TOTAL_TIMEOUT_SECONDS", 0.01 if trigger == "timeout" else 30,
        ), patch("persona_bot.views.agent_view.logger"):
            run = asyncio.create_task(self.progress.run(work()))
            try:
                await asyncio.wait_for(started.wait(), 2)
                if trigger == "button":
                    self.progress._cancel_event.set()
                elif trigger == "parent":
                    run.cancel()
                await asyncio.wait_for(cleaning.wait(), 2)
                self.assertFalse(run.done())
                self.assertFalse(cleaned.is_set())
                if late_button:
                    self.progress._cancel_event.set()
                if repeat:
                    for _ in range(3):
                        run.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(run.done())
                        self.assertFalse(cleaned.is_set())
                release.set()
                if trigger == "parent" or repeat:
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(run, 2)
                else:
                    expected = "canceled" if trigger == "button" else "failed"
                    self.assertEqual(await asyncio.wait_for(run, 2), (expected, None))
                self.assertTrue(cleaned.is_set())
                self.assertEqual(len(children), 2)
                self.assertTrue(all(task.done() for task in children))
            finally:
                release.set()
                await asyncio.gather(run, *children, return_exceptions=True)

    async def test_all_stop_modes_wait_for_worker_finally(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress._cancel_event.clear()
                await self.wait_for_worker_cleanup(trigger)

    async def test_repeated_parent_cancellation_does_not_interrupt_worker_cleanup(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress._cancel_event.clear()
                await self.wait_for_worker_cleanup(trigger, repeat=True)

    async def test_worker_cleanup_exception_preserves_the_stop_outcome(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress._cancel_event.clear()
                await self.wait_for_worker_cleanup(trigger, broken=True)

    async def test_report_returned_after_cancellation_is_not_a_success(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress._cancel_event.clear()
                await self.wait_for_worker_cleanup(trigger, report=True)

    async def test_late_cancel_button_during_cleanup_does_not_change_timeout_outcome(self) -> None:
        await self.wait_for_worker_cleanup("timeout", late_button=True)

    async def wait_for_cancel_waiter_cleanup(self, *, broken_worker=False, broken_waiter=False, cancel=False):
        cleaning, release, cleaned = (asyncio.Event() for _ in range(3))
        children = []
        report = AgentReport("架空の正常な報告", [])

        async def finish_waiter(coro):
            try:
                await coro
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
                if broken_waiter:
                    raise RuntimeError("架空の中止待機の回収失敗")

        def start(coro, **kwargs):
            if kwargs["label"] == "サブエージェントの中止待ち":
                coro = finish_waiter(coro)
            task = spawn(coro, **kwargs)
            children.append(task)
            return task

        async def work():
            if broken_worker:
                raise RuntimeError("架空の作業失敗")
            return report

        with patch.object(agent_view, "spawn", side_effect=start), patch("persona_bot.views.agent_view.logger"):
            run = asyncio.create_task(self.progress.run(work()))
            try:
                await asyncio.wait_for(cleaning.wait(), 2)
                self.assertFalse(run.done())
                self.assertTrue(children[0].done())
                if cancel:
                    for _ in range(3):
                        run.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(run.done())
                        self.assertFalse(cleaned.is_set())
                release.set()
                if cancel:
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(run, 2)
                else:
                    expected = ("failed", None) if broken_worker else ("done", report)
                    self.assertEqual(await asyncio.wait_for(run, 2), expected)
                self.assertTrue(cleaned.is_set())
                self.assertTrue(all(task.done() for task in children))
            finally:
                release.set()
                await asyncio.gather(run, *children, return_exceptions=True)

    async def test_success_and_worker_exception_wait_for_cancel_waiter_cleanup(self) -> None:
        for broken_worker in (False, True):
            with self.subTest(broken_worker=broken_worker):
                await self.wait_for_cancel_waiter_cleanup(broken_worker=broken_worker)

    async def test_cancel_waiter_cleanup_failure_does_not_replace_worker_result(self) -> None:
        await self.wait_for_cancel_waiter_cleanup(broken_waiter=True)

    async def test_parent_cancel_during_cancel_waiter_cleanup_propagates_after_collection(self) -> None:
        await self.wait_for_cancel_waiter_cleanup(cancel=True)


class CancelButtonTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_requester_can_cancel(self) -> None:
        event = asyncio.Event()
        view = agent_view.AgentCancelView(11, event)
        self.addCleanup(view.stop)
        button = view.children[0]

        def interaction(user_id):
            return make_interaction(
                user_id=user_id, response=make_response("send_message", "defer"), without=ONLY_RESPONSE,
            )

        other = interaction(12)
        await button.callback(other)
        other.response.send_message.assert_awaited_once()
        self.assertTrue(other.response.send_message.await_args.kwargs["ephemeral"])
        self.assertFalse(event.is_set())

        requester = interaction(11)
        await button.callback(requester)
        requester.response.defer.assert_awaited_once()
        self.assertTrue(event.is_set())

    async def test_discord_acknowledgement_errors_do_not_escape(self) -> None:
        for user_id in (11, 12):
            with self.subTest(user_id=user_id):
                event = asyncio.Event()
                view = agent_view.AgentCancelView(11, event)
                self.addCleanup(view.stop)
                error = discord.HTTPException(make_http_response(400, "Bad Request"), "架空の送信失敗")
                interaction = make_interaction(
                    user_id=user_id, without=ONLY_RESPONSE,
                    response=make_response(
                        "send_message", "defer", send_message=AsyncMock(side_effect=error), defer=AsyncMock(side_effect=error),
                    ),
                )
                with self.assertLogs("persona_bot.views.agent_view", level="WARNING") as logs:
                    await view.children[0].callback(interaction)
                self.assertEqual(event.is_set(), user_id == 11)
                self.assertTrue(logs.records)
                self.assertNotIn("架空の送信失敗", logs.records[0].getMessage())
                self.assertIs(logs.records[0].exc_info[1], error)

    async def test_callback_is_collected_by_the_owner_on_shutdown(self) -> None:
        owner = TaskOwner()
        event, started, cleaned = (asyncio.Event() for _ in range(3))
        with owner.scope():
            view = agent_view.AgentCancelView(11, event)
        self.addCleanup(view.stop)

        async def defer():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        interaction = make_interaction(
            user_id=11, without=ONLY_RESPONSE, response=make_response("defer", defer=AsyncMock(side_effect=defer)),
        )
        callback = asyncio.create_task(view.children[0].callback(interaction))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertIn(callback, owner._tasks)
            await asyncio.wait_for(owner.shutdown(), 2)
            self.assertTrue(callback.cancelled())
            self.assertTrue(cleaned.is_set())
        finally:
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)

    async def test_stopping_owner_rejects_callback_before_mutating_or_replying(self) -> None:
        owner = TaskOwner()
        event = asyncio.Event()
        with owner.scope():
            view = agent_view.AgentCancelView(11, event)
        self.addCleanup(view.stop)
        owner.stopping = True
        interaction = make_interaction(user_id=11, without=ONLY_RESPONSE, response=make_response("defer"))
        with self.assertRaises(asyncio.CancelledError):
            await view.children[0].callback(interaction)
        self.assertFalse(event.is_set())
        interaction.response.defer.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
