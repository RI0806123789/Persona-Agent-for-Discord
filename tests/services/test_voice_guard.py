"""ボット自身の予期しないボイスチャンネル移動を検知し、接続先を守る（#171）。

Discord には接続せず、ボイス状態の更新イベントと VoiceClient をモックで再現する。
実際の Discord での移動操作（管理者のドラッグ）は未確認。
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config import constants
from persona_bot.core.app_state import AutonomousSession, BotState
from persona_bot.services import autonomous_service
from persona_bot.services import voice_guard as voice_guard_module
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.voice_guard import VoiceGuard
from tests.helpers.discord_mocks import make_http_response

BOT_ID = 99
HOME_ID = 10
MOVED_ID = 20


def make_channel(channel_id: int, *, spec: type = discord.VoiceChannel, **permissions) -> Mock:
    channel = Mock(spec=spec, id=channel_id)
    channel.name = f"架空の通話{channel_id}"
    allowed = dict(view_channel=True, connect=True, speak=True, mute_members=False)
    allowed.update(permissions)
    channel.permissions_for.return_value = SimpleNamespace(**allowed)
    return channel


def voice_state(channel) -> SimpleNamespace:
    return SimpleNamespace(channel=channel)


class VoiceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.home = make_channel(HOME_ID)
        self.moved = make_channel(MOVED_ID)
        self.channels = {HOME_ID: self.home, MOVED_ID: self.moved}
        self.guild = SimpleNamespace(
            id=1, me=object(), voice_client=None,
            get_channel=Mock(side_effect=lambda channel_id: self.channels.get(channel_id)),
        )
        self.voice_client = self.make_voice_client(self.home)
        self.guild.voice_client = self.voice_client
        self.client = SimpleNamespace(user=SimpleNamespace(id=BOT_ID), get_guild=Mock(return_value=self.guild))
        self.autonomous = SimpleNamespace(
            manages_connection=Mock(return_value=False), handle_external_move=AsyncMock(),
        )
        self.guard = VoiceGuard(self.client, self.autonomous)
        self.member = SimpleNamespace(id=BOT_ID, guild=self.guild)
        self.tasks: list[asyncio.Task] = []

        def start(coro, **kwargs):
            task = asyncio.ensure_future(coro)
            self.tasks.append(task)
            return task

        for name, value in {
            "spawn": Mock(side_effect=start),
            "VOICE_MOVE_GUARD_SETTLE_SECONDS": 0,
            "VOICE_MOVE_GUARD_SETTLE_RETRIES": 2,
        }.items():
            patcher = patch.object(voice_guard_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_voice_client(self, channel) -> Mock:
        voice_client = Mock(spec=discord.VoiceClient)
        voice_client.channel = channel
        voice_client.is_connected.return_value = True
        voice_client.is_playing.return_value = True

        async def move_to(target, *, timeout):
            voice_client.channel = target
            # 戻った結果のボイス状態の更新も、実際と同じくイベントとして届く
            self.guard.handle_voice_state_update(self.member, voice_state(self.moved), voice_state(target))

        async def disconnect(*, force):
            if self.guild.voice_client is voice_client:
                self.guild.voice_client = None

        voice_client.move_to = AsyncMock(side_effect=move_to)
        voice_client.disconnect = AsyncMock(side_effect=disconnect)
        return voice_client

    def event(self, before, after, *, member=None) -> None:
        self.guard.handle_voice_state_update(member or self.member, voice_state(before), voice_state(after))

    async def settle(self) -> None:
        while self.tasks:
            await self.tasks.pop(0)

    async def connect_then_move(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()

    def assert_left_without_return(self) -> None:
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guard.home_channel_id(1))

    # ---- 基本の動作 ------------------------------------------------------------

    async def test_manual_move_returns_to_the_original_channel(self) -> None:
        await self.connect_then_move()
        # 移動先で古い読み上げを最後まで流さない
        self.voice_client.stop.assert_called_once()
        self.voice_client.move_to.assert_awaited_once_with(
            self.home, timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS,
        )
        self.assertIs(self.voice_client.channel, self.home)
        self.voice_client.disconnect.assert_not_awaited()
        self.assertEqual(self.guard.home_channel_id(1), HOME_ID)

    async def test_own_connections_disconnects_and_unchanged_updates_are_not_moves(self) -> None:
        self.event(None, self.home)                  # /join・配信の開始
        self.event(self.home, self.home)             # ミュート・発言者の切り替え
        self.event(self.home, None)                  # /leave・配信の終了・外部からの切断
        self.assertIsNone(self.guard.home_channel_id(1))
        self.event(None, self.moved)                 # /join での切り替え（切断→接続）
        self.assertEqual(self.guard.home_channel_id(1), MOVED_ID)
        # ボット以外の利用者の移動は見ない
        self.event(self.moved, self.home, member=SimpleNamespace(id=5, guild=self.guild))
        await self.settle()
        voice_guard_module.spawn.assert_not_called()
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_disabled_guard_leaves_moves_as_they_are(self) -> None:
        with patch.object(voice_guard_module, "VOICE_MOVE_GUARD_ENABLED", False):
            await self.connect_then_move()
        voice_guard_module.spawn.assert_not_called()
        self.voice_client.move_to.assert_not_awaited()

    async def test_move_without_a_seen_connection_returns_to_the_channel_before_the_move(self) -> None:
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()
        self.voice_client.move_to.assert_awaited_once()
        self.assertIs(self.voice_client.channel, self.home)

    async def test_return_to_stage_promotes_the_bot_to_speaker_again(self) -> None:
        self.home = make_channel(HOME_ID, spec=discord.StageChannel, mute_members=True)
        self.channels[HOME_ID] = self.home
        self.voice_client.channel = self.home
        promote = AsyncMock()
        with patch.object(voice_guard_module, "enable_stage_speaker", promote):
            await self.connect_then_move()
        promote.assert_awaited_once_with(self.guild, self.home)
        self.voice_client.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, self.voice_client)
        self.assertEqual(self.guard.home_channel_id(1), HOME_ID)

    def prepare_stage_return(self) -> None:
        """各ケースで、ステージへ戻す対象の接続と復帰先を用意する。"""
        self.guard.release(1)
        self.home = make_channel(HOME_ID, spec=discord.StageChannel, mute_members=True)
        self.channels[HOME_ID] = self.home
        self.voice_client = self.make_voice_client(self.home)
        self.guild.voice_client = self.voice_client

    async def test_hidden_voice_and_stage_targets_leave_without_attempting_a_return(self) -> None:
        for channel_type in (discord.VoiceChannel, discord.StageChannel):
            with self.subTest(channel_type=channel_type.__name__):
                self.home = make_channel(HOME_ID, spec=channel_type, view_channel=False, mute_members=True)
                self.channels[HOME_ID] = self.home
                self.voice_client = self.make_voice_client(self.home)
                self.guild.voice_client = self.voice_client
                promote = AsyncMock()
                with patch.object(voice_guard_module, "enable_stage_speaker", promote), \
                        self.assertLogs(voice_guard_module.logger, level="WARNING") as log:
                    await self.connect_then_move()
                self.assert_left_without_return()
                promote.assert_not_awaited()
                self.home.permissions_for.assert_called_once_with(self.guild.me)
                self.assertTrue(any("保護の継続条件を満たさない" in record.getMessage() for record in log.records))

    async def test_failed_stage_promotion_leaves_and_releases_protection(self) -> None:
        errors = (
            discord.Forbidden(make_http_response(403, "架空の拒否"), "架空の権限変更"),
            discord.HTTPException(make_http_response(500, "架空の障害"), "架空のAPI失敗"),
            RuntimeError("架空のボイス状態取得失敗"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.prepare_stage_return()
                promote = AsyncMock(side_effect=error)
                with patch.object(voice_guard_module, "enable_stage_speaker", promote):
                    await self.connect_then_move()
                self.voice_client.move_to.assert_awaited_once_with(
                    self.home, timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS,
                )
                promote.assert_awaited_once_with(self.guild, self.home)
                self.voice_client.disconnect.assert_awaited_once_with(force=True)
                self.assertIsNone(self.guild.voice_client)
                self.assertIsNone(self.guard.home_channel_id(1))
                # 退出後に同じ保護処理が走っても、入り直しや再昇格はしない。
                await self.guard._protect(1)
                promote.assert_awaited_once()
                self.voice_client.disconnect.assert_awaited_once()

    async def test_stage_promotion_failure_keeps_later_connection_and_home_changes(self) -> None:
        for change in ("replacement", "adopt_moved", "release", "readopt_home"):
            with self.subTest(change=change):
                self.prepare_stage_return()
                entered, finish = asyncio.Event(), asyncio.Event()

                async def promote(*args):
                    entered.set()
                    await finish.wait()
                    raise RuntimeError("架空の昇格失敗")

                with patch.object(voice_guard_module, "enable_stage_speaker", AsyncMock(side_effect=promote)):
                    operation = asyncio.create_task(self.connect_then_move())
                    current = self.voice_client
                    expected_home = HOME_ID
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        if change == "replacement":
                            # 保護先が同じままでも、接続自体が差し替われば触らない。
                            current = self.make_voice_client(self.home)
                            self.guild.voice_client = current
                        elif change == "adopt_moved":
                            current.channel = self.moved
                            self.guard.adopt(1, MOVED_ID)
                            expected_home = MOVED_ID
                        elif change == "release":
                            self.guard.release(1)
                            expected_home = None
                        else:
                            # チャンネルIDが同じでも、再登録された保護情報は別の所有者。
                            self.guard.release(1)
                            self.guard.adopt(1, HOME_ID)
                    finally:
                        finish.set()
                        await asyncio.wait_for(operation, timeout=2)
                self.voice_client.disconnect.assert_not_awaited()
                current.disconnect.assert_not_awaited()
                self.assertIs(self.guild.voice_client, current)
                self.assertEqual(self.guard.home_channel_id(1), expected_home)

    async def test_stage_promotion_cancellation_is_propagated(self) -> None:
        self.prepare_stage_return()
        with patch.object(voice_guard_module, "enable_stage_speaker", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.connect_then_move()
        self.voice_client.disconnect.assert_not_awaited()
        self.assertEqual(self.guard.home_channel_id(1), HOME_ID)

    async def test_failed_stage_exit_is_logged_after_releasing_protection(self) -> None:
        self.prepare_stage_return()
        self.voice_client.disconnect.side_effect = RuntimeError("架空の切断失敗")
        with patch.object(voice_guard_module, "enable_stage_speaker", AsyncMock(side_effect=RuntimeError("架空の昇格失敗"))), \
                self.assertLogs(voice_guard_module.logger, level="ERROR") as log:
            await self.connect_then_move()
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guard.home_channel_id(1))
        self.assertTrue(any("退出に失敗" in record.getMessage() for record in log.records))

    # ---- 戻れない場合は退出し、無限に再試行しない ------------------------------

    async def test_unreturnable_moves_leave_the_moved_channel(self) -> None:
        cases = {
            "元のチャンネルの削除": lambda: self.channels.pop(HOME_ID),
            "接続権限の喪失": lambda: setattr(
                self.home.permissions_for.return_value, "connect", False),
            "発言権限の喪失": lambda: setattr(
                self.home.permissions_for.return_value, "speak", False),
            "ステージで発言者になれない": lambda: self.channels.__setitem__(
                HOME_ID, make_channel(HOME_ID, spec=discord.StageChannel)),
        }
        for name, break_return in cases.items():
            with self.subTest(case=name):
                await self.asyncSetUp()
                break_return()
                await self.connect_then_move()
                self.assert_left_without_return()

    async def test_failed_or_timed_out_return_leaves_instead_of_retrying(self) -> None:
        cases = {
            "移動の例外": AsyncMock(side_effect=discord.HTTPException(Mock(status=500), "架空の失敗")),
            # discord.py の move_to は時間切れでも例外を出さずに戻ることがある
            "時間切れで元へ戻れない": AsyncMock(return_value=None),
        }
        for name, move_to in cases.items():
            with self.subTest(case=name):
                await self.asyncSetUp()
                self.voice_client.move_to = move_to
                await self.connect_then_move()
                move_to.assert_awaited_once()
                self.voice_client.disconnect.assert_awaited_once_with(force=True)
                self.assertIsNone(self.guard.home_channel_id(1))
                # 退出の後に、どこかへ自動で入り直さない
                self.event(self.moved, None)
                await self.settle()
                self.assertIsNone(self.guard.home_channel_id(1))

    async def test_repeated_moves_stop_returning_after_the_limit(self) -> None:
        self.event(None, self.home)
        for _ in range(constants.VOICE_MOVE_GUARD_MAX_RETURNS):
            self.voice_client.channel = self.moved
            self.event(self.home, self.moved)
            await self.settle()
            self.assertIs(self.voice_client.channel, self.home)
        # /join の打ち直し（同じチャンネルの登録し直し）でも歯止めは外れない
        self.guard.adopt(1, HOME_ID)

        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        await self.settle()
        self.assertEqual(self.voice_client.move_to.await_count, constants.VOICE_MOVE_GUARD_MAX_RETURNS)
        self.voice_client.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guard.home_channel_id(1))

    async def test_returns_older_than_the_window_are_not_counted(self) -> None:
        clock = [1000.0]
        with patch.object(voice_guard_module, "monotonic", side_effect=lambda: clock[0]):
            self.event(None, self.home)
            for _ in range(constants.VOICE_MOVE_GUARD_MAX_RETURNS + 2):
                self.voice_client.channel = self.moved
                self.event(self.home, self.moved)
                await self.settle()
                clock[0] += constants.VOICE_MOVE_GUARD_WINDOW_SECONDS + 1
        self.assertEqual(self.voice_client.move_to.await_count, constants.VOICE_MOVE_GUARD_MAX_RETURNS + 2)
        self.voice_client.disconnect.assert_not_awaited()

    async def test_connection_that_never_settles_is_disconnected(self) -> None:
        self.voice_client.is_connected.return_value = False
        await self.connect_then_move()
        self.assert_left_without_return()

    async def test_unsettled_autonomous_connection_is_closed_by_the_autonomous_service(self) -> None:
        """移動後に接続が戻らなくても、切断の前に配信側の終了処理を通す。"""
        self.voice_client.is_connected.return_value = False
        self.autonomous.manages_connection.return_value = True

        async def finish_session(guild):
            self.assertIs(guild.voice_client, self.voice_client)
            self.voice_client.disconnect.assert_not_awaited()
            self.guard.release(guild.id)
            await self.voice_client.disconnect(force=True)

        self.autonomous.handle_external_move.side_effect = finish_session
        await self.connect_then_move()
        self.autonomous.handle_external_move.assert_awaited_once_with(self.guild)
        self.assert_left_without_return()

    async def test_unsettled_starting_connection_is_disconnected_after_external_move_handler(self) -> None:
        """開始待機中など配信側が終了を引き受けない場合も、未接続のまま残さない。"""
        self.voice_client.is_connected.return_value = False
        self.autonomous.manages_connection.return_value = True
        await self.connect_then_move()
        self.autonomous.handle_external_move.assert_awaited_once_with(self.guild)
        self.assert_left_without_return()

    async def test_unsettled_external_move_finishes_notice_session_and_owned_stage(self) -> None:
        """実サービスを通し、移動先の未接続状態でも締め・所有枠・配信記録を回収する。"""
        state = BotState()
        settings = state.get_guild(self.guild.id)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        settings.autonomous_session = AutonomousSession(
            channel_id=HOME_ID, is_stage=True, started_at=now,
            last_human_at=now, last_spoke_at=now,
            stage_started_by_bot=True, stage_instance_id=101,
        )
        self.home = make_channel(HOME_ID, spec=discord.StageChannel)
        self.home.instance = SimpleNamespace(id=101, delete=AsyncMock())
        self.channels[HOME_ID] = self.home
        self.client.get_channel = Mock(return_value=self.home)
        service = AutonomousService(self.client, state, None, None, None, None)
        self.guard = VoiceGuard(self.client, service)
        self.voice_client.is_connected.return_value = False
        original_disconnect = self.voice_client.disconnect.side_effect

        async def disconnect(*, force):
            await original_disconnect(force=force)
            self.event(self.moved, None)

        self.voice_client.disconnect.side_effect = disconnect
        notice = AsyncMock()
        with patch.object(autonomous_service, "send_long_message", notice):
            await self.connect_then_move()
        self.assertIsNone(settings.autonomous_session)
        notice.assert_awaited_once()
        self.assertEqual(notice.call_args.args, (self.home, constants.AUTONOMOUS_CLOSING_NOTICE_MOVED))
        self.assertFalse(notice.call_args.kwargs["allowed_mentions"].everyone)
        self.home.instance.delete.assert_awaited_once()
        self.assert_left_without_return()

    async def test_unsettled_old_snapshot_does_not_stop_a_new_connection_or_registration(self) -> None:
        """安定待ちの最終確認で接続・保護情報が交代したら、新しい配信も止めない。"""
        for change in ("replacement", "readopt"):
            with self.subTest(change=change):
                self.guard = VoiceGuard(self.client, self.autonomous)
                self.autonomous.manages_connection.return_value = True
                self.autonomous.handle_external_move.reset_mock()
                self.voice_client = self.make_voice_client(self.home)
                self.guild.voice_client = self.voice_client
                current = self.voice_client
                checks = 0

                def is_connected():
                    nonlocal checks, current
                    checks += 1
                    if checks == 2:
                        if change == "replacement":
                            current = self.make_voice_client(self.moved)
                            self.guild.voice_client = current
                        else:
                            self.guard.release(1)
                            self.guard.adopt(1, HOME_ID)
                    return False

                self.voice_client.is_connected.side_effect = is_connected
                await self.connect_then_move()
                self.autonomous.handle_external_move.assert_not_awaited()
                self.voice_client.disconnect.assert_not_awaited()
                current.disconnect.assert_not_awaited()
                self.assertEqual(self.guard.home_channel_id(1), HOME_ID)

    # ---- 自律行動と人の操作との競合 --------------------------------------------

    async def test_autonomous_connection_is_closed_by_the_autonomous_service(self) -> None:
        self.autonomous.manages_connection.return_value = True
        await self.connect_then_move()
        self.autonomous.handle_external_move.assert_awaited_once_with(self.guild)
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_leave_during_the_wait_cancels_the_return(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        # 保護処理が動き出す前に /leave された（保護を解いてから切断する）
        self.guard.release(1)
        await self.voice_client.disconnect(force=True)
        await self.settle()
        self.voice_client.move_to.assert_not_awaited()

    async def test_join_in_the_moved_channel_during_the_wait_keeps_the_bot_there(self) -> None:
        self.event(None, self.home)
        self.voice_client.channel = self.moved
        self.event(self.home, self.moved)
        # 移された先の利用者が /join した（同じチャンネルなので接続を使い続ける）
        self.guard.adopt(1, MOVED_ID)
        await self.settle()
        self.voice_client.move_to.assert_not_awaited()
        self.voice_client.disconnect.assert_not_awaited()

    async def test_connection_replaced_during_the_return_is_not_touched(self) -> None:
        replacement = self.make_voice_client(self.moved)

        async def replaced_by_join(target, *, timeout):
            # 戻している間に /join が切り替えた（保護を解き、新しい接続を登録）
            self.guard.release(1)
            self.guild.voice_client = replacement
            self.guard.adopt(1, MOVED_ID)

        self.voice_client.move_to = AsyncMock(side_effect=replaced_by_join)
        await self.connect_then_move()
        self.voice_client.disconnect.assert_not_awaited()
        replacement.disconnect.assert_not_awaited()
        replacement.move_to.assert_not_awaited()
        self.assertEqual(self.guard.home_channel_id(1), MOVED_ID)

    async def assert_join_during_return_is_preserved(
        self, *, replace_connection: bool = False, readopt_original: bool = False,
        fail: bool = True,
    ) -> None:
        entered = asyncio.Event()
        finish = asyncio.Event()

        async def pending_return(target, *, timeout):
            entered.set()
            await finish.wait()
            if fail:
                raise discord.HTTPException(Mock(status=500), "架空の移動失敗")

        self.voice_client.move_to = AsyncMock(side_effect=pending_return)
        operation = asyncio.create_task(self.connect_then_move())
        current = self.voice_client
        target = self.home if readopt_original else self.moved
        try:
            # 旧移動の開始を確かめてから、後続の /join 成功を再現する。
            await asyncio.wait_for(entered.wait(), timeout=1)
            if replace_connection:
                current = self.make_voice_client(target)
                self.guild.voice_client = current
            if readopt_original:
                # 同じチャンネルへ切り直しても、古い保護情報とは別の登録になる。
                self.guard.release(1)
                current.channel = target
            self.guard.adopt(1, target.id)
        finally:
            finish.set()
            await asyncio.wait_for(operation, timeout=1)

        self.voice_client.move_to.assert_awaited_once_with(
            self.home, timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS,
        )
        self.voice_client.disconnect.assert_not_awaited()
        current.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, current)
        self.assertIs(current.channel, target)
        self.assertEqual(self.guard.home_channel_id(1), target.id)

    async def test_failed_return_preserves_home_adopted_on_the_same_connection(self) -> None:
        await self.assert_join_during_return_is_preserved()

    async def test_failed_return_preserves_replacement_connection_and_home(self) -> None:
        await self.assert_join_during_return_is_preserved(replace_connection=True)

    async def test_failed_return_preserves_readopted_home_with_the_same_channel_id(self) -> None:
        await self.assert_join_during_return_is_preserved(readopt_original=True)

    async def test_timed_out_return_preserves_home_adopted_on_the_same_connection(self) -> None:
        # 例外を返さない時間切れでも、人が採用した移動先を優先する。
        await self.assert_join_during_return_is_preserved(fail=False)

    async def test_failed_return_preserves_replacement_with_unchanged_home(self) -> None:
        replacement = self.make_voice_client(self.moved)

        async def replaced_then_failed(target, *, timeout):
            # 保護先が同じでも、接続が違えば古い処理は登録を解いてはいけない。
            self.guild.voice_client = replacement
            raise discord.HTTPException(Mock(status=500), "架空の移動失敗")

        self.voice_client.move_to = AsyncMock(side_effect=replaced_then_failed)
        await self.connect_then_move()
        self.voice_client.disconnect.assert_not_awaited()
        replacement.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, replacement)
        self.assertEqual(self.guard.home_channel_id(1), HOME_ID)


if __name__ == "__main__":
    unittest.main()
