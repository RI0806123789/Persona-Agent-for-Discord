"""デプロイ（deploy.py）の回帰テスト。

テスト専用の模擬 SSH サーバーへ「本物の SSH」で接続し、git pull → 依存の更新 → 再起動 → ヘルスチェックと、
途中で失敗したときの自動の巻き戻しを、1ステップずつ確かめる（本番サーバーには触れない）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "helpers"))

import fake_ssh_server  # noqa: E402
import remote_commands as rc  # noqa: E402
from deploy import DeployManager, DeployRecord, DeployRunner, new_record  # noqa: E402
from fake_ssh_server import logging_line, start_fake_server  # noqa: E402
from profiles import Profile  # noqa: E402
from network_guard import block_network  # noqa: E402
from ssh_session import CommandResult, SshError, SshSession  # noqa: E402

FAST = {"restart_settle_timeout": 5, "health_timeout": 5, "poll_interval": 0.05}


class DeployTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        block_network(self)

    async def asyncSetUp(self) -> None:
        self.key_dir = Path(tempfile.mkdtemp(prefix="pbc-deploy-"))
        # 実際の時間を待たないよう、再起動や起動ログの待ち時間を小さくする
        self.acceptor, self.bot, port, self.live_task = await start_fake_server(
            "127.0.0.1", 0, self.key_dir, live_interval=(30, 60), delay_scale=0.05
        )
        self.repo = self.bot.repo
        self.profile = Profile(
            id="test",
            label="test",
            environment="production",
            host="127.0.0.1",
            port=port,
            username="ubuntu",
            key_path=str(self.key_dir / "client_key"),
            known_hosts=str(self.key_dir / "known_hosts"),
            use_ssh_config=False,
            service="bot",
            bot_dir="/home/ubuntu/Persona-Chat-in-Discord",
            journal_sudo=False,
            note="",
        )
        self.session = SshSession()
        await self.session.connect(self.profile)

    async def asyncTearDown(self) -> None:
        await self.session.disconnect()
        self.live_task.cancel()
        self.acceptor.close()
        await self.acceptor.wait_closed()
        shutil.rmtree(self.key_dir, ignore_errors=True)

    async def deploy(self) -> DeployRecord:
        record = new_record(1, self.profile)
        return await DeployRunner(self.session, self.profile, record, **FAST).run()

    def statuses(self, record: DeployRecord) -> dict[str, str]:
        return {step.key: step.status for step in record.steps}

    def step(self, record: DeployRecord, key: str):
        return next(step for step in record.steps if step.key == key)

    def sent(self, fragment: str) -> list[str]:
        """模擬サーバーへ送られたコマンドのうち、fragment を含むもの。"""
        return [record.command for record in self.session.history if fragment in record.command]

    async def restart_for_health(self, *, health_timeout=0):
        record = new_record(1, self.profile)
        runner = DeployRunner(self.session, self.profile, record, **{**FAST, "health_timeout": health_timeout})
        previous_pid = self.bot.pid
        self.assertTrue(await runner._step("restart", runner._restart))
        return runner, record, previous_pid

    async def test_clock_failure_prevents_restart(self):
        runner = DeployRunner(self.session, self.profile, new_record(1, self.profile), **FAST)
        original = self.session.run

        async def run(command, **kwargs):
            if command == rc.SERVER_TIME_COMMAND:
                return CommandResult("123", "error", 1, 1)
            return await original(command, **kwargs)

        with patch.object(self.session, "run", side_effect=run):
            self.assertFalse(await runner._step("restart", runner._restart))
        self.assertFalse(self.sent("systemctl restart"))

    async def test_cancel_service_job_wait_propagates_cancellation(self):
        runner = DeployRunner(self.session, self.profile, new_record(1, self.profile), **FAST)
        entered = asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.session, "run", side_effect=wait):
            task = asyncio.create_task(runner._wait_for_service_job())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.session.command_state_unknown)

    async def test_rollback_waits_for_systemd_job_before_changing_code(self):
        runner = DeployRunner(self.session, self.profile, new_record(1, self.profile), **FAST)
        runner._restart_attempted = True
        calls = []

        async def wait():
            calls.append("job")

        async def step(key, action):
            calls.append(key)
            return False

        with patch.object(runner, "_wait_for_service_job", side_effect=wait), patch.object(runner, "_step", side_effect=step):
            await runner._rollback()
        self.assertEqual(calls[:2], ["job", "rollback_code"])

    async def test_rollback_checks_job_until_it_finishes(self):
        runner = DeployRunner(self.session, self.profile, new_record(1, self.profile), **FAST)
        with patch.object(self.session, "run", new=AsyncMock(side_effect=[
            CommandResult("Job=42\n", "", 0, 1), CommandResult("Job=\n", "", 0, 1),
        ])) as run, patch("deploy.asyncio.sleep", new=AsyncMock()):
            await runner._wait_for_service_job()
        self.assertEqual(run.await_count, 2)

    async def check_health_with_raw_logs(self, runner, records):
        original_run = self.session.run

        async def run(command, **kwargs):
            if "journalctl" in command:
                return CommandResult("\n".join(json.dumps(record) for record in records), "", 0, 1)
            return await original_run(command, **kwargs)

        with patch.object(self.session, "run", side_effect=run):
            return await runner._step("health", runner._health)

    async def test_old_pid_login_in_time_window_does_not_pass_health(self):
        runner, record, previous_pid = await self.restart_for_health()
        # 1秒前から読む既存の時刻境界内にある、旧プロセスの成功行だけを返す。
        old_login = {"MESSAGE": rc.LOGIN_SUCCESS_TEXT, "_PID": str(previous_pid),
                     "__REALTIME_TIMESTAMP": str((runner._since + 1) * 1_000_000 - 100_000)}
        self.assertFalse(await self.check_health_with_raw_logs(runner, [old_login]))
        self.assertEqual(self.step(record, "health").status, "failed")
        self.assertNotEqual(previous_pid, self.bot.pid)

    async def test_current_pid_login_passes_health(self):
        runner, record, _ = await self.restart_for_health()
        self.assertTrue(await self.check_health_with_raw_logs(runner, [
            {"MESSAGE": rc.LOGIN_SUCCESS_TEXT, "_PID": str(self.bot.pid)},
        ]))
        self.assertEqual(self.step(record, "health").status, "succeeded")

    async def test_login_without_pid_does_not_pass_health(self):
        runner, _, _ = await self.restart_for_health()
        self.assertFalse(await self.check_health_with_raw_logs(runner, [{"MESSAGE": rc.LOGIN_SUCCESS_TEXT}]))

    async def test_logging_prefixed_login_requires_current_journal_pid(self):
        for kind in ("current", "old", "missing"):
            with self.subTest(kind=kind):
                runner, _, previous_pid = await self.restart_for_health()
                entry = {"MESSAGE": logging_line(rc.LOGIN_SUCCESS_TEXT + ": 架空のボット")}
                if kind != "missing":
                    entry["_PID"] = str(self.bot.pid if kind == "current" else previous_pid)
                self.assertEqual(await self.check_health_with_raw_logs(runner, [entry]), kind == "current")

    async def test_health_waits_for_current_pid_after_old_login(self):
        runner, record, old_pid = await self.restart_for_health(health_timeout=5)
        old = {"message": rc.LOGIN_SUCCESS_TEXT, "pid": old_pid}
        current = {"message": rc.LOGIN_SUCCESS_TEXT, "pid": self.bot.pid}
        with patch.object(runner, "_logs_since", new=AsyncMock(side_effect=[[old], [old, current]])) as logs, \
                patch.object(runner, "_sleep", new=AsyncMock()):
            self.assertTrue(await runner._step("health", runner._health))
        self.assertEqual(logs.await_count, 2)
        self.assertEqual(self.step(record, "health").status, "succeeded")

    async def test_process_replacement_and_restart_count_are_detected_during_wait(self):
        for change in ("main_pid", "restarts"):
            with self.subTest(change=change):
                runner, record, _ = await self.restart_for_health(health_timeout=5)
                original_service = runner._service

                async def service(*args, **kwargs):
                    state = await original_service(*args, **kwargs)
                    state[change] += 1
                    return state

                with patch.object(runner, "_service", side_effect=service), \
                        patch.object(runner, "_logs_since", new=AsyncMock(return_value=[])):
                    self.assertFalse(await runner._step("health", runner._health))
                message = self.step(record, "health").message
                self.assertIn("プロセスが変わりました" if change == "main_pid" else "自動再起動しました", message)

    async def test_process_replacement_attaches_recent_logs(self):
        # 落ちてすぐ別PIDで再起動済みでも、原因を追えるよう直近のログを結果に残す（#433）
        for change in ("main_pid", "restarts"):
            with self.subTest(change=change):
                runner, record, _ = await self.restart_for_health(health_timeout=5)
                original_service = runner._service

                async def service(*args, **kwargs):
                    state = await original_service(*args, **kwargs)
                    state[change] += 1
                    return state

                crash = {"message": "架空のクラッシュ原因ログ", "pid": self.bot.pid}
                with patch.object(runner, "_service", side_effect=service), \
                        patch.object(runner, "_logs_since", new=AsyncMock(return_value=[crash])):
                    self.assertFalse(await runner._step("health", runner._health))
                health = self.step(record, "health")
                self.assertIn("架空のクラッシュ原因ログ", health.output)
                self.assertTrue(health.output_contains_logs)

    async def test_unexpected_step_error_is_failed_with_fixed_message(self):
        # 想定外の例外でもステップを「実行中」のまま残さず、例外本文を画面へ出さない（#433）
        record = new_record(1, self.profile)
        runner = DeployRunner(self.session, self.profile, record, **FAST)
        with patch.object(runner, "_pull", new=AsyncMock(side_effect=RuntimeError("秘密のホスト名 /home/secret"))):
            await runner.run()
        self.assertEqual(self.step(record, "pull").status, "failed")
        self.assertEqual(record.status, "failed")
        for step in record.steps:
            self.assertNotIn(step.status, ("running", "pending"), step.key)
            self.assertNotIn("秘密のホスト名", step.message)
        self.assertNotIn("秘密のホスト名", record.message)

    async def test_unexpected_runner_error_settles_steps_and_hides_message(self):
        record = new_record(1, self.profile)
        runner = DeployRunner(self.session, self.profile, record, **FAST)

        async def run_all():
            self._find_step(record, "precheck").status = "running"
            raise RuntimeError("秘密のホスト名 /home/secret")

        with patch.object(runner, "_run_all", side_effect=run_all):
            await runner.run()
        self.assertEqual(record.status, "failed")
        self.assertNotIn("秘密のホスト名", record.message)
        self.assertEqual(self.step(record, "precheck").status, "failed")
        self.assertTrue(all(step.status in ("failed", "skipped") for step in record.steps))

    def _find_step(self, record, key):
        return self.step(record, key)

    async def test_process_replacement_before_final_confirmation_does_not_pass_health(self):
        runner, record, _ = await self.restart_for_health()
        original_service = runner._service
        reads = 0

        async def service(*args, **kwargs):
            nonlocal reads
            state = await original_service(*args, **kwargs)
            reads += 1
            if reads == 2:
                state["main_pid"] += 1
            return state

        with patch.object(runner, "_service", side_effect=service):
            self.assertFalse(await self.check_health_with_raw_logs(runner, [
                {"MESSAGE": rc.LOGIN_SUCCESS_TEXT, "_PID": str(self.bot.pid)},
            ]))
        self.assertIn("プロセスが変わりました", self.step(record, "health").message)

    async def test_rollback_health_rejects_login_from_failed_deployment_process(self):
        before = self.repo.head
        original_run = self.session.run
        previous_attempt_pid = None

        async def run(command, **kwargs):
            nonlocal previous_attempt_pid
            if "journalctl" in command:
                output = json.dumps({"MESSAGE": rc.LOGIN_SUCCESS_TEXT, "_PID": str(previous_attempt_pid)}) if self.repo.head == before else ""
                return CommandResult(output, "", 0, 1)
            result = await original_run(command, **kwargs)
            if "systemctl restart" in command and previous_attempt_pid is None:
                previous_attempt_pid = self.bot.pid
            return result

        record = new_record(1, self.profile)
        with patch.object(self.session, "run", side_effect=run):
            runner = DeployRunner(self.session, self.profile, record, **{**FAST, "health_timeout": 0})
            await runner.run()
        self.assertEqual(record.status, "rollback_failed", record.message)
        self.assertEqual(self.step(record, "rollback_health").status, "failed")
        self.assertEqual(self.repo.head, before)
        self.assertNotEqual(previous_attempt_pid, self.bot.pid)

    # ------------------------------------------------------------ 成功

    async def test_success_runs_every_step_and_records_facts(self) -> None:
        before, target, old_pid = self.repo.head, self.repo.origin, self.bot.pid
        record = await self.deploy()

        self.assertEqual(record.status, "succeeded", record.message)
        self.assertEqual(
            self.statuses(record),
            {"precheck": "succeeded", "pull": "succeeded", "deps": "skipped", "restart": "succeeded", "health": "succeeded"},
        )
        self.assertEqual((record.before_commit, record.after_commit), (before, target))
        self.assertEqual(self.repo.head, target)
        self.assertEqual([commit["subject"] for commit in record.commits], [self.repo.history[3][1], self.repo.history[2][1]])
        # 各ステップで確かめた事実が、すべて ✓ で残る
        for step in record.steps:
            self.assertTrue(all(check.ok for check in step.checks), (step.key, step.checks))
        restart = self.step(record, "restart")
        self.assertIn(f"{old_pid} → {self.bot.pid}", [check.detail for check in restart.checks])
        self.assertNotEqual(self.bot.pid, old_pid)
        health = self.step(record, "health")
        self.assertIn(rc.LOGIN_SUCCESS_TEXT, health.checks[0].detail)
        # 実行したコマンドと出力が画面に出せる形で残る
        self.assertTrue(self.step(record, "pull").commands[0].endswith("git merge --ff-only refs/remotes/origin/main && git rev-parse HEAD"))
        self.assertIn("Fast-forward", self.step(record, "pull").output)
        self.assertEqual(self.sent("-m pip install"), [])  # requirements.txt は変わっていない

    async def test_reinstalls_requirements_only_when_changed(self) -> None:
        self.repo.requirements_changed_at = {3}
        record = await self.deploy()

        self.assertEqual(record.status, "succeeded", record.message)
        deps = self.step(record, "deps")
        self.assertEqual(deps.status, "succeeded")
        self.assertEqual(
            [check.label for check in deps.checks],
            ["pip install が成功（終了コード 0）", "pip check で依存関係の食い違いなし"],
        )
        self.assertEqual(len(self.sent("-m pip install")), 1)
        self.assertEqual(deps.message, "requirements.txt が変わったため、依存関係を入れ直しました。")

    async def test_reinstalls_when_only_constraints_changed(self) -> None:
        # requirements.txt は -c で constraints.txt を読む。推移依存の固定版だけの更新も本番へ入れる（Issue #285）
        self.repo.constraints_changed_at = {2}
        record = await self.deploy()

        self.assertEqual(record.status, "succeeded", record.message)
        deps = self.step(record, "deps")
        self.assertEqual(deps.status, "succeeded")
        self.assertEqual(len(self.sent("-m pip install")), 1)
        self.assertEqual(deps.message, "constraints.txt が変わったため、依存関係を入れ直しました。")
        self.assertTrue(deps.commands[0].endswith("-- requirements.txt constraints.txt"))

    async def test_up_to_date_changes_nothing(self) -> None:
        self.repo.head_index = len(self.repo.history) - 1
        old_pid = self.bot.pid
        record = await self.deploy()

        self.assertEqual(record.status, "up_to_date")
        self.assertEqual(self.statuses(record)["precheck"], "succeeded")
        self.assertTrue(all(step.status == "skipped" for step in record.steps[1:]))
        self.assertEqual(self.bot.pid, old_pid)
        self.assertEqual(self.sent("systemctl restart"), [])

    # ------------------------------------------------------------ 事前確認で止める（何も変えない）

    async def test_refuses_when_server_has_uncommitted_changes(self) -> None:
        self.repo.dirty = 2
        before = self.repo.head
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        precheck = self.step(record, "precheck")
        self.assertEqual(precheck.status, "failed")
        self.assertIn("未コミットの変更", precheck.message)
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.sent("git merge"), [])
        self.assertEqual(self.sent("systemctl restart"), [])

    async def test_refuses_on_another_branch(self) -> None:
        self.repo.branch = "develop"
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        self.assertIn("git switch main", self.step(record, "precheck").message)
        self.assertEqual(self.sent("git fetch"), [])

    async def test_fetch_failure_changes_nothing(self) -> None:
        self.repo.fetch_fails = True
        record = await self.deploy()

        self.assertEqual(record.status, "failed")
        self.assertIn("取り寄せられませんでした", self.step(record, "precheck").message)
        self.assertEqual(self.sent("git merge"), [])

    # ------------------------------------------------------------ 途中で失敗したら、自動で前の版へ戻す

    async def lost_pull_response(self, *, updated=True, unreachable=False, nonzero=False):
        original_run = self.session.run
        sent = False

        async def run(command, **kwargs):
            nonlocal sent
            if "git merge --ff-only" in command:
                sent = True
                if updated:
                    await original_run(command, **kwargs)
                if nonzero:
                    return CommandResult("", "架空の取り込み失敗", 1, 0)
                raise SshError("架空の応答タイムアウト")
            if sent and unreachable:
                raise SshError("架空の通信不能")
            return await original_run(command, **kwargs)

        with patch.object(self.session, "run", side_effect=run):
            return await self.deploy()

    async def test_lost_merge_response_rechecks_head_and_rolls_back(self):
        before, pid = self.repo.head, self.bot.pid
        record = await self.lost_pull_response()
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.bot.pid, pid)
        self.assertEqual(record.after_commit, self.repo.origin)
        self.assertEqual(self.statuses(record)["rollback_code"], "succeeded")
        self.assertIn("再確認", self.step(record, "pull").output)

    async def test_lost_merge_response_without_update_does_not_reset(self):
        before = self.repo.head
        record = await self.lost_pull_response(updated=False)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.after_commit, before)
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.sent("git reset"), [])
        self.assertIn("変えていません", record.message)

    async def test_cancelled_merge_without_update_rechecks_head_and_does_not_reset(self):
        before = self.repo.head
        original = self.session.run

        async def run(command, **kwargs):
            if "git merge --ff-only" in command:
                raise asyncio.CancelledError
            return await original(command, **kwargs)

        record = new_record(1, self.profile)
        with patch.object(self.session, "run", side_effect=run):
            with self.assertRaises(asyncio.CancelledError):
                await DeployRunner(self.session, self.profile, record, **FAST).run()
        self.assertEqual(record.after_commit, before)
        self.assertEqual(record.status, "failed")
        self.assertEqual(self.sent("git reset"), [])

    async def test_restart_rejects_crash_or_increased_restart_count(self):
        for active, sub, restarts in (("failed", "failed", 0), ("inactive", "dead", 0),
                                      ("activating", "auto-restart", 0), ("active", "running", 1)):
            with self.subTest(active=active, sub=sub, restarts=restarts):
                runner = DeployRunner(self.session, self.profile, new_record(1, self.profile), **FAST)
                before = {"main_pid": 10, "restarts": 0}
                after = {"main_pid": 11, "restarts": restarts, "active_state": active, "sub_state": sub}
                with patch.object(runner, "_service", new=AsyncMock(side_effect=[before, after])), \
                     patch.object(runner, "_run", new=AsyncMock(side_effect=[CommandResult("100", "", 0, 0), CommandResult("", "", 0, 0)])):
                    self.assertFalse(await runner._step("restart", runner._restart))

    async def test_unreachable_head_and_rollback_do_not_claim_no_changes(self):
        record = await self.lost_pull_response(unreachable=True)
        self.assertEqual(record.status, "rollback_failed", record.message)
        self.assertIn("変更の有無は不明", self.step(record, "pull").output)
        self.assertNotIn("変えていません", record.message)
        self.assertEqual(self.statuses(record)["rollback_code"], "failed")

    async def test_lost_merge_response_with_failed_reset_reports_rollback_failure(self):
        self.repo.reset_fails = True
        record = await self.lost_pull_response()
        self.assertEqual(record.status, "rollback_failed", record.message)
        self.assertEqual(self.repo.head, self.repo.origin)
        self.assertTrue(self.sent("git reset"))

    async def test_nonzero_merge_rechecks_updated_and_unchanged_head(self):
        before = self.repo.head
        record = await self.lost_pull_response(nonzero=True)
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        record = await self.lost_pull_response(updated=False, nonzero=True)
        self.assertEqual(record.status, "failed", record.message)
        self.assertEqual(record.after_commit, before)

    async def test_pip_failure_rolls_back_without_restart(self) -> None:
        before, old_pid = self.repo.head, self.bot.pid
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}
        record = await self.deploy()

        self.assertEqual(record.status, "rolled_back", record.message)
        statuses = self.statuses(record)
        self.assertEqual((statuses["deps"], statuses["restart"], statuses["health"]), ("failed", "skipped", "skipped"))
        self.assertEqual(statuses["rollback_code"], "succeeded")
        self.assertEqual(statuses["rollback_deps"], "succeeded")  # 前の requirements.txt で入れ直した
        self.assertEqual((statuses["rollback_restart"], statuses["rollback_health"]), ("skipped", "skipped"))
        self.assertEqual(self.repo.head, before)
        self.assertEqual(self.bot.pid, old_pid)  # 再起動していないので、前の版のまま動いている
        self.assertEqual(self.sent("systemctl restart"), [])

    async def test_broken_version_rolls_back_and_restarts_previous(self) -> None:
        before = self.repo.head
        self.repo.broken = {self.repo.origin}
        health_started = asyncio.Event()
        original_startup, original_health = self.bot._startup_logs, DeployRunner._health

        async def startup():
            # 停止はヘルスチェック開始後に起こし、再起動確認との時間依存を除く。
            await health_started.wait()
            await original_startup()

        async def health(runner, step):
            health_started.set()
            await original_health(runner, step)

        with patch.object(self.bot, "_startup_logs", new=startup), patch.object(DeployRunner, "_health", new=health):
            record = await self.deploy()

        self.assertEqual(record.status, "rolled_back", record.message)
        statuses = self.statuses(record)
        self.assertEqual((statuses["restart"], statuses["health"]), ("succeeded", "failed"))
        self.assertIn("止まりました", self.step(record, "health").message)
        self.assertIn("Traceback", self.step(record, "health").output)  # 原因のログが残る
        self.assertEqual(
            [statuses[key] for key in ("rollback_code", "rollback_deps", "rollback_restart", "rollback_health")],
            ["succeeded", "skipped", "succeeded", "succeeded"],
        )
        self.assertEqual(self.repo.head, before)
        self.assertTrue(self.bot.active)
        self.assertFalse(self.bot.failed)

    async def test_rollback_failure_asks_for_manual_steps(self) -> None:
        self.repo.broken = {self.repo.origin}
        self.repo.reset_fails = True
        record = await self.deploy()

        self.assertEqual(record.status, "rollback_failed")
        statuses = self.statuses(record)
        self.assertEqual(statuses["rollback_code"], "failed")
        self.assertEqual(
            [statuses[key] for key in ("rollback_deps", "rollback_restart", "rollback_health")], ["skipped"] * 3
        )
        self.assertIn(f"git reset --keep {record.before_commit}", record.message)
        self.assertIn("手動", record.message)

    # ------------------------------------------------------------ 巻き戻しの前に、サーバーが変わっていた（#381）

    async def deploy_with_external_change(self, change):
        """依存関係の更新が失敗して巻き戻しに入る直前に、サーバーで別の操作（change）が行われた状況を作る。"""
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}
        original = self.session.run
        changed = False

        async def run(command, **kwargs):
            nonlocal changed
            result = await original(command, **kwargs)
            if "-m pip install" in command and not changed:
                changed = True
                change()
            return result

        record = new_record(1, self.profile)
        with patch.object(self.session, "run", side_effect=run):
            await DeployRunner(self.session, self.profile, record, **FAST).run()
        return record

    def assert_rollback_stopped_safely(self, record):
        """巻き戻しを中止し、コードにも再起動にも触れず、画面に手動の安全な手順を出している。"""
        self.assertEqual(record.status, "rollback_failed", record.message)
        statuses = self.statuses(record)
        self.assertEqual(statuses["rollback_code"], "failed")
        self.assertEqual([statuses[key] for key in ("rollback_deps", "rollback_restart", "rollback_health")], ["skipped"] * 3)
        self.assertEqual(self.sent("systemctl restart"), [])
        self.assertIn("中止", record.message)
        self.assertIn("何も手を加えていません", record.message)
        self.assertIn(f"git reset --keep {record.before_commit}", record.message)
        self.assertNotIn("--hard", record.message)
        self.assertFalse(self.step(record, "rollback_code").checks[-1].ok)

    async def test_rollback_keeps_tracked_edits_made_during_deploy(self):
        origin = self.repo.origin
        record = await self.deploy_with_external_change(lambda: setattr(self.repo, "dirty", 2))
        self.assert_rollback_stopped_safely(record)
        self.assertEqual(self.repo.head, origin)  # 戻していない
        self.assertEqual(self.repo.dirty, 2)  # 編集は消えていない
        self.assertIn("2 件", record.message)
        self.assertIn("追跡ファイル", self.step(record, "rollback_code").message)

    async def test_rollback_does_not_move_a_branch_switched_during_deploy(self):
        record = await self.deploy_with_external_change(lambda: setattr(self.repo, "branch", "hotfix"))
        self.assert_rollback_stopped_safely(record)
        self.assertEqual(self.repo.head, self.repo.origin)
        self.assertEqual(self.repo.branch, "hotfix")
        self.assertIn("hotfix", record.message)
        self.assertIn("git switch main", record.message)

    async def test_rollback_does_not_rewrite_an_unexpected_head(self):
        # デプロイ前は 1、取り込み後は 3。その間の 2 は、デプロイの外で動かされたコミット
        record = await self.deploy_with_external_change(lambda: setattr(self.repo, "head_index", 2))
        self.assert_rollback_stopped_safely(record)
        self.assertEqual(self.repo.head_index, 2)

    async def test_rollback_that_cannot_read_server_state_stops_without_changes(self):
        original = self.session.run
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}

        async def run(command, **kwargs):
            if command.startswith("cd ") and "git reset --keep" in command:
                return CommandResult("", "fatal: not a git repository", rc.ROLLBACK_EXIT_UNREADABLE, 1)
            return await original(command, **kwargs)

        record = new_record(1, self.profile)
        with patch.object(self.session, "run", side_effect=run):
            await DeployRunner(self.session, self.profile, record, **FAST).run()
        self.assert_rollback_stopped_safely(record)
        self.assertEqual(self.repo.head, self.repo.origin)
        self.assertIn("読めませんでした", record.message)

    async def test_ordinary_failure_still_rolls_back_with_a_guarded_keep_reset(self):
        before = self.repo.head
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}
        record = await self.deploy()
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        commands = self.step(record, "rollback_code").commands
        self.assertEqual(len(commands), 1)
        self.assertIn("git reset --keep", commands[0])
        self.assertNotIn("--hard", commands[0])
        self.assertEqual(self.sent("--hard"), [])

    # ------------------------------------------------------------ 同時に1つだけ

    @asynccontextmanager
    async def hold_command(self, fragment, *, timeout=None):
        """最初の対象コマンドだけを、遠隔で更新する直前に待機させる。"""
        gate = SimpleNamespace(started=asyncio.Event(), release=asyncio.Event(), finished=asyncio.Event())
        original = fake_ssh_server.dispatch
        original_run = self.session.run

        async def run(command, **kwargs):
            if timeout is not None and fragment in command and not gate.started.is_set():
                kwargs["timeout"] = timeout
            return await original_run(command, **kwargs)

        async def dispatch(command, process, bot):
            if fragment in command and not gate.started.is_set():
                gate.started.set()
                await gate.release.wait()
                try:
                    return await original(command, process, bot)
                finally:
                    gate.finished.set()
            return await original(command, process, bot)

        with patch.object(fake_ssh_server, "dispatch", new=dispatch), patch.object(self.session, "run", side_effect=run):
            try:
                yield gate
            finally:
                gate.release.set()
                if gate.started.is_set():
                    await asyncio.wait_for(gate.finished.wait(), timeout=5)

    async def wait_until(self, predicate):
        """期限超過の表示など、検証対象の状態が届くまで短く待つ。"""
        async def wait():
            while not predicate():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), timeout=5)

    async def test_delayed_merge_timeout_waits_then_rolls_back(self):
        """取り込みが期限を越えても旧HEADだけで未変更と判定しない。"""
        before = self.repo.head
        manager = DeployManager()
        async with self.hold_command("git merge --ff-only", timeout=0.02) as gate:
            record = manager.start(self.session, self.profile, **FAST)
            try:
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                await self.wait_until(lambda: "終了確認を待って" in self.step(record, "pull").message)
                self.assertTrue(manager.running)
                self.assertIsNone(record.after_commit)
                self.assertIsNone(record.finished_at)
                self.assertEqual(self.repo.head, before)
                self.assertFalse(any("git reset" in command for command in self.repo.commands))
                with self.assertRaises(RuntimeError):
                    manager.start(self.session, self.profile, **FAST)
            finally:
                gate.release.set()
                await asyncio.wait_for(manager.wait(), timeout=5)
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(record.after_commit, self.repo.origin)
        self.assertEqual(self.repo.head, before)
        self.assertFalse(manager.running)
        self.assertEqual(len(self.session._conn._channels), 0)

    async def test_delayed_pip_timeout_finishes_before_rollback_install(self):
        """期限を越えたpipと巻き戻しのpipを同時実行しない。"""
        before = self.repo.head
        self.repo.requirements_changed_at = {3}
        manager = DeployManager()
        async with self.hold_command("-m pip install", timeout=0.02) as gate:
            record = manager.start(self.session, self.profile, **FAST)
            try:
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                await self.wait_until(lambda: "終了確認を待って" in self.step(record, "deps").message)
                self.assertTrue(manager.running)
                self.assertEqual(self.repo.head, self.repo.origin)
                self.assertFalse(any("git reset" in command for command in self.repo.commands))
                self.assertEqual(self.sent("pip install"), [])
            finally:
                gate.release.set()
                await asyncio.wait_for(manager.wait(), timeout=5)
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        commands = self.repo.commands
        installs = [index for index, command in enumerate(commands) if "pip install" in command]
        reset = next(index for index, command in enumerate(commands) if "git reset" in command)
        self.assertEqual(len(installs), 2)
        self.assertLess(installs[0], reset)
        self.assertLess(reset, installs[1])

    async def test_repeated_deploy_cancellation_waits_then_rolls_back(self):
        """取り込み中の追加キャンセルでも終了と巻き戻しを先に完了する。"""
        before = self.repo.head
        manager = DeployManager()
        async with self.hold_command("git merge --ff-only") as gate:
            record = manager.start(self.session, self.profile, **FAST)
            try:
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                for _ in range(3):
                    manager._task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(manager.running)
                    self.assertIsNone(record.finished_at)
                    self.assertFalse(any("git reset" in command for command in self.repo.commands))
            finally:
                gate.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await manager.wait()
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        self.assertFalse(manager.running)
        self.assertEqual(len(self.session._conn._channels), 0)

    async def test_repeated_cancellation_during_rollback_waits_for_recovery(self):
        """巻き戻し中のキャンセルでも復旧を完了し、ステップを重複させない。"""
        before = self.repo.head
        self.repo.requirements_changed_at = {3}
        self.repo.pip_fails_at = {self.repo.origin}
        manager = DeployManager()
        async with self.hold_command("git reset --keep") as gate:
            record = manager.start(self.session, self.profile, **FAST)
            try:
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                for _ in range(2):
                    manager._task.cancel()
                    await asyncio.sleep(0)
                    self.assertTrue(manager.running)
            finally:
                gate.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await manager.wait()
        self.assertEqual(record.status, "rolled_back", record.message)
        self.assertEqual(self.repo.head, before)
        keys = [step.key for step in record.steps]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(self.sent("git reset --keep")), 1)

    async def test_canceled_rollback_exception_finishes_record_and_notifies(self):
        """中断後の巻き戻しが例外でも失敗を確定し、通知・排他解放後にキャンセルを伝える。"""
        manager = DeployManager()
        finished = Mock()
        with patch.object(DeployRunner, "_rollback", new=AsyncMock(side_effect=RuntimeError("復旧の解析に失敗"))) as rollback:
            async with self.hold_command("git merge --ff-only") as gate:
                record = manager.start(self.session, self.profile, on_finished=finished, **FAST)
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                manager._task.cancel()
                gate.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(manager.wait(), timeout=5)
            rollback.assert_awaited_once()
        self.assertEqual(record.status, "failed")
        self.assertIsNotNone(record.finished_at)
        self.assertIn("RuntimeError", record.message)
        self.assertTrue(manager._task.cancelled())
        self.assertFalse(manager.active)
        self.assertFalse(manager.running)
        self.assertFalse(manager.state_unknown)
        finished.assert_called_once_with()
        self.assertEqual(len(self.session._conn._channels), 0)
        # 終了確認済みの失敗は実行中の排他を残さない。
        manager.start(self.session, self.profile, **FAST)
        await asyncio.wait_for(manager.wait(), timeout=5)

    async def test_canceled_recovery_exception_keeps_unknown_terminal(self):
        """復旧の補助処理が例外でも終了未確認のフラグを維持し、キャンセルを伝える。"""
        manager = DeployManager()
        finished = Mock()

        async def recover():
            self.session.command_state_unknown = True
            raise RuntimeError("復旧の後片付けに失敗")

        with patch.object(DeployRunner, "_recover_canceled", new=AsyncMock(side_effect=recover)):
            async with self.hold_command("git merge --ff-only") as gate:
                record = manager.start(self.session, self.profile, on_finished=finished, **FAST)
                await asyncio.wait_for(gate.started.wait(), timeout=5)
                manager._task.cancel()
                gate.release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(manager.wait(), timeout=5)
        self.assertEqual(record.status, "unknown")
        self.assertIsNotNone(record.finished_at)
        self.assertIn("RuntimeError", record.message)
        self.assertTrue(manager._task.cancelled())
        self.assertFalse(manager.active)
        self.assertFalse(manager.running)
        self.assertTrue(manager.state_unknown)
        finished.assert_called_once_with()
        with self.assertRaises(RuntimeError):
            manager.start(self.session, self.profile, **FAST)

    async def test_channel_close_without_exit_keeps_deploy_unknown_and_blocks_retry(self):
        """終了通知のない切断は状態不明を保持し、旧HEAD確認や巻き戻しを送らない。"""
        original = fake_ssh_server.dispatch
        after_merge = []

        async def dispatch(command, process, bot):
            if "git merge --ff-only" in command:
                process.close()  # チャネル切断だけでは遠隔処理の終了と扱わない。
                return 0
            after_merge.append(command)
            return await original(command, process, bot)

        manager = DeployManager()
        with patch.object(fake_ssh_server, "dispatch", new=dispatch):
            record = manager.start(self.session, self.profile, **FAST)
            await asyncio.wait_for(manager.wait(), timeout=5)
        self.assertEqual(record.status, "unknown", record.message)
        self.assertIn("状態不明", record.message)
        self.assertNotIn("変えていません", record.message)
        self.assertNotIn("戻しました", record.message)
        self.assertEqual(len(after_merge), 2)  # 事前確認とfetchだけ。
        self.assertEqual(self.sent("git reset"), [])
        self.assertFalse(manager.running)
        self.assertTrue(manager.state_unknown)
        with self.assertRaises(RuntimeError):
            manager.start(self.session, self.profile, **FAST)

    async def test_unknown_pip_does_not_start_rollback(self):
        """pipの終了未確認時はコードと依存の巻き戻しを始めない。"""
        self.repo.requirements_changed_at = {3}
        original = fake_ssh_server.dispatch

        async def dispatch(command, process, bot):
            if "-m pip install" in command:
                process.close()
                return 0
            return await original(command, process, bot)

        manager = DeployManager()
        with patch.object(fake_ssh_server, "dispatch", new=dispatch):
            record = manager.start(self.session, self.profile, **FAST)
            await asyncio.wait_for(manager.wait(), timeout=5)
        self.assertEqual(record.status, "unknown", record.message)
        self.assertEqual(self.repo.head, self.repo.origin)
        self.assertFalse(manager.running)
        self.assertTrue(manager.state_unknown)
        self.assertEqual(self.sent("git reset"), [])
        self.assertEqual(self.sent("pip check"), [])
        self.assertFalse(any(step.phase == "rollback" for step in record.steps))

    async def test_manager_runs_one_deploy_at_a_time(self) -> None:
        manager = DeployManager()
        manager.start(self.session, self.profile, **FAST)
        self.assertTrue(manager.running)
        with self.assertRaises(RuntimeError):
            manager.start(self.session, self.profile, **FAST)
        record = await manager.wait()
        self.assertFalse(manager.running)
        self.assertFalse(manager.state_unknown)
        self.assertEqual(record.status, "succeeded")


@unittest.skipUnless(shutil.which("git") and shutil.which("sh"), "git と sh が必要です")
class RollbackCommandRealGitTests(unittest.TestCase):
    """巻き戻しのコマンドを、隔離した一時リポジトリの本物の git・sh で実行して確かめる（#381）。

    模擬サーバーは文字列の一致で答えるだけなので、「途中の編集を消さない」「別のブランチを動かさない」は
    ここで本物のGitに対して確かめる。サーバー・実リポジトリには触れない。
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "bot"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        for key, value in (("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        self.write("a.txt", "a1\n")
        self.write("b.txt", "b1\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "c1")
        self.before = self.git("rev-parse", "HEAD")
        self.write("a.txt", "a2\n")
        self.git("commit", "-q", "-am", "c2")
        self.after = self.git("rev-parse", "HEAD")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
            cwd=self.repo, capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def write(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text, encoding="utf-8", newline="\n")

    def read(self, name: str) -> str:
        return (self.repo / name).read_text(encoding="utf-8")

    def rollback(self, *, branch: str = "main", env: dict | None = None):
        command = rc.deploy_rollback_command(self.repo.as_posix(), branch, self.before, [self.after])
        result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env)
        return result.returncode, result.stdout.strip()

    def test_clean_tree_is_rolled_back_to_the_previous_commit(self) -> None:
        self.assertEqual(self.rollback(), (0, self.before))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.before)
        self.assertEqual(self.read("a.txt"), "a1\n")

    def test_edit_made_during_deploy_is_kept_and_nothing_is_reset(self) -> None:
        self.write("b.txt", "手元の編集\n")
        self.assertEqual(self.rollback(), (rc.ROLLBACK_EXIT_TREE_CHANGED, "1"))
        self.assertEqual(self.read("b.txt"), "手元の編集\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)

    def test_staged_edit_during_deploy_is_kept(self) -> None:
        self.write("b.txt", "ステージ済みの編集\n")
        self.git("add", "b.txt")
        self.assertEqual(self.rollback()[0], rc.ROLLBACK_EXIT_TREE_CHANGED)
        self.assertEqual(self.read("b.txt"), "ステージ済みの編集\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)

    def test_other_branch_is_not_moved(self) -> None:
        self.git("switch", "-q", "-c", "other")
        self.write("o.txt", "other\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "別ブランチの作業")
        other = self.git("rev-parse", "HEAD")
        self.assertEqual(self.rollback(), (rc.ROLLBACK_EXIT_BRANCH_CHANGED, "other"))
        self.assertEqual(self.git("rev-parse", "HEAD"), other)
        self.assertEqual(self.read("o.txt"), "other\n")
        self.assertEqual(self.git("rev-parse", "main"), self.after)

    def test_detached_head_is_not_rewritten(self) -> None:
        self.git("checkout", "-q", "--detach")
        self.assertEqual(self.rollback(), (rc.ROLLBACK_EXIT_BRANCH_CHANGED, "HEAD"))
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)

    def test_unexpected_head_is_not_rewritten(self) -> None:
        self.write("c.txt", "c\n")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "デプロイの外で進んだコミット")
        moved = self.git("rev-parse", "HEAD")
        self.assertEqual(self.rollback(), (rc.ROLLBACK_EXIT_UNEXPECTED_HEAD, moved))
        self.assertEqual(self.git("rev-parse", "HEAD"), moved)

    def test_already_at_previous_commit_does_nothing_and_keeps_edits(self) -> None:
        self.git("reset", "-q", "--hard", self.before)
        self.write("b.txt", "手元の編集\n")
        self.assertEqual(self.rollback(), (0, self.before))
        self.assertEqual(self.read("b.txt"), "手元の編集\n")

    def test_not_a_git_repository_stops_with_unreadable_state(self) -> None:
        command = rc.deploy_rollback_command(self.root.as_posix(), "main", self.before, [self.after])
        result = subprocess.run(["sh", "-c", command], capture_output=True, text=True)
        self.assertEqual(result.returncode, rc.ROLLBACK_EXIT_UNREADABLE)

    @unittest.skipIf(
        sys.platform == "win32",
        "PATH の先頭に置いた偽の git を sh が選ばない環境がある（Windows の CI で確認）。Linux で確かめる",
    )
    def test_edit_after_the_check_is_protected_by_git_itself(self) -> None:
        """確認の直後、戻す直前に編集されても（競合）、git reset --keep がその編集を守って中止する。"""
        wrapper = self.root / "wrapper"
        wrapper.mkdir()
        script = wrapper / "git"
        # git reset の直前に、戻すコミットで内容が変わるファイル（a.txt）を編集してから、本物の git を呼ぶ
        script.write_text(
            '#!/bin/sh\nif [ "$1" = "reset" ]; then printf "競合する編集\\n" > a.txt; fi\n'
            f'exec "{Path(shutil.which("git")).as_posix()}" "$@"\n',
            encoding="utf-8", newline="\n",
        )
        script.chmod(0o755)
        env = {**os.environ, "PATH": f"{wrapper.as_posix()}{os.pathsep}{os.environ['PATH']}"}
        code, _ = self.rollback(env=env)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.read("a.txt"), "競合する編集\n")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.after)


class DeployCommandTests(unittest.TestCase):
    def test_rejects_unsafe_values(self) -> None:
        for branch in ("main; rm -rf /", "--upload-pack=x", "a..b", "-x"):
            with self.assertRaises(ValueError):
                rc.deploy_fetch_command("/srv/bot", branch)
        good = "a" * 40
        for arguments in (
            ("/srv/bot", "main", "HEAD~1", []),  # コミット ID（40 桁）以外は受け付けない
            ("/srv/bot", "main", good, ["HEAD~1"]),
            ("/srv/bot", "main; id", good, []),
            ("/srv/bot", "--upload-pack=x", good, []),
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                rc.deploy_rollback_command(*arguments)
        with self.assertRaises(ValueError):
            rc.deploy_pip_install_command("/srv/bot", ".venv/bin/python; id")

    def test_deploy_log_round_trip_preserves_commit_hash_and_subject(self):
        command = rc.deploy_log_command("/srv/bot", "a" * 40, "b" * 40)
        self.assertIn("%h%x1f%s", command)
        self.assertEqual(rc.parse_commit_list("abc1234\x1ffix: 架空の修正"), [{"hash": "abc1234", "subject": "fix: 架空の修正"}])
        with self.assertRaises(ValueError):
            rc.parse_commit_list("abc1234\x1f")

    def test_dependency_diff_watches_requirements_and_constraints(self) -> None:
        self.assertEqual(rc.DEPENDENCY_FILES, ("requirements.txt", "constraints.txt"))
        self.assertEqual(
            rc.deploy_requirements_diff_command("/srv/bot", "a" * 40, "b" * 40),
            f"cd /srv/bot && git diff --name-only {'a' * 40} {'b' * 40} -- requirements.txt constraints.txt",
        )

    def test_paths_are_quoted(self) -> None:
        self.assertTrue(rc.deploy_state_command("/home/u/my bot").startswith("cd '/home/u/my bot' && "))
        self.assertEqual(
            rc.deploy_pip_check_command("/srv/bot", "/opt/py3/bin/python"), "cd /srv/bot && /opt/py3/bin/python -m pip check"
        )
        with self.assertRaises(ValueError):
            rc.deploy_pip_check_command("/srv/bot", "/opt/py 3/bin/python")  # 空白を含む場所は受け付けない


if __name__ == "__main__":
    unittest.main()
