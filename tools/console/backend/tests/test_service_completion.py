"""手動サービス操作の遠隔終了・systemdジョブ・排他を架空接続で検証する。"""

import asyncio
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncssh
import app
import remote_commands as rc
from ssh_session import CommandResult, CommandStateUnknownError, NotConnectedError, SshError, SshSession
from test_connection_lifecycle import profile


class ServiceCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.started, self.exit_ready = asyncio.Event(), asyncio.Event()
        self.job_checked, self.job_ready = asyncio.Event(), asyncio.Event()
        self.deploy_started = asyncio.Event()
        self.tasks = []
        self.result = SimpleNamespace(stdout="", stderr="", exit_status=0)

        async def finish(**kwargs):
            self.started.set()
            await self.exit_ready.wait()
            return self.result

        async def read(command, **kwargs):
            if command == rc.service_job_command("bot"):
                self.job_checked.set()
                return CommandResult("Job=\n" if self.job_ready.is_set() else "Job=42\n", "", 0, 1)
            return CommandResult("架空の読み取り結果", "", 0, 1)

        self.process = SimpleNamespace(wait=AsyncMock(side_effect=finish), close=Mock(), wait_closed=AsyncMock())
        self.connection = SimpleNamespace(create_process=AsyncMock(return_value=self.process), run=AsyncMock(side_effect=read))
        self.session = SshSession()
        self.session._conn = self.connection
        self.session.profile = self.selected = profile()
        self.session.connected_at = 100
        self.target = app.ServiceTarget(profile_id=self.selected.id, connected_at=100)
        self.manager = SimpleNamespace(running=False, active=False, state_unknown=False, current=None)

        def start(session, *args, **kwargs):
            session.require_command_completion()
            self.deploy_started.set()
            self.manager.running = True

        self.manager.start = Mock(side_effect=start)
        self.resources.enter_context(patch.multiple(
            app, session=self.session, deploys=self.manager, _connection_change_lock=asyncio.Lock(),
            _service_completion_pending=False, operations=deque(), _invalidate_insights=Mock(),
            SERVICE_CONTROL_TIMEOUT_SECONDS=1, SERVICE_JOB_TIMEOUT_SECONDS=1, SERVICE_JOB_POLL_SECONDS=0.001,
        ))

    async def asyncTearDown(self):
        self.exit_ready.set()
        self.job_ready.set()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    async def begin(self, action="stop"):
        task = self.spawn(app.control_service(action, self.target))
        await asyncio.wait_for(self.started.wait(), 1)
        return task

    async def wait_pending(self):
        for _ in range(100):
            if app._connection_snapshot()["state_unknown"]:
                return
            await asyncio.sleep(0.001)
        self.fail("終了確認待ちが接続状態に反映されませんでした。")

    async def test_timeout_keeps_deploy_blocked_until_process_and_systemd_job_finish(self):
        """応答期限後のSSH終了だけでは、継続中のsystemdジョブを追い越さない。"""
        with patch.object(app, "SERVICE_CONTROL_TIMEOUT_SECONDS", 0.01):
            service = await self.begin()
            await self.wait_pending()
            deploy = self.spawn(app.start_deploy(self.target))
            self.assertTrue(app._connection_change_lock.locked())
            self.assertFalse(service.done())
            self.assertFalse(deploy.done())
            self.connection.run.assert_not_awaited()
            self.exit_ready.set()
            await asyncio.wait_for(self.job_checked.wait(), 1)
            self.assertFalse(service.done())
            self.assertFalse(deploy.done())
            self.manager.start.assert_not_called()
            self.job_ready.set()
            with self.assertRaisesRegex(SshError, "終了を確認しました"):
                await asyncio.wait_for(service, 1)
            await asyncio.wait_for(deploy, 1)
        self.assertTrue(self.deploy_started.is_set())
        self.assertFalse(self.session.command_state_unknown)
        self.assertFalse(app._connection_snapshot()["state_unknown"])
        self.assertFalse(app._connection_change_lock.locked())
        self.process.close.assert_called_once()
        self.process.wait_closed.assert_awaited_once()
        self.assertGreaterEqual(app._invalidate_insights.call_count, 1)

    async def test_known_process_exit_waits_for_job_before_next_service_operation(self):
        self.exit_ready.set()
        first = await self.begin()
        await asyncio.wait_for(self.job_checked.wait(), 1)
        second = self.spawn(app.control_service("restart", self.target))
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertTrue(app._connection_snapshot()["state_unknown"])
        self.connection.create_process.assert_awaited_once()
        self.job_ready.set()
        operations = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertTrue(all(operation["ok"] for operation in operations))
        self.assertFalse(app._connection_snapshot()["state_unknown"])

    async def test_nonzero_exit_with_no_job_is_a_finished_failure_and_allows_retry(self):
        self.exit_ready.set()
        self.job_ready.set()
        self.result.exit_status = 1
        self.result.stderr = "架空の失敗"
        self.assertFalse((await app.control_service("stop", self.target))["ok"])
        self.assertFalse(self.session.command_state_unknown)
        self.result.exit_status = 0
        self.assertTrue((await app.control_service("restart", self.target))["ok"])

    async def test_missing_exit_status_signal_and_connection_loss_remain_unknown(self):
        for status in (None, -1, "connection-lost"):
            with self.subTest(status=status):
                self.session.command_state_unknown = False
                self.connection.run.reset_mock()
                self.exit_ready.set()
                self.result.exit_status = status
                if status == "connection-lost":
                    self.process.wait.side_effect = asyncssh.ConnectionLost("架空の接続断")
                with self.assertRaises(CommandStateUnknownError):
                    await app.control_service("stop", self.target)
                self.assertTrue(self.session.command_state_unknown)
                self.assertFalse(app._connection_change_lock.locked())
                self.connection.run.assert_not_awaited()
                for next_operation in (app.control_service("restart", self.target), app.start_deploy(self.target)):
                    with self.assertRaises(CommandStateUnknownError):
                        await next_operation
                self.assertEqual((await self.session.run("状態の読み取り", label="読み取り")).exit_status, 0)

    async def test_job_query_failure_or_malformed_result_keeps_mutations_blocked(self):
        self.exit_ready.set()
        failures = (
            CommandResult("", "架空の非公開詳細", 1, 1), CommandResult("", "", 0, 1),
            CommandResult("Job=broken\n", "", 0, 1), SshError("架空の応答期限超過"),
            RuntimeError("架空の非公開詳細"),
        )
        for response in failures:
            with self.subTest(response=response):
                self.session.command_state_unknown = False
                self.connection.run.side_effect = response if isinstance(response, Exception) else None
                self.connection.run.return_value = response
                with self.assertRaises(CommandStateUnknownError) as raised:
                    await app.control_service("stop", self.target)
                self.assertNotIn("非公開詳細", str(raised.exception))
                self.assertTrue(self.session.command_state_unknown)
                self.assertTrue(app._connection_snapshot()["state_unknown"])
                self.assertFalse(app._connection_change_lock.locked())

    async def test_unexpected_command_error_is_unknown_without_followup_reads(self):
        self.process.wait.side_effect = RuntimeError("架空の非公開詳細")
        with self.assertRaises(CommandStateUnknownError) as raised:
            await app.control_service("stop", self.target)
        self.assertNotIn("非公開詳細", str(raised.exception))
        self.assertTrue(self.session.command_state_unknown)
        self.connection.run.assert_not_awaited()
        with self.assertRaises(CommandStateUnknownError):
            await app.control_service("restart", self.target)

    async def test_preflight_validation_failure_does_not_claim_a_remote_operation_started(self):
        with self.assertRaises(ValueError):
            await app._run_service_control("bot; id", "stop", "停止")
        self.assertFalse(self.session.command_state_unknown)
        self.connection.create_process.assert_not_awaited()

    async def test_not_connected_before_sending_does_not_mark_command_state_unknown(self):
        """コマンド送信前の未接続は、終了未確認として扱わず、ジョブの確認にも進まない（#467）。"""
        self.session._conn = None
        with self.assertRaises(NotConnectedError):
            await app._run_service_control("bot", "stop", "停止")
        self.assertFalse(self.session.command_state_unknown)
        self.assertFalse(app._service_completion_pending)
        self.assertFalse(app._connection_snapshot()["state_unknown"])
        self.connection.create_process.assert_not_awaited()
        self.connection.run.assert_not_awaited()
        app._invalidate_insights.assert_not_called()

    async def test_disconnect_right_after_precheck_keeps_service_operations_available(self):
        """確認を通った直後の切断（送信前の NotConnectedError）の後も、再接続すれば次の操作を受け付ける（#467）。"""
        with patch.object(self.session, "run", new=AsyncMock(side_effect=NotConnectedError("架空の未接続"))):
            with self.assertRaises(NotConnectedError):
                await app.control_service("stop", self.target)
        self.assertFalse(self.session.command_state_unknown)
        self.assertFalse(app._connection_snapshot()["state_unknown"])
        self.assertFalse(app._connection_change_lock.locked())
        self.connection.run.assert_not_awaited()
        # バックエンドを起動し直さなくても、更新操作が 409（状態不明）で拒否され続けない。
        self.exit_ready.set()
        self.job_ready.set()
        self.assertTrue((await app.control_service("restart", self.target))["ok"])

    async def test_cancellation_with_unexpected_command_error_keeps_unknown_and_cancel(self):
        async def fail(**kwargs):
            self.started.set()
            await self.exit_ready.wait()
            raise RuntimeError("架空の非公開詳細")

        self.process.wait.side_effect = fail
        service = await self.begin()
        service.cancel()
        await asyncio.sleep(0)
        self.exit_ready.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(service, 1)
        self.assertTrue(self.session.command_state_unknown)
        self.connection.run.assert_not_awaited()

    async def test_job_confirmation_deadline_keeps_unknown_instead_of_claiming_failure_finished(self):
        self.exit_ready.set()
        with patch.object(app, "SERVICE_JOB_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(CommandStateUnknownError):
                await app.control_service("stop", self.target)
        self.assertTrue(self.session.command_state_unknown)
        self.assertFalse(app._connection_change_lock.locked())

    async def test_repeated_cancellation_waits_for_both_completion_boundaries(self):
        service = await self.begin()
        for _ in range(2):
            service.cancel()
            await asyncio.sleep(0)
            self.assertFalse(service.done())
            self.assertTrue(app._connection_change_lock.locked())
        self.exit_ready.set()
        await asyncio.wait_for(self.job_checked.wait(), 1)
        for _ in range(2):
            service.cancel()
            await asyncio.sleep(0)
            self.assertFalse(service.done())
            self.assertTrue(app._connection_change_lock.locked())
        self.job_ready.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(service, 1)
        self.assertFalse(self.session.command_state_unknown)
        self.assertFalse(app._connection_snapshot()["state_unknown"])
        self.assertFalse(app._connection_change_lock.locked())

    async def test_cancellation_with_lost_exit_notification_preserves_unknown_and_cancel(self):
        service = await self.begin()
        service.cancel()
        await asyncio.sleep(0)
        self.result.exit_status = None
        self.exit_ready.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(service, 1)
        self.assertTrue(self.session.command_state_unknown)
        self.connection.run.assert_not_awaited()
        self.assertFalse(app._connection_change_lock.locked())
