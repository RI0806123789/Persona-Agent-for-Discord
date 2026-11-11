"""話者の予約と文字起こしの帰属を、音声接続なしで検証する。"""

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import (
    LIVE_BARGE_IN_GAP_MS, LIVE_BARGE_IN_HOLD_SECONDS, LIVE_SPEAKER_REQUEST_TTL_SECONDS,
)
from persona_bot.services.voice_conversation import _HOLD_MAX_FRAMES, _SpeakerState, _TranscriptState, _Utterance

FRAME = (20000).to_bytes(2, "little", signed=True) * 320
QUIET_BOT = {"voiced": True, "bot_speaking": lambda: False, "bot_audible": lambda: False}
AUDIBLE_BOT = {"voiced": True, "bot_speaking": lambda: True, "bot_audible": lambda: True}


class SpeakerStateTests(unittest.TestCase):
    def test_reservation_expires_and_duplicate_requests_do_not_change_order(self):
        state = _SpeakerState()
        state.request_floor(1, 0.0)
        state.request_floor(1, 0.5)
        state.request_floor(2, 1.0)
        self.assertFalse(state.floor_available(2, 1.0))
        self.assertTrue(state.floor_available(2, LIVE_SPEAKER_REQUEST_TTL_SECONDS + 0.5))

    def test_active_speaker_frames_are_forwarded_and_others_reserved(self):
        state = _SpeakerState(active_user=1, speaking=True)
        action, _ = state.receive(1, FRAME, 1.0, **QUIET_BOT)
        self.assertEqual(action, "send")
        for index in range(20):
            state.receive(2, FRAME, 1.0 + index * 0.02, **QUIET_BOT)
        self.assertEqual([user for user, _ in state.reservations], [2])

    def test_speech_while_the_bot_is_audible_is_held_then_sent_after_the_hold(self):
        """ペルソナの声が出ている間は手元に留め、LIVE_BARGE_IN_HOLD_SECONDS 続いたら送る（#409）。"""
        state = _SpeakerState()
        actions = []
        for index in range(200):
            action, participant = state.receive(2, FRAME, index * 0.02, **AUDIBLE_BOT)
            actions.append(action)
            if action == "barge_in":
                break
        self.assertEqual(actions[-1], "barge_in")
        self.assertNotIn("begin", actions)
        self.assertEqual(state.holds_started, 1)
        # 最初の有声フレームから数えて、保持の長さを越えたところで送る
        self.assertAlmostEqual((len(actions) - 1) * 0.02, LIVE_BARGE_IN_HOLD_SECONDS, delta=0.05)
        pending = participant.take_pending_audio()
        self.assertEqual(len(pending), len(actions))  # 話し始め（プリロール）から全部送る
        self.assertLessEqual(len(pending), _HOLD_MAX_FRAMES)
        self.assertIsNone(participant.held)

    def test_held_backchannel_is_dropped_after_a_gap(self):
        state = _SpeakerState()
        for index in range(20):
            state.receive(2, FRAME, index * 0.02, **AUDIBLE_BOT)
        self.assertIsNotNone(state.participants[2].held)
        state.expire_holds(0.4 + LIVE_BARGE_IN_GAP_MS / 1000)
        self.assertIsNone(state.participants[2].held)
        self.assertEqual(state.holds_dropped, 1)

    def test_held_speech_becomes_a_normal_turn_when_the_bot_stops_talking(self):
        state = _SpeakerState()
        for index in range(20):
            state.receive(2, FRAME, index * 0.02, **AUDIBLE_BOT)
        action, participant = state.receive(2, FRAME, 0.42, voiced=True, bot_speaking=lambda: False, bot_audible=lambda: False)
        self.assertEqual(action, "begin")
        self.assertEqual(len(participant.take_pending_audio()), 21)  # 留めていた分も語頭から送る

    def test_silent_frames_do_not_start_a_hold(self):
        state = _SpeakerState()
        for index in range(50):
            action, _ = state.receive(2, FRAME, index * 0.02, voiced=False, bot_speaking=lambda: True, bot_audible=lambda: True)
            self.assertEqual(action, "wait")
        self.assertEqual(state.holds_started, 0)


class TranscriptStateTests(unittest.TestCase):
    def test_overlapping_transcripts_do_not_change_the_previous_owner(self):
        state = _TranscriptState()
        first, second, third = (_Utterance(index, index, 0.0) for index in (1, 2, 3))
        state.assign(first, 1.0)
        state.assign(second, 2.0)
        state.assign(third, 3.0)
        self.assertIsNone(state.owner)
        self.assertIs(state.blocked, first)
        self.assertTrue(second.transcript_incomplete)
        state.close(first)
        self.assertIs(state.owner, third)
        state.reset()
        self.assertTrue(third.transcript_closed)
        self.assertIsNone(state.owner)
