"""Gemini Live の音声会話（#268）で使う、音声形式の変換と再生用の音源。

- Discord が届ける音声は 48kHz / ステレオ / 16bit、Gemini Live が受け取るのは
  16kHz / モノラル / 16bit（`audio/pcm;rate=16000`）。`Downsampler` が変換する。
- Gemini Live が返す音声は 24kHz / モノラル / 16bit、Discord へ送るのは 48kHz / ステレオ。
  `Upsampler` が変換する。
- `LivePlaybackSource` は、届いた順に音声を流す `discord.AudioSource`。1ターン分の応答を
  受け取りながら再生し、割り込み・違反の検出で残りを即座に捨てられる。

**依存は numpy だけ**（読み上げの EQ と同じ方針。`scipy` / `librosa` / `pydub` を使わない）。
どちらの変換も 20ms ごとの小さな断片を続けて処理するため、断片の境目で音が途切れないよう
前の断片の末尾を状態として持ち越す。
"""

from __future__ import annotations

import threading

import discord
import numpy as np

from persona_bot.config.constants import (
    LIVE_DOWNSAMPLE_CUTOFF_HZ,
    LIVE_DOWNSAMPLE_TAPS,
    LIVE_INPUT_SAMPLE_RATE,
    LIVE_OUTPUT_SAMPLE_RATE,
    PCM_INT16_MAX,
    PCM_INT16_MIN,
    TTS_CHANNELS,
    TTS_SAMPLE_RATE,
)

# Discord の 1 フレーム（20ms・48kHz・ステレオ・16bit）のバイト数。
DISCORD_FRAME_BYTES = TTS_SAMPLE_RATE // 50 * TTS_CHANNELS * 2
_SILENT_FRAME = bytes(DISCORD_FRAME_BYTES)


def _lowpass_taps(count: int, cutoff_hz: float, sample_rate: int) -> np.ndarray:
    """窓関数法のローパス FIR。間引きの前に掛けて、折り返し雑音を防ぐ。"""
    center = (count - 1) / 2
    positions = np.arange(count) - center
    taps = np.sinc(2 * cutoff_hz / sample_rate * positions) * np.hamming(count)
    return (taps / taps.sum()).astype(np.float32)


class Downsampler:
    """48kHz ステレオの PCM を 16kHz モノラルへ変換する（1人分の状態を持つ）。"""

    _RATIO = TTS_SAMPLE_RATE // LIVE_INPUT_SAMPLE_RATE

    def __init__(self) -> None:
        self._taps = _lowpass_taps(LIVE_DOWNSAMPLE_TAPS, LIVE_DOWNSAMPLE_CUTOFF_HZ, TTS_SAMPLE_RATE)
        self._history = np.zeros(len(self._taps) - 1, dtype=np.float32)
        # これまでに受け取ったサンプル数を間引き比で割った余り（断片の長さが比の倍数でなくても位相を保つ）。
        self._offset = 0
        self._pending = b""

    def process(self, pcm: bytes) -> bytes:
        data = self._pending + pcm
        frame_bytes = TTS_CHANNELS * 2
        usable = len(data) - len(data) % frame_bytes
        self._pending = data[usable:]
        if usable == 0:
            return b""
        # ステレオフレームの端数は次へ持ち越し、L/R の平均でモノラルにする。
        frames = np.frombuffer(data[:usable], dtype=np.int16).reshape(-1, TTS_CHANNELS)
        mono = frames.astype(np.float32).mean(axis=1)
        buffer = np.concatenate((self._history, mono))
        filtered = np.convolve(buffer, self._taps, mode="valid")
        self._history = buffer[-(len(self._taps) - 1):]
        start = (-self._offset) % self._RATIO
        self._offset = (self._offset + mono.size) % self._RATIO
        decimated = filtered[start::self._RATIO]
        return np.clip(np.rint(decimated), PCM_INT16_MIN, PCM_INT16_MAX).astype(np.int16).tobytes()


class Upsampler:
    """24kHz モノラルの PCM を 48kHz ステレオへ変換する（線形補間。1サンプル遅れる）。"""

    _RATIO = TTS_SAMPLE_RATE // LIVE_OUTPUT_SAMPLE_RATE

    def __init__(self) -> None:
        if self._RATIO != 2 or TTS_SAMPLE_RATE % LIVE_OUTPUT_SAMPLE_RATE:
            raise ValueError("音声出力の補間には正確な2倍のサンプルレートが必要です。")
        self._last = 0.0
        self._pending = b""

    def process(self, pcm: bytes) -> bytes:
        data = self._pending + pcm
        # 16bit の端数は次の断片と合わせて使う（サーバーの区切りが偶数バイトとは限らない）。
        usable = len(data) - len(data) % 2
        self._pending = data[usable:]
        if usable == 0:
            return b""
        samples = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32)
        joined = np.concatenate(([self._last], samples))
        output = np.empty(samples.size * self._RATIO, dtype=np.float32)
        output[0::self._RATIO] = joined[:-1]
        output[1::self._RATIO] = (joined[:-1] + joined[1:]) / self._RATIO
        self._last = float(samples[-1])
        mono = np.clip(np.rint(output), PCM_INT16_MIN, PCM_INT16_MAX).astype(np.int16)
        return np.repeat(mono, TTS_CHANNELS).tobytes()


def frame_rms(pcm: bytes) -> float:
    """16bit PCM の実効値。発話の有無（ローカルの VAD）の判定に使う。"""
    samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


class LivePlaybackSource(discord.AudioSource):
    """1ターン分の応答音声を、届いた順に Discord へ流す音源。

    `read()` は discord.py の再生スレッドから 20ms ごとに呼ばれ、`feed()` / `finish()` /
    `abort()` はイベントループから呼ばれるため、バッファはロックで守る。

    - まだ届いていない（生成が再生に追いつかない）間は無音のフレームを返して待つ。
      `LIVE_PLAYBACK_STALL_SECONDS` 相当の間なにも届かなければ打ち切る（応答が途絶えたとき、
      再生スレッドを残し続けないため）。
    - `finish()` の後は残りを流し切ってから b"" を返し、再生を終える。
    - `abort()` の後は次の `read()` で b"" を返す（割り込み・違反の検出で残りを捨てる）。
    """

    def __init__(self, *, stall_frames: int) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._finished = False
        self._aborted = False
        self._stall_frames = stall_frames
        self._stalled = 0
        self.frames_played = 0

    # discord.AudioSource の契約 -------------------------------------------------

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self._lock:
            self._buffer.clear()

    def read(self) -> bytes:
        with self._lock:
            if self._aborted:
                return b""
            if len(self._buffer) >= DISCORD_FRAME_BYTES:
                frame = bytes(self._buffer[:DISCORD_FRAME_BYTES])
                del self._buffer[:DISCORD_FRAME_BYTES]
                self._stalled = 0
                self.frames_played += 1
                return frame
            if self._finished:
                if not self._buffer:
                    return b""
                # 最後の端数は無音で埋めて1フレームにする。
                frame = bytes(self._buffer) + bytes(DISCORD_FRAME_BYTES - len(self._buffer))
                self._buffer.clear()
                self.frames_played += 1
                return frame
            self._stalled += 1
            if self._stalled > self._stall_frames:
                # ★ 途絶えて打ち切るときは終了済みの印（aborted）を立てる。立てないと、再生スレッドの
                # 終了通知がイベントループで処理されるまでの間に届いたチャンクが、終了済みのこの音源へ
                # feed() されて消える。印があれば、呼び出し側は新しい音源で再生し直せる。
                self._aborted = True
                self._buffer.clear()
                return b""
            return _SILENT_FRAME

    # イベントループ側の操作 -----------------------------------------------------

    def feed(self, pcm: bytes) -> None:
        with self._lock:
            if not self._aborted and not self._finished:
                self._buffer.extend(pcm)

    def finish(self) -> None:
        with self._lock:
            self._finished = True

    def abort(self) -> None:
        with self._lock:
            self._aborted = True
            self._buffer.clear()

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def finished(self) -> bool:
        return self._finished
