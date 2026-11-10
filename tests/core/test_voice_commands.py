"""参加・退出の応答待ちと見送り通知中の脱落を再現する（#206）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.core.background import TaskOwner
from persona_bot.core.voice_commands import (
    VoiceCommandCoordinator,
    join_permission_notice,
    skip_superseded_voice_operation,
)
from tests.helpers.network_guard import block_network


class JoinPermissionNoticeTests(unittest.TestCase):
    """接続を試す前の権限案内（#414。commands.join から移した判定）。"""

    def test_enough_permissions_or_unknown_bot_member_gives_no_notice(self) -> None:
        channel = Mock(spec=discord.VoiceChannel)
        channel.name = "架空のボイス"
        with patch("persona_bot.core.voice_commands.missing_voice_permissions", return_value=[]):
            self.assertIsNone(join_permission_notice(channel, SimpleNamespace()))
        # ボットのMemberを取得できないときは判定できないので、案内せず接続を試す
        with patch("persona_bot.core.voice_commands.missing_voice_permissions") as check:
            self.assertIsNone(join_permission_notice(channel, None))
        check.assert_not_called()

    def test_missing_permissions_are_listed_and_stage_adds_the_mute_hint(self) -> None:
        for channel_type, has_hint in ((discord.VoiceChannel, False), (discord.StageChannel, True)):
            with self.subTest(channel=channel_type.__name__):
                channel = Mock(spec=channel_type)
                channel.name = "架空のチャンネル"
                with patch(
                    "persona_bot.core.voice_commands.missing_voice_permissions", return_value=["接続", "発言"],
                ) as check:
                    notice = join_permission_notice(channel, SimpleNamespace())
                self.assertEqual(check.call_args.kwargs, {"stage_moderation": has_hint})
                self.assertIn("架空のチャンネル", notice)
                self.assertIn("「接続」・「発言」", notice)
                self.assertEqual("「メンバーをミュート」が必要です" in notice, has_hint)


class VoiceCommandCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        block_network(self)
        self.coordinator = VoiceCommandCoordinator()
        self.tasks = []
        self.addAsyncCleanup(self.cleanup_tasks)

    async def cleanup_tasks(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.assertEqual(self.coordinator._guilds, {})

    @staticmethod
    def interaction():
        return SimpleNamespace(
            guild=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def start(self, interaction, change, *, owner=None):
        command = self.coordinator.run(interaction, change)
        task = asyncio.create_task(owner.run(command) if owner is not None else command)
        self.tasks.append(task)
        return task

    async def hold_first_operation(self, *, owner=None):
        entered, release = asyncio.Event(), asyncio.Event()

        async def change(is_current):
            entered.set()
            await release.wait()

        task = self.start(self.interaction(), change, owner=owner)
        await asyncio.wait_for(entered.wait(), 2)
        operations = self.coordinator._guilds[1]
        return task, release, operations

    async def queue_operation(self, *, owner=None):
        deferred = asyncio.Event()
        interaction = self.interaction()
        interaction.response.defer.side_effect = deferred.set
        change = AsyncMock()
        task = self.start(interaction, change, owner=owner)
        await asyncio.wait_for(deferred.wait(), 2)
        return task, interaction, change

    def assert_released(self, operations):
        self.assertEqual(operations.tokens, [])
        self.assertIsNone(operations.latest)
        self.assertEqual(operations.pending, 0)
        self.assertFalse(operations.lock.locked())
        self.assertEqual(self.coordinator._guilds, {})

    def observe_lock_acquisition(self, operations):
        """待機を始める前に差し替え、ロックを取得した時点を観測する。"""
        acquired = asyncio.Event()
        acquire = operations.lock.acquire

        async def observe_acquire():
            await acquire()
            acquired.set()
            return True

        patcher = patch.object(operations.lock, "acquire", side_effect=observe_acquire)
        patcher.start()
        self.addCleanup(patcher.stop)
        return acquired

    async def test_newer_defer_dropout_preserves_waiting_operation(self):
        """Bのロック取得後にCの応答が失敗・中断しても、Bを実行する。"""
        for error in (OSError("架空の応答失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                first, release_first, operations = await self.hold_first_operation()
                acquired = self.observe_lock_acquisition(operations)
                waiting, interaction, change = await self.queue_operation()
                entered, release_defer = asyncio.Event(), asyncio.Event()
                newest = self.interaction()
                newest_change = AsyncMock()

                async def defer():
                    entered.set()
                    await release_defer.wait()
                    raise error

                newest.response.defer.side_effect = defer
                latest = self.start(newest, newest_change)
                await asyncio.wait_for(entered.wait(), 2)
                release_first.set()
                await asyncio.wait_for(acquired.wait(), 2)
                change.assert_not_awaited()
                self.assertFalse(waiting.done())
                # 応答待ちだけを理由に見送らず、待っている間のロックも解放する。
                interaction.followup.send.assert_not_awaited()
                self.assertFalse(operations.lock.locked())
                release_defer.set()
                with self.assertRaises(type(error)):
                    await asyncio.wait_for(latest, 2)
                await asyncio.wait_for(asyncio.gather(first, waiting), 2)
                change.assert_awaited_once()
                interaction.followup.send.assert_not_awaited()
                newest_change.assert_not_awaited()
                self.assert_released(operations)

    async def test_newer_defer_success_executes_only_newest_operation(self):
        """Cの応答待ちが解けたら、Bを接続変更せずCだけ実行する。"""
        first, release_first, operations = await self.hold_first_operation()
        acquired = self.observe_lock_acquisition(operations)
        waiting, interaction, change = await self.queue_operation()
        entered, release_defer = asyncio.Event(), asyncio.Event()
        newest = self.interaction()
        newest_change = AsyncMock()

        async def defer():
            entered.set()
            await release_defer.wait()

        newest.response.defer.side_effect = defer
        latest = self.start(newest, newest_change)
        await asyncio.wait_for(entered.wait(), 2)
        release_first.set()
        await asyncio.wait_for(acquired.wait(), 2)
        interaction.followup.send.assert_not_awaited()
        self.assertFalse(operations.lock.locked())
        release_defer.set()
        await asyncio.wait_for(asyncio.gather(first, waiting, latest), 2)
        change.assert_not_awaited()
        newest_change.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        self.assertIn("後から受け付けた", interaction.followup.send.call_args.args[0])
        self.assert_released(operations)

    async def test_queued_dropout_during_skip_notice_restores_waiting_operation(self):
        """見送り通知を遅らせ、その送信中にロック待ちのCが脱落してもBを残す。"""
        first, release_first, operations = await self.hold_first_operation()
        waiting, interaction, change = await self.queue_operation()
        latest, newest, newest_change = await self.queue_operation()
        waiting_token = operations.tokens[-2]
        entered, release_notice = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            # Bがロックを解放した直後でも、Cが実行を始める前に中断する。
            latest.cancel()
            entered.set()
            await release_notice.wait()

        interaction.followup.send.side_effect = send
        release_first.set()
        await asyncio.wait_for(entered.wait(), 2)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(latest, 2)
        self.assertIs(operations.latest, waiting_token)
        self.assertFalse(waiting.done())
        change.assert_not_awaited()
        release_notice.set()
        await asyncio.wait_for(asyncio.gather(first, waiting), 2)
        change.assert_awaited_once()
        newest_change.assert_not_awaited()
        newest.followup.send.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        self.assert_released(operations)

    async def test_skip_notice_failure_does_not_lose_restored_operation(self):
        """通知自体も失敗した場合、復帰したBの成功を通知の例外で上書きしない。"""
        first, release_first, operations = await self.hold_first_operation()
        waiting, interaction, change = await self.queue_operation()
        latest, _, newest_change = await self.queue_operation()

        async def send(*args, **kwargs):
            latest.cancel()
            await asyncio.gather(latest, return_exceptions=True)
            raise OSError("架空の通知失敗")

        interaction.followup.send.side_effect = send
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, waiting), 2)
        change.assert_awaited_once()
        newest_change.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        self.assert_released(operations)

    async def test_skip_notice_failure_is_reported_when_newer_operation_runs(self):
        """後続の操作が実行されて見送りが確定したときだけ通知の失敗を伝える。"""
        first, release_first, operations = await self.hold_first_operation()
        waiting, interaction, change = await self.queue_operation()
        latest, _, newest_change = await self.queue_operation()
        interaction.followup.send.side_effect = OSError("架空の通知失敗")

        release_first.set()
        with self.assertRaises(OSError):
            await asyncio.wait_for(waiting, 2)
        await asyncio.wait_for(asyncio.gather(first, latest), 2)
        change.assert_not_awaited()
        newest_change.assert_awaited_once()
        interaction.followup.send.assert_awaited_once()
        self.assert_released(operations)

    async def test_skipped_notice_does_not_suppress_a_later_needed_notice(self):
        """通知直前の復帰で送らなかった場合、その後に確定した見送りは通知する。"""
        first, release_first, operations = await self.hold_first_operation()
        waiting, interaction, change = await self.queue_operation()
        latest, _, newest_change = await self.queue_operation()
        next_operation = None
        next_change = AsyncMock()
        calls = 0

        async def skip_notice(current_interaction, is_current):
            nonlocal calls, next_operation
            calls += 1
            if calls == 1:
                latest.cancel()
                await asyncio.gather(latest, return_exceptions=True)
                self.assertTrue(is_current())
                sent = await skip_superseded_voice_operation(current_interaction, is_current)
                self.assertFalse(sent)
                deferred = asyncio.Event()
                newest = self.interaction()
                newest.response.defer.side_effect = deferred.set
                next_operation = self.start(newest, next_change)
                await asyncio.wait_for(deferred.wait(), 2)
                return sent
            return await skip_superseded_voice_operation(current_interaction, is_current)

        with patch("persona_bot.core.voice_commands.skip_superseded_voice_operation", side_effect=skip_notice):
            release_first.set()
            await asyncio.wait_for(asyncio.gather(first, waiting), 2)
            await asyncio.wait_for(next_operation, 2)
        self.assertEqual(calls, 2)
        interaction.followup.send.assert_awaited_once()
        change.assert_not_awaited()
        newest_change.assert_not_awaited()
        next_change.assert_awaited_once()
        self.assert_released(operations)

    async def test_all_newer_defers_must_settle_before_skip_notice(self):
        """最新のDだけが脱落しても、応答待ちのCが残る間はBを見送らない。"""
        first, release_first, operations = await self.hold_first_operation()
        acquired = self.observe_lock_acquisition(operations)
        waiting, interaction, change = await self.queue_operation()
        requests = []
        for _ in range(2):
            entered = asyncio.Event()
            pending = self.interaction()

            async def defer(entered=entered):
                entered.set()
                await asyncio.Event().wait()

            pending.response.defer.side_effect = defer
            requests.append(self.start(pending, AsyncMock()))
            await asyncio.wait_for(entered.wait(), 2)
        requests[-1].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(requests[-1], 2)
        release_first.set()
        await asyncio.wait_for(acquired.wait(), 2)
        interaction.followup.send.assert_not_awaited()
        self.assertFalse(waiting.done())
        self.assertFalse(operations.lock.locked())
        requests[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(requests[0], 2)
        await asyncio.wait_for(asyncio.gather(first, waiting), 2)
        change.assert_awaited_once()
        self.assert_released(operations)

    async def test_shutdown_cancels_defer_and_generation_wait_without_deadlock(self):
        """世代の変化待ちと応答待ちを所有者が同時に回収し、再キャンセルでも残さない。"""
        owner = TaskOwner()
        first, release_first, operations = await self.hold_first_operation(owner=owner)
        acquired = self.observe_lock_acquisition(operations)
        waiting, interaction, change = await self.queue_operation(owner=owner)
        entered = asyncio.Event()
        newest = self.interaction()

        async def defer():
            entered.set()
            await asyncio.Event().wait()

        newest.response.defer.side_effect = defer
        latest = self.start(newest, AsyncMock(), owner=owner)
        await asyncio.wait_for(entered.wait(), 2)
        release_first.set()
        await asyncio.wait_for(acquired.wait(), 2)
        await asyncio.wait_for(first, 2)
        interaction.followup.send.assert_not_awaited()
        self.assertIn(waiting, owner._tasks)
        closing = asyncio.create_task(owner.shutdown())
        self.tasks.append(closing)
        for _ in range(2):
            waiting.cancel()
            latest.cancel()
            await asyncio.sleep(0)
        await asyncio.wait_for(closing, 2)
        self.assertTrue(waiting.cancelled())
        self.assertTrue(latest.cancelled())
        self.assertEqual(owner._tasks, {})
        change.assert_not_awaited()
        self.assert_released(operations)


if __name__ == "__main__":
    unittest.main()
