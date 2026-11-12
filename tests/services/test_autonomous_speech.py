"""自律配信の送信待機中の終了・移動と、応答設定の切替を再現する。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core.app_state import AutonomousSession, BotState
from persona_bot.services import autonomous_service, autonomous_speech
from persona_bot.services.autonomous_service import AutonomousService
from tests.helpers.autonomous_patch import patch_autonomous
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.thread_probe import make_thread_probe
from tests.helpers.persona_helpers import install_persona_catalog


class AutonomousSpeechTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = BotState()
        install_persona_catalog(self)
        self.settings = self.state.get_guild(1)
        self.settings.current_prompt_file = "../prompts/prompt_default.txt"
        self.settings.current_model_name = "old-model"
        self.settings.grounding_enabled = True
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.session = AutonomousSession(
            channel_id=10, is_stage=False, started_at=now,
            last_human_at=now, last_spoke_at=now, busy=True,
        )
        self.settings.autonomous_session = self.session
        self.channel = Mock(spec=discord.VoiceChannel, id=10)
        self.channel.name = "架空の配信チャンネル"

        @asynccontextmanager
        async def typing():
            yield

        self.channel.typing.side_effect = typing
        self.voice = Mock(spec=discord.VoiceClient)
        self.voice.channel = self.channel
        self.voice.is_connected.return_value = True
        self.guild = SimpleNamespace(id=1, voice_client=self.voice)
        self.client = SimpleNamespace(
            user=SimpleNamespace(id=99), get_channel=Mock(return_value=self.channel),
            get_guild=Mock(return_value=self.guild),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("架空の応答", None, [])))
        self.is_blocked = Mock(return_value=False)
        self.service = AutonomousService(
            self.client, self.state, self.gemini,
            SimpleNamespace(check=AsyncMock(return_value=(True, ""))),
            SimpleNamespace(mask_with_log=Mock(side_effect=lambda text, **kwargs: text)),
            SimpleNamespace(log_snapshot=Mock()),
            is_blocked=self.is_blocked,
        )
        self.loader = Mock(return_value="架空のペルソナ")
        loader_patch = patch("persona_bot.config.paths.load_prompt_template", self.loader)
        loader_patch.start()
        self.addCleanup(loader_patch.stop)
        self.history = AsyncMock(return_value="架空の履歴")
        self.sent = AsyncMock()
        self.spoken = AsyncMock()
        for name, replacement in (
            ("fetch_channel_history_text", self.history),
            ("load_image_attachments", AsyncMock(return_value=[])),
            ("send_long_message", self.sent),
            ("send_long_reply", self.sent),
            ("speak_text", self.spoken),
        ):
            patch_autonomous(self, name, replacement)

    async def speak(self, kind):
        if kind == "opening":
            await self.service._speak_opening(self.guild, self.settings, self.session, self.channel)
        else:
            await self.service._speak_monologue(self.guild, self.settings, self.session)

    def change_settings(self) -> None:
        self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
        self.settings.current_model_name = "new-model"
        self.settings.grounding_enabled = False

    async def test_opening_or_monologue_send_failure_stops_the_failed_session(self):
        for kind in ("opening", "monologue"):
            with self.subTest(kind=kind):
                self.settings.autonomous_session = self.session
                self.session.busy = True
                self.voice.disconnect.reset_mock()
                self.sent.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="禁止"), "架空の送信拒否")
                await self.speak(kind)
                self.assertIsNone(self.settings.autonomous_session)
                self.assertFalse(self.session.busy)
                self.voice.disconnect.assert_awaited_once_with(force=True)
                self.spoken.assert_not_awaited()

    async def test_send_failure_does_not_stop_a_replacement_session(self):
        replacement = SimpleNamespace(channel_id=20)

        async def fail(*args, **kwargs):
            self.settings.autonomous_session = replacement
            raise RuntimeError("架空の送信失敗")

        self.sent.side_effect = fail
        await self.speak("opening")
        self.assertIs(self.settings.autonomous_session, replacement)
        self.voice.disconnect.assert_not_awaited()

    async def test_opening_failure_keeps_notice_and_only_monologue_increments_turn(self):
        self.gemini.generate.return_value = (None, None, [])
        await self.speak("opening")
        self.assertEqual(self.sent.await_args.args[1], autonomous_speech.AUTONOMOUS_OPENING_NOTICE)
        self.assertEqual(self.session.turn_count, 0)
        self.spoken.assert_not_awaited()

        self.gemini.generate.return_value = ("架空の応答", None, [])
        await self.speak("monologue")
        self.assertEqual(self.session.turn_count, 1)
        self.spoken.assert_awaited_once()

    async def test_finished_replaced_moved_or_disconnected_session_is_not_spoken_after_send(self) -> None:
        for kind in ("opening", "monologue"):
            for change in ("finished", "replaced", "moved", "disconnected", "missing"):
                with self.subTest(kind=kind, change=change):
                    self.settings.autonomous_session = self.session
                    self.guild.voice_client = self.voice
                    self.voice.channel = self.channel
                    self.voice.is_connected.return_value = True
                    self.sent.reset_mock()
                    self.spoken.reset_mock()

                    async def send(*args, **kwargs):
                        # Discordへの送信が待機している間の /leave・/join・移動を再現する。
                        await asyncio.sleep(0)
                        if change == "finished":
                            self.settings.autonomous_session = None
                        elif change == "replaced":
                            self.settings.autonomous_session = SimpleNamespace(channel_id=10)
                        elif change == "moved":
                            self.voice.channel = SimpleNamespace(id=20)
                        elif change == "disconnected":
                            self.voice.is_connected.return_value = False
                        else:
                            self.guild.voice_client = None

                    self.sent.side_effect = send
                    await self.speak(kind)
                    self.sent.assert_awaited_once()
                    self.spoken.assert_not_awaited()
                    self.assertFalse(self.session.busy)

    async def test_opening_and_monologue_keep_snapshot_during_history_generation_and_send(self) -> None:
        for kind in ("opening", "monologue"):
            for phase in ("history", "generate", "send"):
                with self.subTest(kind=kind, phase=phase):
                    self.settings.current_prompt_file = "../prompts/prompt_default.txt"
                    self.settings.current_model_name = "old-model"
                    self.spoken.reset_mock()
                    self.history.side_effect = None
                    self.gemini.generate.side_effect = None
                    self.sent.side_effect = None

                    async def change(*args, **kwargs):
                        self.change_settings()
                        await asyncio.sleep(0)
                        if phase == "generate":
                            return "架空の応答", None, []
                        return "架空の履歴"

                    {"history": self.history, "generate": self.gemini.generate,
                     "send": self.sent}[phase].side_effect = change
                    await self.speak(kind)
                    self.assertEqual(self.gemini.generate.await_args.args[1], "old-model")
                    self.assertFalse(self.gemini.generate.await_args.kwargs["grounding"])
                    self.spoken.assert_awaited_once_with(
                        self.guild, "架空の応答", "../prompts/prompt_default.txt",
                        is_valid=ANY,
                    )
                    is_valid = self.spoken.await_args.kwargs["is_valid"]
                    self.assertTrue(is_valid())
                    self.settings.autonomous_session = None
                    self.assertFalse(is_valid())
                    self.settings.autonomous_session = self.session

    async def test_unsafe_comment_response_is_not_posted(self):
        with patch.object(autonomous_speech, "screen_bot_response_with_status", new=AsyncMock(return_value=("拒否", False))):
            result = await self.service.handle_comment(self.make_comment(), "架空のコメント", self.settings)
        self.assertIsNone(result)
        self.sent.assert_not_awaited()
        self.spoken.assert_not_awaited()

    async def test_all_speech_paths_load_persona_without_blocking_loop(self) -> None:
        for kind in ("opening", "monologue", "comment"):
            with self.subTest(kind=kind):
                operation, threads = make_thread_probe("架空のペルソナ")
                self.loader.side_effect = operation
                if kind == "comment":
                    result = await self.service.handle_comment(self.make_comment(), "架空のコメント", self.settings)
                    self.assertEqual(result, "架空の応答")
                else:
                    await self.speak(kind)
                self.assertTrue(threads)
                self.assertNotIn(threading.get_ident(), threads)

    async def test_comment_uses_one_snapshot_before_typing_and_for_tts(self) -> None:
        @asynccontextmanager
        async def typing():
            self.change_settings()
            await asyncio.sleep(0)
            yield

        self.channel.typing.side_effect = typing
        message = self.make_comment()
        await self.make_comment_handler().on_message(message)
        self.assertEqual(self.loader.call_args.args[0].name, "prompt_default.txt")
        self.assertEqual(self.gemini.generate.await_args.args[1], "old-model")
        self.assertTrue(self.gemini.generate.await_args.kwargs["grounding"])
        self.spoken.assert_awaited_once_with(
            message, "架空の応答", "../prompts/prompt_default.txt", is_valid=ANY,
        )
        self.assertTrue(self.spoken.await_args.kwargs["is_valid"]())

    def make_comment_handler(self):
        return make_message_handler({
            "client": self.client, "state": self.state, "autonomous": self.service,
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": SimpleNamespace(contains_ng_word=Mock(return_value=False)),
            "moderator": SimpleNamespace(check_and_route=AsyncMock(return_value=(True, "", "none", None))),
            "spawn": lambda coro, **kwargs: coro.close(),
            "play_tts_response": self.spoken,
        }, self)

    def make_comment(self):
        return SimpleNamespace(
            content="架空のコメント", attachments=[], mentions=[], channel=self.channel,
            guild=self.guild, created_at=self.session.started_at,
            author=SimpleNamespace(id=11, bot=False, display_name="架空の視聴者"),
        )

    async def test_comment_is_not_spoken_after_session_finishes_or_voice_moves_during_send(self) -> None:
        handler = self.make_comment_handler()
        message = self.make_comment()
        for change in ("finished", "replaced", "moved", "disconnected"):
            with self.subTest(change=change):
                self.settings.autonomous_session = self.session
                self.session.last_comment_reply_at = None
                self.voice.channel = self.channel
                self.voice.is_connected.return_value = True
                self.sent.reset_mock()
                self.spoken.reset_mock()

                async def send(*args, **kwargs):
                    await asyncio.sleep(0)
                    if change == "finished":
                        self.settings.autonomous_session = None
                    elif change == "replaced":
                        self.settings.autonomous_session = SimpleNamespace(channel_id=10)
                    elif change == "moved":
                        self.voice.channel = SimpleNamespace(id=20)
                    else:
                        self.voice.is_connected.return_value = False

                self.sent.side_effect = send
                await handler.on_message(message)
                self.sent.assert_awaited_once()
                self.spoken.assert_not_awaited()

    async def test_direct_comment_call_keeps_snapshot_during_history_fetch(self) -> None:
        async def history(*args, **kwargs):
            self.change_settings()
            await asyncio.sleep(0)
            return "架空の履歴"

        self.history.side_effect = history
        self.assertEqual(await self.service.handle_comment(
            self.make_comment(), "架空のコメント", self.settings,
        ), "架空の応答")
        self.assertEqual(self.gemini.generate.await_args.args[1], "old-model")
        self.assertTrue(self.gemini.generate.await_args.kwargs["grounding"])

    async def test_all_speech_paths_pass_the_channel_reset_boundary(self) -> None:
        self.settings.channel_reset_points[10] = 77
        for kind in ("opening", "monologue", "comment"):
            with self.subTest(kind=kind):
                self.history.reset_mock()
                if kind == "comment":
                    await self.service.handle_comment(self.make_comment(), "架空のコメント", self.settings)
                else:
                    await self.speak(kind)
                self.assertEqual(self.history.await_args.args[1], 77)
                self.assertIs(self.history.await_args.kwargs["is_blocked"], self.is_blocked)


if __name__ == "__main__":
    unittest.main()
