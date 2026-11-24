"""音声会話のLive送信待ち行列と、終了時の診断統計ログ出力（#495。`VoiceConversation` から分離）。

発話の区切り（`start` / `end`）と音声チャンクの順序を保つ送信キュー制御と、会話終了時に出す
割り込み・エコー除去・応答遅延の集計ログを担当する。`VoiceConversation` が継承するため、
メソッド名・呼び出し方は変わらない。

★ ログは分割前と同じロガー名 `persona_bot.services.voice_conversation` へ出す。
"""

from __future__ import annotations

import asyncio
from collections import Counter
import logging
from statistics import median, quantiles
from typing import TYPE_CHECKING

from persona_bot.config.constants import LIVE_INPUT_CHUNK_MS, LIVE_INPUT_QUEUE_MAX_CHUNKS
from persona_bot.services.voice_conversation_state import _INPUT_BYTES_PER_MS

if TYPE_CHECKING:
    from persona_bot.audio.voice_receive import VoiceReceiver
    from persona_bot.services.live_session import LiveSession
    from persona_bot.services.voice_conversation_state import _SpeakerState

logger = logging.getLogger("persona_bot.services.voice_conversation")

# 送信 1 チャンクのバイト数（16kHz・モノラル・16bit）。
_INPUT_CHUNK_BYTES = _INPUT_BYTES_PER_MS * LIVE_INPUT_CHUNK_MS


class VoiceDiagnosticsMixin:
    """Live への送信キュー制御と、会話終了時の診断統計（割り込み・エコー・遅延）のログ出力。"""

    if TYPE_CHECKING:
        # ホスト（`VoiceConversation`）が持つ属性の宣言。型検査のためだけにあり、実行時には何も定義しない。
        _speaker_state: _SpeakerState
        _barge_in_stats: Counter[str]
        _receiver: VoiceReceiver | None
        _latencies: list[float]
        _send_queue: asyncio.Queue[tuple[str, bytes]]
        _pending_chunk: bytearray
        _queue_full_warned: bool
        _stopped: bool
        _live: LiveSession
        _open_generation: int | None

    # ==================================================================
    # 終了時の診断統計ログ（本文・話者は含めない）
    # ==================================================================

    def _log_barge_in_stats(self) -> None:
        """割り込みの判定の件数（しきい値の実験用。#409）。本文・話者は出さない。"""
        state = self._speaker_state
        stats = self._barge_in_stats
        if not state.holds_started and not stats:
            return
        logger.info(
            "音声会話: 返答中の割り込み 候補 %d 回 / 相槌として見送り %d 回 / Live へ送信 %d 回 / "
            "確定 %d 回 / 未確定のまま返答 %d 回 / 回り込みとして返答を破棄 %d 回",
            state.holds_started, state.holds_dropped, stats["sent"], stats["confirmed"],
            stats["unconfirmed"], stats["echo"],
        )

    def _log_echo_stats(self) -> None:
        """エコー除去の推定遅延・除去量（しきい値の実験用。#409）。受信を止める前に読む。"""
        receiver = self._receiver
        if receiver is None or not hasattr(receiver, "echo_stats"):
            return
        try:
            stats = list(receiver.echo_stats())
            delays = [float(item.delay_ms) for item in stats if item.delay_ms is not None]
            erles = [float(item.erle_db) for item in stats if item.erle_db is not None]
        except Exception:
            logger.debug("音声会話: エコー除去の診断値を読めませんでした。", exc_info=True)
            return
        if not delays:
            return
        logger.info(
            "音声会話: エコー除去 回り込みの遅延を推定できた話者 %d 人（遅延の中央値 %.0f ミリ秒・除去量の中央値 %s）",
            len(delays), median(delays), f"{median(erles):.1f}dB" if erles else "未計測",
        )

    def _log_latency(self) -> None:
        if not self._latencies:
            return
        ordered = sorted(self._latencies)
        if len(ordered) >= 2:
            cuts = quantiles(ordered, n=20, method="inclusive")
            p50, p95 = cuts[9], cuts[18]
        else:
            p50 = p95 = ordered[0]
        logger.info(
            "音声会話: 発話終了から初回音声までの遅延 P50=%.2f秒 / P95=%.2f秒（%d回）",
            p50, p95, len(ordered),
        )

    # ==================================================================
    # Live への送信（発話の区切りと音声の順序を保つため、1本の待ち行列で送る）
    # ==================================================================

    def _enqueue(self, kind: str, payload: bytes = b"") -> None:
        self._send_queue.put_nowait((kind, payload))

    def _queue_audio(self, pcm: bytes) -> None:
        self._pending_chunk.extend(pcm)
        if len(self._pending_chunk) >= _INPUT_CHUNK_BYTES:
            self._flush_chunk()

    def _flush_chunk(self) -> None:
        if not self._pending_chunk:
            return
        chunk = bytes(self._pending_chunk)
        self._pending_chunk.clear()
        if self._send_queue.qsize() >= LIVE_INPUT_QUEUE_MAX_CHUNKS:
            if not self._queue_full_warned:
                self._queue_full_warned = True
                logger.warning("音声会話: Live への送信が詰まっているため、音声の一部を捨てました。")
            return
        self._enqueue("audio", chunk)

    def _clear_send_queue(self) -> None:
        """送信待ちの音声だけを捨てる。

        発話の区切り（start / end）は残す — 送信済みの start に対応する end まで消すと、
        Live 側で発話が開いたままになる。対応の取れない区切りは送信側が捨てる。
        """
        self._pending_chunk.clear()
        markers = []
        while not self._send_queue.empty():
            item = self._send_queue.get_nowait()
            if item[0] != "audio":
                markers.append(item)
        for item in markers:
            self._send_queue.put_nowait(item)

    async def _run_sender(self) -> None:
        while not self._stopped:
            kind, payload = await self._send_queue.get()
            generation = self._live.generation
            if kind == "start":
                if await self._live.activity_start():
                    self._open_generation = generation
            elif self._open_generation != generation:
                # 再接続した新しい接続へ、対応する start の無い音声・end を送らない。
                continue
            elif kind == "audio":
                await self._live.send_audio(payload)
            else:
                await self._live.activity_end()
                self._open_generation = None
