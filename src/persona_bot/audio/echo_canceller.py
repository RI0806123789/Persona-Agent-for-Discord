"""音声会話（#268）のエコー除去（AEC。#409）。

ペルソナの声が参加者のスピーカーからマイクへ回り込むと、ボット自身の声を参加者の発話と
取り違えて割り込みを誤検出したり、自分の声に返答し続ける音声ループが起きる。ここでは
再生した音声を参照信号として覚えておき、受信した音声からその回り込みを取り除く。

**依存は numpy だけ**（#409 の決定事項: 既存のライブラリを優先する。scipy などは使わない）。

1. **参照信号**（`EchoReference`）… 再生スレッドが Discord へ送り出した 20ms ごとの音声を、
   送り出した時刻の位置へ 16kHz モノラルで書き込む（数秒分のリングバッファ。メモリ上だけに持つ）。
2. **時間合わせ** … 受信した音声は RTP のタイムスタンプで連続した時間軸に並べる（話し始めごとに、
   最初のパケットの到着時刻を起点にする）。回り込みの遅延は参照信号との相互相関（GCC-PHAT）で推定する。
   Discord を往復するので遅延は数百ミリ秒あり、通信の揺らぎで変わるため、一定間隔で推定し直す。
3. **線形のエコー除去** … 周波数領域の分割ブロック適応フィルタ（PBFDAF。NLMS）。推定した遅延の手前から
   `LIVE_AEC_FILTER_MS` 分の反響を学習して差し引く。学習は回り込みが主のとき（参照信号との
   コヒーレンスが高いとき）だけ行い、参加者が話している間（ダブルトーク）は止める。
4. **残差の判定** … コヒーレンスが高い（回り込みが主）か、残差が推定したエコーに比べて小さいときは、
   発話の強さを 0 として返す。残響・歪み（コーデック・相手側の音声処理）で残ったエコーを
   発話と誤検出しないため。

参照信号は再生スレッド、エコー除去は受信スレッドで動く。参照信号はロックで守り、エコー除去の
状態（1人分）は受信スレッドだけが触る。処理は 20ms ごとの小さな配列演算で、1コアの VPS でも
受信を遅らせない大きさに収めている（参照信号が鳴っていない間は何も計算しない）。
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Callable

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from persona_bot.audio.live_audio import Downsampler
from persona_bot.config.constants import (
    LIVE_AEC_CONVERGED_ERLE_DB,
    LIVE_AEC_DELAY_MIN_CORRELATION,
    LIVE_AEC_DELAY_MIN_PEAK_RATIO,
    LIVE_AEC_DELAY_MIN_WINDOW_MS,
    LIVE_AEC_DELAY_TOLERANCE_MS,
    LIVE_AEC_DELAY_UPDATE_MS,
    LIVE_AEC_DELAY_WINDOW_MS,
    LIVE_AEC_ECHO_COHERENCE,
    LIVE_AEC_FILTER_MS,
    LIVE_AEC_FILTER_PRE_MS,
    LIVE_AEC_MAX_DELAY_MS,
    LIVE_AEC_REFERENCE_FLOOR_RMS,
    LIVE_AEC_REFERENCE_GAP_MS,
    LIVE_AEC_REFERENCE_SECONDS,
    LIVE_AEC_RESIDUAL_MARGIN,
    LIVE_AEC_SPURT_GAP_MS,
    LIVE_AEC_STEP_SIZE,
    LIVE_INPUT_SAMPLE_RATE,
    PCM_INT16_MAX,
    PCM_INT16_MIN,
)

_RATE = LIVE_INPUT_SAMPLE_RATE


def _samples(ms: float) -> int:
    return int(round(ms * _RATE / 1000))


# 適応フィルタの1ブロック（Discord の1パケット＝20ms）。
BLOCK = _samples(20)
_TAPS = _samples(LIVE_AEC_FILTER_MS + LIVE_AEC_FILTER_PRE_MS)
_PARTITIONS = max(1, math.ceil(_TAPS / BLOCK))
_PRE = _samples(LIVE_AEC_FILTER_PRE_MS)
_MAX_DELAY = _samples(LIVE_AEC_MAX_DELAY_MS)
_WINDOW = _samples(LIVE_AEC_DELAY_WINDOW_MS)
_MIN_WINDOW = _samples(LIVE_AEC_DELAY_MIN_WINDOW_MS)
_UPDATE = _samples(LIVE_AEC_DELAY_UPDATE_MS)
_TOLERANCE = _samples(LIVE_AEC_DELAY_TOLERANCE_MS)
_SPURT_GAP = _samples(LIVE_AEC_SPURT_GAP_MS)
_REFERENCE_GAP = _samples(LIVE_AEC_REFERENCE_GAP_MS)
_FLOOR_ENERGY = LIVE_AEC_REFERENCE_FLOOR_RMS ** 2
# RTP の時計（Opus は 48kHz）から 16kHz への比。1パケットの進みの上限（それを超えたら話し始めとみなす）。
_RTP_RATIO = 3
_RTP_MAX_STEP = _SPURT_GAP * _RTP_RATIO
# GCC-PHAT で使う周波数帯（声の主な帯域）。帯域外は雑音が白色化で強調されるため捨てる。
_GCC_BAND_HZ = (200.0, 4000.0)
# コヒーレンスを平均する周波数帯と、時間方向の平滑化の係数（大きいほど偏りが小さく、反応が遅い）。
_COHERENCE_BAND_HZ = (300.0, 3400.0)
_COHERENCE_SMOOTHING = 0.8
_POWER_SMOOTHING = 0.7
_ERLE_SMOOTHING = 0.95
# 推定済みの遅延を別の遅延へ切り替えるには、新しい遅延の相関係数が今の遅延のこの倍以上要る。
_SWITCH_MARGIN = 1.5
# 正規化の分母に足す、全帯域の平均パワーに対する割合（スペクトルの谷での発散を防ぐ）。
_SPECTRAL_FLOOR = 0.1
# 除去後の方がしばらく（平滑化したエネルギーで）大きいままなら、学習が発散したとみなして学び直す。
_DIVERGENCE_RATIO = 2.0
_DIVERGENCE_SMOOTHING = 0.9
_EPS = 1e-9


def _energy(samples: np.ndarray) -> float:
    return float(np.mean(np.square(samples))) if samples.size else 0.0


def _band_bins(size: int, band: tuple[float, float]) -> slice:
    resolution = _RATE / size
    return slice(max(1, int(band[0] / resolution)), int(band[1] / resolution) + 1)


class _Timeline:
    """絶対サンプル位置で読み書きするリングバッファ。書いていない位置・古すぎる位置は無音（0）。"""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._data = np.zeros(capacity, dtype=np.float32)
        # 書き込み済みの末尾（この位置は含まない）。まだ何も書いていなければ None。
        self.end: int | None = None

    def write(self, start: int, samples: np.ndarray) -> None:
        count = samples.size
        if count == 0:
            return
        if count > self._capacity:
            samples = samples[-self._capacity:]
            start += count - self._capacity
            count = self._capacity
        if self.end is None or start - self.end >= self._capacity or start + count <= self.end - self._capacity:
            # 時間軸が大きく飛んだ。残っている音はすべて古い。
            self._data[:] = 0.0
        elif start > self.end:
            # 間が空いた区間は無音で埋める（前の周回の音を残さない）。
            self._put(self.end, np.zeros(start - self.end, dtype=np.float32))
        self._put(start, samples)
        self.end = start + count if self.end is None else max(self.end, start + count)

    def read(self, start: int, length: int) -> np.ndarray:
        result = np.zeros(length, dtype=np.float32)
        if self.end is None or length <= 0:
            return result
        low = max(start, self.end - self._capacity)
        high = min(start + length, self.end)
        if high <= low:
            return result
        position = low % self._capacity
        first = min(high - low, self._capacity - position)
        result[low - start:low - start + first] = self._data[position:position + first]
        rest = high - low - first
        if rest:
            result[low - start + first:high - start] = self._data[:rest]
        return result

    def _put(self, start: int, samples: np.ndarray) -> None:
        position = start % self._capacity
        first = min(samples.size, self._capacity - position)
        self._data[position:position + first] = samples[:first]
        if samples.size > first:
            self._data[:samples.size - first] = samples[first:]


class EchoReference:
    """再生したペルソナの声（参照信号）を、再生した時刻の位置に 16kHz モノラルで保持する。

    `record()` は discord.py の再生スレッドから、`read()` などは受信スレッドから呼ばれる。
    録音ではない — 数秒分のリングバッファで、古い音は上書きされる（保存もしない）。
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._timeline = _Timeline(_samples(LIVE_AEC_REFERENCE_SECONDS * 1000))
        self._downsampler = Downsampler()
        # 最後に音（しきい値以上）を書いた位置の末尾。
        self._sound_end: int | None = None

    def now_index(self) -> int:
        """いまの時刻を、参照信号の時間軸の位置（16kHz のサンプル数）で返す。"""
        return int(round(self._clock() * _RATE))

    def record(self, frame: bytes) -> None:
        """Discord へ送り出した 20ms（48kHz・ステレオ）を、送り出した時刻の位置へ書く。"""
        samples = np.frombuffer(self._downsampler.process(frame), dtype=np.int16).astype(np.float32)
        if samples.size == 0:
            return
        now = self.now_index()
        with self._lock:
            end = self._timeline.end
            # 再生スレッドは 20ms 刻みの予定表で読み出す。少しの遅れ・追い上げは連続とみなし、
            # 大きく空いたとき（再生が止まっていた）だけ時刻の位置へ張り直す。
            contiguous = end is not None and -BLOCK * 10 <= now - end <= _REFERENCE_GAP
            start = end if contiguous else now
            self._timeline.write(start, samples)
            if _energy(samples) >= _FLOOR_ENERGY:
                self._sound_end = start + samples.size

    def read(self, start: int, length: int) -> np.ndarray:
        with self._lock:
            return self._timeline.read(start, length)

    def has_sound_after(self, index: int) -> bool:
        """index より後に、参照信号の音があったか（回り込みを疑う必要があるか）。"""
        with self._lock:
            return self._sound_end is not None and self._sound_end > index


@dataclass(frozen=True)
class EchoResult:
    """1フレーム分のエコー除去の結果。"""

    # 除去後の音声（16kHz・モノラル・16bit）。参照信号が鳴っていなければ受け取ったまま。
    pcm: bytes
    # 発話の強さ（VAD に使う実効値）。回り込み・残ったエコーと判定したら 0。
    level: float
    # 参照信号が鳴っていた（回り込みがありうる）区間か。
    echo_possible: bool


@dataclass(frozen=True)
class EchoStats:
    """診断用の値（ログに出す。音声や話者の情報は含めない）。"""

    delay_ms: float | None
    erle_db: float | None


class EchoCanceller:
    """1人分の受信音声のエコー除去（受信スレッドだけが呼ぶ）。"""

    def __init__(self, reference: EchoReference) -> None:
        self._reference = reference
        self._mic = _Timeline(_WINDOW + _MAX_DELAY)
        self._mic_end: int | None = None
        self._spurt_start = 0
        self._last_rtp: int | None = None
        self._last_arrival: int | None = None
        self._since_estimate = 0
        self._delay: int | None = None
        self._candidate_delay: int | None = None
        bins = BLOCK + 1
        self._weights = np.zeros((_PARTITIONS, bins), dtype=np.complex128)
        self._power = np.full(bins, _EPS)
        coherence_bins = BLOCK // 2 + 1
        self._window = np.hanning(BLOCK).astype(np.float32)
        self._s_dd = np.zeros(coherence_bins)
        self._s_xx = np.zeros(coherence_bins)
        self._s_dx = np.zeros(coherence_bins, dtype=np.complex128)
        self._coherence_band = _band_bins(BLOCK, _COHERENCE_BAND_HZ)
        self._erle_db: float | None = None
        self._mic_energy = 0.0
        self._residual_energy = 0.0
        # 正則化（参照信号が床の音量のときのパワー程度）。無音に近い帯域で学習が暴れないようにする。
        self._delta = 2 * BLOCK * _FLOOR_ENERGY

    # ------------------------------------------------------------------
    # 公開の入口
    # ------------------------------------------------------------------

    @property
    def delay_samples(self) -> int | None:
        return self._delay

    def stats(self) -> EchoStats:
        delay = self._delay
        return EchoStats(
            delay_ms=None if delay is None else delay * 1000 / _RATE,
            erle_db=self._erle_db,
        )

    def process(self, pcm: bytes, *, rtp_timestamp: int, arrival: int) -> EchoResult:
        """受信した 1 フレーム（16kHz・モノラル）からエコーを除く。

        Args:
            rtp_timestamp: パケットの RTP タイムスタンプ（48kHz）。話し始めの中で時間軸を連続させる。
            arrival: パケットが届いた時刻（参照信号と同じ時間軸の位置。`EchoReference.now_index()`）。
        """
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16).astype(np.float32)
        count = samples.size
        if count == 0:
            return EchoResult(pcm, 0.0, False)
        end = self._place(count, rtp_timestamp, arrival)
        start = end - count
        self._mic.write(start, samples)
        raw_level = math.sqrt(_energy(samples))
        self._since_estimate += count
        if self._since_estimate >= _UPDATE and self._reference.has_sound_after(end - _WINDOW - _MAX_DELAY):
            self._since_estimate = 0
            self._estimate_delay(end)
        if self._delay is None:
            # 遅延が分からない間は取り除けない。回り込みの可能性だけを伝える（後段が文字起こしで確かめる）。
            return EchoResult(pcm, raw_level, self._reference.has_sound_after(start - _MAX_DELAY - _TAPS))
        lead = max(0, self._delay - _PRE)
        if not self._reference.has_sound_after(start - lead - _PARTITIONS * BLOCK):
            return EchoResult(pcm, raw_level, False)
        if count % BLOCK:
            # Discord の音声は 20ms 単位。端数のフレームは取り除かずに通す（判定は後段に任せる）。
            return EchoResult(pcm, raw_level, True)
        cleaned = np.empty(count, dtype=np.float32)
        level = 0.0
        for offset in range(0, count, BLOCK):
            residual, block_level = self._cancel_block(samples[offset:offset + BLOCK], start + offset, lead)
            cleaned[offset:offset + BLOCK] = residual
            level = max(level, block_level)
        output = np.clip(np.rint(cleaned), PCM_INT16_MIN, PCM_INT16_MAX).astype(np.int16).tobytes()
        return EchoResult(output, level, True)

    # ------------------------------------------------------------------
    # 受信音声の時間軸
    # ------------------------------------------------------------------

    def _place(self, count: int, rtp_timestamp: int, arrival: int) -> int:
        """このフレームの末尾の位置（参照信号と同じ時間軸）を返す。"""
        new_spurt = True
        if self._last_rtp is not None and self._last_arrival is not None and self._mic_end is not None:
            step = (rtp_timestamp - self._last_rtp) & 0xFFFFFFFF
            new_spurt = not 0 < step <= _RTP_MAX_STEP or arrival - self._last_arrival > _SPURT_GAP
        if new_spurt:
            # 話し始め（または受信の途切れ）。最初のパケットの到着時刻を起点にし、遅延は推定し直す。
            self._mic_end = arrival
            self._spurt_start = arrival - count
            self._since_estimate = max(0, _UPDATE - _MIN_WINDOW)
        else:
            # パケットが欠けても、RTP のタイムスタンプの進みで位置を合わせる。
            self._mic_end += step // _RTP_RATIO
        self._last_rtp = rtp_timestamp
        self._last_arrival = arrival
        return self._mic_end

    # ------------------------------------------------------------------
    # 遅延の推定（GCC-PHAT）
    # ------------------------------------------------------------------

    def _estimate_delay(self, end: int) -> None:
        available = end - self._spurt_start
        if available < _MIN_WINDOW:
            return
        window = min(_WINDOW, available)
        mic = self._mic.read(end - window, window)
        reference = self._reference.read(end - window - _MAX_DELAY, window + _MAX_DELAY)
        if _energy(mic) < _FLOOR_ENERGY or _energy(reference) < _FLOOR_ENERGY:
            return
        if self._delay is not None:
            current = reference[_MAX_DELAY - self._delay:_MAX_DELAY - self._delay + window]
            if _energy(current) < _FLOOR_ENERGY:
                # ★ 今の遅延なら回り込みが届かない（ボットが話し終えた後の）区間。参加者の声だけの区間で
                # 推定し直すと偽のピークに切り替わるため、推定しない。
                return
        lag = estimate_delay(mic, reference, _MAX_DELAY)
        if lag is None:
            return
        if (self._delay is not None and abs(lag - self._delay) > _TOLERANCE
                and _correlation(mic, reference, _MAX_DELAY - lag) < _SWITCH_MARGIN * _correlation(
                    mic, reference, _MAX_DELAY - self._delay)):
            # ★ 今の遅延の方が同じくらい波形が合っている。ボットが話し終えた境目などで出る偽のピークで
            # 正しい遅延を捨てない（切り替えると、次に話し始めたとき推定し直すまで回り込みを取り除けない）。
            return
        if self._delay is not None and abs(lag - self._delay) <= _TOLERANCE:
            # 同じ遅延の範囲。学習したフィルタを保つため、値は動かさない。
            self._candidate_delay = None
        elif self._candidate_delay is not None and abs(lag - self._candidate_delay) <= _TOLERANCE:
            # 2回続けて同じ遅延になった（最初の推定・相手側のジッタバッファの伸び縮みなど）。
            # 1回だけの推定は偽のピークのことがあるため採用しない。切り替えたら学び直す。
            self._set_delay(lag)
        else:
            self._candidate_delay = lag

    def _set_delay(self, lag: int) -> None:
        self._delay = lag
        self._candidate_delay = None
        self._reset_filter()
        self._s_dd[:] = 0
        self._s_xx[:] = 0
        self._s_dx[:] = 0

    # ------------------------------------------------------------------
    # 線形のエコー除去と残差の判定
    # ------------------------------------------------------------------

    def _cancel_block(self, mic: np.ndarray, start: int, lead: int) -> tuple[np.ndarray, float]:
        span = self._reference.read(start - lead - _PARTITIONS * BLOCK, (_PARTITIONS + 1) * BLOCK).astype(np.float64)
        # partition 0 が最新。各 partition は 2 ブロック分（overlap-save）。
        frames = sliding_window_view(span, 2 * BLOCK)[::BLOCK][::-1]
        spectra = np.fft.rfft(frames, axis=1)
        estimate = np.fft.irfft((self._weights * spectra).sum(axis=0), 2 * BLOCK)[BLOCK:]
        mic64 = mic.astype(np.float64)
        # 学習には本当の誤差（受信音声 − 推定したエコー）を使う。
        error = mic64 - estimate
        mic_energy = float(np.mean(mic64 ** 2))
        error_energy = float(np.mean(error ** 2))
        smoothing = _DIVERGENCE_SMOOTHING
        self._mic_energy = smoothing * self._mic_energy + (1 - smoothing) * mic_energy
        self._residual_energy = smoothing * self._residual_energy + (1 - smoothing) * error_energy
        if self._mic_energy >= _FLOOR_ENERGY and self._residual_energy > _DIVERGENCE_RATIO * self._mic_energy:
            # しばらくの間、取り除いた後の方がずっと大きい。学習が発散したとみなして最初から学び直す。
            self._reset_filter()
            self._residual_energy = self._mic_energy
        fallback = error_energy > mic_energy
        if fallback:
            # 学習の途中（静かな区間・切り替わり）で、差し引くと逆に大きくなるブロックは元の音のまま出す。
            residual, echo_energy, residual_energy = mic64, 0.0, mic_energy
        else:
            residual, echo_energy, residual_energy = error, float(np.mean(estimate ** 2)), error_energy
        aligned_start = _PARTITIONS * BLOCK + lead - (self._delay or 0)
        aligned = span[aligned_start:aligned_start + BLOCK]
        reference_sounding = float(np.mean(aligned ** 2)) >= _FLOOR_ENERGY
        coherence = self._coherence(mic64, aligned)
        # ★ 正規化のパワーは全 partition の平均で取り、上がるときは即座に追従させる（下がるときだけ平滑化）。
        # 最新のブロックだけで取ったり立ち上がりを平滑化したりすると、話し声の途切れ・立ち上がりで
        # 更新量が過大になり、学習が発散する（合成信号で確認した）。
        instant = np.mean(np.abs(spectra) ** 2, axis=0)
        self._power = np.maximum(instant, _POWER_SMOOTHING * self._power + (1 - _POWER_SMOOTHING) * instant)
        residual_level = math.sqrt(residual_energy)
        near_end = self._near_end(residual_level, math.sqrt(echo_energy), coherence, fallback)
        # 学習は「回り込みが主で、参加者の声が乗っていない」ブロックだけで行う（ダブルトークで発散させない）。
        if reference_sounding and coherence >= LIVE_AEC_ECHO_COHERENCE and not near_end:
            self._adapt(spectra, error)
            if residual_energy > 0 and mic_energy >= _FLOOR_ENERGY:
                erle = 10 * math.log10(mic_energy / residual_energy)
                self._erle_db = erle if self._erle_db is None else (
                    _ERLE_SMOOTHING * self._erle_db + (1 - _ERLE_SMOOTHING) * erle
                )
        return residual.astype(np.float32), residual_level if near_end else 0.0

    def _near_end(self, residual_level: float, echo_level: float, coherence: float, fallback: bool) -> bool:
        """除去後の残差に、参加者の声が乗っているか。

        学習が進んでいれば（ERLE が LIVE_AEC_CONVERGED_ERLE_DB 以上）、除去量から見込まれる
        残りのエコー（推定したエコー ÷ ERLE）の LIVE_AEC_RESIDUAL_MARGIN 倍を超える残差を発話とみなす。
        エコーが参加者の声より大きい環境でも、重ねて話した声を拾える（コヒーレンスだけだと
        エコーが大きいほど「回り込みが主」と判定され、割り込みを取りこぼす）。
        学習の前・差し引けなかったブロックでは、参照信号とのコヒーレンスが低いときだけ発話とみなす。
        """
        erle = self._erle_db
        if erle is not None and erle >= LIVE_AEC_CONVERGED_ERLE_DB and not fallback:
            leakage = 10 ** (-erle / 20)
            return residual_level >= LIVE_AEC_RESIDUAL_MARGIN * leakage * echo_level
        return coherence < LIVE_AEC_ECHO_COHERENCE

    def _reset_filter(self) -> None:
        self._weights[:] = 0
        self._erle_db = None

    def _adapt(self, spectra: np.ndarray, error_block: np.ndarray) -> None:
        error = np.fft.rfft(np.concatenate((np.zeros(BLOCK), error_block)))
        # ★ 分母には全帯域の平均に比例する下限も足す。話し声はスペクトルに谷があり、参照がほとんど無い
        # 帯域で分母が小さすぎると、窓の漏れで残った誤差だけで係数が暴れて発散する（合成信号で確認した）。
        total = _PARTITIONS * self._power
        normalizer = total + self._delta + _SPECTRAL_FLOOR * float(np.mean(total))
        updated = self._weights + LIVE_AEC_STEP_SIZE * np.conj(spectra) * error[None, :] / normalizer[None, :]
        # 勾配の拘束: 各 partition を因果的な BLOCK タップに収める（全 partition を1回の FFT でまとめて行う）。
        taps = np.fft.irfft(updated, 2 * BLOCK, axis=1)
        taps[:, BLOCK:] = 0.0
        self._weights = np.fft.rfft(taps, axis=1)

    def _coherence(self, mic: np.ndarray, aligned: np.ndarray) -> float:
        mic_spectrum = np.fft.rfft(mic * self._window)
        ref_spectrum = np.fft.rfft(aligned * self._window)
        alpha = _COHERENCE_SMOOTHING
        self._s_dd = alpha * self._s_dd + (1 - alpha) * np.abs(mic_spectrum) ** 2
        self._s_xx = alpha * self._s_xx + (1 - alpha) * np.abs(ref_spectrum) ** 2
        self._s_dx = alpha * self._s_dx + (1 - alpha) * mic_spectrum * np.conj(ref_spectrum)
        band = self._coherence_band
        numerator = np.abs(self._s_dx[band]) ** 2
        denominator = self._s_dd[band] * self._s_xx[band]
        valid = denominator > _EPS
        if not np.any(valid):
            return 0.0
        return float(np.mean(numerator[valid] / denominator[valid]))


def estimate_delay(mic: np.ndarray, reference: np.ndarray, max_delay: int) -> int | None:
    """受信音声が参照信号から何サンプル遅れているか（GCC-PHAT）。はっきりしなければ None。

    reference は mic と同じ末尾から max_delay だけ前に始まる区間（長さ len(mic) + max_delay）。
    """
    size = 1 << (reference.size + mic.size - 1).bit_length()
    cross = np.fft.rfft(reference, size) * np.conj(np.fft.rfft(mic, size))
    band = _band_bins(size, _GCC_BAND_HZ)
    weighted = np.zeros_like(cross)
    weighted[band] = cross[band] / (np.abs(cross[band]) + _EPS)
    correlation = np.fft.irfft(weighted, size)[: max_delay + 1]
    peak = int(np.argmax(correlation))
    baseline = float(np.mean(np.abs(correlation)))
    if baseline <= 0 or correlation[peak] < LIVE_AEC_DELAY_MIN_PEAK_RATIO * baseline:
        return None
    # 白色化（PHAT）は音の立ち上がり・途切れの形だけでも鋭いピークを作る。その遅延で実際に波形が
    # 似ているか（相関係数）も確かめ、回り込みの無い区間の偽のピークを採用しない。
    if _correlation(mic, reference, peak) < LIVE_AEC_DELAY_MIN_CORRELATION:
        return None
    return max_delay - peak


def _correlation(mic: np.ndarray, reference: np.ndarray, offset: int) -> float:
    """reference[offset:] と mic の相関係数（offset が範囲外・無音なら 0）。"""
    if offset < 0:
        return 0.0
    aligned = reference[offset:offset + mic.size]
    if aligned.size != mic.size:
        return 0.0
    norm = math.sqrt(float(np.dot(mic, mic)) * float(np.dot(aligned, aligned)))
    return float(np.dot(mic, aligned)) / norm if norm > 0 else 0.0
