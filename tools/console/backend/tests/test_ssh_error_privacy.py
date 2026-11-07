"""SSH設定の診断原文と安全な文面を外部通信なしで確認する。"""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import app
from profiles import Profile
from ssh_session import CONNECTION_LOST_SAFE_MESSAGE, SshError, SshSession
from test_api import request


class MissingPath:
    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text

    def is_file(self):
        return False


def profile(**changes):
    values = dict(id="fixture", label="架空の接続先", environment="development", host="fixture.invalid",
                  port=None, username="REMOTE_OWNER", key_path=None, known_hosts="fixture-known",
                  use_ssh_config=False, service="bot", bot_dir="/srv/fixture", journal_sudo=False, note="")
    values.update(changes)
    return Profile(**values)


def local_path(style):
    # 架空の場所をパーツから作る。実際の鍵・ホーム・SSH設定には触れない。
    if style == "windows":
        return "\\".join(("Q:", "Users", "LOCAL_OWNER", ".ssh", "fixture-key"))
    if style == "outside":
        return "/" + "/".join(("vault", "LOCAL_OWNER", "keys", "fixture-key"))
    return "/" + "/".join(("home", "LOCAL_OWNER", ".ssh", "fixture-key"))


class SshErrorPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_log_stream_carries_raw_and_safe_ssh_error_messages(self):
        raw = "秘密鍵が見つかりません: " + local_path("posix")
        safe = "秘密鍵が見つかりません: <LOCAL_PATH>（key_path を確認してください）"
        for safe_message in (safe, None):
            with self.subTest(safe_message=safe_message):
                websocket = SimpleNamespace(
                    headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
                    send_json=AsyncMock(), close=AsyncMock(),
                )
                session = SimpleNamespace(
                    require_profile=profile, generation=1,
                    open_process=AsyncMock(side_effect=SshError(raw, safe_message=safe_message)),
                )
                with patch.object(app, "session", session):
                    await app.stream_logs(websocket)
                websocket.send_json.assert_awaited_once_with({
                    "type": "error", "message": raw, "message_safe": safe_message or (
                        "ログの接続を開始できませんでした。SSHの接続設定と接続状態を確認してから再試行してください。"
                    ),
                })
                websocket.close.assert_awaited_once()

    async def test_log_stream_hides_uncollected_host_from_process_start_error(self):
        session = SshSession()
        session.profile = profile()
        resolved_host = "uncollected-resolved.fixture.invalid"
        session._conn = SimpleNamespace(create_process=AsyncMock(side_effect=OSError(resolved_host)))
        websocket = SimpleNamespace(
            headers={"origin": "http://127.0.0.1:5173"}, accept=AsyncMock(),
            send_json=AsyncMock(), close=AsyncMock(),
        )
        with patch.object(app, "session", session):
            await app.stream_logs(websocket)
        frame = websocket.send_json.await_args.args[0]
        self.assertEqual(frame["type"], "error")
        self.assertIn(resolved_host, frame["message"])
        self.assertIsInstance(frame["message_safe"], str)
        self.assertNotIn(resolved_host, frame["message_safe"])
        self.assertIn("再試行", frame["message_safe"])
        self.assertIn(resolved_host, session.history_list()[0]["error"])
        websocket.close.assert_awaited_once()

    async def failure(self, selected, path):
        session = SshSession()
        with patch("ssh_session.resolve_local_path", return_value=MissingPath(path)), patch(
            "ssh_session.asyncssh.connect", new_callable=AsyncMock,
        ) as connecting:
            with self.assertRaises(SshError) as raised:
                await session.connect(selected)
            connecting.assert_not_awaited()
        return session, raised.exception

    async def test_key_diagnostics_keep_original_and_safe_messages_for_each_path_style(self):
        for style in ("posix", "windows", "outside"):
            with self.subTest(style=style):
                path = local_path(style)
                session, error = await self.failure(profile(key_path="fixture-key"), path)
                self.assertEqual(str(error), f"秘密鍵が見つかりません: {path}")
                safe = getattr(error, "safe_message", None)
                self.assertIsInstance(safe, str)
                self.assertNotIn("LOCAL_OWNER", safe)
                self.assertNotIn(path, safe)
                self.assertIn("key_path", safe)
                self.assertIn("秘密鍵", safe)
                if style != "outside":
                    self.assertIn("<LOCAL_HOME>/.ssh/<KEY_FILE>", safe)
                else:
                    self.assertIn("<LOCAL_PATH>", safe)
                self.assertEqual(session.snapshot().get("last_error_safe"), safe)
                self.assertEqual(session.history_list()[0].get("error_safe"), safe)
                self.assertEqual(session.last_error, str(error))

    async def test_explicit_known_hosts_keeps_setting_and_default_location_guidance(self):
        path = local_path("windows")
        session, error = await self.failure(profile(), path)
        safe = getattr(error, "safe_message", None)
        self.assertIsInstance(safe, str)
        self.assertNotIn("LOCAL_OWNER", safe)
        self.assertIn("known_hosts が見つかりません", safe)
        self.assertIn("~/.ssh/known_hosts", safe)
        self.assertIn("profiles.json", safe)
        self.assertIn(path, str(error))
        self.assertEqual(session.history_list()[0]["error"], str(error))

    async def test_default_known_hosts_keeps_registration_guidance_without_local_owner(self):
        home = Path("/").joinpath("home", "LOCAL_OWNER")
        session = SshSession()
        with patch("ssh_session.Path.home", return_value=home), patch("ssh_session.Path.is_file", return_value=False):
            with self.assertRaises(SshError) as raised:
                await session.connect(profile(known_hosts=None))
        error = raised.exception
        safe = getattr(error, "safe_message", None)
        self.assertIsInstance(safe, str)
        self.assertIn(str(home / ".ssh" / "known_hosts"), str(error))
        self.assertNotIn("LOCAL_OWNER", safe)
        self.assertNotIn("fixture.invalid", safe)
        self.assertIn("<LOCAL_HOME>/.ssh/known_hosts", safe)
        self.assertIn("フィンガープリント", safe)
        self.assertIn("ホスト鍵を登録", safe)

    async def test_api_error_state_and_history_carry_the_same_safe_diagnostic(self):
        selected = profile(key_path="fixture-key")
        session = SshSession()
        with patch.object(app, "session", session), patch.object(app, "load_profiles", return_value=({selected.id: selected}, "profiles.json")), patch.object(
            app, "deploys", SimpleNamespace(active=False, state_unknown=False),
        ), patch.object(app, "_connection_change_lock", asyncio.Lock()), patch(
            "ssh_session.resolve_local_path", return_value=MissingPath(local_path("posix")),
        ):
            status, body = await request("/api/connect", method="POST", body={"profile_id": selected.id})
            self.assertEqual(status, 502)
            error = json.loads(body)
            self.assertIsInstance(error.get("detail_safe"), str)
            self.assertIn("LOCAL_OWNER", error["detail"])
            self.assertNotIn("LOCAL_OWNER", error["detail_safe"])
            _, state = await request("/api/connection")
            _, history = await request("/api/history")
            self.assertEqual(json.loads(state).get("last_error_safe"), error["detail_safe"])
            self.assertEqual(json.loads(history)["commands"][0].get("error_safe"), error["detail_safe"])
            self.assertEqual(json.loads(state)["last_error"], error["detail"])
            self.assertEqual(json.loads(history)["commands"][0]["error"], error["detail"])

    async def test_reconnect_clears_current_safe_message_and_keeps_old_history(self):
        session, error = await self.failure(profile(key_path="fixture-key"), local_path("posix"))
        self.assertIsInstance(session.snapshot().get("last_error_safe"), str)
        first = session.history_list()[0]
        conn = Mock(get_server_host_key=Mock(return_value=None), wait_closed=AsyncMock())
        with patch.object(session, "_build_options", return_value={}), patch("ssh_session.asyncssh.connect", AsyncMock(return_value=conn)):
            await session.connect(profile(host="other.invalid"))
        self.assertIsNone(session.snapshot()["last_error"])
        self.assertIsNone(session.snapshot().get("last_error_safe"))
        self.assertEqual(session.history_list()[1], first)
        self.assertEqual(first["error"], str(error))
        await session.disconnect()
        self.assertIsNone(session.snapshot().get("last_error_safe"))
        self.assertEqual(session.history_list()[1], first)

    async def test_plain_ssh_error_keeps_existing_api_shape_and_clears_previous_safe_message(self):
        session, _ = await self.failure(profile(key_path="fixture-key"), local_path("outside"))
        self.assertIsInstance(session.snapshot().get("last_error_safe"), str)
        with patch.object(session, "_build_options", side_effect=SshError("架空の既存エラー")):
            with self.assertRaises(SshError) as raised:
                await session.connect(profile())
        response = await app.handle_ssh_error(None, raised.exception)
        self.assertEqual(json.loads(response.body), {"detail": "架空の既存エラー"})
        self.assertIsNone(session.snapshot().get("last_error_safe"))
        self.assertIsNone(session.history_list()[0].get("error_safe"))
        self.assertEqual(session.last_error, "架空の既存エラー")

    async def test_disconnect_does_not_reuse_configuration_safe_message(self):
        session, _ = await self.failure(profile(key_path="fixture-key"), local_path("posix"))
        configuration_safe = session.snapshot().get("last_error_safe")
        self.assertIsInstance(configuration_safe, str)
        conn = Mock()
        session._conn = conn
        session._handle_connection_lost(conn, None)
        self.assertEqual(session.snapshot().get("last_error_safe"), CONNECTION_LOST_SAFE_MESSAGE)
        self.assertNotEqual(session.snapshot().get("last_error_safe"), configuration_safe)
        self.assertIn("切れました", session.last_error)

    async def test_disconnect_reason_is_kept_out_of_the_safe_message_and_history(self):
        """切断理由のホスト名・IP・パスは、伏せ字モード用の文面にも履歴の安全な文面にも出さない（#399）。"""
        session = SshSession()
        conn = Mock()
        session._conn = conn
        reason = "Connection lost: fictional-host.invalid (192.0.2.10) /fictional/private/path"
        session._handle_connection_lost(conn, ConnectionResetError(reason))
        snapshot = session.snapshot()
        record = session.history_list()[0]
        # 通常表示の原文には、これまでどおり理由が残る
        self.assertIn(reason, snapshot["last_error"])
        self.assertIn(reason, record["error"])
        for safe in (snapshot["last_error_safe"], record["error_safe"]):
            self.assertEqual(safe, CONNECTION_LOST_SAFE_MESSAGE)
            for secret in ("fictional-host.invalid", "192.0.2.10", "/fictional/private/path"):
                self.assertNotIn(secret, safe)
        # 再接続・切断で現在の安全な文面は消え、履歴は残る（#218 と同じ扱い）
        await session.disconnect()
        self.assertIsNone(session.snapshot().get("last_error_safe"))
        self.assertEqual(session.history_list()[0]["error_safe"], CONNECTION_LOST_SAFE_MESSAGE)
