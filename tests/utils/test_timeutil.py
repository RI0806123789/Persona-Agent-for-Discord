"""日時ユーティリティ（timeutil.py）の回帰テスト。

OSのタイムゾーンDBがない環境でも日本時間を使えること、保存用の時刻の精度と形式、
ISO文字列の解釈、UTCへの正規化、日本語の曜日名を確認する。
"""

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.config.constants import LOG_DATE_FORMAT, LOG_FORMAT
from persona_bot.utils import timeutil
from persona_bot.utils.logging_setup import OperationalFormatter
from persona_bot.utils.timeutil import (
    WEEKDAY_NAMES_JA,
    as_utc,
    format_prompt_now,
    format_weekday,
    parse_iso_datetime,
    resolve_timezone,
    timezone_label,
    utc_now,
    utc_now_iso,
)

TOKYO = timezone(timedelta(hours=9))


class FixedDatetime(datetime):
    """現在時刻を固定する差し替え用。マイクロ秒つきで、切り捨てを確かめられるようにする。"""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 2, 28, 23, 59, 59, 123456, tzinfo=tz)


class UtcNowTests(unittest.TestCase):
    def test_utc_now_is_aware_utc_with_second_precision(self) -> None:
        before = datetime.now(timezone.utc).replace(microsecond=0)
        value = utc_now()
        after = datetime.now(timezone.utc).replace(microsecond=0)
        self.assertEqual(value.utcoffset(), timedelta(0))
        self.assertEqual(value.microsecond, 0)
        self.assertLessEqual(before, value)
        self.assertLessEqual(value, after)

    def test_microseconds_are_truncated_not_rounded(self) -> None:
        with patch.object(timeutil, "datetime", FixedDatetime):
            self.assertEqual(utc_now(), datetime(2026, 2, 28, 23, 59, 59, tzinfo=timezone.utc))

    def test_iso_string_defaults_to_seconds_so_string_comparison_keeps_time_order(self) -> None:
        with patch.object(timeutil, "datetime", FixedDatetime):
            self.assertEqual(utc_now_iso(), "2026-02-28T23:59:59+00:00")
            self.assertEqual(utc_now_iso(timespec="microseconds"), "2026-02-28T23:59:59.123456+00:00")
            self.assertEqual(utc_now_iso(timespec="milliseconds"), "2026-02-28T23:59:59.123+00:00")
        # 文字列の比較が時刻の順になるのは、桁数が揃っているときだけ（リマインダーの期限判定）。
        self.assertRegex(utc_now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertLess("2026-02-28T23:59:59+00:00", "2026-03-01T00:00:00+00:00")

    def test_iso_string_rejects_unknown_precision(self) -> None:
        with self.assertRaises(ValueError):
            utc_now_iso(timespec="years")

    def test_iso_string_round_trips_through_parse(self) -> None:
        with patch.object(timeutil, "datetime", FixedDatetime):
            text = utc_now_iso(timespec="microseconds")
        parsed = parse_iso_datetime(text)
        self.assertEqual(parsed, datetime(2026, 2, 28, 23, 59, 59, 123456, tzinfo=timezone.utc))
        self.assertEqual(parsed.isoformat(timespec="microseconds"), text)


class ParseIsoDatetimeTests(unittest.TestCase):
    def test_trailing_z_is_utc(self) -> None:
        for text in ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.5Z", "2026-01-01T00:00:00.123456Z"):
            with self.subTest(text=text):
                parsed = parse_iso_datetime(text)
                self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parse_iso_datetime("2026-01-01T09:30:15Z"), datetime(2026, 1, 1, 9, 30, 15, tzinfo=timezone.utc))

    def test_offsets_and_naive_values_follow_the_standard(self) -> None:
        parsed = parse_iso_datetime("2026-01-01T09:00:00+09:00")
        self.assertEqual(parsed.utcoffset(), timedelta(hours=9))
        self.assertEqual(parsed, datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
        self.assertIsNone(parse_iso_datetime("2026-01-01T09:00:00").tzinfo)
        self.assertEqual(parse_iso_datetime("2026-01-01").day, 1)

    def test_leap_day_is_valid_only_in_leap_years(self) -> None:
        self.assertEqual(parse_iso_datetime("2024-02-29T12:00:00Z").day, 29)
        self.assertEqual(parse_iso_datetime("2000-02-29T00:00:00Z").month, 2)
        for text in ("2025-02-29T00:00:00Z", "1900-02-29T00:00:00Z"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_iso_datetime(text)

    def test_invalid_text_raises_value_error(self) -> None:
        for text in ("", "Z", "not a date", "2026-13-01T00:00:00Z", "2026-01-01T25:00:00Z", "2026-01-01T00:00:00 UTC"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_iso_datetime(text)

    def test_only_an_uppercase_trailing_z_is_rewritten(self) -> None:
        # 小文字の z は標準のパーサーに任せ、独自の解釈を足さない。
        with self.assertRaises(ValueError):
            parse_iso_datetime("2026-01-01T00:00:00z")


class AsUtcTests(unittest.TestCase):
    def test_naive_value_is_treated_as_utc_without_shifting(self) -> None:
        converted = as_utc(datetime(2026, 1, 1, 12, 0))
        self.assertEqual(converted, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(converted.utcoffset(), timedelta(0))

    def test_aware_value_is_converted_to_the_same_instant(self) -> None:
        source = datetime(2026, 1, 1, 9, 0, tzinfo=TOKYO)
        converted = as_utc(source)
        self.assertEqual(converted.utcoffset(), timedelta(0))
        self.assertEqual(converted, source)
        self.assertEqual((converted.year, converted.hour), (2026, 0))

    def test_conversion_crosses_leap_day_and_year_boundaries(self) -> None:
        self.assertEqual(as_utc(datetime(2024, 3, 1, 0, 30, tzinfo=TOKYO)), datetime(2024, 2, 29, 15, 30, tzinfo=timezone.utc))
        self.assertEqual(
            as_utc(datetime(2026, 12, 31, 20, 0, tzinfo=timezone(timedelta(hours=-5)))),
            datetime(2027, 1, 1, 1, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(as_utc(datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo("Asia/Tokyo"))).day, 31)

    def test_utc_value_is_unchanged_and_input_is_not_mutated(self) -> None:
        source = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(as_utc(source), source)
        naive = datetime(2026, 1, 1, 12, 0)
        as_utc(naive)
        self.assertIsNone(naive.tzinfo)

    def test_naive_and_aware_values_become_comparable(self) -> None:
        # 旧値（タイムゾーンなし）と新しい値を、比較で例外にせず並べられる。
        values = [as_utc(datetime(2026, 1, 1, 12, 0)), as_utc(datetime(2026, 1, 1, 12, 0, tzinfo=TOKYO))]
        self.assertEqual(sorted(values)[0], datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc))


class FormatWeekdayTests(unittest.TestCase):
    def test_a_week_maps_monday_to_sunday(self) -> None:
        week = [format_weekday(datetime(2026, 11, 2) + timedelta(days=offset)) for offset in range(7)]
        self.assertEqual(week, ["月", "火", "水", "木", "金", "土", "日"])
        self.assertEqual(tuple(week), WEEKDAY_NAMES_JA)

    def test_leap_day_and_year_boundary(self) -> None:
        self.assertEqual(format_weekday(datetime(2024, 2, 29)), "木")
        self.assertEqual(format_weekday(datetime(2026, 12, 31)), "木")
        self.assertEqual(format_weekday(datetime(2027, 1, 1)), "金")

    def test_weekday_follows_the_value_own_timezone(self) -> None:
        instant = datetime(2026, 11, 6, 16, 0, tzinfo=timezone.utc)
        self.assertEqual(format_weekday(instant), "金")
        self.assertEqual(format_weekday(instant.astimezone(ZoneInfo("Asia/Tokyo"))), "土")

    def test_naive_and_aware_values_are_both_accepted(self) -> None:
        self.assertEqual(format_weekday(datetime(2026, 11, 7)), format_weekday(datetime(2026, 11, 7, tzinfo=timezone.utc)))


class ResolveTimezoneTests(unittest.TestCase):
    def test_known_name_resolves_and_is_labeled_by_name(self) -> None:
        tokyo = resolve_timezone("Asia/Tokyo", purpose="テスト")
        self.assertEqual(timezone_label(tokyo), "Asia/Tokyo")
        self.assertEqual(datetime(2026, 1, 1, tzinfo=timezone.utc).astimezone(tokyo).hour, 9)

    def test_unknown_name_falls_back_to_utc_without_logging_the_name(self) -> None:
        secret_name = "Private/架空の設定値"
        with self.assertLogs("persona_bot.utils.timeutil", level="WARNING") as logs:
            fallback = resolve_timezone(secret_name, purpose="非公開の用途名")
        self.assertIs(fallback, timezone.utc)
        self.assertEqual(timezone_label(fallback), "UTC")
        # 例外本文（設定値を含む）は、運用ログの Formatter が捨てる。本番と同じ整形で確かめる。
        formatter = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)
        emitted = "\n".join(formatter.format(record) for record in logs.records)
        self.assertIn("UTC で代用します", emitted)
        self.assertIn("ZoneInfoNotFoundError", emitted)
        self.assertNotIn(secret_name, emitted)
        self.assertNotIn("非公開の用途名", emitted)
        self.assertNotIn(secret_name, "\n".join(record.getMessage() for record in logs.records))


class TimezoneDataTests(unittest.TestCase):
    def test_tokyo_resolves_without_system_timezone_database(self) -> None:
        # 別プロセスでOSのDBとZoneInfoキャッシュを使わず、tzdataによる解決を確かめる。
        script = """
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import TZPATH
sys.path.insert(0, 'src')
from persona_bot.utils.timeutil import resolve_timezone, timezone_label
assert TZPATH == (), TZPATH
tokyo = resolve_timezone('Asia/Tokyo', purpose='日本時間の回帰テスト')
assert timezone_label(tokyo) == 'Asia/Tokyo', timezone_label(tokyo)
utc = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)
local = utc.astimezone(tokyo)
assert local.utcoffset() == timedelta(hours=9), local
assert local.isoformat() == '2026-10-19T00:00:00+09:00', local
"""
        result = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=Path(__file__).resolve().parents[2],
            env={**os.environ, "PYTHONTZPATH": "", "PYTHONIOENCODING": "utf-8"},
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("UTC で代用します", result.stdout + result.stderr)


class PromptNowTests(unittest.TestCase):
    """プロンプトへ埋める現在日時の表記（リマインダー判定と委任の子で共通。#469）。"""

    def test_prompt_now_has_weekday_and_optional_zone_label(self):
        tokyo = timezone(timedelta(hours=9))
        fixed = datetime(2026, 11, 19, 9, 30, tzinfo=tokyo)  # 木曜日
        with patch.object(timeutil, "datetime", wraps=datetime) as clock:
            clock.now.return_value = fixed
            self.assertEqual(format_prompt_now(tokyo), "2026-11-19 09:30（木曜日）")
            self.assertEqual(format_prompt_now(tokyo, include_zone=True), f"2026-11-19 09:30（木曜日、{timezone_label(tokyo)}）")


if __name__ == "__main__":
    unittest.main()
