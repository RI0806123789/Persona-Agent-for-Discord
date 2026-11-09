"""退出したサーバーだけの一時状態を、停止処理の後に回収する。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState, _GuildSettingsLoad
from persona_bot.core.guild_lifecycle import prepare_guild_join, remove_guild_state


class GuildRemoveTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_are_inactive_before_stop_completes(self):
        state = BotState()
        old = state.get_guild(1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def stop(*args, **kwargs):
            entered.set()
            await release.wait()

        task = asyncio.create_task(remove_guild_state(
            1, state, SimpleNamespace(stop_session=stop), SimpleNamespace(release=Mock()),
        ))
        await entered.wait()
        self.assertFalse(old.is_active)
        self.assertIs(state.guild_settings[1], old)
        release.set()
        await task

    async def test_rejoin_during_each_stop_preserves_new_state_even_on_cancellation(self):
        for stage in ("autonomous", "voice"):
            for canceled in (False, True):
                with self.subTest(stage=stage, canceled=canceled):
                    state = BotState()
                    old = state.get_guild(1)
                    state.welcomed_guild_ids.add(1)
                    guard = SimpleNamespace(release=Mock())
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def stop(*args, **kwargs):
                        entered.set()
                        await release.wait()

                    autonomous = SimpleNamespace(stop_session=AsyncMock(
                        side_effect=stop if stage == "autonomous" else None,
                    ))
                    conversation = SimpleNamespace(stop=AsyncMock(
                        side_effect=stop if stage == "voice" else None,
                    ))
                    task = asyncio.create_task(remove_guild_state(1, state, autonomous, guard, conversation))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        prepare_guild_join(1, state, guard)
                        self.assertFalse(old.is_active)
                        restored = await state.get_guild_async(1)
                        self.assertIsNot(restored, old)
                        state.welcomed_guild_ids.add(1)
                        # 再参加時に旧保護を解除した後の、新しい接続保護を模擬する。
                        guard.release.reset_mock()
                        if canceled:
                            task.cancel()
                        release.set()
                        if canceled:
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        else:
                            await asyncio.wait_for(task, 2)
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                    self.assertIs(state.guild_settings[1], restored)
                    self.assertTrue(restored.is_active)
                    self.assertIn(1, state.welcomed_guild_ids)
                    guard.release.assert_not_called()
                    if stage == "autonomous":
                        conversation.stop.assert_not_awaited()
                    self.assertEqual(state.guild_removals, {})

    async def test_rejoin_without_settings_restoration_keeps_welcome_and_voice_guard(self):
        state = BotState()
        state.get_guild(1)
        guard = SimpleNamespace(release=Mock())

        async def rejoin(*args, **kwargs):
            prepare_guild_join(1, state, guard)
            state.welcomed_guild_ids.add(1)
            guard.release.reset_mock()

        await remove_guild_state(1, state, SimpleNamespace(stop_session=AsyncMock(side_effect=rejoin)), guard)
        self.assertEqual(state.guild_settings, {})
        self.assertIn(1, state.welcomed_guild_ids)
        guard.release.assert_not_called()
        self.assertEqual(state.guild_removals, {})

    async def test_new_settings_without_join_callback_are_not_removed(self):
        state = BotState()
        old = state.get_guild(1)
        guard = SimpleNamespace(release=Mock())

        async def replace(*args, **kwargs):
            state.forget_guild(1)
            state.get_guild(1)
            state.welcomed_guild_ids.add(1)

        conversation = SimpleNamespace(stop=AsyncMock())
        await remove_guild_state(1, state, SimpleNamespace(stop_session=AsyncMock(side_effect=replace)), guard, conversation)
        self.assertIsNot(state.get_guild(1), old)
        self.assertIn(1, state.welcomed_guild_ids)
        guard.release.assert_not_called()
        conversation.stop.assert_not_awaited()
        self.assertEqual(state.guild_removals, {})

    async def test_invalidated_load_is_dropped_so_new_load_proceeds_during_stop(self):
        """退出で無効にした読込を辞書に残さず、待機中の新しい読込要求を失敗させない（#411）。"""
        db = Mock()
        db.guild_state_load.return_value = ({}, {})
        state = BotState()
        state._db = db
        entered, release = asyncio.Event(), asyncio.Event()
        stale = _GuildSettingsLoad()
        state._guild_loads[1] = stale

        async def stop(*args, **kwargs):
            entered.set()
            await release.wait()

        task = asyncio.create_task(remove_guild_state(
            1, state, SimpleNamespace(stop_session=stop), SimpleNamespace(release=Mock()),
        ))
        await entered.wait()
        self.assertFalse(stale.valid)
        self.assertNotIn(1, state._guild_loads)
        settings = await asyncio.wait_for(state.get_guild_async(1), 2)
        self.assertIs(state.guild_settings[1], settings)
        release.set()
        await task
        # 開始時に未ロードだった退出では、待機中に作られた設定も回収される（孤児にしない）。
        self.assertNotIn(1, state.guild_settings)
        self.assertNotIn(1, state._guild_loads)

    async def test_get_guild_async_replaces_an_invalid_leftover_load(self):
        db = Mock()
        db.guild_state_load.return_value = ({}, {})
        state = BotState()
        state._db = db
        leftover = _GuildSettingsLoad()
        leftover.valid = False
        state._guild_loads[1] = leftover
        settings = await asyncio.wait_for(state.get_guild_async(1), 2)
        self.assertIs(state.guild_settings[1], settings)

    async def test_only_removed_guild_is_released_after_stop_even_on_failure(self):
        for error in (None, OSError("架空の障害"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                state = BotState()
                removed, other, dm = state.get_guild(1), state.get_guild(2), state.get_guild(None)
                state.welcomed_guild_ids.update((1, 2))
                state._db = Mock()

                async def stop(*args, **kwargs):
                    self.assertIs(state.guild_settings[1], removed)
                    if error is not None:
                        raise error

                autonomous = SimpleNamespace(stop_session=AsyncMock(side_effect=stop))
                guard = SimpleNamespace(release=Mock())
                with patch("persona_bot.core.guild_lifecycle.logger"):
                    if isinstance(error, asyncio.CancelledError):
                        with self.assertRaises(asyncio.CancelledError):
                            await remove_guild_state(1, state, autonomous, guard)
                    else:
                        await remove_guild_state(1, state, autonomous, guard)
                autonomous.stop_session.assert_awaited_once_with(1, reason="サーバーからの退出")
                guard.release.assert_called_once_with(1)
                self.assertNotIn(1, state.guild_settings)
                self.assertIs(state.guild_settings[2], other)
                self.assertIs(state.dm_settings, dm)
                self.assertEqual(state.welcomed_guild_ids, {2})
                self.assertEqual(state._db.mock_calls, [])

    async def test_voice_conversation_is_stopped_even_if_it_fails(self):
        """音声会話（#268）もそのサーバー分を止め、失敗しても設定の回収は続ける。"""
        for error in (None, OSError("架空の障害")):
            with self.subTest(error=type(error).__name__):
                state = BotState()
                state.get_guild(1)
                autonomous = SimpleNamespace(stop_session=AsyncMock())
                guard = SimpleNamespace(release=Mock())
                conversation = SimpleNamespace(stop=AsyncMock(side_effect=error))
                with patch("persona_bot.core.guild_lifecycle.logger"):
                    await remove_guild_state(1, state, autonomous, guard, conversation)
                conversation.stop.assert_awaited_once_with(1, reason="サーバーからの退出")
                guard.release.assert_called_once_with(1)
                self.assertNotIn(1, state.guild_settings)

    async def test_unknown_or_duplicate_removal_does_not_leave_settings(self):
        state = BotState()

        async def stop(guild_id, **kwargs):
            state.get_guild(guild_id)

        autonomous = SimpleNamespace(stop_session=AsyncMock(side_effect=stop))
        for _ in range(2):
            await remove_guild_state(1, state, autonomous, Mock())
            self.assertEqual(state.guild_settings, {})
