"""配信の接続喪失と、終了中の接続差し替えを外部通信なしで検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config import constants
from persona_bot.core.app_state import AutonomousSession, BotState
from persona_bot.services import autonomous_lifecycle, autonomous_service
from persona_bot.services import autonomous_stage
from persona_bot.services.autonomous_service import AutonomousService
from tests.helpers.autonomous_patch import patch_autonomous
from persona_bot.core.background import spawn
from persona_bot.core import bot_client
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.commands import register_commands


class AutonomousSessionCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.autonomous_enabled = True
        self.settings.autonomous_last_activity = self.now - timedelta(hours=2)
        self.settings.autonomous_recent_activity = [self.now]
        self.session = self.make_session()
        self.settings.autonomous_session = self.session
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel = Mock(spec=discord.StageChannel, id=10, name="架空のステージ")
        self.channel.instance = self.instance
        self.voice = self.make_voice(self.channel)
        self.guild = SimpleNamespace(id=1, voice_client=self.voice)
        self.client = SimpleNamespace(
            get_guild=Mock(return_value=self.guild),
            get_channel=Mock(return_value=self.channel),
        )
        self.service = AutonomousService(self.client, self.state, None, None, None, None)
        self.notice = AsyncMock()
        patch_autonomous(self, "send_long_message", self.notice)
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        patch_autonomous(self, "spawn", Mock(side_effect=start))

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def make_session(self) -> AutonomousSession:
        session = AutonomousSession(
            channel_id=10, is_stage=True, started_at=self.now,
            last_human_at=self.now, last_spoke_at=self.now,
            stage_started_by_bot=True,
        )
        # 修正前のクラスでも、所有権を示す同じ架空の条件を渡す。
        session.stage_instance_id = 101
        return session

    @staticmethod
    def make_voice(channel) -> Mock:
        voice = Mock(spec=discord.VoiceClient)
        voice.channel = channel
        voice.is_connected.return_value = False
        voice.disconnect = AsyncMock()
        return voice

    def assert_cleared(self) -> None:
        self.assertIsNone(self.settings.autonomous_session)
        self.assertGreater(self.settings.autonomous_last_activity, self.now)
        self.assertEqual(self.settings.autonomous_recent_activity, [])

    def register_voice_commands(self):
        """実サービスを登録したコマンドを、架空のDiscord境界で呼び出す。"""
        commands = {}

        def command(**kwargs):
            def register(callback):
                commands[kwargs["name"]] = callback
                return callback
            return register

        register_commands(
            SimpleNamespace(command=command), self.state, None, None,
            SimpleNamespace(is_blocked=Mock(return_value=False)),
            None, None, None, None, self.service, voice_guard=Mock(),
        )
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True, mute_members=True,
        )
        self.guild.me = SimpleNamespace(
            voice=SimpleNamespace(channel=self.channel), edit=AsyncMock(),
        )
        return commands

    async def overlapping_cleanup(self, phase, action, *, cancel=False, replace=False):
        """枠削除・切断の各境界で、後続操作が共通の回収完了を待つか確認する。"""
        self.settings.autonomous_session = self.make_session()
        self.guild.voice_client = self.voice
        self.voice.is_connected.return_value = True
        entered, release, following = (asyncio.Event() for _ in range(3))
        self.instance.delete.reset_mock(side_effect=True)
        self.voice.disconnect.reset_mock(side_effect=True)
        replacement = self.make_voice(self.channel)
        replacement.is_connected.return_value = True

        async def delete(**kwargs):
            if phase == "stage":
                entered.set()
                await release.wait()

        async def disconnect(**kwargs):
            if phase == "disconnect":
                entered.set()
                await release.wait()
            self.voice.is_connected.return_value = False
            if self.guild.voice_client is self.voice:
                self.guild.voice_client = None

        async def connect(**kwargs):
            self.guild.voice_client = replacement
            return replacement

        self.instance.delete.side_effect = delete
        self.voice.disconnect.side_effect = disconnect
        self.channel.connect = AsyncMock(side_effect=connect)
        commands = self.register_voice_commands()
        interaction = SimpleNamespace(
            guild=self.guild, command=SimpleNamespace(name=action),
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        stop = self.service.stop_session

        async def following_stop(*args, **kwargs):
            following.set()
            return await stop(*args, **kwargs)

        leader = asyncio.create_task(stop(1, reason="架空の先行停止"))
        follower = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assert_cleared()
            if replace:
                self.guild.voice_client = replacement
            with patch.object(self.service, "stop_session", side_effect=following_stop):
                follower = asyncio.create_task(
                    self.service.stop_session(1, reason="架空の後続停止")
                    if action == "stop" else commands[action](interaction)
                )
                await asyncio.wait_for(following.wait(), 2)
                self.assertFalse(leader.done())
                self.assertFalse(follower.done())
                interaction.followup.send.assert_not_awaited()
                self.channel.connect.assert_not_awaited()
                if cancel:
                    for task in (leader, follower):
                        for _ in range(3):
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                            self.assertIn(1, self.service._stopping)
                release.set()
                results = await asyncio.wait_for(asyncio.gather(leader, follower, return_exceptions=True), 2)
            if cancel:
                self.assertTrue(all(isinstance(result, asyncio.CancelledError) for result in results))
            else:
                self.assertIs(results[0], True)
                if action == "stop":
                    self.assertIs(results[1], True)
                else:
                    self.assertIsNone(results[1])
                    interaction.followup.send.assert_awaited_once()
                    reply = interaction.followup.send.call_args.args[0]
                    if action == "join":
                        self.assertIs(self.guild.voice_client, replacement)
                        self.assertTrue(replacement.is_connected())
                        if replace:
                            self.channel.connect.assert_not_awaited()
                            self.assertIn("既に接続しています", reply)
                        else:
                            self.channel.connect.assert_awaited_once()
                            self.assertIn("に接続しました", reply)
                    else:
                        self.assertIn("退出しました", reply)
            self.instance.delete.assert_awaited_once()
            if replace and phase == "stage":
                self.voice.disconnect.assert_not_awaited()
            else:
                self.voice.disconnect.assert_awaited_once_with(force=True)
            replacement.disconnect.assert_not_awaited()
            self.assertEqual(self.service._stopping, {})
            self.assertFalse(await self.service.stop_session(1, reason="架空の回収後確認"))
        finally:
            release.set()
            await asyncio.gather(*[task for task in (leader, follower) if task is not None], return_exceptions=True)

    async def test_overlapping_stops_wait_for_stage_delete_and_disconnect(self):
        for phase in ("stage", "disconnect"):
            with self.subTest(phase=phase):
                await self.overlapping_cleanup(phase, "stop")

    async def test_same_channel_join_waits_for_stage_delete_and_disconnect(self):
        for phase in ("stage", "disconnect"):
            with self.subTest(phase=phase):
                await self.overlapping_cleanup(phase, "join")

    async def test_leave_waits_for_stage_delete_and_disconnect(self):
        for phase in ("stage", "disconnect"):
            with self.subTest(phase=phase):
                await self.overlapping_cleanup(phase, "leave")

    async def test_both_stop_waiters_resist_repeated_cancellation_until_cleanup(self):
        for phase in ("stage", "disconnect"):
            with self.subTest(phase=phase):
                await self.overlapping_cleanup(phase, "stop", cancel=True)

    async def test_join_and_leave_cancellation_waits_for_shared_cleanup(self):
        for phase in ("stage", "disconnect"):
            for action in ("join", "leave"):
                with self.subTest(phase=phase, action=action):
                    await self.overlapping_cleanup(phase, action, cancel=True)

    async def test_overlapping_join_keeps_replacement_connection_during_cleanup(self):
        for phase in ("stage", "disconnect"):
            with self.subTest(phase=phase):
                await self.overlapping_cleanup(phase, "join", replace=True)

    async def test_stop_reentry_from_notice_and_cleanup_does_not_wait_for_itself(self):
        async def nested_stop(*args, **kwargs):
            self.assertTrue(await self.service.stop_session(1, reason="架空の同一処理からの停止"))

        self.notice.side_effect = nested_stop
        self.instance.delete.side_effect = nested_stop
        self.assertTrue(await asyncio.wait_for(
            self.service.stop_session(1, reason="架空の停止", notice="架空の終了案内"), 2,
        ))
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.service._stopping, {})

    async def test_following_stop_waits_even_before_notice_has_finished(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def notice(*args, **kwargs):
            entered.set()
            await release.wait()

        self.notice.side_effect = notice
        leader = asyncio.create_task(self.service.stop_session(
            1, reason="架空の先行停止", notice="架空の終了案内",
        ))
        follower = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assert_cleared()
            follower = asyncio.create_task(self.service.stop_session(1, reason="架空の後続停止"))
            await asyncio.sleep(0)
            self.assertFalse(follower.done())
            self.instance.delete.assert_not_awaited()
            self.voice.disconnect.assert_not_awaited()
            release.set()
            self.assertEqual(await asyncio.wait_for(asyncio.gather(leader, follower), 2), [True, True])
        finally:
            release.set()
            await asyncio.gather(*[task for task in (leader, follower) if task is not None], return_exceptions=True)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.service._stopping, {})

    async def test_shared_cleanup_failure_and_interruption_release_stop_record(self):
        for error in (RuntimeError("架空の回収失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.settings.autonomous_session = self.make_session()
                entered, release = asyncio.Event(), asyncio.Event()

                async def fail(*args):
                    entered.set()
                    await release.wait()
                    raise error

                with patch.object(autonomous_stage, "finish_session_cleanup", side_effect=fail):
                    leader = asyncio.create_task(self.service.stop_session(1, reason="架空の先行停止"))
                    follower = None
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        follower = asyncio.create_task(self.service.stop_session(1, reason="架空の後続停止"))
                        await asyncio.sleep(0)
                        self.assertFalse(follower.done())
                        release.set()
                        results = await asyncio.wait_for(asyncio.gather(leader, follower, return_exceptions=True), 2)
                        self.assertTrue(all(isinstance(result, type(error)) for result in results))
                    finally:
                        release.set()
                        await asyncio.gather(*[task for task in (leader, follower) if task is not None], return_exceptions=True)
                self.assertEqual(self.service._stopping, {})
                self.assertFalse(await self.service.stop_session(1, reason="架空の失敗後確認"))

    async def test_stop_all_cleans_remaining_guilds_before_propagating_cancellation(self):
        self.state.get_guild(2).autonomous_session = self.make_session()
        with patch.object(self.service, "stop_session", new=AsyncMock(side_effect=[asyncio.CancelledError(), True])) as stop:
            with self.assertRaises(asyncio.CancelledError):
                await self.service.stop_all_sessions()
        self.assertEqual([call.args[0] for call in stop.await_args_list], [1, 2])

    async def test_connection_loss_closes_owned_stage_and_forces_disconnect(self) -> None:
        await self.service._tend_session(self.guild, self.settings)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.notice.assert_not_awaited()
        self.assert_cleared()

    async def test_external_move_closes_the_session_with_the_moved_notice(self) -> None:
        # 接続は生きたまま、管理者に別のチャンネルへ移された（#171）
        self.voice.is_connected.return_value = True
        self.voice.channel = Mock(spec=discord.VoiceChannel, id=20)
        self.assertTrue(self.service.manages_connection(1))
        for name, close in {
            "移動の直後（voice_guard から）": lambda: self.service.handle_external_move(self.guild),
            "毎分の見回り（取りこぼしの受け皿）": lambda: self.service._tend_session(self.guild, self.settings),
        }.items():
            with self.subTest(case=name):
                self.settings.autonomous_session = self.make_session()
                self.voice.disconnect.reset_mock()
                self.notice.reset_mock()
                await close()
                self.notice.assert_awaited_once()
                self.assertEqual(self.notice.call_args.args[1], constants.AUTONOMOUS_CLOSING_NOTICE_MOVED)
                self.voice.disconnect.assert_awaited_once_with(force=True)
                self.assert_cleared()
        self.assertFalse(self.service.manages_connection(1))

    async def test_external_move_check_leaves_unmoved_and_starting_sessions_alone(self) -> None:
        self.voice.is_connected.return_value = True
        self.service.stop_session = AsyncMock(return_value=True)
        # 配信のチャンネルに居るまま（移動ではない）
        await self.service.handle_external_move(self.guild)
        # 開始待機中は、開始処理自身の照合（再試行の待ち時間つき）に任せる
        self.voice.channel = Mock(spec=discord.VoiceChannel, id=20)
        self.settings.autonomous_session = None
        self.service._starting[1] = autonomous_service._StartAttempt()
        self.assertTrue(self.service.manages_connection(1))
        await self.service.handle_external_move(self.guild)
        self.service.stop_session.assert_not_awaited()

        self.service._starting.clear()
        self.assertFalse(self.service.manages_connection(1))
        # 設定の無いサーバーを問い合わせても、設定を作らない
        self.assertFalse(self.service.manages_connection(2))
        self.assertNotIn(2, self.state.guild_settings)

    async def test_shutdown_closes_owned_stage_without_sending_notice(self) -> None:
        await self.service.stop_all_sessions()
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.notice.assert_not_awaited()
        self.assert_cleared()

    async def test_client_close_waits_for_the_owned_stage_before_closing_resources(self) -> None:
        order = []
        db = SimpleNamespace(close=Mock(side_effect=lambda: order.append("db")))
        client = PersonaClient(genai_client=object(), database=db, intents=discord.Intents.none())
        client.add_shutdown_callback(self.service.stop_all_sessions)

        async def delete(**kwargs):
            self.assertTrue(client.background_tasks.stopping)
            db.close.assert_not_called()
            await asyncio.sleep(0)
            order.append("stage")

        self.instance.delete.side_effect = delete
        self.voice.disconnect.side_effect = lambda **kwargs: order.append("voice")
        with patch.object(discord.Client, "close", AsyncMock(side_effect=lambda: order.append("discord"))), \
                patch.object(bot_client, "close_genai_client", AsyncMock(side_effect=lambda *_: order.append("gemini"))):
            await asyncio.gather(client.close(), client.close())
        self.assertEqual(order, ["stage", "voice", "discord", "gemini", "db"])
        self.instance.delete.assert_awaited_once()
        self.assert_cleared()

    async def test_shutdown_continues_with_other_guild_after_failure(self) -> None:
        self.state.get_guild(2)
        self.service.stop_session = AsyncMock(side_effect=[OSError("架空の終了失敗"), True])
        await self.service.stop_all_sessions()
        self.assertEqual([call.args[0] for call in self.service.stop_session.await_args_list], [1, 2])

    async def test_reconnect_during_stage_cleanup_is_also_disconnected(self) -> None:
        async def reconnected(**kwargs):
            self.voice.is_connected.return_value = True

        self.instance.delete.side_effect = reconnected
        await self.service._tend_session(self.guild, self.settings)
        self.assertTrue(self.voice.is_connected())
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_kicked_bot_without_voice_client_still_closes_owned_stage(self) -> None:
        self.guild.voice_client = None
        await self.service._tend_session(self.guild, self.settings)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_not_awaited()
        self.assert_cleared()

    async def test_voice_session_without_stage_is_disconnected(self) -> None:
        self.session.is_stage = False
        self.session.stage_started_by_bot = False
        self.session.stage_instance_id = None
        self.channel = Mock(spec=discord.VoiceChannel, id=10)
        self.client.get_channel.return_value = self.channel
        await self.service._tend_session(self.guild, self.settings)
        self.instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_borrowed_stage_is_not_deleted(self) -> None:
        self.session.stage_started_by_bot = False
        self.session.stage_instance_id = None
        await self.service._tend_session(self.guild, self.settings)
        self.instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_replacement_stage_with_another_id_is_not_deleted(self) -> None:
        self.channel.instance = SimpleNamespace(id=202, delete=AsyncMock())
        await self.service._tend_session(self.guild, self.settings)
        self.channel.instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_stage_delete_failure_does_not_skip_disconnect(self) -> None:
        self.instance.delete.side_effect = RuntimeError("架空のステージ終了失敗")
        await self.service._tend_session(self.guild, self.settings)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_disconnect_failure_does_not_restore_finished_session(self) -> None:
        self.voice.disconnect.side_effect = RuntimeError("架空の切断失敗")
        await self.service._tend_session(self.guild, self.settings)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_missing_channel_cache_still_disconnects(self) -> None:
        self.client.get_channel.return_value = None
        await self.service._tend_session(self.guild, self.settings)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_missing_stage_cache_fetches_owned_instance(self) -> None:
        self.channel.instance = None
        self.channel.fetch_instance = AsyncMock(return_value=self.instance)
        await self.service._tend_session(self.guild, self.settings)
        self.channel.fetch_instance.assert_awaited_once()
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_stage_fetch_failure_still_disconnects(self) -> None:
        self.channel.instance = None
        self.channel.fetch_instance = AsyncMock(side_effect=RuntimeError("架空の取得失敗"))
        await self.service._tend_session(self.guild, self.settings)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_new_session_during_stage_fetch_is_untouched(self) -> None:
        replacement = self.make_session()
        self.channel.instance = None

        async def replaced():
            self.settings.autonomous_session = replacement
            return self.instance

        self.channel.fetch_instance = AsyncMock(side_effect=replaced)
        await self.service.stop_session(1, reason="架空の停止")
        self.assertIs(self.settings.autonomous_session, replacement)
        self.instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()

    async def test_stop_logs_the_fixed_reason_phrase(self) -> None:
        """終了理由（固定の定型句）は運用ログへ出し、呼び出し側の引数が黙って捨てられないようにする（#469）。"""
        with self.assertLogs("persona_bot.services.autonomous_service", level="INFO") as captured:
            await self.service.stop_session(1, reason="架空の定型理由")
        self.assertTrue(any("配信を終了しました（理由: 架空の定型理由）" in line for line in captured.output))

    async def test_stage_without_recorded_ownership_id_is_not_deleted(self) -> None:
        self.session.stage_instance_id = None
        await self.service.stop_session(1, reason="架空の停止")
        self.instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_manual_connection_without_session_is_untouched(self) -> None:
        self.settings.autonomous_session = None
        await self.service._tend_session(self.guild, self.settings)
        self.assertFalse(await self.service.stop_session(1, reason="架空の停止"))
        self.voice.disconnect.assert_not_awaited()
        self.instance.delete.assert_not_awaited()

    async def test_manual_replacement_during_cleanup_is_not_disconnected(self) -> None:
        replacement = self.make_voice(self.channel)

        async def manual_join(**kwargs):
            self.guild.voice_client = replacement

        self.instance.delete.side_effect = manual_join
        await self.service.stop_session(1, reason="架空の停止")
        replacement.disconnect.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()
        self.assert_cleared()

    async def test_late_manual_connection_after_kick_is_not_disconnected(self) -> None:
        replacement = self.make_voice(self.channel)
        self.guild.voice_client = None

        async def manual_join(**kwargs):
            self.guild.voice_client = replacement

        self.instance.delete.side_effect = manual_join
        await self.service.stop_session(1, reason="架空の停止")
        replacement.disconnect.assert_not_awaited()
        self.assert_cleared()

    async def test_new_session_during_cleanup_keeps_its_connection_and_state(self) -> None:
        replacement = self.make_session()

        async def replaced(**kwargs):
            self.settings.autonomous_session = replacement
            self.settings.autonomous_last_activity = self.now
            self.settings.autonomous_recent_activity = [self.now]

        self.instance.delete.side_effect = replaced
        await self.service.stop_session(1, reason="架空の停止")
        self.assertIs(self.settings.autonomous_session, replacement)
        self.assertEqual(self.settings.autonomous_last_activity, self.now)
        self.assertEqual(self.settings.autonomous_recent_activity, [self.now])
        self.voice.disconnect.assert_not_awaited()

    async def test_notice_wait_does_not_delete_or_disconnect_new_session(self) -> None:
        replacement = self.make_session()
        replacement.stage_instance_id = 202
        replacement_instance = SimpleNamespace(id=202, delete=AsyncMock())
        replacement_voice = self.make_voice(self.channel)

        async def replaced(*args, **kwargs):
            self.settings.autonomous_session = replacement
            self.channel.instance = replacement_instance
            self.guild.voice_client = replacement_voice

        self.notice.side_effect = replaced
        await self.service.stop_session(1, reason="架空の停止", notice="架空の終了案内")
        self.instance.delete.assert_not_awaited()
        replacement_instance.delete.assert_not_awaited()
        replacement_voice.disconnect.assert_not_awaited()
        self.assertIs(self.settings.autonomous_session, replacement)

    async def test_cancellation_during_stage_cleanup_still_attempts_disconnect(self) -> None:
        self.instance.delete.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.stop_session(1, reason="架空の停止")
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_notice_cancellation_closes_stage_before_reraising(self) -> None:
        entered = asyncio.Event()

        async def notice(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.notice.side_effect = notice
        task = spawn(
            self.service.stop_session(1, reason="架空の停止", notice="架空の終了案内"),
            label="終了案内のキャンセル確認",
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_repeated_cancellation_waits_for_stage_and_voice_cleanup(self) -> None:
        notice_entered, stage_entered, voice_entered = (asyncio.Event() for _ in range(3))
        release_stage, release_voice = asyncio.Event(), asyncio.Event()

        async def notice(*args, **kwargs):
            notice_entered.set()
            await asyncio.Event().wait()

        async def stage(**kwargs):
            stage_entered.set()
            await release_stage.wait()

        async def disconnect(**kwargs):
            voice_entered.set()
            await release_voice.wait()

        self.notice.side_effect = notice
        self.instance.delete.side_effect = stage
        self.voice.disconnect.side_effect = disconnect
        task = spawn(
            self.service.stop_session(1, reason="架空の停止", notice="架空の終了案内"),
            label="繰り返しの終了キャンセル確認",
        )
        try:
            await asyncio.wait_for(notice_entered.wait(), 2)
            task.cancel()
            await asyncio.wait_for(stage_entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            release_stage.set()
            await asyncio.wait_for(voice_entered.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            release_voice.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release_stage.set()
            release_voice.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assert_cleared()

    async def test_started_stage_records_created_instance_id_before_cache_update(self) -> None:
        self.settings.autonomous_session = None
        self.guild.voice_client = None
        self.channel.instance = None
        self.channel.create_instance = AsyncMock(return_value=self.instance)
        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect = AsyncMock(side_effect=connect)
        self.voice.is_connected.return_value = True
        self.guild.me = SimpleNamespace(
            voice=SimpleNamespace(channel=self.channel), edit=AsyncMock(),
        )
        self.service._speak_opening = AsyncMock()
        with (patch.object(autonomous_stage, "pick_channel", return_value=self.channel),
              patch.object(autonomous_service, "is_quiet_hours", return_value=False),
              patch.object(autonomous_lifecycle, "is_quiet_hours", return_value=False)):
            await self.service._start_session(self.guild, self.settings)
        session = self.settings.autonomous_session
        self.assertIsNotNone(session)
        self.assertTrue(session.stage_started_by_bot)
        self.assertEqual(getattr(session, "stage_instance_id", None), self.instance.id)

    async def test_failed_stage_start_disconnects_and_waits_without_resetting_gauge(self) -> None:
        self.settings.autonomous_session = None
        self.channel.connect = AsyncMock(return_value=self.voice)
        self.voice.is_connected.return_value = True
        self.channel.instance = None
        self.channel.create_instance = AsyncMock(return_value=self.instance)
        self.guild.me = SimpleNamespace(
            voice=SimpleNamespace(channel=self.channel),
            edit=AsyncMock(side_effect=RuntimeError("架空の発言権限エラー")),
        )
        last_activity = self.settings.autonomous_last_activity
        before = datetime.now(timezone.utc)
        with (patch.object(autonomous_stage, "pick_channel", return_value=self.channel),
              patch.object(autonomous_service, "is_quiet_hours", return_value=False),
              patch.object(autonomous_lifecycle, "is_quiet_hours", return_value=False)):
            await self.service._start_session(self.guild, self.settings)
        self.assertIsNone(self.settings.autonomous_session)
        self.assertEqual(self.settings.autonomous_start_failures, 1)
        self.assertGreaterEqual(self.settings.autonomous_start_retry_at, before + timedelta(minutes=30))
        self.assertEqual(self.settings.autonomous_last_activity, last_activity)
        self.assertEqual(self.settings.autonomous_recent_activity, [self.now])
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
