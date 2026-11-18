"""音声会話の返答の再生とフェードアウトの調停（#470。`VoiceConversation` から分離）。

返答の音源（`LivePlaybackSource`）の再生開始・終了の後片付け・即時停止・割り込みのフェードアウト・
返答の破棄を扱う。状態は `voice_conversation_state._PlaybackState`（`self._playback_state`）に集め、
読み書きはこのミックスインと、音源を作る `VoiceConversation._on_model_audio` だけから行う。
`VoiceConversation` が継承するので、メソッドの名前・呼び出し方は変わらない。

★ ログは分割前と同じロガー名 `persona_bot.services.voice_conversation` へ出す。ミックスインは
`voice_client` / `_playback_controller` / `_now` / `_stopped` / `_latencies` を `VoiceConversation` から
受け取る前提で、単独では使わない。会話の終了・違反・一時停止は `_abort_playback` で
**フェード中の音源も含めて即座に**止め、割り込みの確定だけが `_fade_out_playback` を使う。
"""

from __future__ import annotations

import asyncio
import logging
from time import monotonic

from persona_bot.audio.live_audio import LivePlaybackSource
from persona_bot.config.constants import (
    LIVE_BARGE_IN_FADE_MS,
    LIVE_ECHO_GUARD_SECONDS,
    LIVE_PLAYBACK_FADE_WAIT_SECONDS,
)
from persona_bot.services.voice_conversation_state import _Utterance

logger = logging.getLogger("persona_bot.services.voice_conversation")

# 割り込みを確定したときに絞り切るまでのフレーム数（20ms 単位）。
_FADE_FRAMES = max(1, LIVE_BARGE_IN_FADE_MS // 20)


class VoicePlaybackMixin:
    """返答の再生・フェードアウト・破棄。再生状態は `self._playback_state` に集約されている。"""

    def _bot_audible(self) -> bool:
        """ペルソナの声がいま出ているか（再生を始めた音源がある）。割り込みは手元で確かめてから送る。"""
        source = self._playback_state.source
        return source is not None and not source.aborted and self._playback_state.started

    def _recently_audible(self, now: float) -> bool:
        """ペルソナの声が出ているか、止まってすぐか（回り込みがまだ届きうる）。"""
        return self._bot_audible() or now - self._playback_state.last_audible_at <= LIVE_ECHO_GUARD_SECONDS


    async def _wait_previous_player(self) -> None:
        async with self._playback_controller.lock:
            await self._playback_controller.wait_for_players()

    async def _start_playback(self) -> None:
        source = self._playback_state.source
        if source is None or self._playback_state.started:
            return
        self._playback_state.started = True
        owner = self._playback_state.owner
        loop = asyncio.get_running_loop()

        def after_playing(error: Exception | None) -> None:
            # discord.py の再生スレッドから呼ばれる。
            if error:
                logger.error("音声会話: 再生エラー。", exc_info=error)
            loop.call_soon_threadsafe(self._on_playback_finished, source)

        try:
            async with self._playback_controller.lock:
                if (self._stopped or self._playback_state.source is not source or source.aborted
                        or owner is None or owner.discarded):
                    self._release_unplayed(source)
                    return
                # 割り込みでフェードアウトさせた音源が鳴り終わるのを待つ（prepare の停止で
                # 絞り切る前にブツ切りしないため）。期限を過ぎたら切り替えを優先する。
                # 会話の時計は再生と独立しているため、待ちの上限は壁時計で数える。
                deadline = monotonic() + LIVE_PLAYBACK_FADE_WAIT_SECONDS
                while self._playback_state.fading is not None and monotonic() < deadline:
                    await asyncio.sleep(0.01)
                await self._playback_controller.prepare(self.voice_client)
                if (self._stopped or self._playback_state.source is not source or source.aborted
                        or owner.discarded or not self.voice_client.is_connected()):
                    self._release_unplayed(source)
                    return
                self._playback_controller.play(self.voice_client, source, after=after_playing)
            if owner.ended_at is not None and owner.playback_started_at is None:
                # stall 後に同じ発話を再開しても初回だけ記録する。
                owner.playback_started_at = self._now()
                self._latencies.append(owner.playback_started_at - owner.ended_at)
        except asyncio.CancelledError:
            source.abort()
            self._on_playback_finished(source)
            raise
        except Exception as error:
            logger.error("音声会話: 再生を開始できませんでした。", exc_info=error)
            source.abort()
            self._on_playback_finished(source)

    def _release_unplayed(self, source: LivePlaybackSource) -> None:
        """再生を始めずに戻るとき、まだ現在の音源なら再生状態を解放する。

        ★ _start_playback は先に _playback_started を立てるため、未接続などで早期に戻ると
        _playback と _playback_started が残り、_bot_speaking() が真に張り付く。
        別の音源に入れ替わっていれば、その音源の状態には触れない。
        """
        if self._playback_state.source is source:
            source.abort()
            self._on_playback_finished(source)

    def _on_playback_finished(self, source: LivePlaybackSource) -> None:
        self._playback_controller.release(source)
        if self._playback_state.fading is source:
            # フェードアウトが鳴り終わった。回り込みの照合は、鳴り終わった時刻を基準に続ける。
            self._playback_state.fading = None
            self._playback_state.last_audible_at = self._now()
        if self._playback_state.source is source:
            if self._playback_state.started:
                self._playback_state.last_audible_at = self._now()
            self._playback_state.source = None
            self._playback_state.owner = None
            self._playback_state.started = False

    def _abort_playback(self) -> None:
        """再生を即座に止める（会話の終了・違反の検出・一時停止など）。フェードアウト中の音源も止める。"""
        fading, self._playback_state.fading = self._playback_state.fading, None
        if fading is not None:
            fading.abort()
            self._stop_player(fading)
        source = self._playback_state.source
        if source is None:
            return
        source.abort()
        self._playback_state.source = None
        self._playback_state.owner = None
        started, self._playback_state.started = self._playback_state.started, False
        if started:
            self._playback_state.last_audible_at = self._now()
            self._stop_player(source)

    def _fade_out_playback(self) -> None:
        """再生中の返答を強めにフェードアウトさせてから止める（割り込みを確定したとき。#409）。

        絞り切るまで（LIVE_BARGE_IN_FADE_MS）は再生スレッドが流し、終わると after から後片付けされる。
        その間に会話が終わったら `_abort_playback` が即座に止める。
        """
        source = self._playback_state.source
        if source is None:
            return
        self._playback_state.source = None
        self._playback_state.owner = None
        started, self._playback_state.started = self._playback_state.started, False
        if started and not source.aborted:
            self._playback_state.last_audible_at = self._now()
            previous_fading, self._playback_state.fading = self._playback_state.fading, source
            if previous_fading is not None and previous_fading is not source:
                # フェードアウト中の音源は1つにする。前のものを残すと鳴り続け、
                # 終了の後片付け（_on_playback_finished）も先の音源のままで追えなくなる。
                previous_fading.abort()
                self._stop_player(previous_fading)
            source.fade_out(_FADE_FRAMES)
        else:
            source.abort()

    def _stop_player(self, source: LivePlaybackSource) -> None:
        try:
            self._playback_controller.stop(self.voice_client, source=source)
        except Exception:
            logger.debug("音声会話: 再生の停止に失敗しました。", exc_info=True)

    def _discard_response(self, utterance: _Utterance, reason: str, *, fade: bool = False) -> None:
        """返答を捨てる。fade が真なら（割り込み）強めにフェードアウトさせ、偽なら即座に止める。"""
        if not utterance.discarded:
            logger.info("音声会話: 返答を破棄しました（%s）。", reason)
        utterance.discarded = True
        if utterance.input_task is not None and utterance.input_task is not asyncio.current_task():
            utterance.input_task.cancel()
        if self._playback_state.owner is utterance:
            if fade:
                self._fade_out_playback()
            else:
                self._abort_playback()
