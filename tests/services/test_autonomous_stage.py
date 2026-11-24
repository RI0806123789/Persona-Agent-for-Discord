"""自律配信の行き先選択とステージ開始をDiscord境界のモックで検証する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import autonomous_stage
from persona_bot.services.autonomous_stage import pick_channel, prepare_stage


class AutonomousChannelSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.member = object()
        self.stage = self.make_channel()
        self.voice = self.make_channel()
        self.guild = SimpleNamespace(
            me=self.member, stage_channels=[self.stage], voice_channels=[self.voice],
        )

    @staticmethod
    def make_channel():
        return AutonomousChannelSelectionTests.make_channel_with_id(None)

    @staticmethod
    def make_channel_with_id(channel_id):
        permissions = SimpleNamespace(
            view_channel=True, connect=True, speak=True, send_messages=True,
            mute_members=True, manage_channels=True, move_members=True,
        )
        return SimpleNamespace(id=channel_id, instance=None, permissions_for=Mock(return_value=permissions))

    def test_stage_is_preferred_and_permissions_are_checked_for_bot_member(self) -> None:
        self.assertIs(pick_channel(self.guild), self.stage)
        self.stage.permissions_for.assert_called_once_with(self.member)

    def test_voice_is_used_when_stage_speaker_permission_is_missing(self) -> None:
        self.stage.permissions_for.return_value.mute_members = False
        self.assertIs(pick_channel(self.guild), self.voice)

    def test_new_stage_needs_management_permissions_but_existing_stage_does_not(self) -> None:
        self.stage.permissions_for.return_value.manage_channels = False
        self.assertIs(pick_channel(self.guild), self.voice)
        self.stage.instance = SimpleNamespace(id=101)
        self.assertIs(pick_channel(self.guild), self.stage)

    def test_channels_without_comment_access_are_excluded(self) -> None:
        self.stage.permissions_for.return_value.send_messages = False
        self.voice.permissions_for.return_value.view_channel = False
        self.assertIsNone(pick_channel(self.guild))

    def test_missing_bot_member_skips_permission_checks(self) -> None:
        self.guild.me = None
        self.assertIsNone(pick_channel(self.guild))
        self.stage.permissions_for.assert_not_called()
        self.voice.permissions_for.assert_not_called()


class DesignatedChannelSelectionTests(unittest.TestCase):
    """管理者が指定した配信先の選び方（#266）。使えない指定は自動選択へ戻す。"""

    def setUp(self) -> None:
        self.member = object()
        self.stages = [self.make_channel(201), self.make_channel(202)]
        self.voices = [self.make_channel(301), self.make_channel(302)]
        self.guild = SimpleNamespace(me=self.member, stage_channels=self.stages, voice_channels=self.voices)

    @staticmethod
    def make_channel(channel_id: int):
        return AutonomousChannelSelectionTests.make_channel_with_id(channel_id)

    def pick(self, *, stage=None, voice=None):
        # 自動選択の random.choice は先頭を返すよう固定し、指定の効果だけを見る
        with patch.object(autonomous_stage.random, "choice", side_effect=lambda items: items[0]):
            return pick_channel(self.guild, stage_channel_id=stage, voice_channel_id=voice)

    def test_unset_designation_keeps_automatic_selection(self) -> None:
        self.assertIs(self.pick(), self.stages[0])

    def test_usable_designated_stage_is_used(self) -> None:
        self.assertIs(self.pick(stage=202), self.stages[1])

    def test_designated_voice_wins_over_automatic_stage(self) -> None:
        # ステージの指定が無ければ、使えるステージがあってもボイスの指定を使う
        self.assertIs(self.pick(voice=302), self.voices[1])

    def test_designated_stage_takes_priority_over_designated_voice(self) -> None:
        self.assertIs(self.pick(stage=202, voice=302), self.stages[1])

    def test_unusable_designated_stage_falls_back_to_designated_voice(self) -> None:
        self.stages[1].permissions_for.return_value.mute_members = False
        self.assertIs(self.pick(stage=202, voice=302), self.voices[1])

    def test_unusable_designations_fall_back_to_automatic_selection(self) -> None:
        self.stages[1].permissions_for.return_value.mute_members = False
        self.voices[1].permissions_for.return_value.speak = False
        self.assertIs(self.pick(stage=202, voice=302), self.stages[0])

    def test_deleted_designations_fall_back_to_automatic_selection(self) -> None:
        self.assertIs(self.pick(stage=999, voice=998), self.stages[0])

    def test_designation_is_looked_up_only_in_its_own_kind(self) -> None:
        # ボイスのIDをステージの指定として渡しても、種別の違うチャンネルは選ばない
        # （見つからない指定と同じく自動選択へ戻る）
        self.assertIs(self.pick(stage=302), self.stages[0])
        self.assertIs(self.pick(voice=202), self.stages[0])

    def test_automatic_fallback_uses_voice_when_no_stage_is_usable(self) -> None:
        for stage in self.stages:
            stage.permissions_for.return_value.mute_members = False
        self.assertIs(self.pick(stage=202, voice=999), self.voices[0])


class AutonomousStagePreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channel = SimpleNamespace(
            id=10, name="架空のステージ", instance=None,
            create_instance=AsyncMock(return_value=self.instance),
        )
        self.member = SimpleNamespace(voice=SimpleNamespace(channel=self.channel), edit=AsyncMock())
        self.guild = SimpleNamespace(me=self.member)

    async def test_created_stage_returns_response_id_without_sending_start_notification(self) -> None:
        self.assertEqual(await prepare_stage(self.guild, self.channel), 101)
        self.assertFalse(self.channel.create_instance.await_args.kwargs["send_start_notification"])
        self.member.edit.assert_awaited_once_with(suppress=False)
        self.assertIsNone(self.channel.instance)

    async def test_existing_stage_is_borrowed_without_ownership(self) -> None:
        self.channel.instance = self.instance
        self.assertIsNone(await prepare_stage(self.guild, self.channel))
        self.channel.create_instance.assert_not_awaited()
        self.member.edit.assert_awaited_once_with(suppress=False)

    async def test_speaker_failure_closes_only_stage_created_by_bot(self) -> None:
        self.member.edit.side_effect = RuntimeError("架空の発言権限エラー")
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_awaited_once()

        self.instance.delete.reset_mock()
        self.channel.instance = self.instance
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_not_awaited()

    async def test_stage_replaced_while_enabling_speaker_is_not_deleted(self) -> None:
        replacement = SimpleNamespace(id=202, delete=AsyncMock())

        async def replaced(**kwargs):
            self.channel.instance = replacement
            raise RuntimeError("架空の発言権限エラー")

        self.member.edit.side_effect = replaced
        with self.assertRaises(RuntimeError):
            await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_not_awaited()
        replacement.delete.assert_not_awaited()

    async def test_voice_cache_is_retried_before_enabling_speaker(self) -> None:
        self.member.voice = None

        async def cache_updated(*args):
            self.member.voice = SimpleNamespace(channel=self.channel)

        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock(side_effect=cache_updated)) as sleep:
            self.assertEqual(await prepare_stage(self.guild, self.channel), 101)
        sleep.assert_awaited_once()
        self.member.edit.assert_awaited_once_with(suppress=False)

    async def test_missing_voice_cache_closes_created_stage_without_editing_member(self) -> None:
        self.member.voice = None
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_awaited_once()
        self.member.edit.assert_not_awaited()

    async def test_other_channel_voice_cache_never_promotes_member_and_rolls_back(self) -> None:
        self.member.voice.channel = SimpleNamespace(id=20)
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await prepare_stage(self.guild, self.channel)
        self.instance.delete.assert_awaited_once()
        self.member.edit.assert_not_awaited()


class DesignationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_passes_saved_designation_from_guild_settings(self) -> None:
        from persona_bot.core.app_state import BotState
        from persona_bot.services.autonomous_service import AutonomousService

        settings = BotState().get_guild(1)
        settings.autonomous_stage_channel_id = 202
        settings.autonomous_voice_channel_id = 302
        guild = SimpleNamespace(id=1, voice_client=None, me=object())
        service = AutonomousService(None, None, None, None, None, None)
        # 行き先が無ければ接続を試みずに戻る（待ち時間も記録しない）
        with patch.object(autonomous_stage, "pick_channel", return_value=None) as picker:
            await service._start_session(guild, settings)
        picker.assert_called_once_with(guild, stage_channel_id=202, voice_channel_id=302)
        self.assertEqual(settings.autonomous_start_failures, 0)
        self.assertIsNone(settings.autonomous_start_retry_at)


if __name__ == "__main__":
    unittest.main()
