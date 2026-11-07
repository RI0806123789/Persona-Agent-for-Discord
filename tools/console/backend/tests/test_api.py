"""API の入口・集計キャッシュ・WebSocket の終了を、本番に触れずに検証する。"""

import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app
import profiles
import remote_commands as rc
from deploy import DeployManager, DeployRunner, new_record
from fake_ssh_server import start_fake_server
from profiles import Profile
from ssh_session import CommandResult, CommandStateUnknownError, NotConnectedError, SshError, SshSession


async def request(path, *, method="GET", host="127.0.0.1", header=True, body=None):
    """追加の HTTP クライアントに依存せず、ASGI の入口から要求を送る。"""
    headers = [(b"host", host.encode()), (b"content-type", b"application/json")]
    if header:
        headers.append((app.CLIENT_HEADER.encode(), app.CLIENT_HEADER_VALUE.encode()))
    messages = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": json.dumps(body).encode() if body else b""})
    sent = []

    async def send(message):
        sent.append(message)

    path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "root_path": "", "query_string": query.encode(), "headers": headers,
             "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", app.PORT)}
    await app.app(scope, messages.get, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    return status, content


class ProfileEncodingApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_encoding_returns_safe_json_for_profile_and_connect_endpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.dumps({"profiles": [], "note": "非公開の架空設定値"}, ensure_ascii=False)
            for data in (payload.encode("cp932"), b'{"profiles": [], "note": "\xff"}'):
                with self.subTest(data_length=len(data)), mock.patch.object(profiles, "PROFILES_PATH", path), \
                        mock.patch.object(app.session, "connect", new_callable=mock.AsyncMock) as connect:
                    path.write_bytes(data)
                    for route, method, body in (("/api/profiles", "GET", None),
                                                ("/api/connect", "POST", {"profile_id": "test"})):
                        status, content = await request(route, method=method, body=body)
                        detail = json.loads(content)["detail"]
                        self.assertEqual(status, 500)
                        self.assertIn("接続先の設定エラー", detail)
                        self.assertIn("profiles.json", detail)
                        self.assertIn("UTF-8", detail)
                        self.assertNotIn(str(path.parent), detail)
                        self.assertNotIn("非公開の架空設定値", detail)
                        self.assertNotIn("UnicodeDecodeError", detail)
                    connect.assert_not_awaited()
                    self.assertEqual(path.read_bytes(), data)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.acceptor, self.bot, port, self.live = await start_fake_server(
            "127.0.0.1", 0, Path(self.temp.name), live_interval=(0.05, 0.1)
        )
        self.profile = Profile(
            id="test", label="検証", environment="development", host="127.0.0.1", port=port,
            username="tester", key_path=str(Path(self.temp.name) / "client_key"),
            known_hosts=str(Path(self.temp.name) / "known_hosts"), use_ssh_config=False,
            service="bot", bot_dir="/srv/test-bot", journal_sudo=False, note="",
        )
        self.session = SshSession()
        self.patch = mock.patch.object(app, "session", self.session)
        self.patch.start()
        app._insights_cache.clear()
        app._insights_lock = asyncio.Lock()
        await self.session.connect(self.profile)

    async def asyncTearDown(self):
        await self.session.disconnect()
        self.patch.stop()
        self.live.cancel()
        await asyncio.gather(self.live, return_exceptions=True)
        self.acceptor.close()
        await self.acceptor.wait_closed()
        self.temp.cleanup()

    async def test_http_rejects_missing_header_and_foreign_host(self):
        for path in ("/api/profiles", "/api/status", "/api/insights", "/api/history"):
            with self.subTest(path=path):
                self.assertEqual((await request(path, header=False))[0], 403)
                self.assertEqual((await request(path, host="foreign.example"))[0], 400)
        status, body = await request("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["service"]["active_state"], "active")

    async def test_connect_accepts_only_a_profile_id(self):
        status, _ = await request("/api/connect", method="POST", body={"profile_id": "test", "host": "foreign.example"})
        self.assertEqual(status, 422)
        status, _ = await request("/api/service/reboot", method="POST")
        self.assertEqual(status, 400)

    async def test_logs_discard_result_after_connection_change(self):
        async def changing_connection(*args, **kwargs):
            self.session.generation += 1
            return CommandResult('{"MESSAGE":"old"}', "", 0, 1)

        with mock.patch.object(self.session, "run", side_effect=changing_connection):
            with self.assertRaises(NotConnectedError):
                await app.get_logs()

    async def test_shutdown_waits_for_service_lock(self):
        lock = asyncio.Lock()
        await lock.acquire()
        with mock.patch.object(app, "_connection_change_lock", lock), mock.patch.object(
            app.deploys, "wait", new_callable=mock.AsyncMock
        ), mock.patch.object(self.session, "disconnect", new_callable=mock.AsyncMock) as disconnect:
            async def shutdown():
                async with app.lifespan(app.app):
                    pass

            task = asyncio.create_task(shutdown())
            await asyncio.sleep(0)
            disconnect.assert_not_awaited()
            lock.release()
            await task
            disconnect.assert_awaited_once()

    async def test_service_timeout_waits_for_fake_ssh_exit_and_checks_systemd_job(self):
        """本物のSSHチャネルでも応答期限後は操作を打ち切らず、Job確認まで待つ。"""
        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
        with mock.patch.object(app, "_connection_change_lock", asyncio.Lock()), mock.patch.object(
            app, "SERVICE_CONTROL_TIMEOUT_SECONDS", 0.01,
        ), mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            service = asyncio.create_task(request("/api/service/stop", method="POST", body=target))
            try:
                for _ in range(100):
                    if app._connection_snapshot()["state_unknown"]:
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(app._connection_snapshot()["state_unknown"])
                self.assertTrue(app._connection_change_lock.locked())
                self.assertFalse(service.done())
                status, _ = await asyncio.wait_for(service, 2)
                self.assertEqual(status, 502)
            finally:
                await asyncio.gather(service, return_exceptions=True)
        control = next(call for call in run.await_args_list if "systemctl stop" in call.args[0])
        self.assertTrue(control.kwargs["wait_for_exit"])
        self.assertIn(rc.service_job_command(self.profile.service), [call.args[0] for call in run.await_args_list])
        self.assertFalse(self.bot.active)
        self.assertFalse(self.session.command_state_unknown)
        self.assertFalse(app._connection_snapshot()["state_unknown"])
        self.assertFalse(app._connection_change_lock.locked())

    async def test_manual_job_confirmation_failure_survives_reconnect_and_allows_reads(self):
        """手動操作から生じた終了不明も、再接続で消さず更新だけを止める。"""
        run = self.session.run

        async def fail_job_confirmation(command, **kwargs):
            if command == rc.service_job_command(self.profile.service):
                return CommandResult("", "架空の非公開詳細", 1, 1)
            return await run(command, **kwargs)

        with mock.patch.object(app, "_connection_change_lock", asyncio.Lock()), mock.patch.object(
            app, "deploys", DeployManager(),
        ), mock.patch.object(app, "load_profiles", return_value=({self.profile.id: self.profile}, "検証用設定")):
            target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
            with mock.patch.object(self.session, "run", side_effect=fail_job_confirmation):
                status, content = await request("/api/service/start", method="POST", body=target)
            self.assertEqual(status, 409)
            self.assertNotIn("非公開詳細", content.decode())
            self.assertTrue(self.session.command_state_unknown)
            self.assertEqual((await request("/api/disconnect", method="POST"))[0], 200)
            self.assertEqual((await request("/api/connect", method="POST", body={"profile_id": self.profile.id}))[0], 200)
            self.assertTrue(app._connection_snapshot()["state_unknown"])
            self.assertEqual((await request("/api/status"))[0], 200)
            target["connected_at"] = self.session.connected_at
            for path in ("/api/service/restart", "/api/deploy"):
                self.assertEqual((await request(path, method="POST", body=target))[0], 409)

    async def test_unknown_deploy_allows_reads_and_reconnect_but_blocks_mutations(self):
        """状態不明でも概要・ログ・DB閲覧と再接続を許可し、更新操作だけを409にする。"""
        manager = DeployManager()
        self.session.command_state_unknown = True
        with mock.patch.object(app, "deploys", manager), mock.patch.object(
            app, "_connection_change_lock", asyncio.Lock(),
        ), mock.patch.object(app, "load_profiles", return_value=({self.profile.id: self.profile}, "検証用設定")):
            # 記録の排他とセッションのフラグをそれぞれ確認する。再接続でも両方を維持する。
            for recorded in (True, False):
                with self.subTest(recorded=recorded):
                    manager.current = new_record(1, self.profile) if recorded else None
                    if manager.current is not None:
                        manager.current.status = "unknown"
                    self.assertFalse(manager.active)
                    with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
                        for path in ("/api/status", "/api/logs", "/api/db", "/api/db/tables/feedback",
                                     "/api/insights?refresh=true", "/api/connection", "/api/history", "/api/deploy"):
                            status, _ = await request(path)
                            self.assertEqual(status, 200, path)
                        self.assertTrue(run.await_count)
                        self.assertTrue(all(not call.kwargs.get("wait_for_exit", False) for call in run.await_args_list))

                    for reconnect in (False, True):
                        if reconnect:
                            self.assertEqual((await request("/api/disconnect", method="POST"))[0], 200)
                            self.assertTrue(self.session.command_state_unknown)
                            status, body = await request(
                                "/api/connect", method="POST", body={"profile_id": self.profile.id},
                            )
                            self.assertEqual(status, 200)
                            self.assertIsNotNone(json.loads(body)["server"])
                            self.assertTrue(self.session.command_state_unknown)
                            self.assertEqual((await request("/api/status"))[0], 200)
                        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
                        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
                            for path in ("/api/service/start", "/api/service/stop", "/api/service/restart", "/api/deploy"):
                                status, _ = await request(path, method="POST", body=target)
                                self.assertEqual(status, 409, path)
                            run.assert_not_awaited()
                        self.assertTrue(self.session.command_state_unknown)
                        self.assertIsNone(manager._task)

    async def test_finished_unknown_deploy_stops_running_but_keeps_mutations_blocked(self):
        """タスクが状態不明で完了しても、読み取り・再接続だけを許可する。"""
        manager = DeployManager()
        with mock.patch.object(DeployRunner, "_run_all", mock.AsyncMock(
            side_effect=CommandStateUnknownError("架空の終了未確認"),
        )):
            manager.start(self.session, self.profile)
            await manager.wait()
        self.assertTrue(manager._task.done())
        self.assertFalse(manager.running)
        self.assertTrue(manager.state_unknown)
        # セッション側の拒否に頼らず、完了したデプロイの記録だけでも拒否する。
        self.assertFalse(self.session.command_state_unknown)
        with mock.patch.object(app, "deploys", manager), mock.patch.object(
            app, "_connection_change_lock", asyncio.Lock(),
        ), mock.patch.object(app, "load_profiles", return_value=({self.profile.id: self.profile}, "検証用設定")):
            for reconnect in (False, True):
                with self.subTest(reconnect=reconnect):
                    if reconnect:
                        self.assertEqual((await request("/api/disconnect", method="POST"))[0], 200)
                        self.assertEqual((await request("/api/connect", method="POST", body={"profile_id": self.profile.id}))[0], 200)
                    status, body = await request("/api/deploy")
                    self.assertEqual(status, 200)
                    snapshot = json.loads(body)
                    self.assertFalse(snapshot["running"])
                    self.assertEqual(snapshot["deploy"]["status"], "unknown")
                    self.assertIsNotNone(snapshot["deploy"]["finished_at"])
                    for path in ("/api/status", "/api/logs", "/api/db", "/api/db/tables/feedback", "/api/insights?refresh=true"):
                        self.assertEqual((await request(path))[0], 200, path)
                    target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
                    with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
                        for path in ("/api/deploy", "/api/service/start", "/api/service/stop", "/api/service/restart"):
                            self.assertEqual((await request(path, method="POST", body=target))[0], 409, path)
                        with self.assertRaises(RuntimeError):
                            manager.start(self.session, self.profile)
                        run.assert_not_awaited()
                    self.assertTrue(manager.state_unknown)

    async def test_connection_snapshot_keeps_unknown_after_switching_profile(self):
        """別の接続先へ移っても、接続APIは記録と独立したセッション全体の状態不明を返す。"""
        other = replace(self.profile, id="other", label="別の架空接続先")
        manager = DeployManager()
        with mock.patch.object(app, "deploys", manager), mock.patch.object(
            app, "_connection_change_lock", asyncio.Lock(),
        ), mock.patch.object(app, "load_profiles", return_value=(
            {self.profile.id: self.profile, other.id: other}, "検証用設定",
        )):
            for session_unknown, deploy_unknown in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(session_unknown=session_unknown, deploy_unknown=deploy_unknown):
                    await self.session.connect(self.profile)
                    self.session.command_state_unknown = session_unknown
                    manager.current = new_record(1, self.profile) if deploy_unknown else None
                    if manager.current is not None:
                        manager.current.status = "unknown"
                        manager.current.finished_at = 1
                    expected = session_unknown or deploy_unknown
                    status, body = await request("/api/connection")
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["state_unknown"], expected)
                    # 切断を挟まない切替でも、接続先とは独立した停止状態を返す。
                    status, body = await request("/api/connect", method="POST", body={"profile_id": other.id})
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["profile"]["id"], other.id)
                    self.assertEqual(json.loads(body)["state_unknown"], expected)
                    status, body = await request("/api/disconnect", method="POST")
                    self.assertEqual(status, 200)
                    self.assertFalse(json.loads(body)["connected"])
                    self.assertEqual(json.loads(body)["state_unknown"], expected)
                    status, body = await request("/api/connect", method="POST", body={"profile_id": other.id})
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["profile"]["id"], other.id)
                    self.assertEqual(json.loads(body)["state_unknown"], expected)
                    status, body = await request("/api/connection")
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["profile"]["id"], other.id)
                    self.assertEqual(json.loads(body)["state_unknown"], expected)
                    self.assertEqual(self.session.command_state_unknown, session_unknown)
                    self.assertEqual(manager.state_unknown, deploy_unknown)
                    if deploy_unknown:
                        self.assertNotEqual(manager.current.profile_id, other.id)
                        status, body = await request("/api/deploy")
                        self.assertEqual(status, 200)
                        self.assertFalse(json.loads(body)["running"])
                        self.assertEqual(json.loads(body)["deploy"]["profile_id"], self.profile.id)
                    self.assertEqual((await request("/api/status"))[0], 200)
                    if expected:
                        target = {"profile_id": other.id, "connected_at": self.session.connected_at}
                        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
                            for path in ("/api/deploy", "/api/service/start", "/api/service/stop", "/api/service/restart"):
                                self.assertEqual((await request(path, method="POST", body=target))[0], 409, path)
                            run.assert_not_awaited()

    async def test_service_actions_record_results_and_change_service_state(self):
        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
        old_pid = self.bot.pid
        for action in ("restart", "stop", "start"):
            status, body = await request(f"/api/service/{action}", method="POST", body=target)
            self.assertEqual(status, 200)
            operation = json.loads(body)
            self.assertTrue(operation["ok"])
            self.assertEqual(operation["profile_id"], self.profile.id)
            self.assertEqual(operation["action"], action)
            self.assertEqual(self.bot.active, action != "stop")
            if action == "restart":
                self.assertNotEqual(old_pid, self.bot.pid)
        status, history = await request("/api/operations")
        self.assertEqual(status, 200)
        self.assertEqual([item["action"] for item in json.loads(history)["operations"][:3]], ["start", "stop", "restart"])

    async def test_service_rejects_stale_missing_and_different_targets(self):
        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
        await self.session.connect(self.profile)
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            for body in (None, target, {**target, "profile_id": "other"}):
                status, _ = await request("/api/service/stop", method="POST", body=body)
                self.assertEqual(status, 409)
            run.assert_not_awaited()
        self.assertTrue(self.bot.active)

    async def test_stop_reaches_fake_ssh_from_restart_wait_starting_and_reloading(self):
        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
        original_show = self.bot.service_show
        for active_state, sub_state in (("activating", "auto-restart"), ("activating", "start"), ("reloading", "reload")):
            with self.subTest(active_state=active_state, sub_state=sub_state):
                self.bot.active = True

                def show():
                    value = original_show()
                    if self.bot.active:
                        value = value.replace("ActiveState=active\n", f"ActiveState={active_state}\n")
                        value = value.replace("SubState=running\n", f"SubState={sub_state}\n")
                    return value

                with mock.patch.object(self.bot, "service_show", side_effect=show):
                    status, body = await request("/api/status")
                    self.assertEqual(status, 200)
                    self.assertEqual(json.loads(body)["service"]["active_state"], active_state)
                    history_count = len(self.session.history)
                    stale = {**target, "connected_at": target["connected_at"] + 1}
                    self.assertEqual((await request("/api/service/stop", method="POST", body=stale))[0], 409)
                    self.assertEqual(len(self.session.history), history_count)
                    status, body = await request("/api/service/stop", method="POST", body=target)
                    self.assertEqual(status, 200)
                    self.assertTrue(json.loads(body)["ok"])
                    self.assertEqual(json.loads(body)["profile_id"], self.profile.id)
                    self.assertFalse(self.bot.active)
                    status, body = await request("/api/status")
                    self.assertEqual(json.loads(body)["service"]["active_state"], "inactive")

    async def test_sudo_failure_is_recorded_with_japanese_instructions(self):
        target = {"profile_id": self.profile.id, "connected_at": self.session.connected_at}
        with mock.patch.object(self.session, "run", side_effect=[
            CommandResult("", "sudo: a password is required", 1, 2), CommandResult("Job=\n", "", 0, 1),
        ]):
            status, body = await request("/api/service/restart", method="POST", body=target)
        self.assertEqual(status, 200)
        operation = json.loads(body)
        self.assertFalse(operation["ok"])
        self.assertIn("パスワード", operation["message"])
        self.assertIn("NOPASSWD", operation["message"])

    async def test_db_summary_is_available_through_api(self):
        status, body = await request("/api/db")
        self.assertEqual(status, 200)
        summary = json.loads(body)
        self.assertEqual(summary["schema_version"], 15)
        self.assertEqual(summary["latest_schema_version"], 15)

    async def test_db_table_rows_are_read_through_api_with_paging(self):
        status, body = await request("/api/db/tables/feedback?offset=100&limit=50")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual((data["table"], data["total"], data["offset"], len(data["rows"])), ("feedback", 130, 100, 30))
        # 架空の DB に対して本物のスクリプトを実行した結果なので、集計の件数と食い違わない
        summary = json.loads((await request("/api/db"))[1])
        self.assertEqual(summary["tables"]["feedback"], data["total"])
        history = self.session.history_list()
        self.assertTrue(any(record["command"].startswith("python3 - --db-table ") for record in history))

    async def test_db_table_rejects_out_of_range_paging_without_running_commands(self):
        """offset・limit の範囲外はログの行数と同じく 422 にし、遠隔のコマンドを送らない（#399）。"""
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            for query in ("offset=-1", "limit=0", "limit=-5", f"limit={rc.DB_TABLE_PAGE_MAX + 1}", "offset=abc"):
                with self.subTest(query=query):
                    self.assertEqual((await request(f"/api/db/tables/feedback?{query}"))[0], 422)
            self.assertEqual(run.await_count, 0)
        # 境界の値はこれまでどおり読める
        for query in ("offset=0&limit=1", f"offset=0&limit={rc.DB_TABLE_PAGE_MAX}"):
            with self.subTest(query=query):
                self.assertEqual((await request(f"/api/db/tables/feedback?{query}"))[0], 200)

    async def test_db_table_rejects_unknown_tables_without_running_commands(self):
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            for path in ("/api/db/tables/sqlite_master", "/api/db/tables/feedback%3B%20id", "/api/db/tables/Feedback"):
                with self.subTest(path=path):
                    self.assertEqual((await request(path))[0], 404)
            self.assertEqual(run.await_count, 0)
        self.assertEqual((await request("/api/db/tables/feedback", header=False))[0], 403)

    async def test_invalid_db_table_output_does_not_expose_remote_error(self):
        for stdout, stderr, code in (('{"table": "profile"}', "", 0), ("", "private detail", 1), ("not json", "", 0)):
            with mock.patch.object(self.session, "run", return_value=CommandResult(stdout, stderr, code, 1)):
                status, body = await request("/api/db/tables/feedback")
                self.assertEqual(status, 502)
                self.assertNotIn(b"private detail", body)

    async def test_connection_change_during_status_or_db_read_rejects_old_results(self):
        operations = {
            "get_status": app.get_status,
            "get_db_summary": app.get_db_summary,
            "get_db_table": lambda: app.get_db_table("feedback"),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                await self.session.connect(self.profile)
                started, release = asyncio.Event(), asyncio.Event()

                async def collect(*_args, **_kwargs):
                    started.set()
                    await release.wait()
                    return CommandResult("{}", "", 0, 1)

                with mock.patch.object(self.session, "run", side_effect=collect):
                    task = asyncio.create_task(operation())
                    await started.wait()
                    await self.session.disconnect()
                    release.set()
                    with self.assertRaises(NotConnectedError):
                        await task

    async def test_invalid_db_output_does_not_expose_remote_error(self):
        for stdout, stderr, code in (("[]", "", 0), ("", "private detail", 1)):
            with mock.patch.object(self.session, "run", return_value=CommandResult(stdout, stderr, code, 1)):
                status, body = await request("/api/db")
                self.assertEqual(status, 502)
                self.assertNotIn(b"private detail", body)

    async def test_insights_cache_coalesces_requests_and_refresh_bypasses_it(self):
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            results = await asyncio.gather(*(app.get_insights() for _ in range(4)))
            self.assertEqual(run.await_count, 1)
            self.assertEqual(sum(not result["cached"] for result in results), 1)
            await app.get_insights(refresh=True)
            self.assertEqual(run.await_count, 2)
            key = next(iter(app._insights_cache))
            saved_at, fetched_at, data = app._insights_cache[key]
            app._insights_cache[key] = (saved_at - 61, fetched_at, data)
            self.assertFalse((await app.get_insights())["cached"])
            self.assertEqual(run.await_count, 3)

    async def test_reconnect_with_same_id_invalidates_cache(self):
        await app.get_insights()
        await self.session.connect(replace(self.profile, journal_sudo=True))
        with mock.patch.object(self.session, "run", wraps=self.session.run) as run:
            self.assertFalse((await app.get_insights())["cached"])
            self.assertIn("--journal-sudo", run.call_args.args[0])

    async def test_connection_change_during_collection_does_not_cache_old_data(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def collect(*_args, **_kwargs):
            started.set()
            await release.wait()
            return CommandResult("{}", "", 0, 1)

        with mock.patch.object(self.session, "run", side_effect=collect):
            task = asyncio.create_task(app.get_insights())
            await started.wait()
            await self.session.disconnect()
            release.set()
            with self.assertRaises(NotConnectedError):
                await task
        self.assertFalse(app._insights_cache)

    async def test_bad_insights_output_is_not_cached_or_exposed(self):
        for stdout, stderr, code in (("[]", "", 0), ("invalid", "", 0), ("", "private detail", 1)):
            with self.subTest(stdout=stdout, code=code):
                with mock.patch.object(self.session, "run", return_value=CommandResult(stdout, stderr, code, 1)):
                    with self.assertRaises(SshError) as raised:
                        await app.get_insights()
                    self.assertNotIn("private detail", str(raised.exception))
                    self.assertFalse(app._insights_cache)

    async def test_invalid_resource_numbers_and_failed_commands_do_not_break_status(self):
        original = self.session.run
        for exit_status in (0, 1):
            async def run(command, **kwargs):
                if command == rc.RESOURCES_COMMAND:
                    return CommandResult("nan 0\ninf 0 0\n0\n", "private detail", exit_status, 1)
                return await original(command, **kwargs)

            with self.subTest(exit_status=exit_status), mock.patch.object(self.session, "run", side_effect=run):
                status, body = await request("/api/status")
                self.assertEqual(status, 200)
                data = json.loads(body)
                self.assertIsNone(data["service"]["uptime_seconds"])
                if exit_status:
                    self.assertTrue(data["resources_error"])
                else:
                    self.assertIsNone(data["resources"]["load"])
                self.assertNotIn(b"private detail", body)

    async def websocket(self, *, origin="http://127.0.0.1:5173", host="127.0.0.1", lines="1"):
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        headers = [(b"host", host.encode())]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "scheme": "ws", "path": "/api/logs/stream", "raw_path": b"/api/logs/stream",
                 "root_path": "", "query_string": f"lines={lines}".encode(), "headers": headers,
                 "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", app.PORT), "subprotocols": []}
        await incoming.put({"type": "websocket.connect"})
        task = asyncio.create_task(app.app(scope, incoming.get, outgoing.put))
        self.addAsyncCleanup(self.close_websocket, task, incoming)
        return task, incoming, outgoing

    async def close_websocket(self, task, incoming):
        if task.cancelled():
            return
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)

    async def test_websocket_rejects_missing_foreign_or_null_origin_and_foreign_host(self):
        for origin, host in ((None, "127.0.0.1"), ("null", "127.0.0.1"),
                             ("http://foreign.example", "127.0.0.1"),
                             ("http://127.0.0.1:5173", "foreign.example")):
            with self.subTest(origin=origin, host=host):
                task, _, outgoing = await self.websocket(origin=origin, host=host)
                await asyncio.wait_for(task, timeout=3)
                messages = []
                while not outgoing.empty():
                    messages.append(outgoing.get_nowait())
                self.assertFalse(any(message["type"] == "websocket.accept" for message in messages))
        self.assertFalse(self.bot.subscribers)

    async def test_websocket_log_line_range_rejects_before_opening_remote_process(self):
        with mock.patch.object(self.session, "open_process", new_callable=mock.AsyncMock) as opening:
            for lines in ("0", "-1", str(rc.MAX_LOG_LINES + 1), "not-a-number"):
                with self.subTest(lines=lines):
                    task, _, outgoing = await self.websocket(lines=lines)
                    await asyncio.wait_for(task, 3)
                    message = outgoing.get_nowait()
                    self.assertEqual(message["type"], "websocket.close")
                    self.assertEqual(message["code"], 1008)
            opening.assert_not_awaited()

    async def test_websocket_log_line_upper_boundary_is_accepted(self):
        task, incoming, outgoing = await self.websocket(lines=str(rc.MAX_LOG_LINES))
        self.assertEqual((await asyncio.wait_for(outgoing.get(), 3))["type"], "websocket.accept")
        while (await asyncio.wait_for(outgoing.get(), 3))["type"] != "websocket.send":
            pass
        await self.close_websocket(task, incoming)

    async def test_websocket_stream_closes_remote_channel_when_client_leaves(self):
        task, incoming, outgoing = await self.websocket()
        self.assertEqual((await asyncio.wait_for(outgoing.get(), 3))["type"], "websocket.accept")
        for _ in range(3):
            message = await asyncio.wait_for(outgoing.get(), 3)
            self.assertEqual(json.loads(message["text"])["type"], "entry")
        await self.close_websocket(task, incoming)
        for _ in range(50):
            if not self.bot.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.bot.subscribers)
        self.assertEqual((await self.session.run("true", label="疎通確認")).exit_status, 0)

    async def test_cancelled_websocket_handler_closes_remote_channel(self):
        task, _, outgoing = await self.websocket()
        while (await asyncio.wait_for(outgoing.get(), 3))["type"] != "websocket.send":
            pass
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(50):
            if not self.bot.subscribers:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.bot.subscribers)
