"""音声会話（#268 の Persona Voice Controller）の同意・発話権・割り込み・モデレーション・記憶を確かめる。

Live の接続・Discord の受信と再生は模擬に差し替え、時計も固定して動かす。
"""

import asyncio
import threading
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.config import constants
from persona_bot.audio import voice as tts_voice
from persona_bot.services import voice_conversation
from persona_bot.services.content_moderator import VERDICT_SAFE, VERDICT_UNKNOWN, VERDICT_UNSAFE
from persona_bot.services.live_session import CLOSED_QUOTA, LiveOptions, LiveUnavailableError
from persona_bot.services.voice_conversation import (
    NOTICE_LIVE_QUOTA, VoiceConversation, VoiceConversationService,
)
from tests.helpers.network_guard import block_network

VOICED = np.full(320, 3000, dtype=np.int16).tobytes()  # 20ms・16kHz・モノラル
SILENT = bytes(640)
OPTIONS = LiveOptions(system_instruction="架空", voice_name="", grounding=False)
CHANNEL_ID = 20
USER_A, USER_B, USER_C, BOT_ID = 11, 12, 13, 99


def member(user_id, *, bot=False):
    return SimpleNamespace(id=user_id, bot=bot, mention=f"<@{user_id}>", __str__=lambda self: f"user{user_id}")


def live_message(**fields):
    content = {
        "input_transcription": None, "output_transcription": None, "model_turn": None,
        "interrupted": None, "generation_complete": None, "turn_complete": None,
    }
    content.update(fields)
    return SimpleNamespace(server_content=SimpleNamespace(**content), usage_metadata=None)


def audio_turn(data: bytes):
    return SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=data))])


class FakeLive:
    def __init__(self):
        self.connected = True
        self.restarts = 0
        self.forgot = False

    async def close(self):
        self.connected = False

    async def restart_fresh(self):
        self.restarts += 1

    def forget_context(self):
        self.forgot = True


class ConversationTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        block_network(self)
        self.clock = [100.0]
        self.channel = SimpleNamespace(
            id=CHANNEL_ID, members=[member(USER_A), member(USER_B), member(BOT_ID, bot=True)],
            send=AsyncMock(),
        )
        self.voice_client = Mock()
        self.voice_client.channel = self.channel
        self.voice_client.is_connected.return_value = True
        self.voice_client.is_playing.return_value = False
        self.voice_client.is_paused.return_value = False
        self.members = {m.id: m for m in self.channel.members}
        self.guild = SimpleNamespace(id=1, voice_client=self.voice_client, get_member=self.members.get)
        self.blocked: set[int] = set()
        self.ng_words = {"架空のNG"}
        self.moderator = SimpleNamespace(
            judge_voice_input=AsyncMock(return_value=VERDICT_SAFE),
            judge_voice_turn=AsyncMock(return_value=(VERDICT_SAFE, (3, -2))),
        )
        self.service = SimpleNamespace(
            genai_client=None, live_connect=Mock(), is_blocked=lambda user_id: user_id in self.blocked,
            ng_filter=SimpleNamespace(contains_ng_word=lambda text: any(w in text for w in self.ng_words)),
            moderator=self.moderator,
            blacklist=SimpleNamespace(record_violation=AsyncMock(return_value=(1, False))),
            memory=SimpleNamespace(append_pending_log=AsyncMock(), flush_if_needed=AsyncMock()),
            affinity=SimpleNamespace(update_emotion=Mock(), save_background=AsyncMock()),
            usage=SimpleNamespace(log_tokens=Mock()),
            end=AsyncMock(),
        )
        self.conversation = VoiceConversation(
            self.service, self.guild, self.voice_client, CHANNEL_ID, self.channel, OPTIONS,
            now=lambda: self.clock[0],
        )
        self.live = FakeLive()
        self.conversation._live = self.live

    async def asyncTearDown(self):
        # 出力判定などの裏のタスクを回収する。
        for _ in range(5):
            await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.clock[0] += seconds

    def consent_all(self) -> None:
        self.conversation.grant_voice(USER_A)
        self.conversation.grant_voice(USER_B)

    def speak(self, user_id: int, frames: int, pcm: bytes = VOICED) -> None:
        for _ in range(frames):
            self.conversation._on_audio(user_id, pcm)
            self.advance(0.02)

    def drain(self) -> list[str]:
        kinds = []
        queue = self.conversation._send_queue
        while not queue.empty():
            kinds.append(queue.get_nowait()[0])
        return kinds

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)

    def start_frames(self) -> int:
        return constants.LIVE_VAD_START_MS // 20

    def barge_in_frames(self) -> int:
        """ペルソナの声が出ている間に、手元の保持を越えて Live へ送られるまで話し続けるフレーム数。"""
        return int(constants.LIVE_BARGE_IN_HOLD_SECONDS / 0.02) + 2

    async def transcript(self, text: str = "", *, finished: bool = False) -> None:
        await self.conversation._on_live_message(
            live_message(input_transcription=SimpleNamespace(text=text, finished=finished)),
        )

    async def complete_utterance(self, user_id=USER_A, transcript="架空の質問") -> None:
        """発言して話し終わり、入力の文字起こしが確定した状態にする。"""
        self.speak(user_id, self.start_frames())
        await self.conversation._on_live_message(
            live_message(input_transcription=SimpleNamespace(text=transcript)),
        )
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()


class GateTests(ConversationTestBase):
    async def test_everyone_present_must_consent_and_bots_are_ignored(self):
        self.conversation.grant_voice(USER_A)
        self.assertFalse(self.conversation.is_open)
        self.assertEqual(self.conversation.waiting_count, 1)
        self.conversation.grant_voice(USER_B)
        self.assertTrue(self.conversation.is_open)

    async def test_blacklisted_member_is_excluded_from_the_consent_check(self):
        self.blocked.add(USER_B)
        self.conversation.grant_voice(USER_A)
        self.assertTrue(self.conversation.is_open)

    async def test_late_joiner_pauses_until_they_consent(self):
        self.consent_all()
        late = member(USER_C)
        self.channel.members.append(late)
        self.conversation.handle_member_update(late, SimpleNamespace(channel=None), SimpleNamespace(channel=self.channel))
        self.assertFalse(self.conversation.is_open)
        self.conversation.grant_voice(USER_C)
        self.assertTrue(self.conversation.is_open)

    async def test_moved_to_another_channel_closes_the_gate(self):
        self.consent_all()
        self.voice_client.channel = SimpleNamespace(id=30, members=self.channel.members)
        self.conversation.tick()
        self.assertFalse(self.conversation.is_open)

    async def test_empty_channel_stays_closed(self):
        self.channel.members = [member(BOT_ID, bot=True)]
        self.conversation._update_gate()
        self.assertFalse(self.conversation.is_open)

    async def test_withdrawal_drops_context_right_away(self):
        self.consent_all()
        self.conversation.withdraw(USER_B)
        self.assertFalse(self.conversation.is_open)
        self.assertTrue(self.live.forgot)
        await self.settle()
        self.assertEqual(self.live.restarts, 1)
        self.conversation.grant_voice(USER_B)  # 再開で二重に張り替えない
        await self.settle()
        self.assertEqual(self.live.restarts, 1)

    async def test_withdrawal_while_the_gate_stays_open_also_drops_context(self):
        """VC を出た人が撤回しても（残った人だけでゲートは開いたまま）、その人の発言を含む文脈を使わない。"""
        self.consent_all()
        leaving = self.members[USER_A]
        self.channel.members.remove(leaving)
        self.conversation.handle_member_update(leaving, SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        self.assertTrue(self.conversation.is_open)
        self.conversation.withdraw(USER_A)
        self.assertTrue(self.conversation.is_open)
        await self.settle()
        self.assertEqual(self.live.restarts, 1)
        self.assertTrue(self.live.forgot)

    async def test_withdrawal_without_prior_consent_keeps_context(self):
        self.conversation.withdraw(USER_A)
        await self.settle()
        self.assertEqual(self.live.restarts, 0)


class SpeakerTests(ConversationTestBase):
    async def test_expired_reservations_do_not_revive_or_block_new_requests(self):
        speaker = self.conversation._speaker_state
        speaker.request_floor(USER_A, 0)
        expired = constants.LIVE_SPEAKER_REQUEST_TTL_SECONDS + 1
        speaker.extend_reservations(expired)
        self.assertFalse(speaker.reservations)
        speaker.request_floor(USER_B, expired)
        self.assertTrue(speaker.floor_available(USER_B, expired))

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.consent_all()

    async def test_unconsented_and_blocked_audio_is_not_sent(self):
        self.conversation.withdraw(USER_B)
        self.conversation.grant_voice(USER_B)
        self.drain()
        self.blocked.add(USER_B)
        self.speak(USER_B, 30)
        self.assertEqual(self.drain(), [])
        self.channel.members.append(member(USER_C))
        self.conversation._update_gate()
        self.speak(USER_C, 30)
        self.assertEqual(self.drain(), [])

    async def test_activity_starts_after_threshold_with_preroll_and_ends_on_silence(self):
        self.speak(USER_A, self.start_frames() - 1)
        self.assertEqual(self.drain(), [])
        self.speak(USER_A, 1)
        kinds = self.drain()
        self.assertEqual(kinds[0], "start")
        self.assertTrue(all(kind == "audio" for kind in kinds[1:]))
        self.assertGreater(len(kinds), 1)  # プリロール（判定前の声）も送る
        self.speak(USER_A, 5, SILENT)
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000)
        self.conversation.tick()
        kinds = self.drain()
        self.assertEqual(kinds[-1], "end")
        self.assertIsNone(self.conversation._active_user)

    async def test_long_utterance_is_cut_at_the_limit(self):
        self.speak(USER_A, self.start_frames())
        self.advance(constants.LIVE_MAX_UTTERANCE_SECONDS)
        self.speak(USER_A, 1)
        self.conversation.tick()
        self.assertEqual(self.drain()[-1], "end")

    async def test_short_noise_does_not_take_the_floor(self):
        self.speak(USER_A, self.start_frames() - 2)
        self.advance(1.0)  # 途切れたら数え直す
        self.speak(USER_A, 3)
        self.assertEqual(self.drain(), [])

    async def test_only_one_speaker_is_sent_and_waiting_speaker_gets_priority(self):
        self.channel.members.append(member(USER_C))
        self.conversation.grant_voice(USER_C)
        self.speak(USER_A, self.start_frames())
        self.drain()
        self.speak(USER_B, self.start_frames() + 2)
        self.assertEqual(self.drain(), [])  # A の発言中は B の声を送らない（混ぜない）
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000)
        self.conversation.tick()
        self.assertEqual(self.drain()[-1], "end")
        # A への返答を待つ間は、割り込みと同じ長さの発言が要る。
        # 待っていた B より先に C が話しても、B に優先して機会を渡す。
        barge_in = constants.LIVE_BARGE_IN_MS // 20
        self.speak(USER_C, barge_in + 2)
        self.assertEqual(self.drain(), [])
        self.speak(USER_B, barge_in)
        self.assertEqual(self.drain()[0], "start")
        self.assertEqual(self.conversation._active_user, USER_B)

    async def test_reservation_expires(self):
        self.speak(USER_A, self.start_frames())
        self.speak(USER_B, self.start_frames())
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000)
        self.conversation.tick()
        self.drain()
        self.advance(constants.LIVE_SPEAKER_REQUEST_TTL_SECONDS + 0.1)
        self.channel.members.append(member(USER_C))
        self.conversation.grant_voice(USER_C)
        self.speak(USER_C, constants.LIVE_BARGE_IN_MS // 20)
        self.assertEqual(self.conversation._active_user, USER_C)

    async def test_start_threshold_returns_to_normal_when_no_reply_arrives(self):
        self.speak(USER_A, self.start_frames())
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000)
        self.conversation.tick()
        self.drain()
        self.assertTrue(self.conversation._bot_speaking())
        self.advance(constants.LIVE_PLAYBACK_STALL_SECONDS)
        self.assertFalse(self.conversation._bot_speaking())
        self.speak(USER_B, self.start_frames())
        self.assertEqual(self.conversation._active_user, USER_B)

    async def test_member_leaving_mid_utterance_ends_it_without_judging(self):
        self.speak(USER_A, self.start_frames())
        self.drain()
        leaving = self.members[USER_A]
        self.channel.members.remove(leaving)
        self.conversation.handle_member_update(leaving, SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        self.assertEqual(self.drain()[-1], "end")
        self.assertTrue(self.conversation._current.discarded)
        await self.settle()
        self.moderator.judge_voice_input.assert_not_awaited()


class ResponseTests(ConversationTestBase):
    async def test_withdrawal_discards_response_after_speech_ends(self):
        self.consent_all()
        await self.complete_utterance()
        turn = self.conversation._current
        self.assertIsNone(self.conversation._active_user)
        self.conversation.withdraw(USER_A)
        self.assertTrue(turn.discarded)
        await self.settle()
        self.service.memory.append_pending_log.assert_not_awaited()

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.consent_all()
        self.conversation.toggle_memory(USER_A)
        self.conversation.toggle_affinity(USER_A)

    async def respond(self, *, audio_bytes=24000, text="架空の返答", complete=True):
        await self.conversation._on_live_message(live_message(
            model_turn=audio_turn(bytes(audio_bytes)), output_transcription=SimpleNamespace(text=text),
        ))
        if complete:
            await self.conversation._on_live_message(live_message(generation_complete=True, turn_complete=True))
        await self.settle()

    async def test_audio_is_buffered_then_played_and_finished(self):
        await self.complete_utterance()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(4800))))
        self.voice_client.play.assert_not_called()  # まだ短いバッファに満たない
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_called_once()
        source = self.voice_client.play.call_args.args[0]
        await self.conversation._on_live_message(live_message(generation_complete=True))
        self.assertTrue(source.finished)
        self.assertEqual(len(self.conversation._latencies), 1)

    async def test_short_reply_starts_playing_when_generation_completes(self):
        await self.complete_utterance()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(960))))
        self.voice_client.play.assert_not_called()
        await self.conversation._on_live_message(live_message(generation_complete=True))
        self.voice_client.play.assert_called_once()

    async def test_stalled_reply_resumes_without_counting_latency_twice(self):
        await self.complete_utterance()
        await self.respond(complete=False)
        first_latency = list(self.conversation._latencies)
        source = self.voice_client.play.call_args.args[0]
        # 通信待ちの無音が上限に達し、再生スレッドから終了が通知される。
        while source.read():
            pass
        self.voice_client.play.call_args.kwargs["after"](None)
        await self.settle()
        self.advance(20)
        await self.respond(complete=False)
        self.assertEqual(self.voice_client.play.call_count, 2)
        self.assertEqual(self.conversation._latencies, first_latency)

    async def test_chunk_arriving_right_after_a_stall_is_played_by_a_new_source(self):
        """再生スレッドの終了通知がループで処理される前に届いたチャンクを、消さずに再生する（#412）。"""
        await self.complete_utterance()
        await self.respond(complete=False)
        source = self.voice_client.play.call_args.args[0]
        while source.read():  # 途絶えが上限に達して、音源が自分で終わる
            pass
        self.assertTrue(source.aborted)
        # after コールバックはまだ処理されていない
        await self.respond(complete=False)
        self.assertEqual(self.voice_client.play.call_count, 2)
        self.assertIsNot(self.voice_client.play.call_args.args[0], source)

    async def test_playback_that_cannot_start_releases_its_state(self):
        """未接続などで再生を始めずに戻ったとき、再生状態を残さない（#412）。"""
        await self.complete_utterance()
        self.voice_client.is_connected.return_value = False
        await self.respond(complete=False)
        self.voice_client.play.assert_not_called()
        self.assertIsNone(self.conversation._playback)
        self.assertFalse(self.conversation._playback_started)

    async def test_failed_playback_does_not_record_latency(self):
        await self.complete_utterance()
        self.voice_client.play.side_effect = discord.ClientException("架空の再生失敗")
        with self.assertLogs(voice_conversation.logger, level="ERROR"):
            await self.respond(complete=False)
        self.assertEqual(self.conversation._latencies, [])

    async def test_replacement_waits_for_previous_player(self):
        await self.assert_replacement_waits(withdraw=False)

    async def test_waiting_replacement_rechecks_consent(self):
        await self.assert_replacement_waits(withdraw=True)

    async def test_aborted_player_is_joined_after_stop_clears_reference(self):
        await self.assert_replacement_waits(withdraw=False, abort_before=True)

    async def test_live_waits_for_tts_cleanup_on_the_same_connection(self):
        await self.assert_live_waits_for_tts(withdraw=False)

    async def test_live_withdrawal_while_waiting_does_not_stop_tts(self):
        await self.assert_live_waits_for_tts(withdraw=True)

    async def test_late_live_withdrawal_does_not_stop_replacement_tts(self):
        await self.complete_utterance()
        await self.respond(complete=False)
        started = asyncio.Event()
        callbacks = []

        def play(source, *, after):
            callbacks.append(after)
            started.set()

        self.voice_client.play.side_effect = play
        tts_task = asyncio.create_task(tts_voice._play_pcm(self.voice_client, bytes(16)))
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.voice_client.stop.reset_mock()
            self.conversation.withdraw(USER_B)
            self.voice_client.stop.assert_not_called()
            callbacks[0](None)
            await asyncio.wait_for(tts_task, 2)
        finally:
            if callbacks:
                callbacks[0](None)
            await asyncio.gather(tts_task, return_exceptions=True)

    async def assert_live_waits_for_tts(self, *, withdraw):
        await self.complete_utterance()
        started = asyncio.Event()
        callbacks = []

        def play(source, *, after):
            callbacks.append(after)
            started.set()

        self.voice_client.play.side_effect = play
        tts_task = asyncio.create_task(tts_voice._play_pcm(self.voice_client, bytes(16)))
        live_task = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            tts_source = self.voice_client.play.call_args.args[0]
            live_task = asyncio.create_task(self.respond(complete=False))
            await asyncio.sleep(0)
            self.assertFalse(live_task.done())
            self.voice_client.play.assert_called_once()
            if withdraw:
                self.conversation.withdraw(USER_B)
            self.voice_client.stop.assert_not_called()
            self.assertFalse(tts_source.stream.closed)
            callbacks[0](None)
            await asyncio.wait_for(asyncio.gather(tts_task, live_task), 2)
            self.assertTrue(tts_source.stream.closed)
            self.assertEqual(self.voice_client.play.call_count, 1 if withdraw else 2)
        finally:
            if callbacks:
                callbacks[0](None)
            await asyncio.gather(*(task for task in (tts_task, live_task) if task is not None), return_exceptions=True)

    async def assert_replacement_waits(self, *, withdraw, abort_before=False):
        await self.complete_utterance()
        if abort_before:
            await self.respond(complete=False)
        self.voice_client.play.reset_mock()
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()

        class FinishingPlayer(threading.Thread):
            def join(self, timeout=None):
                loop.call_soon_threadsafe(entered.set)
                super().join(timeout)

        player = FinishingPlayer(target=lambda: release.wait(5))
        player.start()
        self.voice_client._player = player
        self.voice_client.is_paused.return_value = True

        def stop():
            self.voice_client._player = None
            self.voice_client.is_paused.return_value = False

        self.voice_client.stop.side_effect = stop
        if abort_before:
            self.conversation._abort_playback()
            self.assertIsNone(self.voice_client._player)
        response = asyncio.create_task(self.respond(complete=False))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.voice_client.play.assert_not_called()
            if withdraw:
                self.conversation.withdraw(USER_B)
            release.set()
            await asyncio.wait_for(response, 2)
            if withdraw:
                self.voice_client.play.assert_not_called()
            else:
                self.voice_client.play.assert_called_once()
        finally:
            release.set()
            await asyncio.gather(response, return_exceptions=True)
            player.join()


    async def test_safe_turn_is_remembered_and_updates_affinity_for_consenting_user(self):
        await self.complete_utterance()
        await self.respond()
        self.moderator.judge_voice_turn.assert_awaited_once_with("架空の質問", "架空の返答")
        self.service.memory.append_pending_log.assert_awaited_once_with(str(USER_A), "架空の質問", "架空の返答")
        self.service.affinity.update_emotion.assert_called_once_with(str(USER_A), 3, -2)

    async def test_memory_and_affinity_need_their_own_consent(self):
        await self.complete_utterance(USER_B)
        await self.respond()
        self.service.memory.append_pending_log.assert_not_awaited()
        self.service.affinity.update_emotion.assert_not_called()

    async def test_unknown_verdicts_do_not_update_memory_or_affinity(self):
        for target in ("input", "output"):
            with self.subTest(target=target):
                self.service.memory.append_pending_log.reset_mock()
                self.moderator.judge_voice_input.return_value = VERDICT_UNKNOWN if target == "input" else VERDICT_SAFE
                self.moderator.judge_voice_turn.return_value = (
                    (VERDICT_UNKNOWN, None) if target == "output" else (VERDICT_SAFE, (1, 1))
                )
                await self.complete_utterance()
                await self.respond()
                self.service.memory.append_pending_log.assert_not_awaited()
                self.service.blacklist.record_violation.assert_not_awaited()
                self.assertFalse(self.conversation._current.discarded)  # 判定不能では止めない

    async def test_transcript_growing_after_the_judgement_is_judged_again(self):
        """安全と判定した前半の後に続きが届いたら、続きも含めて判定し直す（未判定の本文を記憶しない）。"""
        verdicts = [VERDICT_SAFE, VERDICT_UNSAFE]
        self.moderator.judge_voice_input.side_effect = lambda text: verdicts.pop(0)
        await self.complete_utterance(transcript="架空の前半")
        await self.settle()
        self.assertEqual(self.moderator.judge_voice_input.await_args_list[0].args[0], "架空の前半")
        await self.conversation._on_live_message(
            live_message(input_transcription=SimpleNamespace(text="と架空の後半")),
        )
        self.advance(1.0)
        for _ in range(3):
            await asyncio.sleep(constants.LIVE_TICK_SECONDS)
        self.assertEqual(self.moderator.judge_voice_input.await_args_list[1].args[0], "架空の前半と架空の後半")
        self.service.blacklist.record_violation.assert_awaited_once()
        await self.respond()
        self.service.memory.append_pending_log.assert_not_awaited()

    async def test_safe_continuation_is_remembered_in_full_after_rejudging(self):
        await self.complete_utterance(transcript="架空の前半")
        await self.settle()
        await self.conversation._on_live_message(
            live_message(input_transcription=SimpleNamespace(text="と後半")),
        )
        self.advance(1.0)
        await self.respond()
        for _ in range(3):
            await asyncio.sleep(constants.LIVE_TICK_SECONDS)
        self.assertEqual(self.moderator.judge_voice_input.await_count, 2)
        self.service.memory.append_pending_log.assert_awaited_once_with(
            str(USER_A), "架空の前半と後半", "架空の返答",
        )

    async def test_unjudged_text_is_not_remembered(self):
        await self.complete_utterance(transcript="架空の前半")
        await self.settle()
        utterance = self.conversation._current
        utterance.input_text += "と判定前の続き"  # 判定していない本文が混ざった状態
        await self.conversation._remember(utterance)
        self.service.memory.append_pending_log.assert_not_awaited()

    async def test_input_ng_word_stops_reply_and_counts_once(self):
        self.speak(USER_A, self.start_frames())
        for chunk in ("架空の", "NG", "です"):
            await self.conversation._on_live_message(
                live_message(input_transcription=SimpleNamespace(text=chunk)),
            )
        await self.settle()
        self.service.blacklist.record_violation.assert_awaited_once()
        self.assertTrue(self.conversation._current.discarded)
        content = self.channel.send.await_args.args[0]
        self.assertIn(f"<@{USER_A}>", content)
        self.assertIn("あと2回", content)
        mentions = self.channel.send.await_args.kwargs["allowed_mentions"]
        self.assertEqual([user.id for user in mentions.users], [USER_A])
        self.assertFalse(mentions.everyone)
        # 返答の音声が届いても再生しない
        self.advance(1.0)
        self.conversation.tick()
        await self.respond()
        self.voice_client.play.assert_not_called()
        self.service.memory.append_pending_log.assert_not_awaited()
        self.service.blacklist.record_violation.assert_awaited_once()

    async def test_ai_unsafe_input_stops_playback_and_counts_once(self):
        self.moderator.judge_voice_input.return_value = VERDICT_UNSAFE
        await self.complete_utterance()
        await self.settle()
        self.service.blacklist.record_violation.assert_awaited_once()
        await self.respond()
        self.voice_client.play.assert_not_called()
        self.service.blacklist.record_violation.assert_awaited_once()

    async def test_output_ng_word_stops_reply_without_blaming_the_user(self):
        await self.complete_utterance()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_called_once()
        self.voice_client.is_playing.return_value = True
        await self.conversation._on_live_message(
            live_message(output_transcription=SimpleNamespace(text="架空のNGワード")),
        )
        source = self.voice_client.play.call_args.args[0]
        self.assertTrue(source.aborted)
        self.voice_client.stop.assert_called()
        await self.respond(text="")
        self.service.blacklist.record_violation.assert_not_awaited()
        self.service.memory.append_pending_log.assert_not_awaited()

    async def test_ai_unsafe_output_discards_the_unplayed_rest(self):
        self.moderator.judge_voice_turn.return_value = (VERDICT_UNSAFE, None)
        await self.complete_utterance()
        await self.respond()
        source = self.voice_client.play.call_args.args[0]
        self.assertTrue(source.aborted)
        self.service.blacklist.record_violation.assert_not_awaited()
        self.service.memory.append_pending_log.assert_not_awaited()

    async def start_reply_and_barge_in(self):
        """A の返答の再生中に、B が保持を越えて話し続ける（Live へ送られる）。"""
        await self.complete_utterance()
        await self.respond(complete=False)
        source = self.voice_client.play.call_args.args[0]
        previous = self.conversation._current
        self.drain()
        self.speak(USER_B, self.barge_in_frames())
        self.assertEqual(self.conversation._active_user, USER_B)
        return source, previous

    async def test_short_backchannel_during_reply_is_not_sent_and_does_not_stop_it(self):
        """ペルソナの声が出ている間の相槌は Live へ送らず、返答も止めない（#409）。"""
        await self.complete_utterance()
        await self.respond(complete=False)
        source = self.voice_client.play.call_args.args[0]
        self.drain()
        self.speak(USER_B, self.start_frames() + 10)  # 0.6秒ほどの「うんうん」
        self.assertIsNone(self.conversation._active_user)
        self.assertEqual(self.drain(), [])
        self.advance(1.0)  # 途切れたので相槌として捨てる
        self.conversation.tick()
        self.assertIsNone(self.conversation._participants[USER_B].held)
        self.assertFalse(source.aborted)
        self.assertFalse(source.fading)
        self.assertEqual(self.conversation._speaker_state.holds_dropped, 1)

    async def test_long_speech_is_sent_with_its_start_and_reply_keeps_playing_until_confirmed(self):
        source, previous = await self.start_reply_and_barge_in()
        kinds = self.drain()
        self.assertEqual(kinds[0], "start")
        # 保持していた話し始め（プリロールを含む約2秒分）もまとめて送る
        self.assertGreaterEqual(kinds.count("audio"), int(constants.LIVE_BARGE_IN_HOLD_SECONDS * 1000) // constants.LIVE_INPUT_CHUNK_MS)
        self.assertFalse(source.aborted)
        self.assertFalse(source.fading)
        self.assertTrue(self.conversation._awaiting_interrupt_ack)
        self.assertTrue(previous.cut_off)
        # 打ち切りの通知までに届いた前の返答の残りは、新しい発言の返答にせず同じ音源へ流す
        before = source.buffered_bytes
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(2400))))
        self.assertGreater(source.buffered_bytes, before)
        self.assertEqual(self.voice_client.play.call_count, 1)
        # 打ち切りの通知を受けても、届いている分は流し切る
        await self.conversation._on_live_message(live_message(interrupted=True))
        self.assertTrue(source.finished)
        self.assertFalse(source.aborted)

    async def test_filler_does_not_confirm_but_words_fade_the_reply_out(self):
        source, previous = await self.start_reply_and_barge_in()
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("えーと、")
        self.assertFalse(source.fading)
        self.assertFalse(previous.discarded)
        await self.transcript("ちょっと待って")
        self.assertTrue(source.fading)
        self.assertTrue(previous.discarded)
        self.assertEqual(self.conversation._current.barge_in, "confirmed")
        # 即座には止めず、強めに絞ってから止める（未再生の残りは捨てる）
        self.voice_client.stop.assert_not_called()
        frames = []
        while chunk := source.read():
            frames.append(chunk)
        self.assertEqual(len(frames), constants.LIVE_BARGE_IN_FADE_MS // 20)
        # 新しい発言の返答はふつうに再生する
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.voice_client.play.reset_mock()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_called_once()

    async def test_unconfirmed_barge_in_reply_replaces_the_old_one_with_a_fade(self):
        """言いよどみだけで割り込みにならなかったら、その発言への返答（確認）で前の返答を差し替える。"""
        source, _ = await self.start_reply_and_barge_in()
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("あのー、えーっと")
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.assertFalse(source.fading)
        self.voice_client.play.reset_mock()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.assertTrue(source.fading)
        self.voice_client.play.assert_called_once()
        self.assertEqual(self.conversation._barge_in_stats["unconfirmed"], 1)

    async def test_reply_to_the_bots_own_voice_coming_back_is_not_played(self):
        """回り込んだ自分の声への返答は再生しない（音声ループの防止。#409）。"""
        await self.complete_utterance()
        await self.respond(complete=False, text="明日は晴れて気持ちのいい一日になりそうですね")
        self.speak(USER_B, self.barge_in_frames())
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("明日は晴れて気持ちのいい一日")
        # 自分の声なので割り込みとは確定しない
        self.assertEqual(self.conversation._current.barge_in, "pending")
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        echo_turn = self.conversation._current
        self.voice_client.play.reset_mock()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_not_called()
        self.assertTrue(echo_turn.discarded)
        self.assertEqual(self.conversation._barge_in_stats["echo"], 1)

    async def test_speech_right_after_the_reply_ends_is_also_checked_for_echo(self):
        await self.complete_utterance()
        await self.respond(text="明日は晴れて気持ちのいい一日になりそうですね")
        source = self.voice_client.play.call_args.args[0]
        while source.read():
            pass
        self.voice_client.play.call_args.kwargs["after"](None)
        await self.settle()
        # 声が止まった直後（回り込みがまだ届きうる）に話し始めた発言
        self.speak(USER_B, self.start_frames())
        self.assertTrue(self.conversation._current.echo_suspect)
        await self.transcript("明日は晴れて気持ちのいい一日")
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.voice_client.play.reset_mock()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_not_called()
        # しばらくたってからの発言は疑わない
        self.advance(constants.LIVE_ECHO_GUARD_SECONDS + 1)
        self.speak(USER_A, self.start_frames())
        self.assertFalse(self.conversation._current.echo_suspect)

    async def test_echo_level_from_the_canceller_does_not_start_a_turn(self):
        """エコー除去が回り込みと判定したフレーム（発話の強さ 0）では話し始めない。"""
        for _ in range(self.barge_in_frames()):
            self.conversation._on_audio(USER_B, VOICED, 0.0)
            self.advance(0.02)
        self.assertIsNone(self.conversation._active_user)
        self.speak(USER_B, self.start_frames())
        self.assertEqual(self.conversation._active_user, USER_B)

    async def test_stop_cuts_a_fading_reply_immediately(self):
        source, _ = await self.start_reply_and_barge_in()
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("ちょっと待って")
        self.assertTrue(source.fading)
        self.conversation._pause()
        self.assertTrue(source.aborted)
        self.assertEqual(source.read(), b"")

    async def test_stop_logs_barge_in_counts_without_text_or_ids(self):
        """しきい値の実験用に件数だけを出す。文字起こしの本文・話者の ID は出さない（#409）。"""
        await self.start_reply_and_barge_in()
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("ちょっと待って")
        with self.assertLogs(voice_conversation.logger, level="INFO") as logs:
            await self.conversation.stop()
        joined = "\n".join(logs.output)
        self.assertIn("候補 1 回", joined)
        self.assertIn("確定 1 回", joined)
        self.assertNotIn("ちょっと待って", joined)
        self.assertNotIn(str(USER_B), joined)

    async def test_stop_logs_echo_canceller_estimates(self):
        receiver = Mock()
        receiver.echo_stats.return_value = [
            SimpleNamespace(delay_ms=240.0, erle_db=18.0), SimpleNamespace(delay_ms=None, erle_db=None),
        ]
        self.conversation._receiver = receiver
        with self.assertLogs(voice_conversation.logger, level="INFO") as logs:
            await self.conversation.stop()
        joined = "\n".join(logs.output)
        self.assertIn("遅延を推定できた話者 1 人", joined)
        self.assertIn("240", joined)
        receiver.stop.assert_called_once()

    async def test_barge_in_while_waiting_for_the_reply_still_cuts_in_right_away(self):
        """まだ声が出ていない（生成待ちの）間は、従来どおり LIVE_BARGE_IN_MS ですぐに割り込む。"""
        await self.complete_utterance()
        self.speak(USER_B, constants.LIVE_BARGE_IN_MS // 20)
        self.assertEqual(self.conversation._active_user, USER_B)
        self.assertIsNone(self.conversation._current.barge_in)

    async def test_barge_in_before_the_first_reply_chunk_still_waits_for_the_interrupt_notice(self):
        """返答の最初のチャンクが届く前の割り込みでも、前の人宛ての遅れた返答を通さない（#412）。"""
        await self.complete_utterance()
        previous = self.conversation._current
        self.assertFalse(previous.response_started)
        self.speak(USER_B, constants.LIVE_BARGE_IN_MS // 20)
        self.assertEqual(self.conversation._active_user, USER_B)
        self.assertTrue(previous.discarded)
        self.assertTrue(self.conversation._awaiting_interrupt_ack)
        self.assertIs(self.conversation._interrupted, previous)
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.voice_client.play.reset_mock()
        # 前の人宛ての返答が遅れて届いても、新しい発言への返答として流さない
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_not_called()
        # interrupted で待ちが解け、前の人の文字起こしの帰属も閉じる
        await self.conversation._on_live_message(live_message(interrupted=True))
        self.assertFalse(self.conversation._awaiting_interrupt_ack)
        self.assertIsNone(self.conversation._interrupted)
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_called_once()

    async def test_missing_interrupt_notice_does_not_block_replies_forever(self):
        await self.complete_utterance()
        await self.respond(complete=False)
        self.speak(USER_B, self.barge_in_frames())
        self.assertTrue(self.conversation._awaiting_interrupt_ack)
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.advance(constants.LIVE_PLAYBACK_STALL_SECONDS)
        self.voice_client.play.reset_mock()
        await self.conversation._on_live_message(live_message(model_turn=audio_turn(bytes(24000))))
        self.voice_client.play.assert_called_once()

    async def test_pause_discards_the_reply_in_progress(self):
        await self.complete_utterance()
        await self.respond(complete=False)
        source = self.voice_client.play.call_args.args[0]
        self.conversation.withdraw(USER_B)
        self.assertTrue(source.aborted)
        self.assertFalse(self.conversation.is_open)

    async def test_reconnect_discards_the_turn_in_progress(self):
        self.speak(USER_A, self.start_frames())
        self.conversation._on_live_reconnected()
        self.assertIsNone(self.conversation._active_user)
        self.assertTrue(self.conversation._current.discarded)
        # 音声は捨て、発話の区切りは残す（対応の取れない区切りは送信側が捨てる）
        self.assertEqual(self.drain(), ["start", "end"])


class SenderTests(ConversationTestBase):
    """発話の区切りと音声の順序を、接続の世代ごとに保って送る。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent = []
        live = self.live
        live.generation = 1

        async def record(kind, payload=None):
            self.sent.append((live.generation, kind))
            return True

        live.activity_start = lambda: record("start")
        live.send_audio = lambda pcm: record("audio")
        live.activity_end = lambda: record("end")
        self.consent_all()

    async def run_sender(self):
        task = asyncio.create_task(self.conversation._run_sender())
        await self.settle()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_start_audio_end_are_sent_in_order(self):
        self.speak(USER_A, self.start_frames())
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000)
        self.conversation.tick()
        await self.run_sender()
        kinds = [kind for _, kind in self.sent]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "end")
        self.assertTrue(set(kinds[1:-1]) == {"audio"})

    async def test_pause_keeps_the_end_of_an_already_started_utterance(self):
        self.speak(USER_A, self.start_frames())
        await self.run_sender()
        self.sent.clear()
        self.speak(USER_A, 10)
        self.conversation.withdraw(USER_B)  # 一時停止。送信待ちの音声は捨てる
        await self.run_sender()
        self.assertEqual(self.sent, [(1, "end")])

    async def test_orphan_markers_are_not_sent_to_a_new_connection(self):
        self.speak(USER_A, self.start_frames())
        await self.run_sender()
        self.sent.clear()
        self.live.generation = 2  # 再接続した
        self.conversation._on_live_reconnected()
        await self.run_sender()
        self.assertEqual(self.sent, [])


class TranscriptOwnershipTests(ConversationTestBase):
    """入力の文字起こしは発話権とは別に帰属させ、遅れて届いた分を次の話者へ付けない。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.consent_all()
        self.conversation.toggle_memory(USER_A)

    async def respond(self, *, complete=True):
        await self.conversation._on_live_message(live_message(
            model_turn=audio_turn(bytes(24000)), output_transcription=SimpleNamespace(text="架空の返答"),
        ))
        if complete:
            await self.conversation._on_live_message(live_message(generation_complete=True, turn_complete=True))
        await self.settle()

    async def barge_in(self) -> None:
        """A の返答中（A の文字起こしはまだ閉じていない）に B が割り込む。"""
        await self.complete_utterance(USER_A, "架空の質問")
        await self.respond(complete=False)
        self.conversation.toggle_memory(USER_B)
        self.speak(USER_B, self.barge_in_frames())
        self.assertEqual(self.conversation._active_user, USER_B)

    async def test_late_fragment_after_barge_in_is_not_given_to_the_next_speaker(self):
        await self.barge_in()
        previous = self.conversation._transcript_blocked
        current = self.conversation._current
        self.assertEqual(previous.user_id, USER_A)
        await self.transcript("架空のNG")  # A の残りか B の冒頭か区別できない
        await self.settle()
        self.service.blacklist.record_violation.assert_not_awaited()
        self.assertEqual(current.input_text, "")
        self.assertEqual(previous.input_text, "架空の質問")
        self.assertTrue(previous.transcript_incomplete)
        self.assertTrue(current.transcript_incomplete)

    async def test_finished_signal_hands_the_following_fragments_to_the_next_speaker(self):
        await self.barge_in()
        current = self.conversation._current
        await self.transcript(finished=True)  # A の文字起こしが閉じた
        await self.transcript("架空のBの発言")
        self.assertEqual(current.input_text, "架空のBの発言")
        self.assertFalse(current.transcript_incomplete)
        await self.transcript("架空のNG")
        await self.settle()
        self.service.blacklist.record_violation.assert_awaited_once()
        self.assertEqual(self.service.blacklist.record_violation.await_args.args[0], USER_B)

    async def test_interrupted_notice_closes_the_previous_transcript(self):
        await self.barge_in()
        current = self.conversation._current
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.transcript("架空のBの発言")
        self.assertEqual(current.input_text, "架空のBの発言")

    async def test_completed_reply_closes_the_transcript_before_the_next_speaker(self):
        await self.complete_utterance(USER_A, "架空の質問")
        await self.respond()  # 返答の終わり（turn_complete）で A の文字起こしは閉じる
        self.speak(USER_B, self.barge_in_frames())
        self.assertIsNone(self.conversation._transcript_blocked)
        await self.transcript("架空のBの発言")
        self.assertEqual(self.conversation._current.input_text, "架空のBの発言")

    async def test_missing_close_signal_times_out_and_skips_memory_for_the_gap(self):
        await self.barge_in()
        current = self.conversation._current
        await self.transcript("架空の断片")  # 捨てられる
        self.advance(constants.LIVE_PLAYBACK_STALL_SECONDS)
        self.conversation.tick()
        self.assertIsNone(self.conversation._transcript_blocked)
        await self.transcript("架空のBの発言")
        self.assertEqual(current.input_text, "架空のBの発言")
        # 冒頭を捨てた可能性がある発言は記憶に使わない
        self.service.memory.append_pending_log.reset_mock()
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        await self.conversation._on_live_message(live_message(interrupted=True))
        await self.respond()
        self.service.memory.append_pending_log.assert_not_awaited()

    async def test_third_speaker_during_ambiguity_loses_the_middle_transcript(self):
        await self.barge_in()
        middle = self.conversation._current
        self.channel.members.append(member(USER_C))
        self.conversation.grant_voice(USER_C)
        self.advance(constants.LIVE_VAD_SILENCE_MS / 1000 + 0.1)
        self.conversation.tick()
        self.speak(USER_C, self.barge_in_frames())
        self.assertTrue(middle.transcript_incomplete)
        await self.transcript(finished=True)
        await self.transcript("架空のCの発言")
        self.assertEqual(self.conversation._current.input_text, "架空のCの発言")
        self.assertEqual(middle.input_text, "")


class LifecycleTests(ConversationTestBase):
    async def test_quota_close_ends_with_notice_and_disconnect(self):
        await self.conversation._on_live_closed(CLOSED_QUOTA)
        await self.settle()
        self.service.end.assert_awaited_once()
        kwargs = self.service.end.await_args.kwargs
        self.assertEqual(kwargs["notice"], NOTICE_LIVE_QUOTA)
        self.assertTrue(kwargs["disconnect"])

    async def test_lost_voice_connection_ends_once_without_disconnecting(self):
        self.voice_client.is_connected.return_value = False
        for _ in range(3):
            self.conversation.tick()
        await self.settle()
        self.service.end.assert_awaited_once()
        self.assertFalse(self.service.end.await_args.kwargs["disconnect"])

    async def test_stop_closes_live_and_finishes_the_panel(self):
        panel = SimpleNamespace(edit=AsyncMock())
        view = Mock()
        self.conversation.panel_message = panel
        self.conversation.panel_view = view
        await self.conversation.stop()
        self.assertFalse(self.live.connected)
        view.stop.assert_called_once()
        self.assertIsNone(panel.edit.await_args.kwargs["view"])
        self.assertIn("終了しました", panel.edit.await_args.kwargs["content"])
        await self.conversation.stop()  # 二度目は何もしない
        self.assertEqual(panel.edit.await_count, 1)

    async def test_stop_waits_for_the_running_panel_update_before_the_final_edit(self):
        """実行中の「会話中です」の編集が終わってから、終了表示に更新する（#412）。"""
        order = []

        async def edit(**kwargs):
            if "view" not in kwargs:
                order.append("update-start")
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    # 通信の後始末に時間がかかる状況を再現する。待たずに終了表示へ進むと、
                    # この後始末より先に「終了表示」の編集が走ってしまう。
                    await asyncio.sleep(0.05)
                    order.append("update-cancelled")
                    raise
            else:
                order.append("final")

        self.conversation.panel_message = SimpleNamespace(edit=edit)
        self.conversation.panel_view = Mock()
        self.conversation._panel_updated_at = -1e9
        self.conversation._schedule_panel_update()
        await self.settle()
        self.assertEqual(order, ["update-start"])
        await self.conversation.stop()
        self.assertEqual(order, ["update-start", "update-cancelled", "final"])

    async def test_usage_is_logged_without_text(self):
        usage = SimpleNamespace(total_token_count=10)
        await self.conversation._on_live_message(SimpleNamespace(server_content=None, usage_metadata=usage))
        self.service.usage.log_tokens.assert_called_once_with("音声会話", usage)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, **kwargs):
        kwargs.setdefault("live_connect", Mock())
        return VoiceConversationService(
            SimpleNamespace(user=SimpleNamespace(id=BOT_ID)), None,
            moderator=None, ng_filter=None, blacklist=SimpleNamespace(is_blocked=Mock(return_value=False)),
            memory=None, affinity=None, usage=None, **kwargs,
        )

    def test_reservation_enforces_the_concurrent_limit(self):
        service = self.make_service()
        for guild_id in range(constants.LIVE_MAX_CONCURRENT_SESSIONS):
            self.assertTrue(service.reserve(guild_id))
        self.assertFalse(service.reserve(99))
        self.assertTrue(service.reserve(0))  # 確保済みのサーバーは何度でも
        service.release_reservation(0)
        self.assertTrue(service.reserve(99))

    async def test_start_failure_releases_the_slot_and_cleans_up(self):
        service = self.make_service()
        self.assertTrue(service.reserve(1))
        guild = SimpleNamespace(id=1, me=None, voice_client=None)
        channel = SimpleNamespace(id=CHANNEL_ID, permissions_for=Mock(), send=AsyncMock())
        settings = SimpleNamespace(prompt_file="", grounding_enabled=False)
        with patch.object(VoiceConversation, "start", AsyncMock(side_effect=LiveUnavailableError(quota=False))), \
                patch.object(VoiceConversation, "stop", AsyncMock()) as stop:
            with self.assertRaises(LiveUnavailableError):
                await service.start(guild, Mock(), channel, settings, fallback_channel=None)
        stop.assert_awaited_once()
        self.assertIsNone(service.get(1))
        self.assertNotIn(1, service._reserved)

    def test_replacement_reservation_keeps_one_slot_and_has_its_own_identity(self):
        service = self.make_service()
        service._sessions[1] = Mock()
        first = service.reserve(1)
        self.assertIs(service.reserve(1), first)
        for guild_id in range(2, constants.LIVE_MAX_CONCURRENT_SESSIONS + 1):
            self.assertTrue(service.reserve(guild_id))
        service._sessions.pop(1)
        self.assertFalse(service.reserve(99))
        service.release_reservation(1)
        second = service.reserve(1)
        self.assertIsNot(first, second)
        service.release_reservation(1, reservation=first)
        self.assertIs(service.reserve(1), second)

    async def test_stop_during_start_never_registers_or_releases_a_new_reservation(self):
        """各待機点で停止し、停止後の予約を古い開始処理が消さないことも確かめる。"""
        for replacing in (False, True):
            for phase in ("prompt", "live", "panel"):
                for reserve_again in (False, True):
                    with self.subTest(replacing=replacing, phase=phase, reserve_again=reserve_again):
                        service = self.make_service()
                        if replacing:
                            service._sessions[1] = SimpleNamespace(stop=AsyncMock())
                        reservation = service.reserve(1)
                        guild = SimpleNamespace(id=1, me=None, voice_client=None)
                        channel = SimpleNamespace(id=CHANNEL_ID)
                        entered, resume = asyncio.Event(), asyncio.Event()

                        async def wait_at(current):
                            if phase == current:
                                entered.set()
                                await resume.wait()

                        async def load(_path):
                            await wait_at("prompt")
                            return "架空のペルソナ"

                        async def start(_conversation):
                            await wait_at("live")

                        async def panel(_conversation):
                            await wait_at("panel")

                        with patch.object(voice_conversation, "load_persona_template", load), \
                                patch.object(VoiceConversation, "start", start), \
                                patch.object(VoiceConversation, "stop", AsyncMock()) as cleanup, \
                                patch.object(service, "_post_panel", panel), \
                                patch.object(voice_conversation, "can_bot_send_messages", return_value=False):
                            task = asyncio.create_task(service.start(
                                guild, Mock(), channel, SimpleNamespace(prompt_file="", grounding_enabled=False),
                                reservation=reservation,
                            ))
                            try:
                                await asyncio.wait_for(entered.wait(), 2)
                                # 既存会話を外した後にも予約枠が残り、上限を超えない。
                                self.assertIn(1, service._reserved)
                                for guild_id in range(2, constants.LIVE_MAX_CONCURRENT_SESSIONS + 1):
                                    self.assertTrue(service.reserve(guild_id))
                                self.assertFalse(service.reserve(99))
                                await service.stop(1, reason="架空の退出")
                                new_reservation = service.reserve(1) if reserve_again else None
                                resume.set()
                                with self.assertRaises(asyncio.CancelledError):
                                    await asyncio.wait_for(task, 2)
                                self.assertIsNone(service.get(1))
                                if reserve_again:
                                    self.assertIs(service._reserved[1], new_reservation)
                                else:
                                    self.assertNotIn(1, service._reserved)
                                self.assertEqual(cleanup.await_count, 0 if phase == "prompt" else 1)
                            finally:
                                resume.set()
                                if not task.done():
                                    task.cancel()
                                await asyncio.gather(task, return_exceptions=True)

    async def test_stop_all_invalidates_starts_before_they_enter_the_service(self):
        service = self.make_service()
        reservation = service.reserve(1)
        await service.stop_all()
        with patch.object(voice_conversation, "load_persona_template", AsyncMock()) as load:
            with self.assertRaises(asyncio.CancelledError):
                await service.start(
                    SimpleNamespace(id=1), Mock(), Mock(), Mock(), reservation=reservation,
                )
        load.assert_not_awaited()
        self.assertFalse(service._reserved)

    async def test_cancellation_waits_for_replaced_conversation_cleanup(self):
        service = self.make_service()
        entered, resume = asyncio.Event(), asyncio.Event()

        async def stop():
            entered.set()
            await resume.wait()

        service._sessions[1] = SimpleNamespace(stop=stop)
        reservation = service.reserve(1)
        task = asyncio.create_task(service.start(
            SimpleNamespace(id=1), Mock(), Mock(), Mock(), reservation=reservation,
        ))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            self.assertIn(1, service._reserved)
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            self.assertIsNone(service.get(1))
            self.assertNotIn(1, service._reserved)
        finally:
            resume.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_old_end_during_replacement_cleanup_preserves_reservation_and_connection(self):
        service = self.make_service(voice_guard=Mock())
        entered, resume = asyncio.Event(), asyncio.Event()

        async def stop():
            # 実際の VoiceConversation.stop() と同じく、二度目の停止は即座に戻る。
            if entered.is_set():
                return
            entered.set()
            await resume.wait()

        voice_client = Mock(spec=discord.VoiceClient, disconnect=AsyncMock(), is_connected=Mock(return_value=True))
        guild = SimpleNamespace(id=1, me=None, voice_client=voice_client)
        old = SimpleNamespace(
            stop=AsyncMock(side_effect=stop), voice_client=voice_client, guild=guild, _notify=AsyncMock(),
        )
        service._sessions[1] = old
        reservation = service.reserve(1)
        with patch.object(VoiceConversation, "start", AsyncMock()), \
                patch.object(service, "_post_panel", AsyncMock()), \
                patch.object(voice_conversation, "can_bot_send_messages", return_value=False):
            task = asyncio.create_task(service.start(
                guild, voice_client, SimpleNamespace(id=CHANNEL_ID),
                SimpleNamespace(prompt_file="", grounding_enabled=False), reservation=reservation,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await service.end(
                    1, reason="旧会話の終了", notice="架空の終了案内", disconnect=True, conversation=old,
                )
                self.assertIs(service._reserved.get(1), reservation)
                old.stop.assert_awaited_once()
                old._notify.assert_not_awaited()
                service.voice_guard.release.assert_not_called()
                voice_client.disconnect.assert_not_awaited()
                resume.set()
                replacement = await asyncio.wait_for(task, 2)
                self.assertIs(service.get(1), replacement)
                self.assertNotIn(1, service._reserved)
            finally:
                resume.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_start_builds_persona_options_and_posts_the_panel(self):
        service = self.make_service()
        guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        sent = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(id=CHANNEL_ID, members=[], send=AsyncMock(return_value=sent))
        settings = SimpleNamespace(prompt_file="", grounding_enabled=True)
        captured = {}

        async def fake_start(conversation):
            captured["options"] = conversation._live._options

        with patch.object(VoiceConversation, "start", fake_start), \
                patch.object(voice_conversation, "can_bot_send_messages", Mock(return_value=True)):
            conversation = await service.start(guild, Mock(channel=channel), channel, settings)
        self.assertIs(service.get(1), conversation)
        self.assertIn(constants.BUILTIN_PERSONA_PROMPT, captured["options"].system_instruction)
        self.assertIn("音声通話での話し方", captured["options"].system_instruction)
        self.assertTrue(captured["options"].grounding)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        self.assertIs(conversation.panel_message, sent)
        conversation.panel_view.stop()
        with patch.object(VoiceConversation, "stop", AsyncMock()):
            self.assertTrue(await service.stop(1, reason="架空"))
            self.assertFalse(await service.stop(1, reason="架空"))

    async def test_member_updates_skip_the_bot_and_unknown_guilds(self):
        service = self.make_service()
        conversation = Mock()
        service._sessions[1] = conversation
        guild = SimpleNamespace(id=1)
        service.handle_voice_state_update(SimpleNamespace(id=BOT_ID, guild=guild), None, None)
        conversation.handle_member_update.assert_not_called()
        service.handle_voice_state_update(SimpleNamespace(id=USER_A, guild=SimpleNamespace(id=2)), None, None)
        conversation.handle_member_update.assert_not_called()
        service.handle_voice_state_update(SimpleNamespace(id=USER_A, guild=guild), "before", "after")
        conversation.handle_member_update.assert_called_once()

    async def test_end_disconnects_only_the_same_connection(self):
        voice_guard = Mock()
        service = self.make_service(voice_guard=voice_guard)
        voice_client = Mock()
        voice_client.is_connected.return_value = True
        voice_client.disconnect = AsyncMock()
        guild = SimpleNamespace(id=1, voice_client=voice_client)
        conversation = SimpleNamespace(voice_client=voice_client, guild=guild, stop=AsyncMock(), _notify=AsyncMock())
        service._sessions[1] = conversation
        await service.end(1, reason="架空", notice="架空の案内", disconnect=True, conversation=conversation)
        conversation._notify.assert_awaited_once_with("架空の案内")
        voice_guard.release.assert_called_once_with(1)
        voice_client.disconnect.assert_awaited_once_with(force=True)
        # 入れ替わった後の古い会話からの終了要求は無視する
        await service.end(1, reason="架空", disconnect=True, conversation=conversation)
        voice_client.disconnect.assert_awaited_once()

    async def test_end_does_not_disconnect_a_conversation_started_while_waiting(self):
        """終了を待つ間に /join が同じ接続で新しい会話を始めたら、その接続を切らない。"""
        for takeover in ("session", "reservation"):
            with self.subTest(takeover=takeover):
                service = self.make_service(voice_guard=Mock())
                voice_client = Mock()
                voice_client.is_connected.return_value = True
                voice_client.disconnect = AsyncMock()
                guild = SimpleNamespace(id=1, voice_client=voice_client)

                async def stop():
                    if takeover == "session":
                        service._sessions[1] = SimpleNamespace(voice_client=voice_client)
                    else:
                        service.reserve(1)

                old = SimpleNamespace(voice_client=voice_client, guild=guild, stop=stop, _notify=AsyncMock())
                service._sessions[1] = old
                await service.end(1, reason="架空", notice="架空の案内", disconnect=True, conversation=old)
                voice_client.disconnect.assert_not_awaited()
                old._notify.assert_not_awaited()
                service.voice_guard.release.assert_not_called()

    async def test_end_preserves_takeover_while_sending_notice(self):
        """終了案内の送信待ちに再開・予約された接続は、旧会話の後片付けで切らない。"""
        for takeover in ("session", "reservation", "none"):
            with self.subTest(takeover=takeover):
                service = self.make_service(voice_guard=Mock())
                voice_client = Mock()
                voice_client.is_connected.return_value = True
                voice_client.disconnect = AsyncMock()
                guild = SimpleNamespace(id=1, me=object(), voice_client=voice_client)
                channel = SimpleNamespace(id=CHANNEL_ID, members=[], send=AsyncMock())
                entered = asyncio.Event()
                resume = asyncio.Event()

                async def notify(_notice):
                    entered.set()
                    await resume.wait()

                old = SimpleNamespace(
                    voice_client=voice_client, guild=guild, stop=AsyncMock(),
                    _notify=AsyncMock(side_effect=notify),
                )
                service._sessions[1] = old
                ending = asyncio.create_task(service.end(
                    1, reason="架空", notice="架空の案内", disconnect=True, conversation=old,
                ))
                new = None
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    old.stop.assert_awaited_once()
                    self.assertIsNone(service.get(1))
                    if takeover != "none":
                        self.assertTrue(service.reserve(1))
                    if takeover == "session":
                        # 登録処理は実物を使い、外部接続とペルソナ読込だけを差し替える。
                        with patch.object(VoiceConversation, "start", AsyncMock()), \
                                patch.object(voice_conversation, "load_persona_template", AsyncMock(return_value="架空")), \
                                patch.object(voice_conversation, "current_persona_catalog") as catalog, \
                                patch.object(voice_conversation, "can_bot_send_messages", return_value=True):
                            catalog.return_value.voice_profile.return_value = {}
                            new = await service.start(guild, voice_client, channel, SimpleNamespace(
                                prompt_file="", grounding_enabled=False,
                            ))
                        self.assertIs(service.get(1), new)
                        self.assertNotIn(1, service._reserved)
                    resume.set()
                    await asyncio.wait_for(ending, timeout=2)
                    old._notify.assert_awaited_once_with("架空の案内")
                    if takeover == "none":
                        service.voice_guard.release.assert_called_once_with(1)
                        voice_client.disconnect.assert_awaited_once_with(force=True)
                    else:
                        service.voice_guard.release.assert_not_called()
                        voice_client.disconnect.assert_not_awaited()
                        if takeover == "session":
                            self.assertIs(service.get(1), new)
                        else:
                            self.assertIn(1, service._reserved)
                finally:
                    resume.set()
                    await asyncio.wait_for(ending, timeout=2)
                    if new is not None and new.panel_view is not None:
                        new.panel_view.stop()

    async def test_live_ending_while_the_panel_is_posted_fails_the_start(self):
        """開始の途中（パネルの投稿を待つ間）に Live が終わったら、声に応答しない会話を登録しない。"""
        for reason, quota in (("quota", True), ("failed", False)):
            with self.subTest(reason=reason):
                service = self.make_service()
                self.assertTrue(service.reserve(1))
                guild = SimpleNamespace(id=1, me=object(), voice_client=None)
                captured = {}

                async def send(*args, **kwargs):
                    await captured["conversation"]._on_live_closed(reason)
                    return SimpleNamespace(edit=AsyncMock())

                channel = SimpleNamespace(id=CHANNEL_ID, members=[], send=AsyncMock(side_effect=send))

                async def fake_start(conversation):
                    captured["conversation"] = conversation

                with patch.object(VoiceConversation, "start", fake_start), \
                        patch.object(VoiceConversation, "stop", AsyncMock()) as stop, \
                        patch.object(voice_conversation, "can_bot_send_messages", Mock(return_value=True)):
                    with self.assertRaises(LiveUnavailableError) as raised:
                        await service.start(guild, Mock(channel=channel), channel, SimpleNamespace(
                            prompt_file="", grounding_enabled=False,
                        ))
                    await asyncio.sleep(0)  # _on_live_closed が予約した end() は未登録の会話に触らない
                self.assertEqual(raised.exception.quota, quota)
                stop.assert_awaited_once()
                self.assertIsNone(service.get(1))
                self.assertNotIn(1, service._reserved)
                if captured["conversation"].panel_view is not None:
                    captured["conversation"].panel_view.stop()  # stop() を差し替えたため、ここで止める

    async def test_start_fails_when_the_consent_panel_cannot_be_posted(self):
        """同意パネルが無いと誰も同意できないため、開始を失敗にして接続と枠を残さない。"""
        settings = SimpleNamespace(prompt_file="", grounding_enabled=False)
        error = discord.HTTPException(SimpleNamespace(status=403, reason="架空"), "架空の拒否")
        for case in ("send_error", "no_channel"):
            with self.subTest(case=case):
                service = self.make_service()
                self.assertTrue(service.reserve(1))
                guild = SimpleNamespace(id=1, me=object(), voice_client=None)
                channel = SimpleNamespace(id=CHANNEL_ID, members=[], send=AsyncMock(side_effect=error))
                sendable = case == "send_error"
                with patch.object(VoiceConversation, "start", AsyncMock()), \
                        patch.object(VoiceConversation, "stop", AsyncMock()) as stop, \
                        patch.object(voice_conversation, "can_bot_send_messages", Mock(return_value=sendable)):
                    with self.assertLogs("persona_bot.services.voice_conversation", level="WARNING"):
                        with self.assertRaises(voice_conversation.ConsentPanelError):
                            await service.start(guild, Mock(channel=channel), channel, settings, fallback_channel=None)
                stop.assert_awaited_once()
                self.assertIsNone(service.get(1))
                self.assertNotIn(1, service._reserved)


if __name__ == "__main__":
    unittest.main()
