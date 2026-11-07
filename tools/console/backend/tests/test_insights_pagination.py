"""参加サーバーのページング・部分取得と、外部取得の時間予算を確認する。"""

import io
import json
import subprocess
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from remote_scripts import insights


def guild(index):
    # 2**53より大きい架空値を算術で作り、丸めず照合できることも確かめる。
    return {"id": str(2 ** 60 + index), "name": f"架空サーバー {index}"}


class InsightsPaginationTests(unittest.TestCase):
    def setUp(self):
        self.collector = insights.InsightsCollector(".", "fixture")
        self.collector.token = "fixture-token"
        self.collector.describe_settings = lambda row: dict(row)
        self.paths = []

    def serve(self, total, *, next_error=None):
        def get(path, token, *, deadline):
            self.paths.append(path)
            self.assertEqual(token, "fixture-token")
            if path == "/users/@me":
                return {"username": "架空ボット"}, 1
            if not path.startswith("/users/@me/guilds?"):
                return None, 1
            params = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
            self.assertEqual(params["limit"], ["200"])
            self.assertEqual(params["with_counts"], ["true"])
            after = int(params.get("after", [str(2 ** 60)])[0]) - 2 ** 60
            if after and next_error:
                raise next_error
            return [guild(index) for index in range(after + 1, min(after + 200, total) + 1)], 1
        self.collector.discord_get = get

    def test_empty_and_multiple_pages_keep_every_setting(self):
        for total in (0, 201, 400, 401):
            with self.subTest(total=total):
                self.paths.clear()
                self.serve(total)
                settings = {2 ** 60 + index: {"saved": index} for index in range(1, total + 1)}
                result = self.collector.collect_discord(settings)
                self.assertTrue(result["ok"])
                self.assertFalse(result["partial"])
                self.assertEqual([row["name"] for row in result["guilds"]],
                                 [f"架空サーバー {index}" for index in range(1, total + 1)])
                self.assertEqual([row["settings"]["saved"] for row in result["guilds"]], list(range(1, total + 1)))
                pages = [path for path in self.paths if path.startswith("/users/@me/guilds?")]
                self.assertEqual(len(pages), total // 200 + 1)
                if total:
                    self.assertNotIn(str(2 ** 60 + total), json.dumps(result))
                self.assertNotIn("fixture-token", json.dumps(result))

    def test_next_page_failure_keeps_fetched_and_unmatched_saved_settings(self):
        for error_type in ("DiscordError", "DiscordRateLimit", "DiscordDeadline"):
            with self.subTest(error_type=error_type):
                self.paths.clear()
                error = getattr(insights, error_type)("架空の取得エラー")
                self.serve(201, next_error=error)
                result = self.collector.collect_discord({2 ** 60 + 201: {"saved": "末尾の設定"}})
                self.assertFalse(result["ok"])
                self.assertTrue(result["partial"])
                self.assertEqual(len(result["guilds"]), 201)
                self.assertEqual(result["guilds"][199]["name"], "架空サーバー 200")
                self.assertEqual(result["guilds"][-1]["name"], "保存済みサーバー 1")
                self.assertEqual(result["guilds"][-1]["settings"], {"saved": "末尾の設定"})
                if error_type in ("DiscordRateLimit", "DiscordDeadline"):
                    self.assertFalse(any("voice-states" in path for path in self.paths))

    def test_first_page_failure_is_unavailable_and_keeps_saved_settings(self):
        self.collector.discord_get = Mock(side_effect=[({"username": "架空ボット"}, 1), insights.DiscordError("失敗")])
        result = self.collector.collect_discord({1: {"saved": True}, 0: {"dm": True}})
        self.assertFalse(result["ok"])
        self.assertFalse(result["partial"])
        self.assertEqual(len(result["guilds"]), 1)

    def test_page_limit_reports_partial_and_keeps_results(self):
        self.serve(401)
        self.enterContext(patch.object(insights, "DISCORD_MAX_GUILD_PAGES", 2))
        result = self.collector.collect_discord({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["guilds"]), 400)
        self.assertIn("取得上限", result["error"])

    def test_duplicate_page_stops_without_duplicate_rows(self):
        page = [guild(index) for index in range(1, 201)]
        self.collector.discord_get = Mock(side_effect=[({"username": "架空ボット"}, 1), (page, 1), (page, 1)] + [(None, 1)] * 100)
        result = self.collector.collect_discord({})
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(len(result["guilds"]), 200)
        self.assertIn("進まない", result["error"])

    def test_overlapping_pages_deduplicate_ids_and_preserve_first_setting(self):
        pages = [[guild(index) for index in range(1, 201)], [guild(200), guild(201)]]
        self.collector.discord_get = Mock(side_effect=[(page, 1) for page in pages])
        guilds, error = self.collector.collect_discord_guilds("fixture-token", deadline=time.monotonic() + 20)
        self.assertIsNone(error)
        self.assertEqual(len(guilds), 201)

    def test_invalid_id_on_later_page_keeps_prior_page(self):
        for value in (True, 1.5, "1e20", "-1", "9" * 5000):
            with self.subTest(kind=type(value).__name__):
                self.collector.discord_get = Mock(side_effect=[([guild(index) for index in range(1, 201)], 1),
                                                           ([{"id": value}], 1)])
                guilds, error = self.collector.collect_discord_guilds("fixture-token", deadline=time.monotonic() + 20)
                self.assertEqual(len(guilds), 200)
                self.assertIsInstance(error, insights.DiscordError)
                self.assertNotIn(str(value) if len(str(value)) > 10 else "fixture-token", str(error))

    def test_voice_requests_are_bounded_and_omissions_remain_unknown(self):
        self.serve(201)
        result = self.collector.collect_discord({})
        self.assertTrue(result["ok"])
        self.assertEqual(sum("voice-states" in path for path in self.paths), 100)
        self.assertIsNone(result["guilds"][99]["voice_error"])
        self.assertIn("取得上限", result["guilds"][100]["voice_error"])

    def test_expired_budget_does_not_send_requests(self):
        self.collector.deadline = time.monotonic() - 1
        with patch("urllib.request.urlopen") as urlopen:
            result = self.collector.collect_discord({})
        self.assertFalse(result["ok"])
        self.assertIn("時間の上限", result["error"])
        urlopen.assert_not_called()

    def test_request_timeout_uses_remaining_budget(self):
        with patch("time.monotonic", return_value=10), patch("urllib.request.urlopen", return_value=io.BytesIO(b"[]")) as urlopen:
            self.collector.discord_get("/users/@me", "fixture-token", deadline=10.5)
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 0.5)

    def test_rate_limit_does_not_expose_response_details(self):
        private_detail = "fixture-private-response"
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                "https://example.invalid/fixture-token", 429, private_detail, {}, io.BytesIO(private_detail.encode()))):
            with self.assertRaises(insights.DiscordRateLimit) as raised:
                self.collector.discord_get("/users/@me", "fixture-token", deadline=time.monotonic() + 1)
        self.assertIn("429", str(raised.exception))
        self.assertNotIn(private_detail, str(raised.exception))
        self.assertNotIn("fixture-token", str(raised.exception))

    def test_late_response_is_not_reported_as_complete(self):
        with patch("time.monotonic", side_effect=[10, 11]), patch("urllib.request.urlopen", return_value=io.BytesIO(b"[]")):
            with self.assertRaises(insights.DiscordDeadline):
                self.collector.discord_get("/users/@me", "fixture-token", deadline=10.5)

    def test_slow_response_body_does_not_extend_deadline(self):
        release = threading.Event()
        finished = threading.Event()

        class SlowResponse(io.BytesIO):
            def read(self):
                release.wait(2)
                return b"[]"

            def close(self):
                super().close()
                finished.set()

        response = SlowResponse()
        started = time.monotonic()
        try:
            with patch("urllib.request.urlopen", return_value=response):
                with self.assertRaises(insights.DiscordDeadline):
                    self.collector.discord_get("/users/@me", "fixture-token", deadline=started + 0.02)
            self.assertLess(time.monotonic() - started, 1)
        finally:
            release.set()
            self.assertTrue(finished.wait(1))

    def test_journal_uses_global_remaining_time_and_stops_when_expired(self):
        self.collector.deadline = 15
        with patch("time.monotonic", return_value=10), patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(self.collector.journal([]), [])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        with patch("time.monotonic", return_value=15), patch("subprocess.run") as run:
            self.assertIsNone(self.collector.journal([]))
        run.assert_not_called()
        self.assertIn("取得時間の上限", self.collector.result["errors"][-1])


if __name__ == "__main__":
    unittest.main()
