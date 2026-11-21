"""Gemini Live の接続（#268）の設定・再開・再接続・利用枠の扱いを、模擬の接続で確かめる。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import errors, types
from persona_bot.config import constants
from persona_bot.services.live_session import (
    CLOSED_FAILED, CLOSED_QUOTA, LiveOptions, LiveSession, LiveUnavailableError,
    build_live_config, is_config_error, is_quota_error,
)
from tests.helpers.network_guard import block_network

OPTIONS = LiveOptions(system_instruction="架空のペルソナ", voice_name="Kore", grounding=True)


def message(**fields):
    base = {"server_content": None, "session_resumption_update": None, "go_away": None, "usage_metadata": None}
    base.update(fields)
    return SimpleNamespace(**base)


class FakeSession:
    """receive() は台本の応答を1つずつ返し、最後に指定の例外を投げる（None なら待ち続ける）。"""

    def __init__(self, script, end=None):
        self.script = list(script)
        self.end = end
        self.sent = []
        self.closed = asyncio.Event()

    async def receive(self):
        while self.script:
            yield self.script.pop(0)
            await asyncio.sleep(0)
            return
        if self.end is not None:
            raise self.end
        await self.closed.wait()
        raise errors.APIError(1000, {"message": "架空の切断"})

    async def send_realtime_input(self, **payload):
        self.sent.append(payload)


class FakeConnect:
    def __init__(self, outcomes):
        # outcomes: FakeSession か、接続時に投げる例外
        self.outcomes = list(outcomes)
        self.configs = []
        self.sessions = []
        self.waiting = asyncio.Event()

    def __call__(self, *, model, config):
        self.configs.append(config)
        outcome = self.outcomes.pop(0)
        outer = self

        class Context:
            async def __aenter__(self):
                if isinstance(outcome, BaseException):
                    raise outcome
                if isinstance(outcome, SlowConnect):
                    # 接続の確立を待たせる（その間に文脈の破棄が求められる場面を作る）。
                    outer.waiting.set()
                    await outcome.release.wait()
                outer.sessions.append(outcome.session if isinstance(outcome, SlowConnect) else outcome)
                return outer.sessions[-1]

            async def __aexit__(self, *exc):
                if isinstance(outcome, SlowConnect):
                    outcome.session.closed.set()
                elif not isinstance(outcome, BaseException):
                    outcome.closed.set()

        return Context()


class SlowConnect:
    def __init__(self, session):
        self.session = session
        self.release = asyncio.Event()


class ConfigTests(unittest.TestCase):
    def test_manual_activity_transcription_resumption_and_compression(self):
        config = build_live_config(OPTIONS, handle="架空のハンドル", minimal=False)
        self.assertEqual(config.response_modalities, [types.Modality.AUDIO])
        self.assertEqual(config.system_instruction, "架空のペルソナ")
        self.assertTrue(config.realtime_input_config.automatic_activity_detection.disabled)
        self.assertEqual(
            config.realtime_input_config.activity_handling, types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        )
        self.assertIsNotNone(config.input_audio_transcription)
        self.assertIsNotNone(config.output_audio_transcription)
        self.assertEqual(config.session_resumption.handle, "架空のハンドル")
        self.assertEqual(config.context_window_compression.trigger_tokens, constants.LIVE_COMPRESSION_TRIGGER_TOKENS)
        self.assertEqual(
            config.context_window_compression.sliding_window.target_tokens, constants.LIVE_COMPRESSION_TARGET_TOKENS,
        )
        self.assertEqual(config.speech_config.voice_config.prebuilt_voice_config.voice_name, "Kore")
        self.assertIsNotNone(config.tools[0].google_search)

    def test_minimal_config_drops_voice_and_search(self):
        config = build_live_config(OPTIONS, handle=None, minimal=True)
        self.assertIsNone(config.speech_config)
        self.assertIsNone(config.tools)
        self.assertIsNone(config.session_resumption.handle)

    def test_no_voice_or_search_unless_requested(self):
        config = build_live_config(
            LiveOptions(system_instruction="x", voice_name="", grounding=False), handle=None, minimal=False,
        )
        self.assertIsNone(config.speech_config)
        self.assertIsNone(config.tools)

    def test_error_classification(self):
        quota = errors.ClientError(429, {"message": "架空の上限"})
        exhausted = errors.APIError(1011, {"message": "You exceeded your current quota"})
        config = errors.APIError(1007, {"message": "Invalid voice"})
        server = errors.ServerError(503, {"message": "架空の障害"})
        self.assertTrue(is_quota_error(quota))
        self.assertTrue(is_quota_error(exhausted))
        self.assertFalse(is_quota_error(config))
        self.assertTrue(is_config_error(config))
        self.assertFalse(is_config_error(quota))
        self.assertFalse(is_config_error(server))


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        block_network(self)
        self.messages = []
        self.closed_reasons = []
        self.reconnected = 0
        self.sleeps = []

    def make(self, outcomes) -> tuple[LiveSession, FakeConnect]:
        connect = FakeConnect(outcomes)

        async def on_message(received):
            self.messages.append(received)

        async def on_closed(reason):
            self.closed_reasons.append(reason)

        def on_reconnected():
            self.reconnected += 1

        async def sleep(seconds):
            self.sleeps.append(seconds)

        session = LiveSession(
            None, OPTIONS, on_message=on_message, on_reconnected=on_reconnected, on_closed=on_closed,
            connect=connect, sleep=sleep,
        )
        self.addAsyncCleanup(session.close)
        return session, connect

    async def wait_until(self, condition, timeout=2.0):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)

    async def test_messages_are_forwarded_and_sends_use_realtime_input(self):
        first = FakeSession([message(server_content="架空の応答")])
        live, _connect = self.make([first])
        await live.start()
        await self.wait_until(lambda: self.messages)
        self.assertTrue(live.connected)
        self.assertTrue(await live.activity_start())
        self.assertTrue(await live.send_audio(b"pcm"))
        self.assertTrue(await live.activity_end())
        self.assertIsInstance(first.sent[0]["activity_start"], types.ActivityStart)
        self.assertEqual(first.sent[1]["audio"].mime_type, constants.LIVE_INPUT_MIME_TYPE)
        self.assertIsInstance(first.sent[2]["activity_end"], types.ActivityEnd)

    async def test_go_away_reconnects_immediately_with_the_resumption_handle(self):
        first = FakeSession([
            message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1")),
            message(go_away=SimpleNamespace(time_left="10s")),
        ])
        second = FakeSession([])
        live, connect = self.make([first, second])
        await live.start()
        await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(connect.configs[1].session_resumption.handle, "h1")
        self.assertEqual(self.sleeps, [])  # 予告による張り替えは待たない
        self.assertEqual(live.generation, 2)

    async def test_unexpected_drop_reconnects_after_backoff(self):
        first = FakeSession([], end=errors.ServerError(503, {"message": "架空の障害"}))
        second = FakeSession([])
        live, _connect = self.make([first, second])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(self.sleeps, [constants.LIVE_RECONNECT_BACKOFF_SECONDS[0]])

    async def test_quota_during_session_closes_without_reconnecting(self):
        first = FakeSession([], end=errors.ClientError(429, {"message": "架空の上限"}))
        live, connect = self.make([first])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.closed_reasons)
        self.assertEqual(self.closed_reasons, [CLOSED_QUOTA])
        self.assertEqual(len(connect.configs), 1)

    async def test_failed_reconnects_are_limited_then_reported(self):
        failure = errors.ServerError(503, {"message": "架空の障害"})
        first = FakeSession([], end=failure)
        outcomes = [first] + [errors.ServerError(503, {"message": "架空"})] * constants.LIVE_RECONNECT_MAX_ATTEMPTS
        live, connect = self.make(outcomes)
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.closed_reasons)
        self.assertEqual(self.closed_reasons, [CLOSED_FAILED])
        self.assertEqual(len(connect.configs), 1 + constants.LIVE_RECONNECT_MAX_ATTEMPTS)

    async def test_failed_resume_retries_with_a_fresh_session(self):
        first = FakeSession(
            [message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1"))],
            end=errors.ServerError(503, {"message": "架空の障害"}),
        )
        second = FakeSession([])
        live, connect = self.make([first, errors.APIError(1008, {"message": "invalid handle"}), second])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(connect.configs[1].session_resumption.handle, "h1")
        self.assertIsNone(connect.configs[2].session_resumption.handle)

    async def test_config_error_at_start_retries_without_voice_and_search(self):
        live, connect = self.make([errors.APIError(1007, {"message": "Invalid voice"}), FakeSession([])])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
        self.assertIsNotNone(connect.configs[0].speech_config)
        self.assertIsNone(connect.configs[1].speech_config)
        self.assertIsNone(connect.configs[1].tools)

    async def test_close_during_start_fails_instead_of_returning_normally(self):
        """開始の途中で close() された場合は、接続成功と誤認させず失敗として伝える（#493）。"""
        live, _connect = self.make([FakeSession([])])
        live._open_with_search_fallback = AsyncMock(return_value=False)
        with self.assertRaises(LiveUnavailableError) as raised:
            await live.start()
        self.assertFalse(raised.exception.quota)

    async def test_start_failure_reports_quota(self):
        # 検索つきの利用枠の超過は検索なしで1回試し直すので、本物の超過は2回続く。
        for outcomes, quota in (
            ([errors.ClientError(429, {"message": "架空の上限"})] * 2, True),
            ([errors.ServerError(503, {"message": "架空の障害"})], False),
        ):
            with self.subTest(quota=quota):
                live, _connect = self.make(outcomes)
                with self.assertLogs("persona_bot.services.live_session", level="ERROR"):
                    with self.assertRaises(LiveUnavailableError) as raised:
                        await live.start()
                self.assertEqual(raised.exception.quota, quota)
                self.assertFalse(live.connected)

    async def test_quota_with_search_at_start_retries_without_search_only(self):
        # 検索ツール付きの接続だけが「quota を超えた」と拒否される場合（Live の検索は無料枠外）。
        rejected = errors.APIError(1011, {"message": "You exceeded your current quota"})
        live, connect = self.make([rejected, FakeSession([])])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
        self.assertIsNotNone(connect.configs[0].tools)
        self.assertIsNone(connect.configs[1].tools)
        # 声は残す（検索だけを外す）。
        self.assertIsNotNone(connect.configs[1].speech_config)
        self.assertTrue(live.connected)

    async def test_search_quota_then_configuration_fallback(self):
        quota = errors.APIError(1011, {"message": "quota exceeded"})
        config = errors.ClientError(400, {"message": "設定未対応"})
        live, connect = self.make([quota, config, FakeSession([])])
        await live.start()
        self.assertEqual(len(connect.configs), 3)
        self.assertTrue(live._minimal)
        self.assertTrue(live._search_off)

    async def test_cancel_during_connection_closes_pending_context(self):
        live, _ = self.make([])
        entered, closed = asyncio.Event(), asyncio.Event()

        class Context:
            async def __aenter__(self):
                entered.set()
                await asyncio.Event().wait()

            async def __aexit__(self, *args):
                closed.set()

        live._connect_live = lambda **kwargs: Context()
        task = asyncio.create_task(live.start())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())

    async def test_quota_without_search_is_not_retried(self):
        options = LiveOptions(system_instruction="x", voice_name="Kore", grounding=False)
        connect = FakeConnect([errors.ClientError(429, {"message": "架空の上限"})])

        async def ignore(*_args):
            return None

        live = LiveSession(
            None, options, on_message=ignore, on_reconnected=lambda: None, on_closed=ignore, connect=connect,
        )
        with self.assertLogs("persona_bot.services.live_session", level="ERROR"):
            with self.assertRaises(LiveUnavailableError) as raised:
                await live.start()
        self.assertTrue(raised.exception.quota)
        self.assertEqual(len(connect.configs), 1)

    async def test_quota_with_search_on_reconnect_retries_without_search(self):
        first = FakeSession([], end=errors.ServerError(503, {"message": "架空の障害"}))
        rejected = errors.APIError(1011, {"message": "You exceeded your current quota"})
        second = FakeSession([])
        live, connect = self.make([first, rejected, second])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            await live.start()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertIsNotNone(connect.configs[1].tools)
        self.assertIsNone(connect.configs[2].tools)
        self.assertEqual(self.closed_reasons, [])
        self.assertEqual(self.sleeps, [constants.LIVE_RECONNECT_BACKOFF_SECONDS[0]])

    async def test_search_fallback_on_immediate_reconnect_does_not_wait(self):
        rejected = errors.APIError(1011, {"message": "架空の quota 超過"})
        live, connect = self.make([rejected, FakeSession([])])
        live._go_away = True
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            self.assertTrue(await live._reconnect())
        self.assertEqual(self.sleeps, [])
        self.assertEqual(len(connect.configs), 2)
        self.assertIsNotNone(connect.configs[0].tools)
        self.assertIsNone(connect.configs[1].tools)

    async def test_search_fallback_on_last_reconnect_attempt_is_tried(self):
        failed = errors.ServerError(503, {"message": "架空の障害"})
        rejected = errors.ClientError(429, {"message": "架空の上限"})
        outcomes = [failed] * (constants.LIVE_RECONNECT_MAX_ATTEMPTS - 1) + [rejected, FakeSession([])]
        live, connect = self.make(outcomes)
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            self.assertTrue(await live._reconnect())
        self.assertEqual(len(connect.configs), constants.LIVE_RECONNECT_MAX_ATTEMPTS + 1)
        self.assertIsNone(connect.configs[-1].tools)
        self.assertEqual(len(self.sleeps), constants.LIVE_RECONNECT_MAX_ATTEMPTS)
        self.assertEqual(self.closed_reasons, [])

    async def test_quota_after_reconnect_search_fallback_ends_without_more_attempts(self):
        rejected = errors.ClientError(429, {"message": "架空の上限"})
        live, connect = self.make([rejected, rejected])
        with self.assertLogs("persona_bot.services.live_session", level="WARNING"):
            self.assertFalse(await live._reconnect())
        self.assertEqual(len(connect.configs), 2)
        self.assertEqual(self.closed_reasons, [CLOSED_QUOTA])
        self.assertEqual(len(self.sleeps), 1)

    async def test_pump_does_not_repeat_an_empty_stream(self):
        class EmptySession:
            calls = 0

            async def receive(self):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("閉じたストリームを再受信しました。")
                if False:
                    yield

        live, _connect = self.make([])
        empty = EmptySession()
        live._session = empty
        self.assertIsNone(await live._pump())
        self.assertEqual(empty.calls, 1)

    async def test_restart_fresh_handles_a_clean_receive_end(self):
        class CleanCloseSession(FakeSession):
            calls = 0

            async def receive(self):
                self.calls += 1
                if self.calls > 1:
                    raise AssertionError("閉じたストリームを再受信しました。")
                await self.closed.wait()
                if False:
                    yield

        first = CleanCloseSession([])
        second = FakeSession([])
        live, connect = self.make([first, second])
        await live.start()
        await self.wait_until(lambda: first.calls == 1)
        await live.restart_fresh()
        await self.wait_until(lambda: self.reconnected == 1)
        self.assertEqual(first.calls, 1)
        self.assertIs(connect.sessions[-1], second)
        self.assertEqual(self.sleeps, [])

    async def test_pump_continues_normal_turns_but_stops_after_session_replacement(self):
        first = FakeSession([message(server_content="1回目"), message(server_content="2回目")])
        live, _connect = self.make([])
        live._session = first

        async def replace_after_second(received):
            self.messages.append(received)
            if len(self.messages) == 2:
                live._session = FakeSession([])

        live._on_message = replace_after_second
        self.assertIsNone(await asyncio.wait_for(live._pump(), timeout=2))
        self.assertEqual([item.server_content for item in self.messages], ["1回目", "2回目"])

    async def test_close_stops_without_reconnecting(self):
        first = FakeSession([])
        live, connect = self.make([first])
        await live.start()
        await live.close()
        await asyncio.sleep(0.05)
        self.assertFalse(live.connected)
        self.assertEqual(len(connect.configs), 1)
        self.assertEqual(self.closed_reasons, [])
        self.assertFalse(await live.send_audio(b"pcm"))

    async def test_withdrawal_while_reconnecting_does_not_adopt_the_resumed_session(self):
        """再接続の途中で同意が撤回されたら、撤回前のハンドルで開いた接続を使わずに新しくつなぐ。"""
        first = FakeSession(
            [message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1"))],
            end=errors.ServerError(503, {"message": "架空の障害"}),
        )
        resumed = SlowConnect(FakeSession([]))
        fresh = FakeSession([])
        live, connect = self.make([first, resumed, fresh])
        with self.assertLogs("persona_bot.services.live_session", level="INFO") as logs:
            await live.start()
            await self.wait_until(connect.waiting.is_set)
            self.assertEqual(connect.configs[1].session_resumption.handle, "h1")
            await live.restart_fresh()  # まだ接続していないので、待ち受けの側で照合する
            resumed.release.set()
            await self.wait_until(lambda: self.reconnected == 1)
        self.assertTrue(resumed.session.closed.is_set())  # 撤回前の文脈の接続は閉じた
        self.assertIsNone(connect.configs[2].session_resumption.handle)
        self.assertIs(connect.sessions[-1], fresh)
        self.assertTrue(any("文脈の破棄" in record.getMessage() for record in logs.records))

    async def test_handle_from_the_old_connection_is_ignored_after_forgetting(self):
        live, _connect = self.make([FakeSession([])])
        await live.start()
        live.forget_context()
        await live._handle(message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="古い")))
        self.assertIsNone(live._resume_handle)

    async def test_restart_fresh_drops_the_handle(self):
        first = FakeSession([message(session_resumption_update=SimpleNamespace(resumable=True, new_handle="h1"))])
        second = FakeSession([])
        live, connect = self.make([first, second])
        await live.start()
        await self.wait_until(lambda: self.messages)
        await live.restart_fresh()
        await self.wait_until(lambda: self.reconnected == 1)
        self.assertIsNone(connect.configs[1].session_resumption.handle)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
