"""TTS と Live の停止・開始・再生スレッドの終了待機を接続ごとに管理する。"""

from __future__ import annotations

import asyncio
import threading
from typing import Callable
from weakref import WeakKeyDictionary, WeakSet

import discord

from persona_bot.core.background import run_in_thread


def get_audio_player(voice_client: discord.VoiceClient) -> threading.Thread | None:
    """stop() が参照を消す前に、実際の再生スレッドを控える。"""
    # discord.py の stop() と after はスレッドの終了・音源の cleanup を待たない。
    player = getattr(voice_client, "_player", None)
    return player if isinstance(player, threading.Thread) else None


class PlaybackController:
    """同じ接続の再生切替を直列化し、停止済みスレッドと音源の所有者を保持する。

    prepare/play と TTS の終了待機は lock の下で呼ぶ。同期の stop は Live の割り込みからも
    呼べるが、source を指定した場合は所有者が一致するときだけ停止する。
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        # 実行中の Thread は threading 自身が保持する。終了後の Thread 経由で
        # VoiceClient を生かし続け、弱参照キャッシュから接続を回収できなくしない。
        self._players: WeakSet[threading.Thread] = WeakSet()
        self._source: discord.AudioSource | None = None

    def _remember_player(self, voice_client: discord.VoiceClient) -> None:
        player = get_audio_player(voice_client)
        if player is not None:
            self._players.add(player)

    async def wait_for_players(self) -> None:
        """キャンセルされても実スレッドが終わるまで所有権を返さない。"""
        canceled: asyncio.CancelledError | None = None
        while players := list(self._players):
            player = players[0]
            try:
                await run_in_thread(player.join)
            except asyncio.CancelledError as error:
                # run_in_thread は join の完了を待ってから伝えてくる。残りのスレッドも待ち終えてから再送出する。
                canceled = error
            finally:
                # join が終わった（キャンセル時も）スレッドは記録から外す。残すと同じスレッドを待ち直し続ける。
                self._players.discard(player)
        if canceled is not None:
            raise canceled

    def stop(self, voice_client: discord.VoiceClient, *, source: discord.AudioSource | None = None) -> None:
        if source is not None and self._source is not source:
            return
        self._remember_player(voice_client)
        if source is not None or voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        self._source = None

    async def prepare(self, voice_client: discord.VoiceClient) -> None:
        """前の再生を止め、共有エンコーダーを使い始める前に終了を待つ。"""
        try:
            self.stop(voice_client)
        finally:
            await self.wait_for_players()

    def play(
        self, voice_client: discord.VoiceClient, source: discord.AudioSource,
        *, after: Callable[[Exception | None], None],
    ) -> None:
        self._source = source
        try:
            voice_client.play(source, after=after)
        finally:
            self._remember_player(voice_client)

    def release(self, source: discord.AudioSource) -> None:
        if self._source is source:
            self._source = None


_controllers: WeakKeyDictionary[discord.VoiceClient, PlaybackController] = WeakKeyDictionary()


def get_playback_controller(voice_client: discord.VoiceClient) -> PlaybackController:
    """TTS と Live が同じ接続に対して同じ制御器を使う。"""
    controller = _controllers.get(voice_client)
    if controller is None:
        controller = PlaybackController()
        _controllers[voice_client] = controller
    return controller
