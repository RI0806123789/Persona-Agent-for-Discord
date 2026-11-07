"""通常会話の受付上限、分岐と中断後の枠回収を検証する（#198）。"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core import chat_admission
from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread
from persona_bot.services.agent_plan import AgentPlan
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.network_guard import block_network
from tests.helpers.persona_helpers import install_empty_persona_catalog
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class ChatAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Mock(return_value=100.0)
        for name, value in (
            ("CHAT_MAX_REQUESTS_PER_USER", 2), ("CHAT_RATE_WINDOW_SECONDS", 10.0),
            ("CHAT_MAX_CONCURRENT_PER_GUILD", 2), ("CHAT_MAX_CONCURRENT_GLOBAL", 3),
            ("CHAT_RATE_MAX_USERS", 3),
        ):
            patcher = patch.object(chat_admission, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(chat_admission.time, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admission = chat_admission.ChatAdmission()

    def test_user_window_is_shared_across_guilds_and_dm(self):
        """同じ利用者は別サーバーやDMへ移っても期間内の受付上限を共有する。"""
        for guild in (1, 2):
            self.assertIsNone(self.admission.try_acquire(11, guild))
            self.admission.release(guild)
        self.assertIsNotNone(self.admission.try_acquire(11, None))
        self.assertIsNone(self.admission.try_acquire(12, None))

    def test_window_expires_at_exact_boundary(self):
        """期間の境界で受付を再開し、見送った入力で時刻を延ばさない。"""
        for now in (100.0, 105.0):
            self.clock.return_value = now
            self.assertIsNone(self.admission.try_acquire(11, 1))
            self.admission.release(1)
        self.clock.return_value = 109.999
        self.assertIsNotNone(self.admission.try_acquire(11, 1))
        self.clock.return_value = 110.0
        self.assertIsNone(self.admission.try_acquire(11, 1))
        self.admission.release(1)
        self.assertEqual(list(self.admission._accepted[11]), [105.0, 110.0])

    def test_full_guild_does_not_consume_user_window(self):
        """満杯時に新しい利用者の受付記録を作らず、後で再試行できる。"""
        self.assertIsNone(self.admission.try_acquire(11, 1))
        self.assertIsNone(self.admission.try_acquire(12, 1))
        self.assertIsNotNone(self.admission.try_acquire(13, 1))
        self.assertNotIn(13, self.admission._accepted)
        self.admission.release(1)
        self.assertIsNone(self.admission.try_acquire(13, 1))

    def test_release_without_a_slot_preserves_other_guilds_and_dm(self):
        """未確保・二重の返却で他の枠や受付記録を減らさず、全体上限も保つ。"""
        for guild in (1, None):
            self.admission.release(guild)
        self.assertEqual(self.admission._running, 0)
        self.assertIsNone(self.admission.try_acquire(11, 1))
        self.assertIsNone(self.admission.try_acquire(12, None))
        self.admission.release(2)
        self.assertEqual(self.admission._running, 2)
        self.admission.release(1)
        self.admission.release(1)
        self.assertEqual(self.admission._running, 1)
        self.assertEqual(self.admission._running_by_guild, {None: 1})
        self.admission.release(None)
        self.admission.release(None)
        self.assertEqual(self.admission._running, 0)
        self.assertEqual(self.admission._running_by_guild, {})
        self.assertEqual(list(self.admission._accepted), [11, 12])

    def test_invalid_release_does_not_bypass_global_capacity(self):
        for user, guild in ((11, 1), (12, 1), (13, None)):
            self.assertIsNone(self.admission.try_acquire(user, guild))
        self.admission.release(2)
        # 同じ利用者の2件目を別サーバーへ送っても、全体上限は残る。
        self.assertEqual(self.admission.try_acquire(11, 2), chat_admission.CHAT_BUSY_NOTICE)
        self.assertEqual(self.admission._running, 3)

    def test_record_capacity_preserves_live_windows_and_recovers(self):
        """記録上限でも期間内の利用者を追い出さず、期限切れだけを掃除する。"""
        for user in (11, 12, 13):
            self.assertIsNone(self.admission.try_acquire(user, 1))
            self.admission.release(1)
        self.assertIsNotNone(self.admission.try_acquire(14, 1))
        self.assertIsNone(self.admission.try_acquire(11, 1))
        self.admission.release(1)
        self.assertIsNotNone(self.admission.try_acquire(11, 1))
        self.assertEqual(len(self.admission._accepted), 3)
        self.clock.return_value = 110.0
        self.assertIsNone(self.admission.try_acquire(14, 1))
        self.assertEqual(list(self.admission._accepted), [14])

    def test_expiration_uses_last_acceptance_order(self):
        """再受付した利用者を末尾へ移し、他の期限切れ記録を残さない。"""
        for user, now in ((11, 100.0), (12, 101.0), (11, 105.0)):
            self.clock.return_value = now
            self.assertIsNone(self.admission.try_acquire(user, 1))
            self.admission.release(1)
        self.clock.return_value = 111.0
        self.assertIsNone(self.admission.try_acquire(13, 1))
        self.assertEqual(list(self.admission._accepted), [11, 13])

    def test_notice_capacity_preserves_live_records_and_expires(self):
        """案内は人数上限を守り、生きた記録を追い出さず期限ちょうどで回復する。"""
        for user in (11, 12, 13):
            self.assertTrue(self.admission.try_claim_notice(user))
        for user in (11, 14):
            self.assertFalse(self.admission.try_claim_notice(user))
        self.assertEqual(list(self.admission._notified), [11, 12, 13])
        self.clock.return_value = 109.999
        self.assertFalse(self.admission.try_claim_notice(11))
        self.clock.return_value = 110.0
        self.assertTrue(self.admission.try_claim_notice(14))
        self.assertEqual(list(self.admission._notified), [14])

    def test_record_capacity_returns_busy_notice(self):
        """受付記録が満杯の新規利用者には混雑を理由とする。"""
        for user in (11, 12, 13):
            self.assertIsNone(self.admission.try_acquire(user, 1))
            self.admission.release(1)
        self.assertEqual(self.admission.try_acquire(14, 1), chat_admission.CHAT_BUSY_NOTICE)


def close_coroutine(coro, **kwargs):
    # 会話統計の背景処理は実行せず、受付と枠の寿命だけを確認する。
    coro.close()


class MessageAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        block_network(self)
        install_empty_persona_catalog(self)
        self.state = BotState()
        for name, value in (
            ("CHAT_MAX_REQUESTS_PER_USER", 2), ("CHAT_MAX_CONCURRENT_PER_GUILD", 2),
            ("CHAT_MAX_CONCURRENT_GLOBAL", 3),
        ):
            patcher = patch.object(chat_admission, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check_route_and_plan=AsyncMock(return_value=(True, "", "none", None, None)),
        )
        self.images = AsyncMock(return_value=[])
        self.handler = make_message_handler({
            "client": SimpleNamespace(user=SimpleNamespace(id=99)), "state": self.state,
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "auto_topic": SimpleNamespace(mark_activity=Mock(), watch_channel=Mock()),
            "autonomous": SimpleNamespace(
                mark_activity=Mock(), is_comment_channel=Mock(return_value=False),
                claim_comment_slot=Mock(return_value=object()), handle_comment=AsyncMock(),
                can_speak_session=Mock(return_value=False),
            ),
            "ng_filter": SimpleNamespace(
                contains_ng_word=Mock(return_value=False),
                mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
            ),
            "moderator": self.moderator, "stats": SimpleNamespace(record=AsyncMock()),
            "documents": SimpleNamespace(enqueue_from_message=AsyncMock()),
            "reminders": SimpleNamespace(create_from_request=AsyncMock(return_value="登録しました。")),
            "load_image_attachments": self.images, "spawn": close_coroutine,
            "play_tts_response": AsyncMock(), "fetch_channel_history_text": AsyncMock(return_value=""),
        }, self)
        self.handler.handle_chat_message = AsyncMock(return_value=None)
        self.handler._handle_agent_request = AsyncMock(return_value=None)
        self.handler.notify_violation = AsyncMock()
        self.tasks = []

    async def asyncTearDown(self) -> None:
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def message(self, user=11, guild=1, *, content="架空の質問です。", attachments=()):
        settings = self.state.get_guild(guild)
        settings.current_respond_mode = "all"
        return SimpleNamespace(
            author=SimpleNamespace(id=user, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=guild) if guild is not None else None,
            channel=Mock(spec=discord.TextChannel if guild is not None else discord.DMChannel, id=10),
            content=content, mentions=[], attachments=list(attachments),
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reply=AsyncMock(),
        )

    def start(self, message):
        task = asyncio.create_task(self.handler.on_message(message))
        self.tasks.append(task)
        return task

    def assert_released(self):
        self.assertEqual(self.handler._chat_admission._running, 0)
        self.assertEqual(self.handler._chat_admission._running_by_guild, {})

    async def test_rejection_notice_is_shared_across_reasons_guilds_and_dm(self):
        """連投と混雑の案内を区別し、理由・サーバー・DMをまたいで期間内1回にする。"""
        admission = self.handler._chat_admission
        clock = Mock(return_value=99.0)
        with patch.object(chat_admission.time, "monotonic", clock):
            for _ in range(2):
                self.assertIsNone(admission.try_acquire(11, 1))
                admission.release(1)
            clock.return_value = 100.0
            rate = self.message(content="サブエージェントに架空の資料を調べてほしい")
            await self.handler.on_message(rate)
            self.assertEqual(rate.reply.call_args.args[0], chat_admission.CHAT_RATE_LIMIT_NOTICE)
            self.assertEqual(rate.reply.call_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
            duplicate = self.message(guild=None)
            await self.handler.on_message(duplicate)
            duplicate.reply.assert_not_awaited()

            # 受付記録が消え、別の人で混雑した時も案内自身の期間を基準にする。
            clock.return_value = 159.0
            for user in (21, 22):
                self.assertIsNone(admission.try_acquire(user, 2))
            busy_duplicate = self.message(guild=2)
            await self.handler.on_message(busy_duplicate)
            busy_duplicate.reply.assert_not_awaited()
            clock.return_value = 160.0
            busy = self.message(guild=2)
            await self.handler.on_message(busy)
            self.assertEqual(busy.reply.call_args.args[0], chat_admission.CHAT_BUSY_NOTICE)
            self.assertEqual(busy.reply.call_args.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
            await self.handler.on_message(busy)
            busy.reply.assert_awaited_once()
            admission.release(2)
            admission.release(2)
        self.moderator.check_and_route.assert_not_awaited()
        self.moderator.check_route_and_plan.assert_not_awaited()
        self.handler.handle_chat_message.assert_not_awaited()
        self.images.assert_not_awaited()
        self.assert_released()

    async def test_notice_is_claimed_before_send_finishes(self):
        """最初の案内送信が待機中でも同時入力から二重送信しない。"""
        admission = self.handler._chat_admission
        for user in (21, 22):
            self.assertIsNone(admission.try_acquire(user, 1))
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await proceed.wait()

        first = self.message()
        first.reply.side_effect = send
        task = self.start(first)
        await asyncio.wait_for(entered.wait(), 2)
        others = [self.message() for _ in range(20)]
        await asyncio.gather(*(self.handler.on_message(message) for message in others))
        self.assertTrue(all(message.reply.await_count == 0 for message in others))
        proceed.set()
        await task
        first.reply.assert_awaited_once()
        self.moderator.check_and_route.assert_not_awaited()
        admission.release(1)
        admission.release(1)

    async def test_notice_send_failure_is_logged_and_not_retried(self):
        """Discord障害・その他の送信例外を外へ漏らさず、連投による再試行も防ぐ。"""
        for user in (21, 22):
            self.assertIsNone(self.handler._chat_admission.try_acquire(user, 1))
        for user, error in ((11, discord.DiscordException("架空の失敗")), (12, RuntimeError("架空の失敗"))):
            with self.subTest(例外=type(error).__name__):
                message = self.message(user=user)
                message.reply.side_effect = error
                with self.assertLogs("persona_bot.core.message_handler", level="ERROR") as logs:
                    await self.handler.on_message(message)
                self.assertIn(type(error).__name__, logs.records[-1].getMessage())
                self.assertNotIn("架空の失敗", LOG_FORMATTER.format(logs.records[-1]))
                self.assertIs(logs.records[-1].exc_info[1], error)
                await self.handler.on_message(message)
                message.reply.assert_awaited_once()
        self.moderator.check_and_route.assert_not_awaited()
        self.handler.handle_chat_message.assert_not_awaited()
        self.handler._chat_admission.release(1)
        self.handler._chat_admission.release(1)

    async def test_parallel_user_input_is_bounded_before_moderation(self):
        """同じ利用者の20件が入力AIや応答へ無制限に並行到達しない。"""
        entered = asyncio.Event()
        proceed = asyncio.Event()

        async def route(*args):
            if self.moderator.check_and_route.await_count == 2:
                entered.set()
            await proceed.wait()
            return True, "", "none", None

        self.moderator.check_and_route.side_effect = route
        messages = [self.message() for _ in range(20)]
        tasks = [self.start(message) for message in messages]
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.sleep(0)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(sum(task.done() for task in tasks), 18)
        self.handler.handle_chat_message.assert_not_awaited()
        proceed.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.handler.handle_chat_message.await_count, 2)
        self.assertEqual(sum(message.reply.await_count for message in messages), 1)
        await self.handler.on_message(self.message(guild=2))
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        await self.handler.on_message(self.message(user=12))
        self.assertEqual(self.moderator.check_and_route.await_count, 3)
        self.assert_released()

    async def test_guild_global_and_dm_limits_apply_until_reply_finishes(self):
        """判定後も返信終了まで枠を保持し、別サーバーとDMにも全体上限を適用する。"""
        entered = asyncio.Queue()
        proceed = asyncio.Event()

        async def reply(message, *args, **kwargs):
            entered.put_nowait(message.author.id)
            await proceed.wait()

        self.handler.handle_chat_message.side_effect = reply
        for user, guild in ((11, 1), (12, 1)):
            self.start(self.message(user, guild))
            await asyncio.wait_for(entered.get(), 2)
        rejected = self.message(13, 1, attachments=[SimpleNamespace(filename="架空.png", content_type="image/png")])
        await self.handler.on_message(rejected)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.images.assert_not_awaited()
        rejected.reply.assert_awaited_once()
        self.assertEqual(rejected.reply.call_args.args[0], chat_admission.CHAT_BUSY_NOTICE)
        self.start(self.message(13, 2))
        await asyncio.wait_for(entered.get(), 2)
        await self.handler.on_message(self.message(14, None))
        self.assertEqual(self.moderator.check_and_route.await_count, 3)
        proceed.set()
        await asyncio.gather(*self.tasks)
        self.handler.handle_chat_message.side_effect = None
        await self.handler.on_message(self.message(14, None))
        self.assertEqual(self.moderator.check_and_route.await_count, 4)
        self.assert_released()

    async def test_dm_users_share_concurrent_limit(self):
        """異なる利用者のDMにも共有する同時実行上限がある。"""
        proceed = asyncio.Event()

        async def reply(*args, **kwargs):
            await proceed.wait()

        self.handler.handle_chat_message.side_effect = reply
        tasks = [self.start(self.message(user, None)) for user in (11, 12, 13)]
        await asyncio.sleep(0)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertTrue(tasks[-1].done())
        proceed.set()
        await asyncio.gather(*tasks)
        self.assert_released()

    async def test_rejections_do_not_change_blacklist_gate_or_local_ng(self):
        """受付が満杯でも先行するブラックリスト・宛先・ローカルNGの判定を保つ。"""
        admission = self.handler._chat_admission
        for user in (21, 22):
            self.assertIsNone(admission.try_acquire(user, 1))
        self.handler.blacklist.is_blocked.return_value = True
        await self.handler.on_message(self.message())
        self.handler.auto_topic.mark_activity.assert_not_called()
        self.handler.blacklist.is_blocked.return_value = False
        message = self.message()
        self.state.get_guild(1).current_respond_mode = "mention"
        await self.handler.on_message(message)
        self.handler.ng_filter.contains_ng_word.assert_not_called()
        self.state.get_guild(1).current_respond_mode = "all"
        self.handler.ng_filter.contains_ng_word.return_value = True
        self.handler.ng_filter.find_ng_words = Mock(return_value=["架空の禁止語"])
        await self.handler.on_message(message)
        self.handler.notify_violation.assert_awaited_once()
        self.moderator.check_and_route.assert_not_awaited()
        self.assertNotIn(11, admission._accepted)
        message.reply.assert_not_awaited()
        self.assertEqual(admission._notified, {})
        admission.release(1)
        admission.release(1)

    async def test_each_early_return_releases_execution_slot(self):
        """AI拒否・未対応添付・文書・リマインダー・委任の各終了で枠を返す。"""
        cases = ("AI拒否", "未対応添付", "文書", "文書過大", "リマインダー", "委任")
        for index, case in enumerate(cases):
            with self.subTest(分岐=case):
                message = self.message(user=30 + index)
                self.moderator.check_and_route.return_value = (case != "AI拒否", "", "none", {} if case == "リマインダー" else None)
                if case in ("文書", "文書過大", "未対応添付"):
                    message.attachments = [SimpleNamespace(
                        filename="架空.exe" if case == "未対応添付" else "架空.txt",
                        content_type=None, size=10**9 if case == "文書過大" else 10,
                    )]
                if case == "委任":
                    message.content = "サブエージェントを使って架空の資料を調べて"
                    self.moderator.check_route_and_plan.return_value = (True, "", "none", None, AgentPlan(delegate=True))
                await self.handler.on_message(message)
                self.assert_released()
                self.assertIsNone(self.handler._chat_admission.try_acquire(50 + index, 1))
                self.handler._chat_admission.release(1)
        self.handler.documents.enqueue_from_message.assert_awaited_once()
        self.handler.reminders.create_from_request.assert_awaited_once()
        self.handler._handle_agent_request.assert_awaited_once()
        self.handler.handle_chat_message.assert_not_awaited()

    async def test_exceptions_from_each_stage_release_execution_slot(self):
        """入力判定・文書・登録・委任・通常返信・通知の例外でも実行枠を残さない。"""
        cases = ("入力判定", "文書", "登録", "委任", "通常返信", "通知")
        for index, case in enumerate(cases):
            with self.subTest(失敗箇所=case):
                message = self.message(user=60 + index)
                self.moderator.check_and_route.return_value = (True, "", "none", {} if case == "登録" else None)
                failing = self.handler.handle_chat_message
                if case == "入力判定":
                    failing = self.moderator.check_and_route
                elif case == "文書":
                    message.attachments = [SimpleNamespace(filename="架空.txt", content_type=None, size=10)]
                    failing = self.handler.documents.enqueue_from_message
                elif case == "登録":
                    failing = self.handler.reminders.create_from_request
                elif case == "委任":
                    message.content = "サブエージェントを使って架空の資料を調べて"
                    self.moderator.check_route_and_plan.return_value = (True, "", "none", None, AgentPlan(delegate=True))
                    failing = self.handler._handle_agent_request
                elif case == "通知":
                    message.attachments = [SimpleNamespace(filename="架空.exe", content_type=None)]
                    failing = message.reply
                failing.side_effect = RuntimeError("架空の失敗")
                with self.assertRaisesRegex(RuntimeError, "架空の失敗"):
                    await self.handler.on_message(message)
                failing.side_effect = None
                self.assert_released()

    async def test_cancellation_at_each_branch_releases_execution_slot(self):
        """文書受付・リマインダー・委任・通常応答の中断でも枠を回収する。"""
        for index, case in enumerate(("入力判定", "文書", "リマインダー", "委任", "通常応答")):
            with self.subTest(中断箇所=case):
                message = self.message(user=70 + index)
                self.moderator.check_and_route.return_value = (True, "", "none", {} if case == "リマインダー" else None)
                target = self.handler.handle_chat_message
                if case == "入力判定":
                    target = self.moderator.check_and_route
                elif case == "文書":
                    message.attachments = [SimpleNamespace(filename="架空.txt", content_type=None, size=10)]
                    target = self.handler.documents.enqueue_from_message
                elif case == "リマインダー":
                    target = self.handler.reminders.create_from_request
                elif case == "委任":
                    message.content = "サブエージェントを使って架空の資料を調べて"
                    self.moderator.check_route_and_plan.return_value = (True, "", "none", None, AgentPlan(delegate=True))
                    target = self.handler._handle_agent_request
                entered = asyncio.Event()

                async def pause(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                target.side_effect = pause
                task = self.start(message)
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(self.handler._chat_admission._running, 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                target.side_effect = None
                self.assert_released()

    async def test_full_admission_skips_agent_history_and_planning(self):
        """通常枠が満杯なら委任の判定や履歴取得にも進まない。"""
        for user in (21, 22):
            self.assertIsNone(self.handler._chat_admission.try_acquire(user, 1))
        await self.handler.on_message(self.message(content="サブエージェントを使って架空の資料を調べて"))
        self.moderator.check_and_route.assert_not_awaited()
        self.moderator.check_route_and_plan.assert_not_awaited()
        self.handler._handle_agent_request.assert_not_awaited()
        self.handler._chat_admission.release(1)
        self.handler._chat_admission.release(1)

    async def test_repeated_cancellation_waits_for_worker_before_releasing(self):
        """繰り返しキャンセルしても同期ワーカーの終了前には枠を返さない。"""
        entered = asyncio.Event()
        proceed = threading.Event()
        self.addCleanup(proceed.set)
        loop = asyncio.get_running_loop()

        def worker():
            loop.call_soon_threadsafe(entered.set)
            if not proceed.wait(5):
                raise AssertionError("架空のワーカーが再開されませんでした。")
            return True, "", "none", None

        async def route(*args):
            return await run_in_thread(worker)

        self.moderator.check_and_route.side_effect = route
        task = self.start(self.message())
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(self.handler._chat_admission._running, 1)
        proceed.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_released()
        self.moderator.check_and_route.side_effect = None
        await self.handler.on_message(self.message())
        await self.handler.on_message(self.message())
        self.assertEqual(self.moderator.check_and_route.await_count, 2)

    async def test_comment_limit_is_preserved_when_normal_slots_are_full(self):
        """通常会話が満杯でも配信コメントは専用の受付枠を使う。"""
        for user in (21, 22):
            self.assertIsNone(self.handler._chat_admission.try_acquire(user, 1))
        self.handler.autonomous.is_comment_channel.return_value = True
        await self.handler.on_message(self.message())
        self.handler.autonomous.claim_comment_slot.assert_called_once()
        self.handler.autonomous.handle_comment.assert_awaited_once()
        self.handler.autonomous.claim_comment_slot.return_value = None
        await self.handler.on_message(self.message())
        self.assertEqual(self.moderator.check_and_route.await_count, 1)
        self.assertEqual(self.handler._chat_admission._notified, {})
        self.handler._chat_admission.release(1)
        self.handler._chat_admission.release(1)
        self.assert_released()

    async def test_ended_comment_stream_must_acquire_normal_slot(self):
        """配信が判定中に終わっても満杯の通常経路へ抜けられない。"""
        for user in (21, 22):
            self.assertIsNone(self.handler._chat_admission.try_acquire(user, 1))
        self.handler.autonomous.is_comment_channel.side_effect = [True, True, False]
        rejected = self.message()
        await self.handler.on_message(rejected)
        self.moderator.check_and_route.assert_awaited_once()
        self.handler.handle_chat_message.assert_not_awaited()
        self.assertEqual(rejected.reply.call_args.args[0], chat_admission.CHAT_BUSY_NOTICE)
        self.handler._chat_admission.release(1)
        self.handler._chat_admission.release(1)
        self.handler.autonomous.is_comment_channel.side_effect = [True, True, False]
        await self.handler.on_message(self.message())
        self.handler.handle_chat_message.assert_awaited_once()
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
