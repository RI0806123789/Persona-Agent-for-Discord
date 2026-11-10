"""接続・ステージ準備の待機中に開始条件が変わった場合を検証する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core.app_state import BotState
from persona_bot.core.background import spawn
from persona_bot.core.commands import register_commands
from persona_bot.services import autonomous_service, autonomous_stage
from persona_bot.services.autonomous_service import AutonomousService


class AutonomousStartRaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.autonomous_enabled = True
        self.channel = Mock(spec=discord.StageChannel, id=10)
        self.channel.name = "架空のステージ"
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel.instance = None
        self.channel.create_instance = AsyncMock(return_value=self.instance)
        self.channel.fetch_instance = AsyncMock(return_value=self.instance)
        self.voice = self.make_voice(self.channel)
        self.member = SimpleNamespace(voice=SimpleNamespace(channel=self.channel), edit=AsyncMock())
        self.guild = SimpleNamespace(id=1, voice_client=None, me=self.member)
        self.client = SimpleNamespace(get_guild=Mock(return_value=self.guild), get_channel=Mock(return_value=self.channel))
        self.service = AutonomousService(self.client, self.state, None, None, None, None)
        self.opening = AsyncMock()
        self.service._speak_opening = self.opening

        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect = AsyncMock(side_effect=connect)
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        for target, name, replacement in (
            (autonomous_stage, "pick_channel", Mock(return_value=self.channel)),
            (autonomous_service, "is_quiet_hours", Mock(return_value=False)),
            (autonomous_service, "spawn", start),
        ):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks, return_exceptions=True)

    @staticmethod
    def make_voice(channel):
        return Mock(spec=discord.VoiceClient, channel=channel,
                    is_connected=Mock(return_value=True), disconnect=AsyncMock())

    async def start(self):
        await self.service._start_session(self.guild, self.settings)
        await asyncio.gather(*self.tasks)

    def assert_not_started(self):
        self.assertIsNone(self.settings.autonomous_session)
        self.opening.assert_not_awaited()

    async def test_normal_start_registers_only_after_preparation(self) -> None:
        async def edit(**kwargs):
            self.assertIsNone(self.settings.autonomous_session)

        self.member.edit.side_effect = edit
        await self.start()
        self.assertEqual(self.settings.autonomous_session.channel_id, 10)
        self.assertEqual(self.settings.autonomous_session.stage_instance_id, 101)
        self.opening.assert_awaited_once()
        self.voice.disconnect.assert_not_awaited()

    async def test_changed_conditions_during_connect_abort_before_stage_creation(self) -> None:
        for change in ("disabled", "quiet", "disconnected", "missing", "moved", "replaced"):
            with self.subTest(change=change):
                self.settings.autonomous_enabled = True
                self.settings.autonomous_start_failures = 0
                self.settings.autonomous_start_retry_at = None
                self.guild.voice_client = None
                self.voice.channel = self.channel
                self.voice.is_connected.return_value = True
                self.voice.disconnect.reset_mock()
                autonomous_service.is_quiet_hours.return_value = False
                replacement = self.make_voice(SimpleNamespace(id=20))

                async def connect(**kwargs):
                    await asyncio.sleep(0)
                    self.guild.voice_client = self.voice
                    if change == "disabled":
                        self.settings.autonomous_enabled = False
                    elif change == "quiet":
                        autonomous_service.is_quiet_hours.return_value = True
                    elif change == "disconnected":
                        self.voice.is_connected.return_value = False
                    elif change == "missing":
                        self.guild.voice_client = None
                    elif change == "moved":
                        self.voice.channel = SimpleNamespace(id=20)
                    else:
                        self.guild.voice_client = replacement
                    return self.voice

                self.channel.connect.side_effect = connect
                await self.start()
                self.assert_not_started()
                self.channel.create_instance.assert_not_awaited()
                replacement.disconnect.assert_not_awaited()
                self.assertEqual(self.settings.autonomous_start_failures,
                                 0 if change in ("disabled", "quiet") else 1)
                if change not in ("missing", "replaced"):
                    self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_stop_during_connect_revokes_unregistered_start(self) -> None:
        async def connect(**kwargs):
            self.assertIsNone(self.guild.voice_client)
            self.assertTrue(await self.service.stop_session(1, reason="架空の開始中止"))
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect
        await self.start()
        self.assert_not_started()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.settings.autonomous_start_failures, 0)

    async def test_connect_error_after_stop_is_not_counted_as_start_failure(self) -> None:
        for error in (RuntimeError("架空の接続失敗"), asyncio.TimeoutError()):
            with self.subTest(error=type(error).__name__):
                async def connect(**kwargs):
                    await self.service.stop_session(1, reason="架空の開始中止")
                    raise error

                self.channel.connect.side_effect = connect
                await self.start()
                self.assert_not_started()
                self.assertEqual(self.settings.autonomous_start_failures, 0)
                self.assertIsNone(self.settings.autonomous_start_retry_at)

    async def test_stage_error_after_stop_is_not_counted_as_start_failure(self) -> None:
        async def edit(**kwargs):
            await self.service.stop_session(1, reason="架空の準備中止")
            raise RuntimeError("架空の発言者への昇格失敗")

        self.member.edit.side_effect = edit
        await self.start()
        self.assert_not_started()
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.settings.autonomous_start_failures, 0)
        self.assertIsNone(self.settings.autonomous_start_retry_at)

    async def test_stop_or_disable_during_preparation_closes_only_owned_stage(self) -> None:
        for borrowed in (False, True):
            for change in ("stop", "disabled", "quiet", "moved", "disconnected"):
                with self.subTest(borrowed=borrowed, change=change):
                    self.settings.autonomous_enabled = True
                    autonomous_service.is_quiet_hours.return_value = False
                    self.voice.channel = self.channel
                    self.voice.is_connected.return_value = True
                    self.channel.instance = self.instance if borrowed else None
                    self.voice.disconnect.reset_mock()
                    self.instance.delete.reset_mock()

                    async def edit(**kwargs):
                        if change == "stop":
                            self.assertTrue(await self.service.stop_session(1, reason="架空の準備中止"))
                        elif change == "disabled":
                            self.settings.autonomous_enabled = False
                        elif change == "quiet":
                            autonomous_service.is_quiet_hours.return_value = True
                        elif change == "moved":
                            self.voice.channel = SimpleNamespace(id=20)
                        else:
                            self.voice.is_connected.return_value = False
                        await asyncio.sleep(0)

                    self.member.edit.side_effect = edit
                    await self.start()
                    self.assert_not_started()
                    self.assertEqual(self.instance.delete.await_count, 0 if borrowed else 1)
                    self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_replaced_connection_during_preparation_is_not_disconnected(self) -> None:
        replacement = self.make_voice(SimpleNamespace(id=20))

        async def edit(**kwargs):
            self.guild.voice_client = replacement

        self.member.edit.side_effect = edit
        await self.start()
        self.assert_not_started()
        self.instance.delete.assert_awaited_once()
        replacement.disconnect.assert_not_awaited()

    async def test_overlapping_starts_do_not_connect_twice(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(**kwargs):
            entered.set()
            await release.wait()
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect
        task = spawn(self.start(), label="開始競合の検証")
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = spawn(self.service._start_session(self.guild, self.settings), label="重複開始の検証")
            await asyncio.sleep(0)
            self.channel.connect.assert_awaited_once()
        finally:
            release.set()
            await task
            if second is not None:
                await second

    async def test_leave_while_connection_is_pending_stops_the_attempt(self) -> None:
        commands = self.make_commands()
        interaction = SimpleNamespace(
            guild=self.guild, user=SimpleNamespace(id=11), response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def connect(**kwargs):
            await commands["leave"](interaction)
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect
        await self.start()
        self.assert_not_started()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    def make_commands(self):
        commands = {}

        def command(**kwargs):
            def register(callback):
                commands[kwargs["name"]] = callback
                return callback
            return register

        register_commands(SimpleNamespace(command=command), self.state, None, None,
                          SimpleNamespace(is_blocked=Mock(return_value=False)),
                          None, None, None, None, self.service)
        return commands

    async def test_join_during_start_keeps_the_human_requested_connection(self) -> None:
        commands = self.make_commands()
        target = Mock(spec=discord.VoiceChannel, id=20)
        target.name = "架空の通話チャンネル"
        target.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        replacement = self.make_voice(target)

        async def join_connect(**kwargs):
            # /join は接続の待ち時間（timeout）を指定して connect する
            self.guild.voice_client = replacement
            return replacement

        target.connect = AsyncMock(side_effect=join_connect)
        interaction = SimpleNamespace(
            guild=self.guild,
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=target)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def connect(**kwargs):
            await commands["join"](interaction)
            return self.voice

        self.channel.connect.side_effect = connect
        await self.start()
        self.assert_not_started()
        self.assertIs(self.guild.voice_client, replacement)
        replacement.disconnect.assert_not_awaited()
        target.connect.assert_awaited_once()

    async def test_same_channel_join_waits_for_start_and_owned_stage_cleanup(self) -> None:
        commands = self.make_commands()
        entered, release = asyncio.Event(), asyncio.Event()
        cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
        replacement = self.make_voice(self.channel)
        self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True, mute_members=True)
        interaction = SimpleNamespace(
            guild=self.guild,
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        async def prepare(*args):
            entered.set()
            await release.wait()
            return self.instance.id

        async def disconnect(**kwargs):
            cleanup_entered.set()
            await cleanup_release.wait()
            self.guild.voice_client = None
            self.voice.is_connected.return_value = False

        async def connect(**kwargs):
            self.guild.voice_client = self.voice if self.channel.connect.await_count == 1 else replacement
            return self.guild.voice_client

        self.channel.connect.side_effect = connect
        self.voice.disconnect.side_effect = disconnect
        with patch.object(autonomous_stage, "prepare_stage", AsyncMock(side_effect=prepare)), patch(
            "persona_bot.core.voice_commands.announce_voice_join", new_callable=AsyncMock,
        ) as announce:
            start = spawn(self.start(), label="ステージ準備待機の検証")
            join = None
            try:
                await asyncio.wait_for(entered.wait(), 2)
                join = spawn(commands["join"](interaction), label="同一チャンネルjoinの検証")
                await asyncio.sleep(0)
                self.assertFalse(join.done())
                announce.assert_not_awaited()
                release.set()
                await asyncio.wait_for(cleanup_entered.wait(), 2)
                self.assertFalse(join.done())
                cleanup_release.set()
                await asyncio.wait_for(asyncio.gather(start, join), 2)
            finally:
                release.set()
                cleanup_release.set()
                await asyncio.gather(start, *([join] if join else []), return_exceptions=True)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        replacement.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, replacement)
        self.assert_not_started()
        self.assertEqual(self.channel.connect.await_count, 2)
        announce.assert_awaited_once()

    async def test_stop_waits_for_pending_connect_despite_repeated_cancellation(self) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def connect(**kwargs):
            entered.set()
            await release.wait()
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect
        start = spawn(self.start(), label="接続待機中止の検証")
        stop = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            stop = spawn(self.service.stop_session(1, reason="架空の停止"), label="開始中止待機の検証")
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 2)
            await start
        finally:
            release.set()
            await asyncio.gather(start, *([stop] if stop else []), return_exceptions=True)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(self.service._starting, {})
        self.assert_not_started()


if __name__ == "__main__":
    unittest.main()
