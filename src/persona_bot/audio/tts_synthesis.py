"""読み上げの音声合成（#470。`voice.py` から分離）。

主エンジンの edge-tts で mp3 を取得し、失敗したら gTTS へ退避する。得た mp3 は miniaudio で
メモリ上のまま Discord へそのまま送出できる生 PCM（48kHz / ステレオ / 16bit）へデコードし、
ペルソナごとの音色（EQ）を掛ける。再生・キューには触れない。

★ edge-tts は公式に third-party 利用が認められた API ではないため、gTTS への退避を消さないこと。
FFmpeg には依存しない（返信のたびに外部プロセスを起動すると小メモリ環境で OOM になる）。
ログは分割前と同じロガー名 `persona_bot.audio.voice` へ出す（テストが差し替える `_edge_tts_failing`・
`_fetch_*`・`_decode_to_pcm` はこのモジュールの名前）。
"""

import asyncio
import io
import logging
from typing import Any, Mapping

import edge_tts
import miniaudio
import numpy as np
from gtts import gTTS

from persona_bot.audio.voice_dsp import apply_equalizer, needs_equalizer
from persona_bot.config.constants import (
    EDGE_TTS_VOICE_FEMALE,
    PCM_INT16_MAX,
    PCM_INT16_MIN,
    TTS_CHANNELS,
    TTS_EDGE_TIMEOUT_SECONDS,
    TTS_GTTS_CONNECT_TIMEOUT_SECONDS,
    TTS_GTTS_READ_TIMEOUT_SECONDS,
    TTS_LANG,
    TTS_SAMPLE_RATE,
)
from persona_bot.core.background import run_in_thread

logger = logging.getLogger("persona_bot.audio.voice")


# edge-tts が失敗し続けている間、返信のたびに同じログを出さないための状態。
# 「失敗し始めたとき」と「復旧したとき」の2回だけ記録する。
_edge_tts_failing = False


async def _fetch_edge_mp3(text: str, profile: Mapping[str, Any]) -> bytes:
    """edge-tts で mp3 を取得する。

    edge-tts は async ネイティブで、通信は WebSocket 越しの非同期処理。
    そのままイベントループ上で await してよい（待っている間ループは塞がらない）。
    **`asyncio.to_thread` の中で `asyncio.run()` して呼ばないこと** —
    イベントループが入れ子になり正常に動作しない。
    """
    communicate = edge_tts.Communicate(
        text,
        str(profile.get("voice") or EDGE_TTS_VOICE_FEMALE),
        rate=str(profile.get("rate") or "+0%"),
        pitch=str(profile.get("pitch") or "+0Hz"),
    )
    buffer = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    return buffer.getvalue()


def _fetch_gtts_mp3(text: str) -> bytes:
    """退避用。gTTS で mp3 を取得する（同期処理なので別スレッドから呼ぶこと）。"""
    with io.BytesIO() as buffer:
        gTTS(
            text=text, lang=TTS_LANG,
            timeout=(TTS_GTTS_CONNECT_TIMEOUT_SECONDS, TTS_GTTS_READ_TIMEOUT_SECONDS),
        ).write_to_fp(buffer)
        return buffer.getvalue()


async def _fetch_mp3(text: str, profile: Mapping[str, Any]) -> bytes:
    """主エンジン（edge-tts）で合成し、駄目なら gTTS へ退避する。"""
    global _edge_tts_failing

    failure = None
    try:
        mp3 = await asyncio.wait_for(
            _fetch_edge_mp3(text, profile), timeout=TTS_EDGE_TIMEOUT_SECONDS,
        )
        if mp3:
            if _edge_tts_failing:
                logger.info('TTS: edge-tts が復旧しました。通常の読み上げに戻ります。')
                _edge_tts_failing = False
            return mp3
    except Exception as error:
        failure = error

    if not _edge_tts_failing:
        # 失敗が続く間は返信のたびにログが溢れるため、状態が変わったときだけ残す。
        logger.warning('TTS: edge-tts で音声を取得できなかったため gTTS で読み上げます。', exc_info=failure)
        _edge_tts_failing = True

    # 退避時は話者・高さ・速さを再現できないためキャラ差は失われる（音色だけは掛かる）。
    return await run_in_thread(_fetch_gtts_mp3, text)


def _decode_to_pcm(mp3_bytes: bytes, profile: Mapping[str, Any]) -> bytes:
    """mp3 を Discord へそのまま送出できる生 PCM に変換し、音色を適用する。

    デコードも EQ も同期の CPU 処理のため、呼び出し側から `run_in_thread` で
    別スレッドに逃がして使う（イベントループを止めるとボット全体の応答が固まる）。
    """
    if not needs_equalizer(profile):
        # 音色を変えないペルソナ。miniaudio 側で 48kHz / ステレオ / 16bit への
        # 変換まで済ませ、numpy を一切通さない。
        decoded = miniaudio.decode(
            mp3_bytes,
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=TTS_CHANNELS,
            sample_rate=TTS_SAMPLE_RATE,
        )
        return decoded.samples.tobytes()

    # 音色を変えるペルソナ。EQ はモノラル1ch分だけ掛ければ足りるので、
    # ステレオ化は最後に回す（計算量が半分で済む）。
    decoded = miniaudio.decode(
        mp3_bytes,
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=TTS_SAMPLE_RATE,
    )
    mono = np.frombuffer(decoded.samples.tobytes(), dtype=np.int16)
    shaped = apply_equalizer(mono, profile, sample_rate=TTS_SAMPLE_RATE)

    # EQ のブーストで int16 の範囲を超えることがあるので、必ずクリップしてから丸める。
    clipped = np.clip(shaped, PCM_INT16_MIN, PCM_INT16_MAX).astype(np.int16)
    # モノラル1ch分を L/R へ複製してインターリーブする（Discord はステレオしか受け取らない）。
    return np.repeat(clipped, TTS_CHANNELS).tobytes()


async def _synthesize_pcm(text: str, profile: Mapping[str, Any]) -> bytes:
    """テキストを Discord へそのまま送出できる生 PCM に変換する。

    ネットワーク待ち（edge-tts）は非同期のままイベントループ上で、
    CPU 処理（デコードと EQ）は別スレッドで実行する。
    """
    mp3_bytes = await _fetch_mp3(text, profile)
    return await run_in_thread(_decode_to_pcm, mp3_bytes, profile)
