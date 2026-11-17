"""配信中の権限変更を、生成前に検知して接続・所有枠を片付けることを確認する。"""

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
from persona_bot.core.background import spawn
from persona_bot.services import autonomous_service, autonomous_stage
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.utils import discord_helpers


class AutonomousSessionPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.autonomous_enabled = True
        self.voice = Mock(spec=discord.VoiceClient)
        self.voice.is_connected.return_value = True
        self.voice.disconnect = AsyncMock()
        self.member = SimpleNamespace(voice=None)
        self.guild = SimpleNamespace(id=1, me=self.member, voice_client=self.voice)
        self.client = SimpleNamespace(
            get_guild=Mock(return_value=self.guild), get_channel=Mock(),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock())
        self.service = AutonomousService(self.client, self.state, self.gemini, None, None, None)
        self.notice = AsyncMock()
        self.spawn = Mock(side_effect=lambda coro, **kwargs: coro.close())

        def start(coro, **kwargs):
            # 発話の未生成を検証しつつ、共有する後片付けタスクは実際に完了させる。
            if kwargs.get("_protected"):
                return spawn(coro, **kwargs)
            return self.spawn(coro, **kwargs)

        for name, replacement in (
            ("is_quiet_hours", Mock(return_value=False)),
            ("send_long_message", self.notice), ("spawn", start),
        ):
            patcher = patch.object(autonomous_service, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.prepare_channel(stage=False)

    def prepare_channel(self, *, stage: bool) -> None:
        now = datetime.now(timezone.utc)
        self.permissions = SimpleNamespace(
            view_channel=True, send_messages=True, speak=True, connect=True,
            manage_channels=True, mute_members=True, move_members=True,
        )
        self.channel = Mock(spec=discord.StageChannel if stage else discord.VoiceChannel, id=10)
        self.channel.name = "架空の配信先"
        self.channel.permissions_for.return_value = self.permissions
        self.channel.connect = AsyncMock()
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel.instance = self.instance if stage else None
        self.voice.channel = self.channel
        self.member.voice = SimpleNamespace(channel=self.channel, suppress=False)
        self.client.get_channel.return_value = self.channel
        self.guild.stage_channels = [self.channel] if stage else []
        self.guild.voice_channels = [] if stage else [self.channel]
        self.session = AutonomousSession(
            channel_id=10, is_stage=stage, started_at=now, last_human_at=now,
            last_spoke_at=now - timedelta(minutes=constants.AUTONOMOUS_TALK_INTERVAL_MINUTES + 1),
            stage_started_by_bot=stage, stage_instance_id=101 if stage else None,
        )
        self.settings.autonomous_session = self.session
        self.settings.autonomous_last_activity = now - timedelta(hours=2)
        self.settings.autonomous_recent_activity = [now]
        self.voice.disconnect.reset_mock()
        self.notice.reset_mock()
        self.spawn.reset_mock()

    def assert_stopped_without_generation(self) -> None:
        self.assertIsNone(self.settings.autonomous_session)
        self.assertEqual(self.settings.autonomous_recent_activity, [])
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.spawn.assert_not_called()
        self.gemini.generate.assert_not_awaited()

    async def test_lost_view_or_send_permission_stops_without_closing_post_or_generation(self) -> None:
        for stage in (False, True):
            for permission, label in (("view_channel", "チャンネルを見る"), ("send_messages", "メッセージを送信")):
                with self.subTest(stage=stage, permission=permission):
                    self.prepare_channel(stage=stage)
                    setattr(self.permissions, permission, False)
                    with self.assertLogs("persona_bot.services.autonomous_service", level="INFO") as output:
                        await self.service._tend_session(self.guild, self.settings)
                    self.assert_stopped_without_generation()
                    self.notice.assert_not_awaited()
                    self.channel.permissions_for.assert_called_with(self.guild.me)
                    self.assertTrue(any(label in record.getMessage() for record in output.records))
                    self.assertTrue(any("配信を終了しました" in record.getMessage() for record in output.records))
                    if stage:
                        self.instance.delete.assert_awaited_once()
                    else:
                        self.instance.delete.assert_not_awaited()

    async def test_lost_voice_speak_permission_posts_closing_notice_and_stops_before_generation(self) -> None:
        self.permissions.speak = False
        await self.service._tend_session(self.guild, self.settings)
        self.assert_stopped_without_generation()
        self.notice.assert_awaited_once_with(
            self.channel, constants.AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS,
            allowed_mentions=discord_helpers.NO_MENTIONS,
        )

    async def test_stage_moved_to_audience_stops_and_closes_owned_instance(self) -> None:
        self.prepare_channel(stage=True)
        self.member.voice.suppress = True
        await self.service._tend_session(self.guild, self.settings)
        self.assert_stopped_without_generation()
        self.instance.delete.assert_awaited_once()
        self.notice.assert_awaited_once()

    async def test_permissions_are_checked_even_during_existing_speech(self) -> None:
        self.permissions.send_messages = False
        self.session.busy = True
        await self.service._tend_session(self.guild, self.settings)
        self.assert_stopped_without_generation()
        self.notice.assert_not_awaited()

    async def test_sufficient_permissions_allow_next_monologue(self) -> None:
        for stage in (False, True):
            with self.subTest(stage=stage):
                self.prepare_channel(stage=stage)
                await self.service._tend_session(self.guild, self.settings)
                self.assertIs(self.settings.autonomous_session, self.session)
                self.assertTrue(self.session.busy)
                self.spawn.assert_called_once()
                self.voice.disconnect.assert_not_awaited()
                self.notice.assert_not_awaited()

    async def test_stage_speaker_does_not_need_start_permissions_or_voice_speak(self) -> None:
        self.prepare_channel(stage=True)
        for permission in ("connect", "speak", "manage_channels", "mute_members", "move_members"):
            setattr(self.permissions, permission, False)
        await self.service._tend_session(self.guild, self.settings)
        self.assertIs(self.settings.autonomous_session, self.session)
        self.spawn.assert_called_once()
        self.voice.disconnect.assert_not_awaited()

    async def test_missing_member_or_channel_cache_does_not_imply_permission_loss(self) -> None:
        for cache in ("member", "channel"):
            with self.subTest(cache=cache):
                self.prepare_channel(stage=False)
                self.session.last_spoke_at = datetime.now(timezone.utc)
                self.guild.me = None if cache == "member" else self.member
                if cache == "channel":
                    self.client.get_channel.return_value = None
                await self.service._tend_session(self.guild, self.settings)
                self.assertIs(self.settings.autonomous_session, self.session)
                self.voice.disconnect.assert_not_awaited()
                self.channel.permissions_for.assert_not_called()
        # 判定不能でも、従来の無効化による停止は妨げない。
        self.settings.autonomous_enabled = False
        await self.service._tend_session(self.guild, self.settings)
        self.assert_stopped_without_generation()

    async def test_missing_or_stale_stage_voice_state_does_not_imply_audience(self) -> None:
        for voice_state in (None, SimpleNamespace(channel=SimpleNamespace(id=20), suppress=True)):
            with self.subTest(voice_state=voice_state):
                self.prepare_channel(stage=True)
                self.member.voice = voice_state
                self.session.last_spoke_at = datetime.now(timezone.utc)
                await self.service._tend_session(self.guild, self.settings)
                self.assertIs(self.settings.autonomous_session, self.session)
                self.voice.disconnect.assert_not_awaited()

    async def test_permission_denied_channel_is_excluded_from_immediate_restart(self) -> None:
        for stage, permission in ((False, "speak"), (False, "send_messages"), (True, "send_messages")):
            with self.subTest(stage=stage, permission=permission):
                self.prepare_channel(stage=stage)
                setattr(self.permissions, permission, False)
                await self.service._tend_session(self.guild, self.settings)
                self.assert_stopped_without_generation()
                self.assertIsNone(autonomous_stage.pick_channel(self.guild))
                # ゲージが再び満ちても、開始候補に入らず接続APIを呼ばない。
                self.guild.voice_client = None
                with patch.object(autonomous_service, "compute_talk_desire", return_value=100):
                    await self.service._check_guild(1, self.settings)
                self.channel.connect.assert_not_awaited()
                self.assertEqual(self.settings.autonomous_start_failures, 0)
                self.assertIsNone(self.settings.autonomous_start_retry_at)
                self.guild.voice_client = self.voice


if __name__ == "__main__":
    unittest.main()
