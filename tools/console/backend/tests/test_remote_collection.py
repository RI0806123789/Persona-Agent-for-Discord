"""遠隔スクリプトのimport境界・実行ごとの状態と標準入力CLIを確認する。"""

from contextlib import ExitStack, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from remote_scripts import db_table, insights
from tests.test_remote_scripts import make_fake_bot, run_script


class RemoteCollectionTests(unittest.TestCase):
    def test_import_does_not_read_runtime_arguments_or_collect(self):
        folder = Path(__file__).resolve().parents[1] / "remote_scripts"
        for name in ("insights", "db_table"):
            with self.subTest(script=name), ExitStack() as stack:
                output = stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(sys, "argv", ["fixture"]))
                boundaries = [stack.enter_context(patch(target, side_effect=AssertionError("import時の外部参照")))
                              for target in (
                                  "pathlib.Path.read_text", "pathlib.Path.exists", "pathlib.Path.is_file",
                                  "pathlib.Path.is_dir", "pathlib.Path.iterdir", "os.path.exists",
                                  "sqlite3.connect", "subprocess.run", "urllib.request.urlopen",
                              )]
                spec = importlib.util.spec_from_file_location("fixture_" + name, folder / (name + ".py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertTrue(callable(module.collect))
                self.assertEqual(output.getvalue(), "")
                for boundary in boundaries:
                    boundary.assert_not_called()

    def test_table_collect_calls_do_not_share_rows_or_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE feedback (message_id INTEGER, user_id TEXT, rating INTEGER)")
                con.execute("INSERT INTO feedback VALUES (1, '11', 1)")
                con.commit()
            finally:
                con.close()
            first = db_table.collect(db_path, "feedback")
            missing = db_table.collect(db_path, "reminders")
            again = db_table.collect(db_path, "feedback", 1, 10)
            self.assertEqual(len(first["rows"]), 1)
            self.assertTrue(missing["errors"])
            self.assertEqual(again["errors"], [])
            self.assertEqual(again["rows"], [])
            self.assertEqual(again["total"], 1)
            self.assertEqual(first["errors"], [])
            self.assertEqual(len(first["rows"]), 1)

    def test_insights_calls_do_not_share_settings_errors_or_time_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing"
            ready = root / "ready"
            make_fake_bot(ready, layout="src", with_autonomous_column=True)
            with patch.dict(os.environ, {"TOKEN_DISCORD": ""}), \
                    patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
                first = insights.collect(missing, "fixture")
                second = insights.collect(ready, "fixture")
                self.assertTrue(first["errors"])
                self.assertFalse(first["autonomous_supported"])
                self.assertEqual(second["errors"], [])
                self.assertTrue(second["autonomous_supported"])
                self.assertEqual(second["dm_settings"]["persona"], "ツンデレ")
                collector = insights.InsightsCollector(ready, "fixture")
                with patch("time.monotonic", return_value=10):
                    previous = collector.collect()
                collector.bot_dir = missing
                with patch("time.monotonic", return_value=100):
                    current = collector.collect()
                self.assertEqual(collector.deadline, 100 + insights.INSIGHTS_TIMEOUT_SECONDS)
                self.assertEqual(previous["errors"], [])
                self.assertTrue(current["errors"])
                self.assertEqual(previous["source_dir"], "src")
                self.assertIsNone(current["source_dir"])
                self.assertFalse(current["autonomous_supported"])
                self.assertIsNone(current["dm_settings"]["persona"])

    def test_standard_input_cli_outputs_one_ascii_json_with_existing_arguments(self):
        folder = Path(__file__).resolve().parents[1] / "remote_scripts"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            make_fake_bot(root, layout="src", with_autonomous_column=True)
            # CLIだけを通す子プロセスでも、systemctl・journalctl・Discordへは接続しない。
            prelude = """
import subprocess
subprocess.run = lambda command, **kwargs: subprocess.CompletedProcess(command, 0, '', '')
"""
            completed = run_script(prelude + (folder / "insights.py").read_text(encoding="utf-8"),
                                   ["--insights", str(root), "fixture", "--journal-sudo"],
                                   env={"TOKEN_DISCORD": ""})
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(completed.stdout.isascii())
            self.assertEqual(len(completed.stdout.splitlines()), 1)
            result = json.loads(completed.stdout)
            self.assertEqual(result["source_dir"], "src")
            self.assertEqual(result["dm_settings"]["persona"], "ツンデレ")
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["activity"]["conversations"], 0)
            self.assertFalse(result["discord"]["ok"])

    def test_table_cli_keeps_optional_argument_defaults_and_safe_errors(self):
        script = Path(db_table.__file__).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.db")
            for arguments, offset, limit in (([], 0, 50), (["9"], 0, 50), (["9", "bad"], 0, 50),
                                             (["-1", "10000"], 0, db_table.MAX_LIMIT)):
                with self.subTest(arguments=arguments):
                    completed = run_script(script, ["--db-table", missing, "feedback"] + arguments)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(len(completed.stdout.splitlines()), 1)
                    result = json.loads(completed.stdout)
                    self.assertEqual((result["offset"], result["limit"]), (offset, limit))
                    self.assertEqual(result["errors"], ["bot.db が見つかりません。"])
                    self.assertNotIn(missing, completed.stdout)


if __name__ == "__main__":
    unittest.main()
