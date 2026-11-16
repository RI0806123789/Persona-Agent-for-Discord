"""模擬 SSH サーバーへ「本物の SSH」で接続する結合テスト。

ホスト鍵の検証・鍵認証・コマンド実行・ログのストリーム・サービス操作を、
本番サーバーに触れずに確かめる（127.0.0.1 の空きポートだけを使う）。
"""

import asyncio
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import asyncssh  # noqa: E402

import remote_commands as rc  # noqa: E402
from ssh_fixtures import FakeSshEnvironment  # noqa: E402
from ssh_session import SshError, SshSession  # noqa: E402


class SshIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = await FakeSshEnvironment.start(live_interval=(0.2, 0.4))
        self.addAsyncCleanup(self.env.close)
        self.key_dir, self.bot, self.profile = self.env.key_dir, self.env.bot, self.env.profile
        self.acceptor, self.live_task = self.env.acceptor, self.env.live_task
        self.session = SshSession()
        # 後に登録した片付けから先に実行される（接続を切ってから模擬サーバーを閉じる）。
        self.addAsyncCleanup(self.session.disconnect)

    async def test_connect_and_read_status(self):
        await self.session.connect(self.profile)
        self.assertTrue(self.session.connected)
        self.assertTrue(self.session.fingerprint.startswith("SHA256:"))

        service = await self.session.run(rc.service_show_command("bot"), label="サービス状態")
        resources = await self.session.run(rc.RESOURCES_COMMAND, label="リソース")
        parsed_resources = rc.parse_resources(resources.stdout)
        status = rc.parse_service_show(service.stdout, "bot", parsed_resources["uptime_seconds"])
        self.assertEqual(status["active_state"], "active")
        self.assertGreater(status["uptime_seconds"], 0)
        self.assertEqual(parsed_resources["cpu_count"], 2)

        git = rc.parse_git((await self.session.run(rc.git_command("/home/ubuntu/x"), label="Git")).stdout)
        self.assertEqual(git["branch"], "main")
        self.assertGreaterEqual(len(self.session.history), 4)
        self.assertEqual(len(self.bot.connections), 1)

    async def test_reads_host_name_user_and_key_from_ssh_config(self):
        home = self.key_dir / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh/config").write_text(
            f'Host test-alias\n  HostName 127.0.0.1\n  Port {self.profile.port}\n'
            f'  User tester\n  IdentityFile "{(self.key_dir / "client_key").as_posix()}"\n', encoding="utf-8"
        )
        profile = replace(self.profile, host="test-alias", port=None, username=None, key_path=None, use_ssh_config=True)
        with mock.patch("ssh_session.Path.home", return_value=home):
            await self.session.connect(profile)
        self.assertEqual((await self.session.run("true", label="疎通確認")).exit_status, 0)

    async def test_disabled_ssh_config_does_not_redirect_the_host(self):
        home = self.key_dir / "home"
        (home / ".ssh").mkdir(parents=True)
        (home / ".ssh/config").write_text("Host 127.0.0.1\n  HostName 127.0.0.2\n", encoding="utf-8")
        with mock.patch("ssh_session.Path.home", return_value=home):
            await self.session.connect(self.profile)
        self.assertTrue(self.session.connected)

    async def test_logs_and_live_stream(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.journal_command("bot", lines=20, follow=False, use_sudo=False), label="ログ取得"
        )
        entries = [entry for line in result.stdout.splitlines() if (entry := rc.parse_journal_line(line))]
        self.assertEqual(len(entries), 20)

        process = await self.session.open_process(
            rc.journal_command("bot", lines=1, follow=True, use_sudo=False), label="ログ追跡"
        )
        try:
            async def receive_entries():
                received = []
                async for line in process.stdout:
                    received.append(rc.parse_journal_line(line))
                    if len(received) >= 3:  # 過去1件 + 新着2件
                        return received
                return received

            # Python 3.10にもあるwait_forで、同じ待機上限を保つ。
            received = await asyncio.wait_for(receive_entries(), timeout=5)
            self.assertEqual(len(received), 3)
        finally:
            process.close()
        # 閉じたあとも同じ接続で別のコマンドを実行できる
        self.assertEqual((await self.session.run("true", label="疎通確認")).exit_status, 0)

    async def test_restart_changes_pid(self):
        await self.session.connect(self.profile)
        before = self.bot.pid
        result = await self.session.run(rc.service_control_command("bot", "restart"), label="再起動")
        self.assertEqual(result.exit_status, 0)
        self.assertNotEqual(self.bot.pid, before)
        self.assertTrue(self.bot.active)

        await self.session.run(rc.service_control_command("bot", "stop"), label="停止")
        service = await self.session.run(rc.service_show_command("bot"), label="サービス状態")
        self.assertEqual(rc.parse_service_show(service.stdout, "bot", None)["active_state"], "inactive")

    async def test_db_summary_via_stdin(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.db_summary_command(self.profile.bot_dir), label="DB", input=rc.DB_SUMMARY_SCRIPT
        )
        self.assertEqual(result.exit_status, 0)
        self.assertIn('"schema_version": 15', result.stdout)

    async def test_db_table_via_stdin(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.db_table_command(self.profile.bot_dir, "reminders", 0, 3), label="DB の中身", input=rc.DB_TABLE_SCRIPT
        )
        self.assertEqual(result.exit_status, 0, result.stderr)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual((data["table"], data["errors"], len(data["rows"])), ("reminders", [], 3))
        self.assertGreater(data["total"], 3)

    async def test_insights_via_stdin(self):
        await self.session.connect(self.profile)
        result = await self.session.run(
            rc.insights_command(self.profile.bot_dir, "bot"), label="状態", input=rc.INSIGHTS_SCRIPT
        )
        self.assertEqual(result.exit_status, 0)
        data = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertTrue(data["discord"]["ok"])
        self.assertEqual(len(data["discord"]["guilds"]), 3)

    async def test_missing_key_keeps_specific_error_in_response_and_history(self):
        missing = self.key_dir / "missing_client_key"
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, key_path=str(missing)))
        expected = f"秘密鍵が見つかりません: {missing}"
        self.assertEqual(str(context.exception), expected)
        self.assertEqual(self.session.last_error, expected)
        self.assertEqual(self.session.history_list()[0]["error"], expected)
        self.assertFalse(self.session.connected)

    async def test_missing_known_hosts_keeps_path_and_remedy(self):
        missing = self.key_dir / "missing_known_hosts"
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, known_hosts=str(missing)))
        self.assertIn(f"known_hosts が見つかりません: {missing}", str(context.exception))
        self.assertIn("profiles.json", str(context.exception))
        self.assertFalse(self.session.connected)

    async def test_missing_default_known_hosts_keeps_registration_guidance(self):
        home = self.key_dir / "empty_home"
        with mock.patch("ssh_session.Path.home", return_value=home):
            with self.assertRaises(SshError) as context:
                await self.session.connect(replace(self.profile, known_hosts=None))
        self.assertIn(str(home / ".ssh/known_hosts"), str(context.exception))
        self.assertIn("フィンガープリント", str(context.exception))
        self.assertFalse(self.session.connected)

    async def test_rejects_unknown_host_key(self):
        """known_hosts に別の鍵が書かれていたら、接続してはいけない（なりすまし対策）。"""
        other_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().split()
        fake_known_hosts = self.key_dir / "fake_known_hosts"
        fake_known_hosts.write_text(f"[127.0.0.1]:{self.profile.port} {other_key[0]} {other_key[1]}\n")
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, known_hosts=str(fake_known_hosts)))
        self.assertIn("ホスト鍵", str(context.exception))
        self.assertFalse(self.session.connected)

    async def test_rejects_empty_known_hosts(self):
        empty = self.key_dir / "empty_known_hosts"
        empty.write_text("")
        with self.assertRaises(SshError):
            await self.session.connect(replace(self.profile, known_hosts=str(empty)))
        self.assertFalse(self.session.connected)

    async def test_rejects_wrong_client_key(self):
        wrong_key = self.key_dir / "wrong_key"
        asyncssh.generate_private_key("ssh-ed25519").write_private_key(wrong_key)
        with self.assertRaises(SshError) as context:
            await self.session.connect(replace(self.profile, key_path=str(wrong_key)))
        self.assertIn("認証", str(context.exception))

    async def test_detects_connection_loss(self):
        await self.session.connect(self.profile)
        for conn in list(self.bot.connections):
            conn.close()  # サーバー側から切る
        async def wait_for_disconnect():
            while self.session.connected:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait_for_disconnect(), timeout=5)
        self.assertIn("切れました", self.session.last_error)


if __name__ == "__main__":
    unittest.main()
