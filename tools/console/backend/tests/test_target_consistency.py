"""接続先照合・状態更新とWebSocket終了を外部接続なしで確認する。"""
import asyncio
import ast
import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app
from remote_scripts.insights import InsightsCollector
from deploy import DeployManager, DeployRunner, StepFailed, new_record
from ssh_session import CommandResult, NotConnectedError
from test_api import request
from test_connection_lifecycle import profile


class TargetConsistencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_deploy_requires_confirmed_profile_and_connection_time(self):
        selected = profile()
        session = SimpleNamespace(require_profile=lambda: selected, connected_at=100)
        manager = SimpleNamespace(running=False, state_unknown=False, current=None, start=Mock())
        with patch.object(app, "session", session), patch.object(app, "deploys", manager):
            for target in (None, {"profile_id": "old", "connected_at": 100}, {"profile_id": selected.id, "connected_at": 99}):
                status, _ = await request("/api/deploy", method="POST", body=target)
                self.assertEqual(status, 409)
            manager.start.assert_not_called()
            status, _ = await request("/api/deploy", method="POST", body={"profile_id": selected.id, "connected_at": 100})
            self.assertEqual(status, 200)
            self.assertIs(manager.start.call_args.kwargs["on_finished"], app._invalidate_insights)

    async def test_service_success_and_deploy_completion_invalidate_insights(self):
        selected = profile()
        session = SimpleNamespace(require_profile=lambda: selected, connected_at=100,
            run=AsyncMock(side_effect=[CommandResult("", "", 0, 1), CommandResult("Job=\n", "", 0, 1)]),
            require_command_completion=lambda: None)
        app._insights_cache["fictional"] = (0, 0, {})
        with patch.object(app, "session", session), patch.object(app, "deploys", SimpleNamespace(running=False, state_unknown=False)):
            operation = await app.control_service("restart", app.ServiceTarget(profile_id=selected.id, connected_at=100))
        self.assertTrue(operation["ok"])
        self.assertFalse(app._insights_cache)
        manager = DeployManager()
        with patch.object(DeployRunner, "run", AsyncMock()):
            manager.start(session, selected, on_finished=app._invalidate_insights)
            app._insights_cache["fictional"] = (0, 0, {})
            await manager.wait()
        self.assertFalse(app._insights_cache)

    async def test_inflight_insights_cannot_repopulate_invalidated_cache(self):
        selected = profile()
        async def run(*args, **kwargs):
            app._invalidate_insights()
            return CommandResult('{"old":true}', "", 0, 1)
        session = SimpleNamespace(require_profile=lambda: selected, generation=1, connected=True, run=run)
        app._invalidate_insights()
        with patch.object(app, "session", session):
            await app.get_insights()
        self.assertFalse(app._insights_cache)

    async def test_server_metadata_is_not_written_after_generation_change_or_command_failure(self):
        selected = profile()
        for changed, exit_status in ((True, 0), (False, 1)):
            session = SimpleNamespace(connect=AsyncMock(), generation=1, connected=True, server_info=None, snapshot=lambda: {},
                                      command_state_unknown=False)
            async def run(*args, **kwargs):
                if changed:
                    session.generation += 1
                    session.server_info = {"hostname": "新接続の値"}
                return CommandResult("old metadata", "", exit_status, 1)
            session.run = run
            with patch.object(app, "session", session), patch.object(app, "load_profiles", return_value=({selected.id: selected}, "fixture")), patch.object(app.rc, "parse_server_info") as parse:
                await app._connect(app.ConnectRequest(profile_id=selected.id))
            parse.assert_not_called()
            self.assertEqual(session.server_info, {"hostname": "新接続の値"} if changed else None)

    async def test_read_apis_reject_disconnect_without_generation_change(self):
        selected = profile()
        for read in (app.get_status, app.get_db_summary, lambda: app.get_db_table("reminders")):
            with self.subTest(read=read):
                session = SimpleNamespace(require_profile=lambda: selected, generation=1, connected=True)
                async def run(*args, **kwargs):
                    session.connected = False
                    return CommandResult('{"table":"reminders"}', "", 0, 1)
                session.run = run
                with patch.object(app, "session", session):
                    with self.assertRaises(NotConnectedError):
                        await read()

    async def test_head_rejects_nonzero_exit_even_with_valid_looking_stdout(self):
        selected = profile()
        session = SimpleNamespace(run=AsyncMock(return_value=CommandResult("main\n" + "a" * 40, "", 1, 1)))
        record = new_record(1, selected)
        runner = DeployRunner(session, selected, record)
        with self.assertRaises(StepFailed):
            await runner._head(record.steps[0])

    async def test_websocket_disconnect_during_send_reaps_both_directions_and_process(self):
        selected = profile()
        async def lines():
            yield '{"MESSAGE":"架空のログ"}'
        process = SimpleNamespace(stdout=lines(), stderr=SimpleNamespace(read=AsyncMock(return_value="")),
                                  close=Mock(), wait_closed=AsyncMock())
        receiving_done = asyncio.Event()
        async def receive():
            try:
                await asyncio.Future()
            finally:
                receiving_done.set()
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
            send_json=AsyncMock(side_effect=app.WebSocketDisconnect()), receive=receive, close=AsyncMock())
        session = SimpleNamespace(require_profile=lambda: selected, generation=1, open_process=AsyncMock(return_value=process))
        with patch.object(app, "session", session):
            await app.stream_logs(websocket)
        self.assertTrue(receiving_done.is_set())
        process.close.assert_called_once()
        process.wait_closed.assert_awaited_once()

    async def test_log_stream_discards_process_started_for_old_generation(self):
        process = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
        session = SimpleNamespace(require_profile=profile, generation=1)

        async def open_process(*args, **kwargs):
            session.generation += 1
            return process

        session.open_process = open_process
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
                                    send_json=AsyncMock(), close=AsyncMock())
        with patch.object(app, "session", session):
            await app.stream_logs(websocket)
        process.close.assert_called_once()
        process.wait_closed.assert_awaited_once()
        self.assertEqual(websocket.send_json.await_args.args[0]["type"], "error")

    async def test_log_stream_discards_lines_after_generation_change(self):
        session = SimpleNamespace(require_profile=profile, generation=1)

        async def lines():
            session.generation += 1
            yield '{"MESSAGE":"old"}'

        process = SimpleNamespace(stdout=lines(), stderr=SimpleNamespace(read=AsyncMock(return_value="")),
                                  close=Mock(), wait_closed=AsyncMock())
        session.open_process = AsyncMock(return_value=process)
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
                                    send_json=AsyncMock(), receive=AsyncMock(side_effect=lambda: asyncio.Future()),
                                    close=AsyncMock())
        with patch.object(app, "session", session):
            await app.stream_logs(websocket)
        websocket.send_json.assert_not_awaited()
        process.close.assert_called_once()

    async def test_repeated_websocket_handler_cancellation_waits_for_transfer_cleanup(self):
        receiving = asyncio.Event()
        cleaning = asyncio.Event()
        release = asyncio.Event()
        async def lines():
            try:
                receiving.set()
                await asyncio.Future()
                yield ""
            finally:
                cleaning.set()
                await release.wait()
        process = SimpleNamespace(stdout=lines(), stderr=SimpleNamespace(read=AsyncMock(return_value="")), close=Mock(), wait_closed=AsyncMock())
        websocket = SimpleNamespace(headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(), send_json=AsyncMock(), receive=AsyncMock(side_effect=lambda: asyncio.Future()), close=AsyncMock())
        async def receive():
            await asyncio.Future()
        websocket.receive = receive
        session = SimpleNamespace(require_profile=profile, generation=1, open_process=AsyncMock(return_value=process))
        with patch.object(app, "session", session):
            task = asyncio.create_task(app.stream_logs(websocket))
            try:
                await asyncio.wait_for(receiving.wait(), 2)
                task.cancel()
                await asyncio.wait_for(cleaning.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        process.close.assert_called_once()
        process.wait_closed.assert_awaited_once()
        websocket.close.assert_awaited_once()


class InsightsChoicesConsistencyTests(unittest.TestCase):
    def test_current_bot_choices_and_defaults_are_read_without_import(self):
        root = Path(__file__).resolve().parents[4]
        config = root / "src/persona_bot/config"
        choices_tree = ast.parse((config / "choices.py").read_text(encoding="utf-8-sig"))
        collector = InsightsCollector(root, "fixture")
        constants = collector.read_constants(config / "constants.py")
        collector.constants = constants
        collector.choices_tree = choices_tree
        defaults = {"MODEL_CHOICES": "DEFAULT_MODEL_NAME", "RESPOND_MODE_CHOICES": "DEFAULT_RESPOND_MODE",
                    "AUTO_TOPIC_INTERVAL_CHOICES": "DEFAULT_AUTO_TOPIC_INTERVAL_HOURS"}
        for choice_name, default_name in defaults.items():
            assignment = next(node for node in choices_tree.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == choice_name for target in node.targets))
            expected = {}
            for call in assignment.value.elts:
                args = {item.arg: item.value for item in call.keywords}
                name = ast.literal_eval(args["name"])
                value_node = args["value"]
                value = constants[value_node.id] if isinstance(value_node, ast.Name) else ast.literal_eval(value_node)
                expected[value] = name
            with self.subTest(choice=choice_name):
                self.assertTrue(expected)
                self.assertEqual(collector.choice_names(choice_name), expected)
                self.assertIn(constants[default_name], expected)
        collector.models = collector.choice_names("MODEL_CHOICES")
        collector.modes = collector.choice_names("RESPOND_MODE_CHOICES")
        collector.auto_topic_intervals = collector.choice_names("AUTO_TOPIC_INTERVAL_CHOICES")
        effective = collector.describe_settings({})
        self.assertEqual(effective["model"], collector.models[constants["DEFAULT_MODEL_NAME"]])
        self.assertEqual(effective["respond_mode"], collector.modes[constants["DEFAULT_RESPOND_MODE"]])
        self.assertEqual(effective["auto_topic_interval_hours"], constants["DEFAULT_AUTO_TOPIC_INTERVAL_HOURS"])
        for interval in collector.auto_topic_intervals:
            self.assertEqual(collector.describe_settings({"auto_topic_interval_hours": interval})["auto_topic_interval_hours"], interval)



class SummaryIndexTests(unittest.TestCase):
    def test_direct_import_uses_feedback_period_index_and_preserves_database(self):
        script = Path(__file__).resolve().parents[1] / "remote_scripts/db_summary.py"
        spec = importlib.util.spec_from_file_location("test_db_summary_module", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            con = sqlite3.connect(db_path)
            try:
                con.executescript("CREATE TABLE feedback(guild_id INTEGER, created_at TEXT, rating INTEGER); CREATE INDEX idx_feedback_guild_created ON feedback(guild_id,created_at);")
                con.execute("INSERT INTO feedback VALUES(1,datetime('now'),1)")
                con.commit()
            finally:
                con.close()
            original = db_path.read_bytes()
            statements = []
            connect = sqlite3.connect
            def traced(*args, **kwargs):
                connection = connect(*args, **kwargs)
                connection.set_trace_callback(statements.append)
                return connection
            with patch.object(module.sqlite3, "connect", side_effect=traced):
                result = module.collect(str(db_path), str(Path(directory) / "blacklist.json"))
            self.assertEqual(result["feedback"], {"good": 1, "bad": 0})
            query = next(sql for sql in statements if "SUM(rating = 1)" in sql)
            con = connect(db_path)
            try:
                plans = " ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN " + query))
                self.assertIn("SEARCH feedback USING INDEX idx_feedback_guild_created", plans)
                self.assertIn("created_at>? AND created_at<?", plans)
            finally:
                con.close()
            self.assertEqual(db_path.read_bytes(), original)
