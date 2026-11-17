"""remote_commands.py（コマンドの組み立てと出力の読み取り）のテスト。外部通信なし。"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import remote_commands as rc  # noqa: E402


class CommandBuildTest(unittest.TestCase):
    def test_commit_list_uses_unit_separator_and_preserves_subject_spaces(self):
        self.assertEqual(rc.parse_commit_list("abc1234\x1f題名\ndef5678\x1ffix: 架空の修正\n\n"), [
            {"hash": "abc1234", "subject": "題名"}, {"hash": "def5678", "subject": "fix: 架空の修正"},
        ])

    def test_commit_list_rejects_invalid_hash_and_missing_subject(self):
        for line in ("nothexg\x1f件名", "abc12\x1f短すぎる", "a" * 41 + "\x1f長すぎる", "abc1234", "abc1234\x1f ", "abc1234\x1f件名\x1f別項目"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                rc.parse_commit_list(line)
        self.assertEqual(rc.parse_commit_list("A" * 40 + "\x1ffix: 空白を  維持"), [{"hash": "A" * 40, "subject": "fix: 空白を  維持"}])

    def test_service_control_allows_only_whitelisted_actions(self):
        self.assertEqual(rc.service_control_command("bot", "restart"), "sudo -n systemctl restart bot")
        with self.assertRaises(ValueError):
            rc.service_control_command("bot", "reboot")

    def test_service_job_requires_an_explicit_property_line(self):
        self.assertEqual(rc.service_job_command("bot"), "systemctl show bot --no-pager --all --property=Job")
        for text, expected in (("Job=\n", 0), ("Job=0\n", 0), ("Job=42\n", 42)):
            with self.subTest(text=text):
                self.assertEqual(rc.parse_service_job(text), expected)
        for text in ("", "Job=-1", "42", "Job=0\nJob=42", "Job=0\nActiveState=active"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                rc.parse_service_job(text)
        with self.assertRaises(ValueError):
            rc.service_job_command("bot; id")

    def test_rejects_service_name_with_shell_symbols(self):
        with self.assertRaises(ValueError):
            rc.service_show_command("bot; rm -rf ~")
        with self.assertRaises(ValueError):
            rc.journal_command("bot && id", lines=10, follow=False, use_sudo=False)
        for service in ("bot\n", "--help", "-bot"):
            with self.subTest(service=service), self.assertRaises(ValueError):
                rc.service_show_command(service)

    def test_journal_command_clamps_lines_and_adds_flags(self):
        command = rc.journal_command("bot", lines=999999, follow=True, use_sudo=True)
        self.assertEqual(command, f"sudo -n journalctl -u bot -n {rc.MAX_LOG_LINES} -o json --no-pager -f")

    def test_insights_command(self):
        self.assertEqual(
            rc.insights_command("/home/u/my bot", "bot"),
            "python3 - --insights '/home/u/my bot' bot",
        )
        with self.assertRaises(ValueError):
            rc.insights_command("/srv/bot", "bot; id")

    def test_paths_are_quoted(self):
        self.assertIn("cd '/home/u/my bot' &&", rc.git_command("/home/u/my bot"))
        self.assertEqual(
            rc.db_summary_command("/srv/bot/"),
            "python3 - /srv/bot/data/memory/bot.db /srv/bot/data/ng/blacklist.json",
        )

    def test_db_table_command_accepts_only_known_tables_and_clamps_paging(self):
        self.assertEqual(
            rc.db_table_command("/home/u/my bot/", "feedback", 50, 50),
            "python3 - --db-table '/home/u/my bot/data/memory/bot.db' feedback 50 50",
        )
        self.assertTrue(rc.db_table_command("/srv/bot", "reminders", -3, 10**6).endswith(" reminders 0 200"))
        self.assertTrue(rc.db_table_command("/srv/bot", "profile", 0, 0).endswith(" profile 0 1"))
        for table in ("sqlite_master", "feedback; id", "../feedback", "Feedback"):
            with self.subTest(table=table), self.assertRaises(ValueError):
                rc.db_table_command("/srv/bot", table, 0, 50)


class NeedsSudoPasswordTest(unittest.TestCase):
    def test_detects_both_sudo_refusals(self):
        self.assertTrue(rc.needs_sudo_password("sudo: a password is required"))
        self.assertTrue(rc.needs_sudo_password("sudo: a terminal is required to read the password"))

    def test_other_errors_are_not_password_requests(self):
        for stderr in ("", "Failed to restart bot.service: Unit not found.", "permission denied"):
            with self.subTest(stderr=stderr):
                self.assertFalse(rc.needs_sudo_password(stderr))


class ParseTest(unittest.TestCase):
    def test_parse_service_show_active(self):
        text = (
            "Description=Persona bot\nLoadState=loaded\nActiveState=active\nSubState=running\n"
            "MainPID=1234\nNRestarts=2\nMemoryCurrent=104857600\nCPUUsageNSec=2500000000\n"
            "ExecMainStartTimestampMonotonic=100000000\nUnitFileState=enabled\nResult=success\n"
        )
        status = rc.parse_service_show(text, "bot", host_uptime_seconds=400.0)
        self.assertEqual(status["active_state"], "active")
        self.assertEqual(status["main_pid"], 1234)
        self.assertEqual(status["memory_bytes"], 104857600)
        self.assertAlmostEqual(status["cpu_seconds"], 2.5)
        self.assertAlmostEqual(status["uptime_seconds"], 300.0)

    def test_parse_service_show_inactive_has_no_uptime(self):
        text = "ActiveState=inactive\nMainPID=0\nMemoryCurrent=[not set]\nCPUUsageNSec=18446744073709551615\n"
        status = rc.parse_service_show(text, "bot", host_uptime_seconds=400.0)
        self.assertIsNone(status["main_pid"])
        self.assertIsNone(status["memory_bytes"])
        self.assertIsNone(status["cpu_seconds"])
        self.assertIsNone(status["uptime_seconds"])

    def test_parse_resources(self):
        text = (
            "1000.50 1500.00\n0.12 0.08 0.05 1/234 5678\n2\n"
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:      1000 600 100 10 300 350\n"
            "Swap:     2000 500 1500\n"
            " 50000 20000 30000\n"
        )
        resources = rc.parse_resources(text)
        self.assertEqual(resources["uptime_seconds"], 1000.5)
        self.assertEqual(resources["load"], [0.12, 0.08, 0.05])
        self.assertEqual(resources["cpu_count"], 2)
        self.assertEqual(resources["memory"], {"total": 1000, "used": 650, "available": 350})
        self.assertEqual(resources["swap"]["used"], 500)
        self.assertEqual(resources["disk"], {"total": 50000, "used": 20000, "available": 30000})

    def test_parse_resources_tolerates_garbage(self):
        resources = rc.parse_resources("oops\n")
        self.assertIsNone(resources["memory"])
        self.assertIsNone(resources["disk"])

    def test_nonfinite_and_out_of_range_status_values_are_empty(self):
        for value in ("nan", "inf", "-1", "1e9999"):
            with self.subTest(value=value):
                resources = rc.parse_resources(f"{value} 0\n{value} 0 0\n0\n")
                self.assertIsNone(resources["uptime_seconds"])
                self.assertIsNone(resources["load"])
                self.assertIsNone(resources["cpu_count"])
                json.dumps(resources, allow_nan=False)
        status = rc.parse_service_show("CPUUsageNSec=" + "9" * 400 + "\nMainPID=-2", "bot", None)
        self.assertIsNone(status["cpu_seconds"])
        self.assertIsNone(status["main_pid"])

    def test_parse_git(self):
        git = rc.parse_git("main\nabc1234\x1ffix: 修正\x1f2026-10-16T21:14:05+09:00\nver0.1.7\n      3\n")
        self.assertEqual(git["branch"], "main")
        self.assertEqual(git["subject"], "fix: 修正")
        self.assertEqual(git["dirty_files"], 3)

    def test_parse_journal_line(self):
        line = json.dumps(
            {"MESSAGE": "ログイン成功", "__REALTIME_TIMESTAMP": "1760000000123456", "PRIORITY": "6",
             "_PID": "42", "SYSLOG_IDENTIFIER": "python"}
        )
        entry = rc.parse_journal_line(line)
        self.assertEqual(entry, {"ts": 1760000000123, "message": "ログイン成功", "priority": 6, "pid": 42,
                                 "source": "python"})

    def test_parse_journal_line_with_byte_array_message(self):
        line = json.dumps({"MESSAGE": list("あ".encode()) + [0xFF], "__REALTIME_TIMESTAMP": "1000"})
        entry = rc.parse_journal_line(line)
        self.assertTrue(entry["message"].startswith("あ"))

    def test_parse_journal_line_ignores_non_json(self):
        self.assertIsNone(rc.parse_journal_line("-- No entries --"))
        self.assertIsNone(rc.parse_journal_line('{"MESSAGE":' + "9" * 5000 + "}"))
        self.assertIsNone(rc.parse_journal_line("[]"))
        self.assertEqual(rc.parse_journal_line('{"MESSAGE":[-1,256,null]}')["message"], "")


if __name__ == "__main__":
    unittest.main()
