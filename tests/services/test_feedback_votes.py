"""👍👎 の評価が、届いた順と残っている絵文字のとおりに保存されることを検証する。

一時 DB と実際の FeedbackService を使い、Discord のリアクションイベントは架空の
payload に置き換える。保存の確定順は threading.Event で同期処理を止めて制御する。
"""

import asyncio
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.database import Database
from persona_bot.services import feedback_service
from persona_bot.services.feedback_service import FeedbackService
from tests.helpers.discord_mocks import make_channel, make_guild, make_message

GOOD = feedback_service.FEEDBACK_REACTION_GOOD
BAD = feedback_service.FEEDBACK_REACTION_BAD
BOT_ID = 99
GUILD_ID = 1
VOTER_ID = 11


class FeedbackVoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        with patch("builtins.print"):
            self.db.initialize()
        self.addCleanup(self.db.close)
        enabled = patch.object(feedback_service, "FEEDBACK_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.service = FeedbackService(SimpleNamespace(user=SimpleNamespace(id=BOT_ID)), self.db)
        self.message_id = self.track(500)

    def track(self, message_id: int, *, model_name: str = "架空のモデル",
              prompt_file: str = "persona.txt") -> int:
        sent = make_message(message_id=message_id, channel=make_channel(channel_id=10),
                            guild=make_guild(guild_id=GUILD_ID))
        self.service.track_reply(sent, target_user_id="12", model_name=model_name, prompt_file=prompt_file)
        return message_id

    def payload(self, emoji: str, *, user_id: int = VOTER_ID, message_id: int | None = None,
                bot: bool = False) -> SimpleNamespace:
        return SimpleNamespace(message_id=message_id or self.message_id, user_id=user_id,
                               emoji=emoji, member=SimpleNamespace(bot=bot))

    async def add(self, emoji: str, **kwargs) -> None:
        await self.service.handle_reaction_add(self.payload(emoji, **kwargs))

    async def remove(self, emoji: str, **kwargs) -> None:
        payload = self.payload(emoji, **kwargs)
        payload.member = None  # 削除イベントには member が付かない
        await self.service.handle_reaction_remove(payload)

    async def clear(self, emoji: str | None = None, *, message_id: int | None = None) -> None:
        payload = SimpleNamespace(message_id=message_id or self.message_id)
        if emoji is None:
            await self.service.handle_reaction_clear(payload)
        else:
            payload.emoji = emoji
            await self.service.handle_reaction_clear_emoji(payload)

    def totals(self) -> tuple[int, int, int]:
        rows = self.db.feedback_summary(GUILD_ID)
        return (sum(row["good"] for row in rows), sum(row["bad"] for row in rows),
                sum(row["total"] for row in rows))

    def vote(self):
        return self.service._votes[self.message_id][str(VOTER_ID)]

    def block_first_write(self, name: str, *, error: Exception | None = None):
        """最初の書き込みだけを、テストが許可するまで DB のロックの手前で止める。"""
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        original = getattr(self.db, name)
        calls = []

        def blocked(*args, **kwargs):
            calls.append(kwargs.get("rating"))
            if len(calls) == 1:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("書き込みを再開できませんでした。")
                if error is not None:
                    raise error
            return original(*args, **kwargs)

        patcher = patch.object(self.db, name, side_effect=blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 待機中のスレッドを残さないよう、失敗したテストでも必ず再開させる。
        self.addCleanup(release.set)
        return entered, release, calls

    # ---- 両方押し・片方の取り消し（#155） -------------------------------------

    async def test_vote_follows_the_reactions_that_remain(self) -> None:
        cases = [
            ([("add", GOOD), ("add", BAD), ("remove", BAD)], (1, 0, 1)),
            ([("add", BAD), ("add", GOOD), ("remove", GOOD)], (0, 1, 1)),
            # 採用していない側だけを外しても、採用中の評価は残る。
            ([("add", GOOD), ("add", BAD), ("remove", GOOD)], (0, 1, 1)),
            # 両方が付いていても1票。
            ([("add", GOOD), ("add", BAD)], (0, 1, 1)),
            ([("add", GOOD), ("add", BAD), ("remove", GOOD), ("remove", BAD)], (0, 0, 0)),
            ([("add", GOOD), ("add", BAD), ("remove", BAD), ("remove", GOOD)], (0, 0, 0)),
            ([("add", GOOD)], (1, 0, 1)),
            ([("add", GOOD), ("remove", GOOD)], (0, 0, 0)),
            # 外して付け直した方が、最後に付けた評価になる。
            ([("add", GOOD), ("add", BAD), ("remove", GOOD), ("add", GOOD)], (1, 0, 1)),
        ]
        for index, (operations, expected) in enumerate(cases):
            with self.subTest(operations=operations):
                message_id = self.track(1000 + index)
                before = self.totals()
                for action, emoji in operations:
                    await getattr(self, action)(emoji, message_id=message_id)
                after = self.totals()
                self.assertEqual(tuple(a - b for a, b in zip(after, before)), expected)

    async def test_removing_the_unadopted_reaction_does_not_write(self) -> None:
        await self.add(GOOD)
        await self.add(BAD)
        with patch.object(self.db, "feedback_record", wraps=self.db.feedback_record) as record, \
                patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
            await self.remove(GOOD)
        record.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_breakdown_by_model_and_persona_is_kept(self) -> None:
        other = self.track(600, model_name="別の架空のモデル", prompt_file="other.txt")
        await self.add(GOOD)
        await self.add(BAD, message_id=other)
        await self.add(GOOD, user_id=13, message_id=other)
        rows = {(row["model_name"], row["prompt_file"]): (row["good"], row["bad"], row["total"])
                for row in self.db.feedback_summary(GUILD_ID)}
        self.assertEqual(rows, {("架空のモデル", "persona.txt"): (1, 0, 1),
                                ("別の架空のモデル", "other.txt"): (1, 1, 2)})

    async def test_excluded_events_do_not_touch_database(self) -> None:
        with patch.object(self.db, "feedback_record", wraps=self.db.feedback_record) as record, \
                patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
            await self.add(GOOD, user_id=BOT_ID)  # ボット自身の初期リアクション
            await self.add(GOOD, user_id=13, bot=True)  # 他のボット
            await self.add("🎉")  # 対象外の絵文字
            await self.add(GOOD, message_id=999)  # 追跡していないメッセージ
            await self.remove(GOOD, user_id=BOT_ID)
            await self.remove(GOOD, user_id=13)  # 追加を受け付けていない利用者
            await self.remove("🎉")
            await self.remove(GOOD, message_id=999)
        record.assert_not_called()
        delete.assert_not_called()

    async def test_known_bot_removals_do_not_change_an_accepted_vote(self) -> None:
        await self.add(GOOD)
        for source in ("member", "cache"):
            with self.subTest(source=source):
                payload = self.payload(GOOD, bot=True)
                if source == "cache":
                    payload.member = None
                    self.service._client.get_user = Mock(return_value=SimpleNamespace(bot=True))
                with patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
                    await self.service.handle_reaction_remove(payload)
                delete.assert_not_called()
                self.assertEqual(self.totals(), (1, 0, 1))
        # raw削除にmemberもキャッシュもなくても、受け付け済みの人間の取り消しは有効。
        self.service._client.get_user.return_value = None
        await self.remove(GOOD)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.assertEqual(self.db.feedback_summary(GUILD_ID), [])

    async def test_state_is_dropped_with_the_tracked_reply(self) -> None:
        await self.add(GOOD)
        with patch.object(feedback_service, "FEEDBACK_TRACKED_MESSAGES_MAX", 2):
            self.track(501)
            self.track(502)
        self.assertNotIn(self.message_id, self.service._votes)
        # 追跡を外れた返信へのイベントは従来どおり数えない（記録済みの票は残る）。
        await self.remove(GOOD)
        self.assertEqual(self.totals(), (1, 0, 1))

    # ---- 全削除・絵文字単位の一括削除（#391） ---------------------------------

    async def test_emoji_clear_adopts_opposite_votes_and_all_clear_keeps_tracking(self) -> None:
        await self.add(GOOD)
        await self.add(BAD)
        await self.add(BAD, user_id=13)
        original = self.vote()
        await self.clear(BAD)
        self.assertEqual(self.totals(), (1, 0, 1))
        self.assertEqual(self.vote().adopted()[0], 1)
        await self.clear()
        self.assertEqual(self.totals(), (0, 0, 0))
        self.assertIn(self.message_id, self.service._tracked)
        self.assertIs(self.vote(), original)
        await self.add(BAD)
        self.assertEqual(self.totals(), (0, 1, 1))
        self.assertIs(self.vote(), original)

    async def test_clear_ignores_untracked_replies_and_other_emoji(self) -> None:
        await self.add(GOOD)
        with patch.object(self.db, "feedback_record", wraps=self.db.feedback_record) as record, \
                patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
            await self.clear("🎉")
            await self.clear(message_id=999)
            await self.clear(GOOD, message_id=999)
        record.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(self.totals(), (1, 0, 1))

    async def test_unadopted_emoji_clear_does_not_write(self) -> None:
        await self.add(GOOD)
        await self.add(BAD)
        with patch.object(self.db, "feedback_record", wraps=self.db.feedback_record) as record, \
                patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
            await self.clear(GOOD)
        record.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_clear_after_restart_or_eviction_keeps_historical_votes(self) -> None:
        await self.add(GOOD)
        restarted = FeedbackService(self.service._client, self.db)
        await restarted.handle_reaction_clear(SimpleNamespace(message_id=self.message_id))
        with patch.object(feedback_service, "FEEDBACK_TRACKED_MESSAGES_MAX", 2):
            self.track(501)
            self.track(502)
        await self.clear()
        self.assertEqual(self.totals(), (1, 0, 1))

    async def test_delayed_add_cannot_revive_cleared_vote(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        clearing = asyncio.create_task(self.clear())
        await asyncio.sleep(0)
        self.assertEqual(self.vote().reactions, {})
        release.set()
        await asyncio.wait_for(asyncio.gather(first, clearing), 5)
        self.assertEqual(self.totals(), (0, 0, 0))

    async def test_later_other_voter_add_survives_bulk_clear_wait(self) -> None:
        await self.add(GOOD)
        await self.add(GOOD, user_id=13)
        entered, release, _ = self.block_first_write("feedback_delete")
        clearing = asyncio.create_task(self.clear())
        await asyncio.wait_for(entered.wait(), 2)
        # 一括削除は全員の状態を更新済み。別の評価者は、先頭の保存を待たず再追加できる。
        self.assertEqual(self.service._votes[self.message_id]["13"].reactions, {})
        await asyncio.wait_for(self.add(BAD, user_id=13), 2)
        release.set()
        await asyncio.wait_for(clearing, 5)
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_later_same_voter_add_survives_clear_of_an_inflight_vote(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        clearing = asyncio.create_task(self.clear())
        await asyncio.sleep(0)
        later = asyncio.create_task(self.add(BAD))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, clearing, later), 5)
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_later_removal_wins_while_emoji_clear_restores_opposite_vote(self) -> None:
        await self.add(GOOD)
        await self.add(BAD)
        entered, release, _ = self.block_first_write("feedback_record")
        clearing = asyncio.create_task(self.clear(BAD))
        await asyncio.wait_for(entered.wait(), 2)
        removing = asyncio.create_task(self.remove(GOOD))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(clearing, removing), 5)
        self.assertEqual(self.totals(), (0, 0, 0))

    async def test_cancelled_bulk_clear_finishes_all_voters_before_returning(self) -> None:
        await self.add(GOOD)
        await self.add(GOOD, user_id=13)
        entered, release, _ = self.block_first_write("feedback_delete")
        clearing = asyncio.create_task(self.clear())
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(2):
            clearing.cancel()
            await asyncio.sleep(0)
            self.assertFalse(clearing.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(clearing, 5)
        self.assertEqual(self.totals(), (0, 0, 0))
        self.assertTrue(all(not vote.lock.locked() for vote in self.service._votes[self.message_id].values()))

    async def test_failed_clear_save_does_not_skip_other_voters(self) -> None:
        await self.add(GOOD)
        await self.add(GOOD, user_id=13)
        original = self.db.feedback_delete
        calls = 0

        def delete(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("架空の取り消し保存失敗")
            return original(*args)

        with patch.object(self.db, "feedback_delete", side_effect=delete), \
                self.assertLogs("persona_bot.services.feedback_service", level="ERROR"):
            await self.clear()
        self.assertEqual(calls, 2)
        self.assertEqual(self.totals(), (1, 0, 1))
        self.assertLess(self.vote().saved_version, self.vote().version)
        await self.add(BAD)
        self.assertEqual(self.totals(), (0, 1, 1))

    # ---- 保存の確定順（#154） ------------------------------------------------

    async def test_delayed_add_does_not_revive_a_later_removal(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.remove(GOOD))
        await asyncio.sleep(0.05)
        # 後のイベントは、先の保存が確定するまで書き込まない。
        self.assertFalse(second.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual(self.db.feedback_summary(GUILD_ID), [])
        self.assertFalse(self.vote().lock.locked())

    async def test_delayed_good_does_not_overwrite_a_later_bad(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.add(BAD))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_waiting_events_write_only_the_latest_state(self) -> None:
        entered, release, calls = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        with patch.object(self.db, "feedback_delete", wraps=self.db.feedback_delete) as delete:
            waiting = [asyncio.create_task(self.remove(GOOD)), asyncio.create_task(self.add(BAD))]
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, *waiting), 5)
        # 取り消しの保存は、その時点の最新（👎）を書くので、途中の削除は書かない。
        delete.assert_not_called()
        self.assertEqual(calls, [1, -1])
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_failed_write_releases_lock_and_next_event_writes_latest_state(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record", error=OSError("架空の書き込み失敗"))
        with self.assertLogs("persona_bot.services.feedback_service", level="ERROR") as log:
            first = asyncio.create_task(self.add(GOOD))
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(self.remove(GOOD))
            await asyncio.sleep(0.05)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertTrue(any("評価の記録に失敗しました" in record.getMessage() for record in log.records))
        self.assertEqual(self.db.feedback_summary(GUILD_ID), [])
        self.assertFalse(self.vote().lock.locked())
        await self.add(BAD)
        self.assertEqual(self.totals(), (0, 1, 1))

    async def test_cancelled_save_waits_for_worker_and_keeps_later_event(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.add(BAD))
        await asyncio.sleep(0.05)
        first.cancel()
        await asyncio.sleep(0.05)
        # 同期処理の完了を待ってからキャンセルを伝える（run_in_thread の契約）。
        self.assertFalse(first.done())
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsNone(results[1])
        self.assertEqual(self.totals(), (0, 1, 1))
        self.assertFalse(self.vote().lock.locked())

    async def test_cancelled_waiting_event_is_rewritten_by_the_next_event(self) -> None:
        entered, release, _ = self.block_first_write("feedback_record")
        first = asyncio.create_task(self.add(GOOD))
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(self.add(BAD))
        await asyncio.sleep(0.05)
        second.cancel()
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 5)
        self.assertIsNone(results[0])
        self.assertIsInstance(results[1], asyncio.CancelledError)
        self.assertFalse(self.vote().lock.locked())
        # 取り消された保存の分は未反映のまま残る。
        self.assertEqual(self.totals(), (1, 0, 1))
        # 採用中の 👎 は変わらないイベントでも、未反映の状態があれば最新を書き直す。
        await self.remove(GOOD)
        self.assertEqual(self.totals(), (0, 1, 1))


if __name__ == "__main__":
    unittest.main()
