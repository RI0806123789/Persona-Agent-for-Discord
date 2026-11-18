"""ステージ準備のキャンセルでも、所有枠と開始時の接続を片付けることを検証する。"""

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
from persona_bot.services import autonomous_lifecycle, autonomous_service, autonomous_stage
from persona_bot.services.autonomous_service import AutonomousService
from tests.helpers.autonomous_patch import patch_autonomous


class AutonomousStartCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = BotState().get_guild(1)
        self.settings.autonomous_enabled = True
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel = Mock(spec=discord.StageChannel, id=10)
        self.channel.name = "架空のステージ"
        self.channel.instance = None
        self.channel.create_instance = AsyncMock(return_value=self.instance)
        self.voice = Mock(spec=discord.VoiceClient)
        self.voice.channel = self.channel
        self.voice.is_connected.return_value = True
        self.voice.disconnect = AsyncMock()
        self.channel.connect = AsyncMock(return_value=self.voice)
        self.member = SimpleNamespace(voice=SimpleNamespace(channel=self.channel), edit=AsyncMock())
        self.guild = SimpleNamespace(id=1, voice_client=self.voice, me=self.member)

        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            return self.voice

        self.channel.connect.side_effect = connect
        self.service = AutonomousService(None, None, None, None, None, None)
        picker = patch.object(autonomous_stage, "pick_channel", return_value=self.channel)
        picker.start()
        self.addCleanup(picker.stop)
        patch_autonomous(self, "is_quiet_hours", Mock(return_value=False))

    async def test_connect_cancellation_disconnects_only_client_created_by_attempt(self) -> None:
        for replace in (False, True):
            with self.subTest(replace=replace):
                replacement = SimpleNamespace(disconnect=AsyncMock())
                self.voice.disconnect.reset_mock()

                async def connect(**kwargs):
                    self.guild.voice_client = kwargs["cls"](object(), self.channel)
                    await asyncio.sleep(0)
                    if replace:
                        self.guild.voice_client = replacement
                    raise asyncio.CancelledError

                self.channel.connect.side_effect = connect
                with patch.object(autonomous_service.discord, "VoiceClient", return_value=self.voice):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.service._start_session(self.guild, self.settings)
                self.assertEqual(self.voice.disconnect.await_count, 0 if replace else 1)
                replacement.disconnect.assert_not_awaited()
                self.assertNotIn(1, self.service._starting)
                self.assertEqual(self.settings.autonomous_start_failures, 0)

    async def test_repeated_connect_cancellation_waits_for_owned_disconnect(self) -> None:
        connecting, disconnecting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def connect(**kwargs):
            self.guild.voice_client = kwargs["cls"](object(), self.channel)
            connecting.set()
            await asyncio.Event().wait()

        async def disconnect(**kwargs):
            disconnecting.set()
            await release.wait()

        self.channel.connect.side_effect = connect
        self.voice.disconnect.side_effect = disconnect
        with patch.object(autonomous_service.discord, "VoiceClient", return_value=self.voice):
            task = spawn(self.service._start_session(self.guild, self.settings), label="接続キャンセルの検証")
            try:
                await asyncio.wait_for(connecting.wait(), 2)
                task.cancel()
                await asyncio.wait_for(disconnecting.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_cancellation_during_voice_cache_wait_closes_stage_and_disconnects(self) -> None:
        self.member.voice = None
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.service._start_session(self.guild, self.settings)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.settings.autonomous_session)

    async def test_cancellation_during_speaker_edit_closes_only_owned_stage(self) -> None:
        self.member.edit.side_effect = asyncio.CancelledError
        for borrowed in (False, True):
            with self.subTest(borrowed=borrowed):
                self.channel.instance = self.instance if borrowed else None
                self.instance.delete.reset_mock()
                self.voice.disconnect.reset_mock()
                with self.assertRaises(asyncio.CancelledError):
                    await self.service._start_session(self.guild, self.settings)
                self.assertEqual(self.instance.delete.await_count, 0 if borrowed else 1)
                self.voice.disconnect.assert_awaited_once_with(force=True)
                self.assertIsNone(self.settings.autonomous_session)

    async def test_cancellation_keeps_replacement_stage_and_voice_connection(self) -> None:
        replacement_instance = SimpleNamespace(id=202, delete=AsyncMock())
        replacement_voice = Mock(spec=discord.VoiceClient)
        replacement_voice.disconnect = AsyncMock()

        async def replaced(**kwargs):
            self.channel.instance = replacement_instance
            self.guild.voice_client = replacement_voice
            raise asyncio.CancelledError

        self.member.edit.side_effect = replaced
        for initial_voice in (self.voice, None):
            with self.subTest(initial_voice_missing=initial_voice is None):
                self.guild.voice_client = initial_voice
                self.channel.instance = None
                with self.assertRaises(asyncio.CancelledError):
                    await self.service._start_session(self.guild, self.settings)
        self.instance.delete.assert_not_awaited()
        replacement_instance.delete.assert_not_awaited()
        self.voice.disconnect.assert_not_awaited()
        replacement_voice.disconnect.assert_not_awaited()

    async def test_cleanup_error_does_not_hide_cancellation_or_skip_disconnect(self) -> None:
        self.member.edit.side_effect = asyncio.CancelledError
        self.instance.delete.side_effect = RuntimeError("架空のステージ削除エラー")
        with self.assertRaises(asyncio.CancelledError):
            await self.service._start_session(self.guild, self.settings)
        self.instance.delete.assert_awaited_once()
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_repeated_cancellation_waits_until_stage_and_voice_cleanup_finish(self) -> None:
        editing, deleting, disconnecting = (asyncio.Event() for _ in range(3))
        release_stage, release_voice = asyncio.Event(), asyncio.Event()

        async def edit(**kwargs):
            editing.set()
            await asyncio.Event().wait()

        async def delete(**kwargs):
            deleting.set()
            await release_stage.wait()

        async def disconnect(**kwargs):
            disconnecting.set()
            await release_voice.wait()

        self.member.edit.side_effect = edit
        self.instance.delete.side_effect = delete
        self.voice.disconnect.side_effect = disconnect
        task = spawn(self.service._start_session(self.guild, self.settings), label="開始キャンセルの検証")
        try:
            await asyncio.wait_for(editing.wait(), 2)
            task.cancel()
            await asyncio.wait_for(deleting.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
            release_stage.set()
            await asyncio.wait_for(disconnecting.wait(), 2)
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
        self.assertIsNone(self.settings.autonomous_session)


if __name__ == "__main__":
    unittest.main()
