"""/join の権限確認・接続の再利用・接続待ち時間を確かめる（#67・#87）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config import constants
from persona_bot.core.app_state import BotState
from persona_bot.core.commands import register_commands
from persona_bot.core import bot_client
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.voice_commands import VoiceCommandCoordinator
from persona_bot.services import autonomous_stage
from tests.helpers.network_guard import block_network
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class JoinCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        block_network(self)
        self.commands = {}

        def command(**kwargs):
            def register(callback):
                self.commands[kwargs["name"]] = callback
                return callback
            return register

        self.autonomous = SimpleNamespace(stop_session=AsyncMock(return_value=False))
        self.voice_guard = Mock(name="voice_guard")
        self.tree = SimpleNamespace(command=command)
        self.coordinator = VoiceCommandCoordinator()
        self.addCleanup(lambda: self.assertEqual(self.coordinator._guilds, {}))
        with patch("persona_bot.core.commands.VoiceCommandCoordinator", return_value=self.coordinator):
            register_commands(self.tree, BotState(), None, None,
                              SimpleNamespace(is_blocked=Mock(return_value=False)),
                              None, None, None, None, self.autonomous, voice_guard=self.voice_guard)
        self.channel = self.make_channel(20)
        self.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.interaction = SimpleNamespace(
            guild=self.guild,
            command=SimpleNamespace(name="join"),
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    @staticmethod
    def make_channel(channel_id: int) -> Mock:
        channel = Mock(spec=discord.VoiceChannel, id=channel_id)
        channel.name = f"架空の通話{channel_id}"
        channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True)
        channel.connect = AsyncMock()
        return channel

    @staticmethod
    def make_voice_client(channel: Mock, *, connected: bool = True) -> Mock:
        voice_client = Mock(spec=discord.VoiceClient)
        voice_client.channel = channel
        voice_client.is_connected.return_value = connected
        voice_client.disconnect = AsyncMock()
        return voice_client

    async def join(self) -> str:
        await self.commands["join"](self.interaction)
        return self.interaction.followup.send.call_args.args[0]

    async def test_same_channel_keeps_the_current_connection(self) -> None:
        voice_client = self.make_voice_client(self.channel)
        self.guild.voice_client = voice_client
        reply = await self.join()
        # 自律行動の配信の後片付けは従来どおり先に行う
        self.autonomous.stop_session.assert_awaited_once()
        voice_client.disconnect.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        self.assertIn("既に接続しています", reply)

    async def test_requester_moves_or_leaves_while_stopping_autonomous_session(self) -> None:
        for destination in (self.make_channel(30), None):
            with self.subTest(destination=destination):
                self.interaction.user.voice = SimpleNamespace(channel=self.channel)
                self.interaction.followup.send.reset_mock()
                existing = self.make_voice_client(self.make_channel(40))
                self.guild.voice_client = existing

                async def stop(*args, **kwargs):
                    self.interaction.user.voice = SimpleNamespace(channel=destination) if destination else None

                self.autonomous.stop_session.side_effect = stop
                await self.commands["join"](self.interaction)
                existing.disconnect.assert_not_awaited()
                self.channel.connect.assert_not_awaited()
                if destination:
                    destination.connect.assert_not_awaited()
                self.assertIn("もう一度 /join", self.interaction.followup.send.await_args.args[0])

    async def test_requester_moves_during_disconnect(self) -> None:
        existing = self.make_voice_client(self.make_channel(40))
        self.guild.voice_client = existing

        async def disconnect(**kwargs):
            self.interaction.user.voice.channel = self.make_channel(30)
            self.guild.voice_client = None

        existing.disconnect.side_effect = disconnect
        await self.commands["join"](self.interaction)
        self.channel.connect.assert_not_awaited()
        self.assertIn("もう一度 /join", self.interaction.followup.send.await_args.args[0])

    async def test_requester_moves_while_deferring_response(self) -> None:
        async def defer():
            self.interaction.user.voice.channel = self.make_channel(30)

        self.interaction.response.defer.side_effect = defer
        await self.commands["join"](self.interaction)
        self.autonomous.stop_session.assert_not_awaited()
        self.channel.connect.assert_not_awaited()
        self.assertIn("もう一度 /join", self.interaction.followup.send.await_args.args[0])

    async def test_channel_name_mentions_cannot_notify_other_users_or_roles(self) -> None:
        self.channel.name = "@everyone @here <@&22> <@33>"
        for outcome in ("connected", "already_connected", "missing_permissions", "forbidden"):
            with self.subTest(outcome=outcome):
                self.interaction.response.send_message.reset_mock()
                self.interaction.followup.send.reset_mock()
                self.guild.voice_client = self.make_voice_client(self.channel) if outcome == "already_connected" else None
                self.channel.permissions_for.return_value.connect = outcome != "missing_permissions"
                self.channel.connect.side_effect = (
                    discord.Forbidden(SimpleNamespace(status=403, reason="禁止"), "架空の拒否")
                    if outcome == "forbidden" else None
                )
                await self.commands["join"](self.interaction)
                send = (self.interaction.response.send_message if outcome == "missing_permissions"
                        else self.interaction.followup.send)
                send.assert_awaited_once()
                self.assertIn(self.channel.name, send.await_args.args[0])
                self.assertEqual(send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})

    async def test_other_channel_or_dropped_connection_reconnects_with_timeout(self) -> None:
        cases = {
            "別のチャンネル": self.make_voice_client(self.make_channel(30)),
            "切れた接続": self.make_voice_client(self.channel, connected=False),
        }
        for name, voice_client in cases.items():
            with self.subTest(case=name):
                self.channel.connect.reset_mock()
                self.guild.voice_client = voice_client
                reply = await self.join()
                voice_client.disconnect.assert_awaited_once_with(force=True)
                self.channel.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS, cls=ANY)
                self.assertIn("に接続しました", reply)

    async def test_join_registers_the_chosen_channel_as_the_intended_destination(self) -> None:
        # 初回の接続・同じチャンネルの再利用のどちらも、人が選んだ先を意図した接続先にする（#171）
        await self.join()
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        self.voice_guard.adopt.reset_mock()
        self.guild.voice_client = self.make_voice_client(self.channel)
        await self.join()
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        self.voice_guard.release.assert_not_called()

    async def test_switching_releases_the_guard_before_disconnecting(self) -> None:
        old = self.make_voice_client(self.make_channel(30))
        self.guild.voice_client = old
        order = Mock()
        order.attach_mock(self.voice_guard.release, "release")
        order.attach_mock(old.disconnect, "disconnect")
        order.attach_mock(self.voice_guard.adopt, "adopt")
        await self.join()
        self.assertEqual([call[0] for call in order.mock_calls], ["release", "disconnect", "adopt"])

    async def test_failed_join_does_not_register_a_destination(self) -> None:
        self.channel.connect.side_effect = asyncio.TimeoutError
        await self.join()
        self.voice_guard.adopt.assert_not_called()

    async def test_join_errors_hide_internal_exception_details(self) -> None:
        for error in (discord.ClientException("架空の内部設定情報"), RuntimeError("架空の内部設定情報")):
            with self.subTest(error=type(error).__name__):
                self.interaction.followup.send.reset_mock()
                self.channel.connect.side_effect = error
                with self.assertLogs("persona_bot.core.voice_commands", level="WARNING") as logs:
                    reply = await self.join()
                self.assertNotIn("架空の内部設定情報", reply)
                self.assertNotIn(type(error).__name__, reply)
                log_text = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
                self.assertNotIn("架空の内部設定情報", log_text)
                self.assertIn(type(error).__name__, log_text)
                self.assertTrue(any(record.exc_info and record.exc_info[1] is error for record in logs.records))

    def use_sdk_voice_cache(self):
        """Discord SDKの登録・解除を通し、外部への接続だけを差し替える。"""
        client = discord.Client(intents=discord.Intents.none())
        client._connection.loop = asyncio.get_running_loop()
        state = client._connection

        class CachedGuild:
            id = 1
            me = object()

            @property
            def voice_client(self):
                return state._get_voice_client(self.id)

        self.guild = CachedGuild()
        self.interaction.guild = self.guild
        self.channel.guild = self.guild
        self.channel._state = state
        self.channel._get_voice_client_key.return_value = (self.guild.id, "guild_id")

        async def connect(**kwargs):
            return await discord.abc.Connectable.connect(self.channel, **kwargs)

        async def close():
            # テスト専用のキャッシュだけを空にし、実接続の切断へ進ませない。
            state._voice_clients.clear()
            await client.close()

        self.channel.connect.side_effect = connect
        self.addAsyncCleanup(close)
        return state

    async def test_failed_connect_cleans_sdk_cache_even_when_disconnect_fails(self) -> None:
        self.use_sdk_voice_cache()
        errors = (asyncio.TimeoutError(), discord.Forbidden(Mock(status=403), "架空の拒否"),
                  discord.ClientException("架空の状態不整合"), RuntimeError("架空の接続失敗"))
        for error in errors:
            for disconnect_fails in (False, True):
                with self.subTest(error=type(error).__name__, disconnect_fails=disconnect_fails):
                    created, disconnected = [], []

                    async def fail(voice, **kwargs):
                        created.append(voice)
                        raise error

                    async def disconnect(voice, *, force):
                        self.assertTrue(force)
                        disconnected.append(voice)
                        if disconnect_fails:
                            raise OSError("架空の切断失敗")
                        voice.cleanup()

                    self.voice_guard.reset_mock()
                    with patch.object(discord.VoiceClient, "connect", new=fail), patch.object(
                        discord.VoiceClient, "disconnect", new=disconnect,
                    ):
                        await self.join()
                    self.assertIsNone(self.guild.voice_client)
                    self.assertTrue(disconnected)
                    self.assertTrue(all(voice is created[0] for voice in disconnected))
                    self.voice_guard.adopt.assert_not_called()
                    # キャッシュが片付いたので、次の接続をSDKに拒否されない。
                    async def succeed(voice, **kwargs):
                        return None

                    with patch.object(discord.VoiceClient, "connect", new=succeed):
                        await self.join()
                    self.assertIsNotNone(self.guild.voice_client)
                    self.guild.voice_client.cleanup()

    async def test_failed_connect_does_not_clean_replacement_or_healthy_connection(self) -> None:
        state = self.use_sdk_voice_cache()
        for replacement_connected in (False, True, None):
            with self.subTest(replacement_connected=replacement_connected):
                replacement = self.make_voice_client(self.channel, connected=bool(replacement_connected))

                async def fail(voice, **kwargs):
                    if replacement_connected is None:
                        # 例外時点では、この試行の接続自身が既に正常につながっている。
                        voice.is_connected = Mock(return_value=True)
                    else:
                        state._add_voice_client(self.guild.id, replacement)
                    raise RuntimeError("架空の接続後のエラー")

                self.voice_guard.reset_mock()
                with patch.object(discord.VoiceClient, "connect", new=fail), patch.object(
                    discord.VoiceClient, "disconnect", new=AsyncMock(),
                ) as disconnect:
                    await self.join()
                disconnect.assert_not_awaited()
                self.voice_guard.release.assert_not_called()
                if replacement_connected is not None:
                    self.assertIs(self.guild.voice_client, replacement)
                self.assertIsNotNone(self.guild.voice_client)
                state._voice_clients.clear()

    async def test_connect_rejected_before_creation_keeps_existing_connection(self) -> None:
        state = self.use_sdk_voice_cache()
        replacement = self.make_voice_client(self.channel)

        async def connect(**kwargs):
            # 接続開始の直前に別経路の接続が登録された場合、SDKは生成前に拒否する。
            state._add_voice_client(self.guild.id, replacement)
            return await discord.abc.Connectable.connect(self.channel, **kwargs)

        self.channel.connect.side_effect = connect
        with patch.object(discord.VoiceClient, "disconnect", new=AsyncMock()) as disconnect:
            await self.join()
        self.assertIs(self.guild.voice_client, replacement)
        replacement.disconnect.assert_not_awaited()
        disconnect.assert_not_awaited()
        self.voice_guard.release.assert_not_called()

    async def test_cleanup_failure_does_not_remove_connection_replaced_during_wait(self) -> None:
        state = self.use_sdk_voice_cache()
        entered, release = asyncio.Event(), asyncio.Event()
        replacement = self.make_voice_client(self.make_channel(30))

        async def fail(voice, **kwargs):
            raise RuntimeError("架空の接続失敗")

        async def disconnect(voice, *, force):
            entered.set()
            await release.wait()
            raise OSError("架空の切断失敗")

        with patch.object(discord.VoiceClient, "connect", new=fail), patch.object(
            discord.VoiceClient, "disconnect", new=disconnect,
        ):
            task = asyncio.create_task(self.commands["join"](self.interaction))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                state._add_voice_client(self.guild.id, replacement)
                self.voice_guard.adopt(1, 30)
                release.set()
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertIs(self.guild.voice_client, replacement)
        replacement.disconnect.assert_not_awaited()
        self.voice_guard.release.assert_called_once_with(1)

    async def test_failed_connect_cleanup_waits_through_repeated_cancellation(self) -> None:
        self.use_sdk_voice_cache()
        entered, release = asyncio.Event(), asyncio.Event()

        async def fail(voice, **kwargs):
            raise RuntimeError("架空の接続失敗")

        async def disconnect(voice, *, force):
            entered.set()
            await release.wait()
            voice.cleanup()

        with patch.object(discord.VoiceClient, "connect", new=fail), patch.object(
            discord.VoiceClient, "disconnect", new=disconnect,
        ):
            task = asyncio.create_task(self.commands["join"](self.interaction))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertIsNotNone(self.guild.voice_client)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertIsNone(self.guild.voice_client)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_leave_releases_the_guard_before_disconnecting(self) -> None:
        current = self.make_voice_client(self.channel)
        self.guild.voice_client = current
        order = Mock()
        order.attach_mock(self.voice_guard.release, "release")
        order.attach_mock(current.disconnect, "disconnect")
        await self.commands["leave"](self.interaction)
        self.assertEqual([call[0] for call in order.mock_calls], ["release", "disconnect"])
        self.voice_guard.release.assert_called_once_with(1)

    async def test_first_join_connects_with_timeout(self) -> None:
        reply = await self.join()
        self.channel.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS, cls=ANY)
        self.assertIn("に接続しました", reply)

    async def test_disconnect_failure_sends_followup_without_connecting(self) -> None:
        for connected in (True, False):
            with self.subTest(connected=connected):
                self.interaction.followup.send.reset_mock()
                old = self.make_voice_client(self.make_channel(30), connected=connected)
                old.disconnect.side_effect = OSError("架空の切断失敗")
                self.guild.voice_client = old
                reply = await self.join()
                old.disconnect.assert_awaited_once_with(force=True)
                self.interaction.followup.send.assert_awaited_once()
                self.assertIn("切り替えられませんでした", reply)
                self.assertNotIn("架空の切断失敗", reply)
                self.channel.connect.assert_not_awaited()

    def another_interaction(self, *, guild=None, channel=None, name="join"):
        return SimpleNamespace(
            guild=guild if guild is not None else self.guild,
            command=SimpleNamespace(name=name),
            user=Mock(spec=discord.Member, voice=SimpleNamespace(channel=channel or self.channel)),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=Mock(return_value=False)),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def connect_to_guild(self, guild, channel, *, started=None, release=None):
        """接続・切断に応じて架空サーバーの唯一の接続参照を更新する。"""
        voice = self.make_voice_client(channel)

        async def connect(**kwargs):
            if started is not None:
                started.set()
                await release.wait()
            guild.voice_client = voice
            return voice

        async def disconnect(**kwargs):
            guild.voice_client = None
            voice.is_connected.return_value = False

        channel.connect.side_effect = connect
        voice.disconnect.side_effect = disconnect
        return voice

    async def test_leave_during_join_defer_prevents_late_connection(self):
        """配信がなくても、応答待ちの参加が退出完了後に接続しない。"""
        entered, release = asyncio.Event(), asyncio.Event()

        async def defer():
            entered.set()
            await release.wait()

        self.interaction.response.defer.side_effect = defer
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            leaving = self.another_interaction()
            await asyncio.wait_for(self.commands["leave"](leaving), 2)
            self.assertIn("参加していません", leaving.followup.send.call_args.args[0])
            release.set()
            await asyncio.wait_for(joining, 2)
        finally:
            release.set()
            await asyncio.gather(joining, return_exceptions=True)
        self.channel.connect.assert_not_awaited()
        self.assertIn("後から受け付けた", self.interaction.followup.send.call_args.args[0])
        self.voice_guard.adopt.assert_not_called()
        await self.commands["join"](self.another_interaction())
        self.channel.connect.assert_awaited_once()

    async def test_leave_during_autonomous_stop_invalidates_waiting_join(self):
        """配信停止を待つ参加は、後着の退出を受け付けたら接続を見送る。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stop(*args, **kwargs):
            if self.autonomous.stop_session.await_count == 1:
                entered.set()
                await release.wait()
            return False

        self.autonomous.stop_session.side_effect = stop
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            self.assertFalse(exiting.done())
            self.assertEqual(self.autonomous.stop_session.await_count, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(joining, exiting), 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        self.channel.connect.assert_not_awaited()
        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(self.autonomous.stop_session.await_count, 2)

    async def test_leave_during_connect_waits_then_disconnects_that_connection(self):
        """接続開始後の退出は、参加の採用・案内を待ってから確実に切断する。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        order = Mock()
        order.attach_mock(self.voice_guard.adopt, "adopt")
        order.attach_mock(self.interaction.followup.send, "joined")
        order.attach_mock(self.voice_guard.release, "release")
        order.attach_mock(voice.disconnect, "disconnect")
        order.attach_mock(leaving.followup.send, "left")
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            voice.disconnect.assert_not_awaited()
            self.assertFalse(exiting.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(joining, exiting), 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        self.channel.connect.assert_awaited_once()
        voice.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guild.voice_client)
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
        leaving.followup.send.assert_awaited_once_with("退出しました。")
        self.assertEqual(
            [entry[0] for entry in order.mock_calls],
            ["adopt", "joined", "release", "disconnect", "left"],
        )

    async def test_newer_defer_dropout_after_connect_keeps_join_adopted_and_announced(self):
        """接続完了時に後続が応答待ちでも参加を採用・案内し、その後の脱落で失わない。"""
        for name in ("join", "leave"):
            for dropout in ("failure", "cancellation"):
                with self.subTest(command=name, dropout=dropout):
                    self.guild.voice_client = None
                    self.voice_guard.reset_mock()
                    self.autonomous.stop_session.reset_mock()
                    self.channel.connect.reset_mock()
                    self.interaction.followup.send.reset_mock()
                    connected, release_connect = asyncio.Event(), asyncio.Event()
                    voice = self.connect_to_guild(
                        self.guild, self.channel, started=connected, release=release_connect,
                    )
                    other_channel = self.make_channel(30)
                    newest = self.another_interaction(channel=other_channel, name=name)
                    deferring, release_defer = asyncio.Event(), asyncio.Event()

                    async def defer():
                        deferring.set()
                        await release_defer.wait()
                        raise OSError("架空の応答失敗")

                    newest.response.defer.side_effect = defer
                    tasks = [asyncio.create_task(self.commands["join"](self.interaction))]
                    try:
                        await asyncio.wait_for(connected.wait(), 2)
                        operations = self.coordinator._guilds[self.guild.id]
                        tasks.append(asyncio.create_task(self.commands[name](newest)))
                        await asyncio.wait_for(deferring.wait(), 2)
                        release_connect.set()
                        await asyncio.wait_for(tasks[0], 2)
                        # 後続の応答が決着する前に、接続の採用と成功案内を済ませる。
                        self.assertFalse(tasks[1].done())
                        self.assertIs(self.guild.voice_client, voice)
                        self.voice_guard.adopt.assert_called_once_with(1, 20)
                        self.interaction.followup.send.assert_awaited_once()
                        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
                        self.assertEqual(operations.pending, 1)
                        self.assertEqual(set(operations.tokens), operations.deferring)
                        self.assertEqual(len(operations.deferring), 1)
                        self.assertFalse(operations.lock.locked())
                        if dropout == "cancellation":
                            tasks[1].cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(tasks[1], 2)
                            newest.response.send_message.assert_not_awaited()
                        else:
                            release_defer.set()
                            await asyncio.wait_for(tasks[1], 2)
                            newest.response.send_message.assert_awaited_once()
                            self.assertTrue(newest.response.send_message.call_args.kwargs["ephemeral"])
                    finally:
                        release_connect.set()
                        release_defer.set()
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    self.assertIs(self.guild.voice_client, voice)
                    voice.disconnect.assert_not_awaited()
                    self.voice_guard.release.assert_not_called()
                    self.voice_guard.adopt.assert_called_once_with(1, 20)
                    self.interaction.followup.send.assert_awaited_once()
                    self.channel.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS, cls=ANY)
                    other_channel.connect.assert_not_awaited()
                    self.autonomous.stop_session.assert_awaited_once()
                    newest.followup.send.assert_not_awaited()
                    self.assertEqual(operations.tokens, [])
                    self.assertEqual(operations.deferring, set())
                    self.assertIsNone(operations.latest)
                    self.assertEqual(operations.pending, 0)
                    self.assertFalse(operations.lock.locked())
                    self.assertEqual(self.coordinator._guilds, {})

    async def test_canceled_leave_before_lock_restores_connecting_join(self):
        """ロック前に退出が脱落したら、接続中の参加を採用して案内する。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            operations = self.coordinator._guilds[self.guild.id]
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            exiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(exiting, 2)
            release.set()
            await asyncio.wait_for(joining, 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        self.assertIs(self.guild.voice_client, voice)
        voice.disconnect.assert_not_awaited()
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
        leaving.followup.send.assert_not_awaited()
        self.assertEqual(operations.tokens, [])
        self.assertIsNone(operations.latest)
        self.assertEqual(operations.pending, 0)
        self.assertEqual(self.coordinator._guilds, {})

    async def test_failed_leave_defer_restores_connecting_join(self):
        """退出の応答保留に失敗しても、接続中の参加を無効にし続けない。"""
        entered, release = asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        leaving = self.another_interaction(name="leave")
        leaving.response.defer.side_effect = OSError("架空の応答失敗")
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.commands["leave"](leaving)
            leaving.response.send_message.assert_awaited_once()
            self.assertTrue(leaving.response.send_message.call_args.kwargs["ephemeral"])
            release.set()
            await asyncio.wait_for(joining, 2)
        finally:
            release.set()
            await asyncio.gather(joining, return_exceptions=True)
        self.assertIs(self.guild.voice_client, voice)
        voice.disconnect.assert_not_awaited()
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])

    async def test_canceled_newest_leave_keeps_previous_waiting_leave_current(self):
        """最新の退出だけが脱落しても、その前の有効な退出へ世代を戻す。"""
        entered, release = asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exits = []
        leaves = []
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for _ in range(2):
                deferred = asyncio.Event()
                leaving = self.another_interaction()
                leaving.response.defer.side_effect = lambda: deferred.set()
                leaves.append(leaving)
                exits.append(asyncio.create_task(self.commands["leave"](leaving)))
                await asyncio.wait_for(deferred.wait(), 2)
            exits[-1].cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(exits[-1], 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(joining, exits[0]), 2)
        finally:
            release.set()
            await asyncio.gather(joining, *exits, return_exceptions=True)
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
        self.voice_guard.adopt.assert_called_once_with(1, 20)
        leaves[0].followup.send.assert_awaited_once_with("退出しました。")
        leaves[1].followup.send.assert_not_awaited()
        voice.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guild.voice_client)

    async def test_newer_defer_dropout_preserves_waiting_join_and_leave(self):
        """先行接続の後でCの応答が失敗・中断しても、待機していたBの参加・退出を実行する。"""
        for name in ("join", "leave"):
            for error in (OSError("架空の応答失敗"), asyncio.CancelledError()):
                with self.subTest(command=name, error=type(error).__name__):
                    self.guild.voice_client = None
                    self.voice_guard.reset_mock()
                    self.interaction.followup.send.reset_mock()
                    connected, release_connect = asyncio.Event(), asyncio.Event()
                    old = self.connect_to_guild(
                        self.guild, self.channel, started=connected, release=release_connect,
                    )
                    other_channel = self.make_channel(30)
                    current = self.connect_to_guild(self.guild, other_channel)
                    waiting = self.another_interaction(channel=other_channel, name=name)
                    queued, deferring, release_defer = asyncio.Event(), asyncio.Event(), asyncio.Event()
                    waiting.response.defer.side_effect = queued.set
                    newest = self.another_interaction(name="leave")

                    async def defer():
                        deferring.set()
                        await release_defer.wait()
                        raise error

                    newest.response.defer.side_effect = defer
                    tasks = [asyncio.create_task(self.commands["join"](self.interaction))]
                    try:
                        await asyncio.wait_for(connected.wait(), 2)
                        operations = self.coordinator._guilds[self.guild.id]
                        tasks.append(asyncio.create_task(self.commands[name](waiting)))
                        await asyncio.wait_for(queued.wait(), 2)
                        tasks.append(asyncio.create_task(self.commands["leave"](newest)))
                        await asyncio.wait_for(deferring.wait(), 2)
                        release_connect.set()
                        await asyncio.wait_for(tasks[0], 2)
                        self.voice_guard.adopt.assert_called_once_with(1, 20)
                        self.interaction.followup.send.assert_awaited_once()
                        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
                        self.assertFalse(tasks[1].done())
                        waiting.followup.send.assert_not_awaited()
                        release_defer.set()
                        if isinstance(error, asyncio.CancelledError):
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(tasks[2], 2)
                        else:
                            await asyncio.wait_for(tasks[2], 2)
                            newest.response.send_message.assert_awaited_once()
                        await asyncio.wait_for(tasks[1], 2)
                    finally:
                        release_connect.set()
                        release_defer.set()
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    old.disconnect.assert_awaited_once_with(force=True)
                    waiting.followup.send.assert_awaited_once()
                    if name == "leave":
                        self.assertIsNone(self.guild.voice_client)
                        other_channel.connect.assert_not_awaited()
                        waiting.followup.send.assert_awaited_once_with("退出しました。")
                    else:
                        self.assertIs(self.guild.voice_client, current)
                        other_channel.connect.assert_awaited_once()
                        self.assertEqual(self.voice_guard.adopt.call_args_list, [call(1, 20), call(1, 30)])
                    self.assertEqual(operations.tokens, [])
                    self.assertEqual(operations.deferring, set())
                    self.assertEqual(operations.pending, 0)
                    self.assertEqual(self.coordinator._guilds, {})

    async def test_newest_cancellation_during_skip_notice_preserves_waiting_leave(self):
        """見送り通知の送信中にCが脱落しても、Bの退出が先行接続を確実に切る。"""
        connected, release_connect = asyncio.Event(), asyncio.Event()
        old = self.connect_to_guild(self.guild, self.channel, started=connected, release=release_connect)
        waiting = self.another_interaction(name="leave")
        newest = self.another_interaction(name="join")
        queued, newest_queued = asyncio.Event(), asyncio.Event()
        waiting.response.defer.side_effect = queued.set
        newest.response.defer.side_effect = newest_queued.set
        entered, release_notice = asyncio.Event(), asyncio.Event()
        tasks = [asyncio.create_task(self.commands["join"](self.interaction))]

        async def send(content):
            if "後から受け付けた" in content:
                tasks[2].cancel()
                entered.set()
                await release_notice.wait()

        waiting.followup.send.side_effect = send
        try:
            await asyncio.wait_for(connected.wait(), 2)
            tasks.append(asyncio.create_task(self.commands["leave"](waiting)))
            await asyncio.wait_for(queued.wait(), 2)
            tasks.append(asyncio.create_task(self.commands["join"](newest)))
            await asyncio.wait_for(newest_queued.wait(), 2)
            release_connect.set()
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[2], 2)
            old.disconnect.assert_not_awaited()
            release_notice.set()
            await asyncio.wait_for(asyncio.gather(*tasks[:2]), 2)
        finally:
            release_connect.set()
            release_notice.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        old.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.guild.voice_client)
        self.assertEqual(waiting.followup.send.await_count, 2)
        self.assertEqual(waiting.followup.send.call_args.args[0], "退出しました。")
        newest.followup.send.assert_not_awaited()

    async def test_later_join_during_connect_switches_after_adopting_and_announcing_first_join(self):
        """先行参加を採用・案内してから、別チャンネルへの後続参加が接続を切り替える。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        other_channel = self.make_channel(30)
        current = self.connect_to_guild(self.guild, other_channel)
        later = self.another_interaction(channel=other_channel)
        later.response.defer.side_effect = lambda: deferred.set()
        order = Mock()
        order.attach_mock(self.voice_guard.adopt, "adopt")
        order.attach_mock(self.interaction.followup.send, "joined")
        order.attach_mock(self.voice_guard.release, "release")
        order.attach_mock(old.disconnect, "disconnect")
        order.attach_mock(other_channel.connect, "connect")
        order.attach_mock(later.followup.send, "moved")
        first = asyncio.create_task(self.commands["join"](self.interaction))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(self.commands["join"](later))
            await asyncio.wait_for(deferred.wait(), 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
        old.disconnect.assert_awaited_once_with(force=True)
        self.assertIs(self.guild.voice_client, current)
        self.assertEqual(self.voice_guard.adopt.call_args_list, [call(1, 20), call(1, 30)])
        later.followup.send.assert_awaited_once()
        self.assertIn("に接続しました", later.followup.send.call_args.args[0])
        self.assertEqual(
            [entry[0] for entry in order.mock_calls],
            ["adopt", "joined", "release", "disconnect", "connect", "adopt", "moved"],
        )

    async def test_concurrent_joins_recheck_and_reuse_the_finished_connection(self):
        """同時参加は接続を重複生成せず、ロック取得後に既存接続を取り直す。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        later = self.another_interaction()
        later.response.defer.side_effect = lambda: deferred.set()
        first = asyncio.create_task(self.commands["join"](self.interaction))
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second = asyncio.create_task(self.commands["join"](later))
            await asyncio.wait_for(deferred.wait(), 2)
            self.assertFalse(second.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
        self.channel.connect.assert_awaited_once()
        voice.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, voice)
        self.assertIn("既に接続", later.followup.send.call_args.args[0])

    async def test_other_guild_can_join_while_one_guild_is_connecting(self):
        """一方のサーバーの接続待ちは別サーバーの操作を妨げない。"""
        entered, release = asyncio.Event(), asyncio.Event()
        self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        other_guild = SimpleNamespace(id=2, me=object(), voice_client=None)
        other_channel = self.make_channel(30)
        other_voice = self.connect_to_guild(other_guild, other_channel)
        first = asyncio.create_task(self.commands["join"](self.interaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(self.commands["join"](self.another_interaction(
                guild=other_guild, channel=other_channel,
            )), 2)
            self.assertIs(other_guild.voice_client, other_voice)
            self.assertFalse(first.done())
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)

    async def test_canceled_connect_keeps_lock_until_settled_then_leave_can_finish(self):
        """接続中の繰り返しキャンセルでも、退出は接続完了より先に終わらない。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        voice = self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            for _ in range(2):
                joining.cancel()
                await asyncio.sleep(0)
                self.assertFalse(joining.done())
                self.assertFalse(exiting.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(joining, 2)
            await asyncio.wait_for(exiting, 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        voice.disconnect.assert_awaited_once()
        self.assertIsNone(self.guild.voice_client)

    async def test_leave_during_switch_disconnect_prevents_reconnect(self):
        """既存接続の切断待ちで退出を受け付けたら、切断後に新規接続しない。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old = self.make_voice_client(self.make_channel(30))
        self.guild.voice_client = old

        async def disconnect(**kwargs):
            entered.set()
            await release.wait()
            self.guild.voice_client = None

        old.disconnect.side_effect = disconnect
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            release.set()
            await asyncio.wait_for(asyncio.gather(joining, exiting), 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        old.disconnect.assert_awaited_once()
        self.channel.connect.assert_not_awaited()
        self.assertIsNone(self.guild.voice_client)

    async def test_canceled_leave_keeps_lock_until_disconnect_finishes(self):
        """退出中の繰り返しキャンセルでも、切断終了後に次の参加を始める。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        old = self.make_voice_client(self.channel)
        self.guild.voice_client = old

        async def disconnect(**kwargs):
            entered.set()
            await release.wait()
            self.guild.voice_client = None

        old.disconnect.side_effect = disconnect
        joining = self.another_interaction()
        joining.response.defer.side_effect = lambda: deferred.set()
        exiting = asyncio.create_task(self.commands["leave"](self.interaction))
        entering = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            entering = asyncio.create_task(self.commands["join"](joining))
            await asyncio.wait_for(deferred.wait(), 2)
            for _ in range(2):
                exiting.cancel()
                await asyncio.sleep(0)
                self.assertFalse(exiting.done())
                self.assertFalse(entering.done())
                self.channel.connect.assert_not_awaited()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(exiting, 2)
            await asyncio.wait_for(entering, 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (exiting, entering) if task is not None], return_exceptions=True)
        self.channel.connect.assert_awaited_once()

    async def test_canceled_lock_waiter_does_not_block_later_operation(self):
        """ロック待ちの退出が中断したら参加を再び有効にし、次の操作も実行できる。"""
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def stop(*args, **kwargs):
            if self.autonomous.stop_session.await_count == 1:
                entered.set()
                await release.wait()
            return False

        self.autonomous.stop_session.side_effect = stop
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            exiting = asyncio.create_task(self.commands["leave"](leaving))
            await asyncio.wait_for(deferred.wait(), 2)
            exiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(exiting, 2)
            release.set()
            await asyncio.wait_for(joining, 2)
        finally:
            release.set()
            await asyncio.gather(*[task for task in (joining, exiting) if task is not None], return_exceptions=True)
        self.channel.connect.assert_awaited_once()
        self.assertIn("に接続しました", self.interaction.followup.send.call_args.args[0])
        self.channel.connect.reset_mock()
        await asyncio.wait_for(self.commands["join"](self.another_interaction()), 2)
        self.channel.connect.assert_awaited_once()

    async def test_dropped_request_does_not_restore_join_superseded_by_completed_leave(self):
        """退出実行後の別要求の脱落で、応答待ちだった古い参加を復活させない。"""
        entered, release = asyncio.Event(), asyncio.Event()

        async def defer():
            entered.set()
            await release.wait()

        self.interaction.response.defer.side_effect = defer
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await self.commands["leave"](self.another_interaction())
            dropped = self.another_interaction()
            dropped.response.defer.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await self.commands["leave"](dropped)
            release.set()
            await asyncio.wait_for(joining, 2)
        finally:
            release.set()
            await asyncio.gather(joining, return_exceptions=True)
        self.channel.connect.assert_not_awaited()
        self.voice_guard.adopt.assert_not_called()
        self.assertIn("後から受け付けた", self.interaction.followup.send.call_args.args[0])

    async def test_later_join_wins_while_earlier_join_is_waiting_for_defer(self):
        """参加同士でも応答の完了順に左右されず、最後に選んだ接続先を保持する。"""
        entered, release = asyncio.Event(), asyncio.Event()

        async def defer():
            entered.set()
            await release.wait()

        self.interaction.response.defer.side_effect = defer
        other_channel = self.make_channel(30)
        voice = self.connect_to_guild(self.guild, other_channel)
        earlier = asyncio.create_task(self.commands["join"](self.interaction))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(self.commands["join"](self.another_interaction(channel=other_channel)), 2)
            release.set()
            await asyncio.wait_for(earlier, 2)
        finally:
            release.set()
            await asyncio.gather(earlier, return_exceptions=True)
        self.channel.connect.assert_not_awaited()
        other_channel.connect.assert_awaited_once()
        voice.disconnect.assert_not_awaited()
        self.assertIs(self.guild.voice_client, voice)

    async def test_connect_failure_releases_operation_for_next_join(self):
        """接続エラー後にも同じサーバーで再操作できる。"""
        self.channel.connect.side_effect = OSError("架空の接続失敗")
        await self.join()
        self.channel.connect.side_effect = None
        self.channel.connect.reset_mock()
        await asyncio.wait_for(self.commands["join"](self.another_interaction()), 2)
        self.channel.connect.assert_awaited_once()

    async def test_defer_failure_and_cancellation_allow_later_operations(self):
        """応答前の失敗・中断で操作状態を残さず、後続操作を実行できる。"""
        for error in (OSError("架空の応答失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                interaction = self.another_interaction()
                interaction.response.defer.side_effect = error
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await self.commands["join"](interaction)
                else:
                    await self.commands["join"](interaction)
                    interaction.response.send_message.assert_awaited_once()
                    self.assertTrue(interaction.response.send_message.call_args.kwargs["ephemeral"])
                self.channel.connect.reset_mock()
                await asyncio.wait_for(self.commands["join"](self.another_interaction()), 2)
                self.channel.connect.assert_awaited_once()

    async def test_shutdown_recovers_connect_and_lock_waiter_without_deadlock(self):
        """終了管理が接続中とロック待ちを同時回収しても、共有接続を先に閉じない。"""
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        register_commands(
            self.tree, BotState(), None, None, SimpleNamespace(is_blocked=Mock(return_value=False)),
            None, None, None, None, self.autonomous, task_owner=client.background_tasks,
        )
        entered, release, deferred = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.connect_to_guild(self.guild, self.channel, started=entered, release=release)
        leaving = self.another_interaction()
        leaving.response.defer.side_effect = lambda: deferred.set()
        joining = asyncio.create_task(self.commands["join"](self.interaction))
        exiting = closing = None
        with patch.object(discord.Client, "close", new=AsyncMock()) as discord_close, patch.object(
            bot_client, "close_genai_client", new=AsyncMock(),
        ) as gemini_close:
            try:
                await asyncio.wait_for(entered.wait(), 2)
                exiting = asyncio.create_task(self.commands["leave"](leaving))
                await asyncio.wait_for(deferred.wait(), 2)
                closing = asyncio.create_task(client.close())
                for _ in range(2):
                    await asyncio.sleep(0)
                    joining.cancel()
                    closing.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(closing.done())
                    discord_close.assert_not_awaited()
                    gemini_close.assert_not_awaited()
                    client._database.close.assert_not_called()
                release.set()
                await asyncio.wait_for(asyncio.gather(joining, exiting, closing, return_exceptions=True), 2)
                await client.close()
            finally:
                release.set()
                await client.close()
                await asyncio.gather(*[task for task in (joining, exiting, closing) if task is not None], return_exceptions=True)
            discord_close.assert_awaited_once()
            gemini_close.assert_awaited_once()
            client._database.close.assert_called_once()
        self.assertTrue(joining.cancelled())
        self.assertTrue(exiting.cancelled())
        self.channel.connect.assert_awaited_once()

    async def test_disconnect_cancellation_propagates_without_reconnecting(self) -> None:
        old = self.make_voice_client(self.make_channel(30))
        old.disconnect.side_effect = asyncio.CancelledError
        self.guild.voice_client = old
        with self.assertRaises(asyncio.CancelledError):
            await self.join()
        self.channel.connect.assert_not_awaited()
        self.interaction.followup.send.assert_not_awaited()

    def make_stage(self, *, can_promote=True):
        stage = Mock(spec=discord.StageChannel, id=40)
        stage.name = "架空のステージ"
        stage.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=True, speak=True, mute_members=can_promote)
        stage.connect = AsyncMock()
        self.interaction.user.voice.channel = stage
        self.guild.me = SimpleNamespace(voice=SimpleNamespace(channel=stage), edit=AsyncMock())
        return stage

    async def test_stage_join_promotes_bot_before_reporting_speaking(self) -> None:
        stage = self.make_stage()
        reply = await self.join()
        stage.connect.assert_awaited_once_with(timeout=constants.VOICE_CONNECT_TIMEOUT_SECONDS, cls=ANY)
        self.guild.me.edit.assert_awaited_once_with(suppress=False)
        self.assertIn("発言者", reply)

    async def test_existing_stage_connection_is_promoted_without_reconnect(self) -> None:
        stage = self.make_stage()
        self.guild.voice_client = self.make_voice_client(stage)
        reply = await self.join()
        stage.connect.assert_not_awaited()
        self.guild.me.edit.assert_awaited_once_with(suppress=False)
        self.assertIn("既に接続", reply)

    async def test_stage_without_moderator_permission_explains_before_connect(self) -> None:
        stage = self.make_stage(can_promote=False)
        await self.commands["join"](self.interaction)
        self.assertIn("メンバーをミュート", self.interaction.response.send_message.call_args.args[0])
        self.interaction.response.defer.assert_not_awaited()
        self.autonomous.stop_session.assert_not_awaited()
        stage.connect.assert_not_awaited()

    async def test_moved_stage_voice_state_is_not_promoted_or_reported_as_speaker(self) -> None:
        self.make_stage()
        self.guild.me.voice.channel = SimpleNamespace(id=50)
        with patch.object(autonomous_stage.asyncio, "sleep", new=AsyncMock()):
            reply = await self.join()
        self.guild.me.edit.assert_not_awaited()
        self.assertIn("発言者になれませんでした", reply)
        self.assertNotIn("発言者として読み上げ", reply)

    async def test_missing_permissions_reject_before_changing_existing_connection(self) -> None:
        for connect, speak in ((False, True), (True, False), (False, False)):
            with self.subTest(connect=connect, speak=speak):
                self.interaction.response.send_message.reset_mock()
                self.channel.permissions_for.return_value = SimpleNamespace(view_channel=True, connect=connect, speak=speak)
                current = self.make_voice_client(self.make_channel(30))
                self.guild.voice_client = current

                await self.commands["join"](self.interaction)

                self.channel.permissions_for.assert_called_with(self.guild.me)
                self.interaction.response.send_message.assert_awaited_once()
                reply = self.interaction.response.send_message.call_args.args[0]
                self.assertIn(self.channel.name, reply)
                self.assertEqual("「接続」" in reply, not connect)
                self.assertEqual("「発言」" in reply, not speak)
                self.assertIn("権限", reply)
                self.interaction.response.defer.assert_not_awaited()
                self.autonomous.stop_session.assert_not_awaited()
                current.disconnect.assert_not_awaited()
                self.channel.connect.assert_not_awaited()
                self.interaction.followup.send.assert_not_awaited()


    async def test_hidden_voice_and_stage_are_rejected_before_changing_connections(self) -> None:
        for channel in (self.channel, self.make_stage()):
            with self.subTest(channel=channel.name):
                self.interaction.user.voice.channel = channel
                channel.permissions_for.return_value.view_channel = False
                self.interaction.response.send_message.reset_mock()
                current = self.make_voice_client(self.make_channel(30))
                self.guild.voice_client = current
                await self.commands["join"](self.interaction)
                text = self.interaction.response.send_message.call_args.args[0]
                self.assertIn("「チャンネルを見る」", text)
                self.assertNotIn("「接続」", text)
                self.assertNotIn("「発言」", text)
                self.interaction.response.defer.assert_not_awaited()
                self.autonomous.stop_session.assert_not_awaited()
                self.voice_guard.release.assert_not_called()
                current.disconnect.assert_not_awaited()
                channel.connect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
