"""一時DBを使い、並行登録でもユーザー別の上限を維持することを検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import REMINDER_MAX_PENDING_PER_USER
from persona_bot.core.database import Database
from persona_bot.services import reminder_service
from persona_bot.services.reminder_service import ReminderService
import discord
from persona_bot.utils import timeutil


class ReminderCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_dates_outside_utc_range_are_rejected_during_parsing(self):
        service = ReminderService(None, None, None)
        for text, tz in (
            ("9999-12-31T23:59:59-05:00", timezone.utc),
            ("0001-01-01T00:00:00+09:00", timezone.utc),
            ("0001-01-01T00:00:00", ZoneInfo("Asia/Tokyo")),
            ("9999-12-31 23:59:59", timezone(timedelta(hours=-5))),
        ):
            with self.subTest(text=text, tz=tz):
                service._tz = tz
                self.assertIsNone(service._parse_at(text))

    async def test_extreme_dates_fall_back_to_chat_without_registering(self):
        db = Mock()
        service = ReminderService(None, db, None)
        for text in ("9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+09:00",
                     "9999-12-31T23:59:59", "0001-01-01T00:00:00"):
            with self.subTest(text=text):
                result = await service.create_from_request(Mock(), {"at": text, "message": "架空の予定"})
                self.assertIsNone(result)
        db.reminder_add.assert_not_called()

    async def test_archived_thread_is_reopened_before_notifying_in_the_same_thread(self):
        for locked, manage, fails in ((False, False, False), (True, True, False), (True, False, False), (False, True, True)):
            with self.subTest(locked=locked, manage=manage, fails=fails):
                channel = Mock(spec=discord.Thread, archived=True, locked=locked, send=AsyncMock())
                channel.guild = SimpleNamespace(me=object())
                channel.permissions_for.return_value = SimpleNamespace(
                    view_channel=True, send_messages_in_threads=True, manage_threads=manage,
                )
                channel.edit = AsyncMock(return_value=channel)
                if fails:
                    channel.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="禁止"), "架空の再開拒否")
                calls = Mock()
                calls.attach_mock(channel.edit, "edit")
                calls.attach_mock(channel.send, "send")
                service = ReminderService(Mock(get_channel=Mock(return_value=channel)), None, Mock(
                    mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
                ))
                await service._notify({"channel_id": 10, "user_id": "11", "message": "架空の予定"})
                if locked and not manage:
                    channel.edit.assert_not_awaited()
                else:
                    channel.edit.assert_awaited_once_with(archived=False, reason="登録済みリマインダーの通知")
                if fails or (locked and not manage):
                    channel.send.assert_not_awaited()
                else:
                    channel.send.assert_awaited_once()
                    self.assertEqual([call[0] for call in calls.mock_calls], ["edit", "send"])
                channel.parent.send.assert_not_called()

    def test_z_dates_work_when_standard_parser_rejects_z_like_python_310(self):
        def parse(text):
            if text.endswith("Z"):
                raise ValueError("Python 3.10相当の解析")
            return datetime.fromisoformat(text)

        service = ReminderService(None, None, None)
        with patch.object(timeutil, "datetime") as parser:
            parser.fromisoformat.side_effect = parse
            parsed = service._parse_at("2026-10-18T09:00:00Z")
            self.assertEqual(parsed, datetime(2026, 10, 18, 9, tzinfo=timezone.utc))
            self.assertTrue(service._is_stale({"remind_at": "2026-10-18T09:00:00Z"}, parsed + timedelta(hours=1)))
            self.assertIsNotNone(reminder_service._to_local_naive("2026-10-18T09:00:00Z", timezone.utc))
            self.assertNotIn("Z", reminder_service._format_local("2026-10-18T09:00:00Z", timezone.utc))
            self.assertEqual(reminder_service.next_occurrence(
                remind_at="2026-10-18T09:00:00Z", recurrence="daily", anchor="2026-10-18T09:00:00Z",
                after=parsed + timedelta(hours=1), tz=timezone.utc,
            ), "2026-10-19T09:00:00+00:00")
        self.assertIsNone(service._parse_at("架空の不正日時"))

    async def test_notifications_check_channel_and_thread_permissions_and_preserve_dm(self):
        for kind, view, send, member in (
            ("text", False, True, True), ("text", True, False, True), ("text", True, True, False),
            ("thread", True, False, True), ("thread", False, True, True),
            ("text", True, True, True), ("thread", True, True, True), ("dm", True, True, True),
        ):
            with self.subTest(kind=kind, view=view, send=send, member=member):
                permissions = SimpleNamespace(view_channel=view, send_messages=send, send_messages_in_threads=send)
                if kind == "thread":
                    permissions.send_messages = False
                channel = Mock(spec=discord.Thread if kind == "thread" else discord.TextChannel,
                               send=AsyncMock(), archived=False, locked=False)
                channel.guild = None if kind == "dm" else SimpleNamespace(me=object() if member else None)
                channel.permissions_for.return_value = permissions
                service = ReminderService(Mock(get_channel=Mock(return_value=channel)), None, Mock(
                    mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
                ))
                await service._notify({"channel_id": 10, "user_id": "11", "message": "架空の予定"})
                if view and send and member:
                    channel.send.assert_awaited_once()
                    self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].to_dict()["users"], [11])
                else:
                    channel.send.assert_not_awaited()


class ReminderDaylightSavingTests(unittest.TestCase):
    """夏時間の切り替えをまたぐ繰り返しの次回時刻を確認する（#222）。"""

    NEW_YORK = ZoneInfo("America/New_York")
    TOKYO = ZoneInfo("Asia/Tokyo")

    def next_at(self, remind_at, kind, after, tz=None, anchor=None):
        return reminder_service.next_occurrence(
            remind_at=remind_at, recurrence=kind, anchor=anchor or remind_at,
            after=after, tz=tz or self.NEW_YORK,
        )

    def local(self, iso, tz=None):
        return datetime.fromisoformat(iso).astimezone(tz or self.NEW_YORK)

    def test_catch_up_from_second_ambiguous_hour_returns_future_for_all_kinds(self) -> None:
        # 2026-11-01 06:15 UTC は夏時間終了後の二度目の 01:15（EST）。
        after = datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc)
        for kind, previous, expected in (
            ("daily", "2026-10-31T05:30:00+00:00", "2026-11-02T06:30:00+00:00"),
            ("weekly", "2026-10-25T05:30:00+00:00", "2026-11-08T06:30:00+00:00"),
            ("monthly", "2026-10-01T05:30:00+00:00", "2026-12-01T06:30:00+00:00"),
        ):
            with self.subTest(kind=kind):
                result = self.next_at(previous, kind, after)
                self.assertEqual(result, expected)
                self.assertGreater(datetime.fromisoformat(result), after)
                self.assertEqual(self.local(result).strftime("%H:%M"), "01:30")

    def test_every_moment_around_transitions_gets_future_wall_clock_time(self) -> None:
        # 夏時間の終了日と開始日の前後を15分刻みで走査し、どの時点からでも未来の時刻を返す。
        # 存在しない 02:30 だけは 03:30 へずらし、それ以外は元の壁時計の時刻にする。
        fall_back = datetime(2026, 10, 31, 4, tzinfo=timezone.utc)
        spring_forward = datetime(2026, 3, 7, 6, tzinfo=timezone.utc)
        cases = (
            ("daily", "2026-10-30T05:30:00+00:00", fall_back, {"01:30"}),
            ("weekly", "2026-10-25T05:30:00+00:00", fall_back, {"01:30"}),
            ("monthly", "2026-10-01T05:30:00+00:00", fall_back, {"01:30"}),
            ("daily", "2026-03-06T07:30:00+00:00", spring_forward, {"02:30", "03:30"}),
            ("weekly", "2026-03-01T07:30:00+00:00", spring_forward, {"02:30", "03:30"}),
            ("monthly", "2026-02-08T07:30:00+00:00", spring_forward, {"02:30", "03:30"}),
        )
        for kind, previous, start, wall_clock in cases:
            for step in range(4 * 48):
                after = start + timedelta(minutes=15 * step)
                with self.subTest(kind=kind, after=after.isoformat()):
                    result = self.next_at(previous, kind, after)
                    self.assertGreater(datetime.fromisoformat(result), after)
                    self.assertIn(self.local(result).strftime("%H:%M"), wall_clock)

    def test_ambiguous_day_without_downtime_notifies_once_at_first_occurrence(self) -> None:
        first = self.next_at(
            "2026-10-31T05:30:00+00:00", "daily", datetime(2026, 10, 31, 5, 30, 1, tzinfo=timezone.utc),
        )
        # 曖昧な 01:30 は1回目（EDT）を使う。
        self.assertEqual(first, "2026-11-01T05:30:00+00:00")
        second = self.next_at(first, "daily", datetime(2026, 11, 1, 5, 30, 1, tzinfo=timezone.utc),
                              anchor="2026-10-31T05:30:00+00:00")
        # 同じ日の二度目の 01:30 では通知せず、翌日の 01:30（EST）へ進む。
        self.assertEqual(second, "2026-11-02T06:30:00+00:00")

    def test_nonexistent_time_is_shifted_once_and_returns_to_anchor_time(self) -> None:
        for kind, anchor, expected in (
            ("daily", "2026-03-07T07:30:00+00:00",
             ["2026-03-08T07:30:00+00:00", "2026-03-09T06:30:00+00:00", "2026-03-10T06:30:00+00:00"]),
            ("weekly", "2026-03-01T07:30:00+00:00",
             ["2026-03-08T07:30:00+00:00", "2026-03-15T06:30:00+00:00", "2026-03-22T06:30:00+00:00"]),
            ("monthly", "2026-02-08T07:30:00+00:00",
             ["2026-03-08T07:30:00+00:00", "2026-04-08T06:30:00+00:00", "2026-05-08T06:30:00+00:00"]),
        ):
            with self.subTest(kind=kind):
                current, results = anchor, []
                for _ in expected:
                    current = self.next_at(
                        current, kind, datetime.fromisoformat(current) + timedelta(seconds=1), anchor=anchor,
                    )
                    results.append(current)
                # 存在しない 02:30 は 03:30（EDT）へずらし、次の回からは 02:30 に戻す。
                self.assertEqual(results, expected)
                self.assertEqual([self.local(value).strftime("%H:%M") for value in results],
                                 ["03:30", "02:30", "02:30"])

    def test_registration_parses_boundary_times_like_the_recurrence_rule(self) -> None:
        """登録時の日時の解釈も fold=0 の規則で、繰り返しの次回時刻と食い違わない（#398）。"""
        service = ReminderService(None, None, None)
        service._tz = self.NEW_YORK
        for text, expected in (
            # 曖昧な時刻（夏時間の終わり）は1回目＝夏時間の側
            ("2026-11-01 01:30", "2026-11-01T05:30:00+00:00"),
            ("2026-11-01T01:30:00", "2026-11-01T05:30:00+00:00"),
            # 存在しない時刻（夏時間の始まり）は進める前のオフセットで解釈して後ろへずらす（03:30 EDT）
            ("2026-03-08 02:30", "2026-03-08T07:30:00+00:00"),
            ("2026-03-08T02:30:00", "2026-03-08T07:30:00+00:00"),
        ):
            with self.subTest(text=text):
                parsed = service._parse_at(text)
                self.assertEqual(parsed.isoformat(), expected)
                naive = datetime.fromisoformat(text.replace(" ", "T"))
                self.assertEqual(parsed, reminder_service._local_to_utc(naive, self.NEW_YORK))
                # 元の fold に左右されない
                self.assertEqual(
                    reminder_service._local_to_utc(naive.replace(fold=1), self.NEW_YORK), parsed,
                )
        # 時差の書かれた値はそのまま使う
        self.assertEqual(service._parse_at("2026-11-01T01:30:00-05:00").isoformat(), "2026-11-01T06:30:00+00:00")

    def test_ordinary_days_tokyo_and_month_end_keep_existing_behavior(self) -> None:
        normal = self.next_at("2026-06-10T13:00:00+00:00", "daily",
                              datetime(2026, 6, 10, 13, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(normal, "2026-06-11T13:00:00+00:00")
        tokyo = self.next_at("2026-11-01T00:00:00+00:00", "daily",
                             datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc), tz=self.TOKYO)
        self.assertEqual(tokyo, "2026-11-02T00:00:00+00:00")
        # 月末の丸めは初回のアンカーを保つ（1/31 → 2/28 → 3/31）。
        anchor = "2026-01-31T00:00:00+00:00"
        february = self.next_at(anchor, "monthly", datetime(2026, 1, 31, 0, 0, 1, tzinfo=timezone.utc),
                                tz=self.TOKYO)
        march = self.next_at(february, "monthly", datetime(2026, 2, 28, 0, 0, 1, tzinfo=timezone.utc),
                             tz=self.TOKYO, anchor=anchor)
        self.assertEqual((february, march), ("2026-02-28T00:00:00+00:00", "2026-03-31T00:00:00+00:00"))
        # 長期停止明けも1回で未来へ追い付く（秒精度・UTC の形式は変えない）。
        caught_up = self.next_at("2026-01-01T00:00:00+00:00", "weekly",
                                 datetime(2026, 6, 1, 12, 34, 56, 789, tzinfo=timezone.utc), tz=self.TOKYO)
        self.assertEqual(caught_up, "2026-06-04T00:00:00+00:00")

    def test_catch_up_limit_still_stops_the_series(self) -> None:
        with patch("builtins.print"):
            result = reminder_service.next_occurrence(
                remind_at="2026-01-01T00:00:00+00:00", recurrence="daily", anchor="",
                after=datetime(2026, 12, 1, tzinfo=timezone.utc), tz=self.NEW_YORK, max_steps=3,
            )
        self.assertIsNone(result)

    def test_repeated_claim_at_same_moment_does_not_reclaim_the_series(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Database(Path(directory.name) / "bot.db")
        db.initialize()
        self.addCleanup(db.close)
        service = ReminderService(None, db, None)
        service._tz = self.NEW_YORK
        now = datetime(2026, 11, 1, 6, 15, tzinfo=timezone.utc)
        ids = {
            kind: db.reminder_add(
                user_id="11", user_name="架空の利用者", guild_id=1, channel_id=10,
                message=f"架空の予定-{kind}", remind_at=previous, created_at="2026-09-01T00:00:00+00:00",
                recurrence=kind, recurrence_anchor=previous,
            )
            for kind, previous in (
                ("daily", "2026-10-31T05:30:00+00:00"),
                ("weekly", "2026-10-25T05:30:00+00:00"),
                ("monthly", "2026-10-01T05:30:00+00:00"),
            )
        }
        first = db.reminder_claim_due(now.isoformat(), service._build_next_at(now))
        self.assertEqual(sorted(record["reminder_id"] for record in first), sorted(ids.values()))
        self.assertEqual(db.reminder_claim_due(now.isoformat(), service._build_next_at(now)), [])
        for kind, reminder_id in ids.items():
            with self.subTest(kind=kind):
                stored = dict(db._conn.execute(
                    "SELECT status, remind_at FROM reminders WHERE reminder_id = ?", (reminder_id,)
                ).fetchone())
                self.assertEqual(stored["status"], "pending")
                self.assertGreater(datetime.fromisoformat(stored["remind_at"]), now)


class ReminderRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.db.initialize()
        self.addCleanup(self.db.close)
        self.service = ReminderService(None, self.db, None)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        clock = patch.object(reminder_service, "_utc_now", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def add_record(self, *, db=None, user_id="11", guild_id=1, recurrence="none"):
        return (db or self.db).reminder_add(
            user_id=user_id, user_name="架空の利用者", guild_id=guild_id,
            channel_id=10, message="架空の予定",
            remind_at=(self.now + timedelta(days=1)).isoformat(),
            created_at=self.now.isoformat(), recurrence=recurrence,
            recurrence_anchor=(self.now + timedelta(days=1)).isoformat() if recurrence != "none" else "",
        )

    async def register(self, *, guild_id=1, recurrence="none", user_id=11):
        message = SimpleNamespace(
            author=SimpleNamespace(id=user_id, display_name="架空の利用者"),
            guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
            channel=SimpleNamespace(id=10),
        )
        return await self.service.create_from_request(message, {
            "at": (self.now + timedelta(days=1)).isoformat(),
            "message": "架空の予定", "repeat": recurrence,
        })

    async def check_last_slot(self, *, mixed: bool) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER - 1):
            self.add_record()
        original_add = self.db.reminder_add
        barrier = threading.Barrier(2)
        inserted_ids = []

        def synchronized_add(**kwargs):
            # 実際の登録入口に両スレッドが到達してから、最後の1枠を取り合う。
            barrier.wait(timeout=5)
            reminder_id = original_add(**kwargs)
            inserted_ids.append(reminder_id)
            return reminder_id

        with patch.object(self.db, "reminder_add", side_effect=synchronized_add) as add:
            results = await asyncio.wait_for(
                asyncio.gather(
                    self.register(),
                    self.register(guild_id=None if mixed else 1, recurrence="weekly" if mixed else "none"),
                ),
                timeout=10,
            )
        self.assertEqual(add.call_count, 2)
        self.assertFalse(barrier.broken)
        self.assertEqual(len(inserted_ids), 2)
        self.assertEqual(sum(reminder_id is not None for reminder_id in inserted_ids), 1)
        self.assertEqual(sum("お知らせします" in (result or "") for result in results), 1)
        self.assertEqual(sum("1人あたり" in (result or "") for result in results), 1)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_last_slot_accepts_only_one_concurrent_request(self) -> None:
        await self.check_last_slot(mixed=False)

    async def test_guild_dm_and_recurring_requests_share_the_limit(self) -> None:
        await self.check_last_slot(mixed=True)

    async def test_many_requests_never_exceed_limit(self) -> None:
        kinds = ("none", "daily", "weekly", "monthly")
        results = await asyncio.gather(*(
            self.register(guild_id=None if i % 2 else 1, recurrence=kinds[i % len(kinds)])
            for i in range(REMINDER_MAX_PENDING_PER_USER + 12)
        ))
        self.assertEqual(sum("お知らせします" in (result or "") for result in results),
                         REMINDER_MAX_PENDING_PER_USER)
        self.assertEqual(sum("1人あたり" in (result or "") for result in results), 12)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_other_user_has_independent_slots(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        self.assertIn("1人あたり", await self.register())
        self.assertIn("お知らせします", await self.register(user_id=12))
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)
        self.assertEqual(self.db.reminder_count_pending("12"), 1)

    async def test_cancelled_and_completed_reminders_do_not_use_slots(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        record = self.db.reminder_list_pending("11")[0]
        self.db.reminder_cancel(record["reminder_id"], "11")
        self.assertIn("お知らせします", await self.register())
        self.db.reminder_claim_due((self.now + timedelta(days=2)).isoformat(), lambda record: None)
        self.assertEqual(self.db.reminder_count_pending("11"), 0)
        self.assertIn("お知らせします", await self.register(recurrence="daily"))

    async def test_database_error_does_not_return_success(self) -> None:
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "CREATE TRIGGER fail_test_reminder BEFORE INSERT ON reminders "
                "BEGIN SELECT RAISE(ABORT, 'fictional write failure'); END"
            )
        result = await self.register()
        self.assertIn("登録に失敗", result)
        self.assertNotIn("お知らせします", result)
        self.assertEqual(self.db.reminder_count_pending("11"), 0)

    async def test_database_insert_at_limit_returns_no_id(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER):
            self.add_record()
        self.assertIsNone(self.add_record())
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)

    async def test_separate_connections_cannot_claim_the_same_slot(self) -> None:
        for _ in range(REMINDER_MAX_PENDING_PER_USER - 1):
            self.add_record()
        other_db = Database(self.path)
        other_db.initialize()
        self.addCleanup(other_db.close)
        barrier = threading.Barrier(2)

        def add(db):
            barrier.wait(timeout=5)
            return self.add_record(db=db)

        results = await asyncio.wait_for(
            asyncio.gather(asyncio.to_thread(add, self.db), asyncio.to_thread(add, other_db)),
            timeout=10,
        )
        self.assertFalse(barrier.broken)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.db.reminder_count_pending("11"), REMINDER_MAX_PENDING_PER_USER)


if __name__ == "__main__":
    unittest.main()
