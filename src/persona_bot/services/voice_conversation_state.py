"""音声会話の状態オブジェクト（同意・参加者のVAD・発言・話者の管理・文字起こしの帰属）。

`voice_conversation.py` から切り出した。Discord・Live・再生には触れず、時刻と音声フレームを
受け取って判定だけを返す純粋な状態（#415）。`VoiceConversation` は `_speaker_state` と
`_transcript_state` を通して使う。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from persona_bot.config.constants import (
    LIVE_BARGE_IN_GAP_MS,
    LIVE_BARGE_IN_HOLD_SECONDS,
    LIVE_BARGE_IN_MS,
    LIVE_INPUT_SAMPLE_RATE,
    LIVE_SPEAKER_QUEUE_MAX,
    LIVE_SPEAKER_REQUEST_TTL_SECONDS,
    LIVE_VAD_PREROLL_MS,
    LIVE_VAD_START_GAP_MS,
    LIVE_VAD_START_MS,
)

# 16kHz・モノラル・16bit の1ミリ秒あたりのバイト数。
_INPUT_BYTES_PER_MS = LIVE_INPUT_SAMPLE_RATE * 2 // 1000
_PREROLL_FRAMES = max(1, LIVE_VAD_PREROLL_MS // 20)
# 割り込みの候補として手元に留める音声の上限（保持の長さ＋プリロール。20ms 単位）。
# ★ 送るときは一度に送信待ちへ積むため、LIVE_INPUT_QUEUE_MAX_CHUNKS の範囲に収める（超えた分は捨てられる）。
_HOLD_MAX_FRAMES = int(LIVE_BARGE_IN_HOLD_SECONDS * 50) + _PREROLL_FRAMES


@dataclass
class ConsentState:
    """接続中だけ保持する同意（user_id の集合）。記憶・親密度は音声処理とは別に数える。"""

    voice: set[int] = field(default_factory=set)
    memory: set[int] = field(default_factory=set)
    affinity: set[int] = field(default_factory=set)


@dataclass
class _Participant:
    """1人分のローカル VAD の状態。プリロールは話し始めの判定前の音声（語頭の欠け防止）。"""

    preroll: deque[bytes] = field(default_factory=lambda: deque(maxlen=_PREROLL_FRAMES))
    voiced_ms: float = 0.0
    last_voiced_at: float = float("-inf")
    # ペルソナの声が出ている間に話し始めた、割り込みの候補の音声（まだ Live へ送っていない。メモリ上だけ）。
    held: list[bytes] | None = None
    held_started_at: float = 0.0
    held_last_voiced_at: float = float("-inf")

    def take_pending_audio(self) -> list[bytes]:
        """話し始めに Live へ送る音声（留めていた候補か、プリロール）を取り出す。"""
        frames = self.held if self.held is not None else list(self.preroll)
        self.drop_pending_audio()
        return frames

    def drop_pending_audio(self) -> None:
        self.held = None
        self.preroll.clear()
        self.voiced_ms = 0.0


@dataclass
class _Utterance:
    """参加者の1回の発言と、それに対するボットの返答（1ターン分）。"""

    turn_id: int
    user_id: int
    started_at: float
    ended_at: float | None = None
    input_text: str = ""
    input_updated_at: float = float("-inf")
    output_text: str = ""
    input_verdict: str | None = None
    # input_verdict が判定した本文。判定の後に文字起こしが増えたら判定し直し、
    # 判定していない本文を記憶に保存しない。
    judged_input: str | None = None
    output_verdict: str | None = None
    emotion: tuple[int, int] | None = None
    # 返答を捨てた（違反・割り込み・一時停止）。以後の音声は再生しない。
    discarded: bool = False
    violation_recorded: bool = False
    output_ng: bool = False
    response_started: bool = False
    response_complete: bool = False
    first_audio_at: float | None = None
    playback_started_at: float | None = None
    input_task: asyncio.Task[Any] | None = None
    # 入力の文字起こしが出そろった（サーバーの finished・返答の終わり・割り込みの通知・時間切れ）。
    transcript_closed: bool = False
    # 帰属を確定できない文字起こしを捨てた。足りない文字起こしは記憶に使わない。
    transcript_incomplete: bool = False
    # ペルソナの声が出ている間に話し始めた発言の割り込みの状態（None: 割り込みではない、
    # "pending": 文字起こしで確かめている、"confirmed": 割り込みと確定した）。
    barge_in: str | None = None
    # この発言が割り込んだ返答（確定したらフェードアウトさせる）。
    interrupts: _Utterance | None = None
    # ペルソナの声が出ている間・止まった直後に話し始めた（自分の声の回り込みを疑う）。
    echo_suspect: bool = False
    # サーバー側で生成を打ち切られた返答（最後まで話していないので記憶に使わない）。
    cut_off: bool = False


@dataclass
class _SpeakerState:
    """音量判定、発言者と順番待ちを独立して管理する。"""

    participants: dict[int, _Participant] = field(default_factory=dict)
    reservations: deque[tuple[int, float]] = field(default_factory=deque)
    active_user: int | None = None
    speaking: bool = False
    # 診断用の件数（割り込みの候補にした回数・相槌として捨てた回数）。
    holds_started: int = 0
    holds_dropped: int = 0

    def receive(
        self, user_id: int, pcm: bytes, now: float, *, voiced: bool,
        bot_speaking: Callable[[], bool], bot_audible: Callable[[], bool],
    ) -> tuple[str, _Participant]:
        """1フレームを判定して、行うことを返す。

        - "send": 発話中の人の続き。"begin": 話し始め。"barge_in": ペルソナの声が出ている間の割り込みの候補を
          Live へ送る。"hold": 割り込みの候補として手元に留めている。"wait": まだ何もしない。
        """
        participant = self.participants.setdefault(user_id, _Participant())
        if self.speaking and self.active_user == user_id:
            if voiced:
                participant.last_voiced_at = now
            return "send", participant
        if participant.held is not None:
            if now - participant.held_last_voiced_at < LIVE_BARGE_IN_GAP_MS / 1000:
                return self._continue_hold(user_id, participant, pcm, now, voiced=voiced, audible=bot_audible())
            # 声が途切れた。相槌・言いよどみとみなして捨て（Live へは送らない）、このフレームから数え直す。
            self.holds_dropped += 1
            participant.drop_pending_audio()
        participant.preroll.append(pcm)
        if now - participant.last_voiced_at > LIVE_VAD_START_GAP_MS / 1000:
            participant.voiced_ms = 0.0
        if not voiced:
            return "wait", participant
        participant.voiced_ms += len(pcm) / _INPUT_BYTES_PER_MS
        participant.last_voiced_at = now
        audible = bot_audible()
        threshold = LIVE_VAD_START_MS if audible or not bot_speaking() else LIVE_BARGE_IN_MS
        if participant.voiced_ms < threshold:
            return "wait", participant
        if audible:
            # ペルソナの声が出ている。すぐには送らず、話し続けるか手元に留めて確かめる。
            self.holds_started += 1
            participant.held = list(participant.preroll)
            participant.held_started_at = now - participant.voiced_ms / 1000
            participant.held_last_voiced_at = now
            participant.preroll.clear()
            return "hold", participant
        if self.speaking or not self.floor_available(user_id, now):
            self.request_floor(user_id, now)
            return "wait", participant
        return "begin", participant

    def _continue_hold(
        self, user_id: int, participant: _Participant, pcm: bytes, now: float, *, voiced: bool, audible: bool,
    ) -> tuple[str, _Participant]:
        held = participant.held
        if held is not None and len(held) < _HOLD_MAX_FRAMES:
            held.append(pcm)
        if voiced:
            participant.held_last_voiced_at = now
            participant.last_voiced_at = now
        if not audible:
            # ペルソナの声が止まった。割り込みではなく、ふつうの話し始めとして送る。
            if self.speaking or not self.floor_available(user_id, now):
                self.request_floor(user_id, now)
                return "hold", participant
            return "begin", participant
        if now - participant.held_started_at < LIVE_BARGE_IN_HOLD_SECONDS:
            return "hold", participant
        if self.speaking or not self.floor_available(user_id, now):
            self.request_floor(user_id, now)
            return "hold", participant
        return "barge_in", participant

    def expire_holds(self, now: float) -> None:
        """声が途切れたまま（パケットが来ない）の割り込みの候補を捨てる（見回りから呼ぶ）。"""
        for participant in self.participants.values():
            if participant.held is not None and now - participant.held_last_voiced_at >= LIVE_BARGE_IN_GAP_MS / 1000:
                self.holds_dropped += 1
                participant.drop_pending_audio()

    def request_floor(self, user_id: int, now: float) -> None:
        self._expire_reservations(now)
        if any(queued == user_id for queued, _ in self.reservations):
            return
        if len(self.reservations) < LIVE_SPEAKER_QUEUE_MAX:
            self.reservations.append((user_id, now + LIVE_SPEAKER_REQUEST_TTL_SECONDS))

    def remove_reservation(self, user_id: int) -> None:
        self.reservations = deque(item for item in self.reservations if item[0] != user_id)

    def floor_available(self, user_id: int, now: float) -> bool:
        self._expire_reservations(now)
        return not self.reservations or self.reservations[0][0] == user_id

    def _expire_reservations(self, now: float) -> None:
        self.reservations = deque(item for item in self.reservations if item[1] > now)

    def extend_reservations(self, now: float) -> None:
        self._expire_reservations(now)
        self.reservations = deque(
            (user_id, now + LIVE_SPEAKER_REQUEST_TTL_SECONDS) for user_id, _ in self.reservations
        )


@dataclass
class _TranscriptState:
    """遅延した文字起こしを別の話者に帰属させない。"""

    owner: _Utterance | None = None
    blocked: _Utterance | None = None
    next: _Utterance | None = None
    blocked_since: float = float("-inf")
    interrupted: _Utterance | None = None

    def assign(self, utterance: _Utterance, now: float) -> None:
        if self.blocked is not None:
            if self.next is not None:
                self.next.transcript_incomplete = True
            self.next = utterance
        elif self.owner is None or self.owner.transcript_closed:
            self.owner = utterance
        else:
            self.blocked, self.next, self.owner = self.owner, utterance, None
            self.blocked_since = now

    def close(self, utterance: _Utterance | None) -> None:
        if utterance is None:
            return
        utterance.transcript_closed = True
        if utterance is self.blocked or utterance is self.next:
            self.blocked = None
            self.owner, self.next = self.next, None

    def reset(self) -> None:
        self.close(self.blocked)
        self.close(self.owner)
        self.owner = self.blocked = self.next = self.interrupted = None
