"""エコー除去（#409）を、合成した「ペルソナの声」と回り込み（遅延・反響・雑音）で確かめる。

実際の Discord（相手側のスピーカー・マイク・音声処理）での効き方は検証用サーバーで確かめる。
ここでは時間合わせ・遅延推定・線形の除去・発話の判定が、決まった条件で働くことだけを見る。
"""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio import echo_canceller
from persona_bot.audio.echo_canceller import (
    BLOCK, EchoCanceller, EchoReference, _Timeline, estimate_delay,
)
from persona_bot.config.constants import LIVE_VAD_ENERGY_THRESHOLD

RATE = 16000


def speech_like(seconds: float, seed: int, level: float) -> np.ndarray:
    """声に似た信号（フォルマントのある雑音を音節ほどの周期で区切り、ところどころ間を空ける）。"""
    rng = np.random.default_rng(seed)
    count = int(seconds * RATE)
    spectrum = np.fft.rfft(rng.standard_normal(count))
    freqs = np.fft.rfftfreq(count, 1 / RATE)
    shape = (np.exp(-((freqs - 500) / 300) ** 2) + 0.6 * np.exp(-((freqs - 1500) / 500) ** 2)
             + 0.3 * np.exp(-((freqs - 2800) / 700) ** 2))
    signal = np.fft.irfft(spectrum * shape, count)
    t = np.arange(count) / RATE
    envelope = np.clip(np.sin(2 * np.pi * 4 * t + rng.uniform(0, 6)), 0, None) ** 0.5
    gate = np.ones(count)
    position = 0
    while position < count:
        position += int(rng.uniform(0.8, 1.6) * RATE)
        gap = int(rng.uniform(0.15, 0.35) * RATE)
        gate[position:position + gap] = 0
        position += gap
    return signal / np.std(signal) * level * envelope * gate


def to_pcm(samples: np.ndarray) -> bytes:
    return np.clip(np.rint(samples), -32768, 32767).astype(np.int16).tobytes()


def echo_path(reference: np.ndarray, delay: int, gain: float, seed: int) -> np.ndarray:
    """遅延と短い反響（40ms）を付けた回り込み。"""
    rng = np.random.default_rng(seed)
    length = int(0.04 * RATE)
    t = np.arange(length) / RATE
    response = np.exp(-t / 0.008) * rng.standard_normal(length) * 0.3
    response[0] = 1.0
    response = response / np.max(np.abs(response)) * gain
    echo = np.convolve(reference, response)[: reference.size]
    return np.concatenate((np.zeros(delay), echo[: reference.size - delay]))


class Simulation:
    """20ms ごとに「再生（参照信号の記録）→ 受信（エコー除去）」を進める。"""

    def __init__(self) -> None:
        self.clock = [0.0]
        self.reference = EchoReference(clock=lambda: self.clock[0])
        self.canceller = EchoCanceller(self.reference)

    def run(self, played: np.ndarray, received: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        frames = received.size // BLOCK
        levels = np.zeros(frames)
        output = np.zeros(frames * BLOCK)
        for index in range(frames):
            self.clock[0] = index * 0.02
            block = played[index * BLOCK:(index + 1) * BLOCK]
            # 再生は 48kHz・ステレオ（参照信号の記録が 16kHz・モノラルへ変換する）。
            self.reference.record(to_pcm(np.repeat(np.repeat(block, 3), 2)))
            result = self.canceller.process(
                to_pcm(received[index * BLOCK:(index + 1) * BLOCK]),
                rtp_timestamp=960 * index, arrival=(index + 1) * BLOCK,
            )
            levels[index] = result.level
            output[index * BLOCK:(index + 1) * BLOCK] = np.frombuffer(result.pcm, dtype=np.int16)
        return levels, output


def frame_rms(signal: np.ndarray) -> np.ndarray:
    frames = signal.size // BLOCK
    return np.sqrt(np.mean(signal[: frames * BLOCK].reshape(frames, BLOCK) ** 2, axis=1))


class TimelineTests(unittest.TestCase):
    def test_gap_is_silent_and_old_samples_are_forgotten(self):
        timeline = _Timeline(100)
        timeline.write(0, np.ones(10, dtype=np.float32))
        timeline.write(20, np.full(10, 2.0, dtype=np.float32))
        values = timeline.read(0, 30)
        self.assertTrue(np.all(values[:10] == 1) and np.all(values[10:20] == 0) and np.all(values[20:] == 2))
        # 容量を超えて進むと古い位置は無音として読める（前の周回の音を返さない）。
        timeline.write(150, np.full(10, 3.0, dtype=np.float32))
        self.assertTrue(np.all(timeline.read(0, 30) == 0))
        self.assertTrue(np.all(timeline.read(150, 10) == 3))
        # まだ書いていない未来も無音。
        self.assertTrue(np.all(timeline.read(160, 10) == 0))

    def test_wraparound_read_and_write(self):
        timeline = _Timeline(16)
        data = np.arange(30, dtype=np.float32)
        timeline.write(0, data[:12])
        timeline.write(12, data[12:30])
        np.testing.assert_array_equal(timeline.read(14, 16), data[14:30])


class ReferenceTests(unittest.TestCase):
    def test_frames_are_contiguous_and_a_long_pause_reanchors(self):
        clock = [0.0]
        reference = EchoReference(clock=lambda: clock[0])
        tone = np.full(320, 1000.0)
        frame = to_pcm(np.repeat(np.repeat(tone, 3), 2))
        reference.record(frame)
        clock[0] = 0.025  # 少し遅れて読み出されても続きとして並べる
        reference.record(frame)
        self.assertEqual(reference._timeline.end, 640)
        clock[0] = 1.0  # 再生が止まっていた
        reference.record(frame)
        self.assertEqual(reference._timeline.end, 16000 + 320)
        self.assertTrue(np.all(reference.read(700, 15000) == 0))
        self.assertTrue(reference.has_sound_after(16000))
        self.assertFalse(reference.has_sound_after(16320))


class DelayEstimateTests(unittest.TestCase):
    def test_known_delay_is_found_and_unrelated_sound_is_rejected(self):
        played = speech_like(2.0, 1, 3000)
        delay = 4000
        mic_end = played.size
        window = 9600
        received = echo_path(played, delay, 0.5, 2)
        reference = played[mic_end - window - 16000:mic_end]
        lag = estimate_delay(received[mic_end - window:mic_end], reference, 16000)
        self.assertIsNotNone(lag)
        self.assertLess(abs(lag - delay), 48)
        unrelated = speech_like(2.0, 9, 3000)
        self.assertIsNone(estimate_delay(unrelated[mic_end - window:mic_end], reference, 16000))


class CancellerTests(unittest.TestCase):
    def test_without_reference_audio_passes_through_unchanged(self):
        simulation = Simulation()
        voice = speech_like(1.0, 4, 2000)
        pcm = to_pcm(voice[:BLOCK])
        result = simulation.canceller.process(pcm, rtp_timestamp=0, arrival=BLOCK)
        self.assertEqual(result.pcm, pcm)
        self.assertFalse(result.echo_possible)
        self.assertAlmostEqual(result.level, float(np.sqrt(np.mean(voice[:BLOCK].round() ** 2))), delta=1.0)

    def test_echo_is_removed_and_not_counted_as_speech(self):
        simulation = Simulation()
        played = speech_like(6.0, 1, 3000)
        received = echo_path(played, 4000, 0.5, 3) + np.random.default_rng(5).standard_normal(played.size) * 20
        levels, output = simulation.run(played, received)
        delay = simulation.canceller.delay_samples
        self.assertIsNotNone(delay)
        self.assertLess(abs(delay - 4000), 64)
        late = slice(4 * RATE, 6 * RATE)
        erle = 10 * np.log10(np.mean(received[late] ** 2) / np.mean(output[late] ** 2))
        self.assertGreater(erle, 10.0)
        echo_frames = frame_rms(echo_path(played, 4000, 0.5, 3)) >= LIVE_VAD_ENERGY_THRESHOLD
        after_convergence = np.arange(levels.size) >= 100
        mistaken = levels[echo_frames & after_convergence] >= LIVE_VAD_ENERGY_THRESHOLD
        self.assertLess(np.mean(mistaken), 0.05)
        self.assertGreater(simulation.canceller.stats().erle_db, 10.0)

    def test_voice_over_the_echo_is_still_detected(self):
        simulation = Simulation()
        played = speech_like(8.0, 1, 3000)
        voice = speech_like(8.0, 8, 2000)
        voice[: 4 * RATE] = 0
        received = echo_path(played, 4000, 0.5, 3) + voice
        levels, _ = simulation.run(played, received)
        voiced = frame_rms(voice) >= LIVE_VAD_ENERGY_THRESHOLD
        self.assertGreater(np.mean(levels[voiced] >= LIVE_VAD_ENERGY_THRESHOLD), 0.6)

    def test_lost_packets_keep_the_timeline_by_rtp_timestamp(self):
        simulation = Simulation()
        canceller = simulation.canceller
        canceller.process(bytes(640), rtp_timestamp=1000, arrival=320)
        canceller.process(bytes(640), rtp_timestamp=1000 + 960 * 3, arrival=1000)
        self.assertEqual(canceller._mic_end, 320 + 320 * 3)
        # 間が空きすぎたら話し始めとみなし、届いた時刻を起点にする。
        canceller.process(bytes(640), rtp_timestamp=1000 + 960 * 100, arrival=40000)
        self.assertEqual(canceller._mic_end, 40000)

    def test_frames_that_are_not_twenty_milliseconds_pass_through(self):
        simulation = Simulation()
        played = speech_like(3.0, 1, 3000)
        simulation.run(played, echo_path(played, 1600, 0.5, 3))
        self.assertIsNotNone(simulation.canceller.delay_samples)
        odd = to_pcm(np.full(100, 500.0))
        result = simulation.canceller.process(odd, rtp_timestamp=960 * 151, arrival=151 * BLOCK + 100)
        self.assertEqual(result.pcm, odd)

    def test_processing_cost_stays_small(self):
        """1コアの VPS でも受信を遅らせない目安（20ms のフレームを数ミリ秒以内）。"""
        import time

        simulation = Simulation()
        played = speech_like(3.0, 1, 3000)
        started = time.perf_counter()
        simulation.run(played, echo_path(played, 4000, 0.5, 3))
        per_frame = (time.perf_counter() - started) / (played.size // BLOCK)
        self.assertLess(per_frame, 0.005)


class ConstantsTests(unittest.TestCase):
    def test_reference_buffer_covers_the_delay_search(self):
        self.assertGreater(
            echo_canceller._samples(echo_canceller.LIVE_AEC_REFERENCE_SECONDS * 1000),
            echo_canceller._MAX_DELAY + echo_canceller._WINDOW + echo_canceller._TAPS,
        )


if __name__ == "__main__":
    unittest.main()
