"""音声会話（#268）の音声形式の変換と、再生用の音源を確かめる。"""

from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.audio.live_audio import (
    DISCORD_FRAME_BYTES, Downsampler, LivePlaybackSource, Upsampler, frame_rms,
)


def _stereo_tone(frequency: float, seconds: float, amplitude: float = 8000.0) -> bytes:
    """48kHz・ステレオ・16bit の正弦波（L/R 同じ）。"""
    t = np.arange(int(48000 * seconds)) / 48000
    mono = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.int16)
    return np.repeat(mono, 2).tobytes()


def _rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class DownsamplerTests(unittest.TestCase):
    def test_speech_band_tone_keeps_level_at_one_third_length(self):
        pcm = _stereo_tone(440, 1.0)
        output = np.frombuffer(Downsampler().process(pcm), dtype=np.int16)
        self.assertEqual(output.size, 16000)
        # フィルタの立ち上がり分を除き、音量はほぼ保たれる
        self.assertAlmostEqual(_rms(output[200:]), 8000 / np.sqrt(2), delta=300)

    def test_tone_above_eight_kilohertz_is_suppressed_instead_of_aliasing(self):
        output = np.frombuffer(Downsampler().process(_stereo_tone(12000, 1.0)), dtype=np.int16)
        # 16kHz では表せない 12kHz を間引くと 4kHz に折り返す。ローパスで十分に落とす。
        self.assertLess(_rms(output[200:]), 8000 / np.sqrt(2) * 0.05)

    def test_twenty_millisecond_frames_match_one_shot_conversion(self):
        pcm = _stereo_tone(523, 0.5)
        whole = Downsampler().process(pcm)
        streaming = Downsampler()
        frame = 960 * 4  # 20ms
        pieces = b"".join(streaming.process(pcm[i:i + frame]) for i in range(0, len(pcm), frame))
        self.assertEqual(pieces, whole)

    def test_odd_length_fragments_keep_the_decimation_phase(self):
        pcm = _stereo_tone(300, 0.2)
        whole = Downsampler().process(pcm)
        streaming = Downsampler()
        sizes = [4 * 7, 4 * 11, 4 * 5]
        pieces, position, index = [], 0, 0
        while position < len(pcm):
            size = sizes[index % len(sizes)]
            pieces.append(streaming.process(pcm[position:position + size]))
            position += size
            index += 1
        self.assertEqual(b"".join(pieces), whole)

    def test_too_short_input_returns_nothing(self):
        self.assertEqual(Downsampler().process(b"\x01\x00"), b"")

    def test_arbitrary_byte_fragments_preserve_stereo_frames(self):
        pcm = _stereo_tone(440, 0.05)
        whole = Downsampler().process(pcm)
        for size in (1, 2, 3, 5, 7, 13):
            with self.subTest(size=size):
                streaming = Downsampler()
                pieces = [streaming.process(pcm[i:i + size]) for i in range(0, len(pcm), size)]
                self.assertEqual(b"".join(pieces), whole)

    def test_incomplete_stereo_frame_waits_without_advancing_filter(self):
        pcm = bytes(range(8))
        streaming = Downsampler()
        self.assertEqual(streaming.process(pcm[:3]), b"")
        self.assertEqual(streaming.process(b""), b"")
        self.assertEqual(streaming.process(pcm[3:]), Downsampler().process(pcm))


class UpsamplerTests(unittest.TestCase):
    def test_twenty_four_kilohertz_mono_becomes_stereo_with_double_length(self):
        mono = (np.arange(100, dtype=np.int16) * 10).tobytes()
        output = np.frombuffer(Upsampler().process(mono), dtype=np.int16).reshape(-1, 2)
        self.assertEqual(output.shape, (200, 2))
        np.testing.assert_array_equal(output[:, 0], output[:, 1])
        # 補間された点は前後の平均（1サンプル遅れる）
        self.assertEqual(output[3, 0], 5)

    def test_odd_bytes_are_carried_to_the_next_fragment(self):
        data = (np.arange(10, dtype=np.int16) * 100).tobytes()
        upsampler = Upsampler()
        joined = upsampler.process(data[:5]) + upsampler.process(data[5:])
        self.assertEqual(joined, Upsampler().process(data))

    def test_empty_input_returns_nothing(self):
        self.assertEqual(Upsampler().process(b""), b"")


class FrameRmsTests(unittest.TestCase):
    def test_silence_and_tone(self):
        self.assertEqual(frame_rms(bytes(640)), 0.0)
        self.assertEqual(frame_rms(b""), 0.0)
        tone = np.full(320, 1000, dtype=np.int16).tobytes()
        self.assertAlmostEqual(frame_rms(tone), 1000.0)


class PlaybackSourceTests(unittest.TestCase):
    def test_is_a_pcm_audio_source(self):
        source = LivePlaybackSource(stall_frames=5)
        self.assertIsInstance(source, discord.AudioSource)
        self.assertFalse(source.is_opus())

    def test_stall_marks_the_source_as_ended_so_later_chunks_need_a_new_source(self):
        source = LivePlaybackSource(stall_frames=1)
        source.read()  # 無音で待つ
        self.assertFalse(source.aborted)
        self.assertEqual(source.read(), b"")  # 途絶えて打ち切る
        self.assertTrue(source.aborted)
        source.feed(bytes(DISCORD_FRAME_BYTES))  # 終了済みの音源には溜めない
        self.assertEqual(source.buffered_bytes, 0)

    def test_frames_then_silence_while_waiting_then_stops_after_stall(self):
        source = LivePlaybackSource(stall_frames=2)
        source.feed(b"\x01" * DISCORD_FRAME_BYTES)
        self.assertEqual(source.read(), b"\x01" * DISCORD_FRAME_BYTES)
        self.assertEqual(source.read(), bytes(DISCORD_FRAME_BYTES))
        self.assertEqual(source.read(), bytes(DISCORD_FRAME_BYTES))
        self.assertEqual(source.read(), b"")

    def test_finish_flushes_the_remainder_padded_then_ends(self):
        source = LivePlaybackSource(stall_frames=10)
        source.feed(b"\x02" * (DISCORD_FRAME_BYTES + 10))
        source.finish()
        self.assertEqual(len(source.read()), DISCORD_FRAME_BYTES)
        last = source.read()
        self.assertEqual(last, b"\x02" * 10 + bytes(DISCORD_FRAME_BYTES - 10))
        self.assertEqual(source.read(), b"")
        self.assertEqual(source.frames_played, 2)

    def test_abort_discards_unplayed_audio_and_ignores_later_feed(self):
        source = LivePlaybackSource(stall_frames=10)
        source.feed(b"\x03" * DISCORD_FRAME_BYTES * 3)
        source.abort()
        source.feed(b"\x03" * DISCORD_FRAME_BYTES)
        self.assertEqual(source.read(), b"")
        self.assertEqual(source.buffered_bytes, 0)
        self.assertTrue(source.aborted)


if __name__ == "__main__":
    unittest.main()
