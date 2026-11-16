"""Gemini Live の接続（1つのボイスチャンネル分。#268 の LiveSessionManager）。

接続を張りっぱなしにして、参加者の音声を流し込み、返ってくる音声・文字起こしを
呼び出し側（`voice_conversation.VoiceConversation`）へ渡す。切断時の扱い:

- **セッションの再開（Session Resumption）**: サーバーが配る再開ハンドルを覚えておき、
  接続が切れたら同じ文脈のまま再接続する。予告（go_away）を受けたらすぐ張り替える。
- **文脈の圧縮（Context Window Compression）**: 長時間の会話でも上限で切れないよう、
  古い部分をサーバー側で削らせる（値は constants.LIVE_COMPRESSION_*）。
- **再接続は回数制限つき**（`LIVE_RECONNECT_MAX_ATTEMPTS`）。再開に失敗したら新しい
  セッションで試し、それも駄目なら諦めて `on_closed("failed")` を呼ぶ。
- **利用枠の超過（429 など）では再接続しない**（無制限に叩き続けない。#268 の決定事項）。
  `on_closed("quota")` を呼んで終える。
- `close()` の後は再接続しない（`/leave` の後に勝手につなぎ直さない）。

同意の撤回などで「これまでの文脈を引き継がない」必要があるときは `restart_fresh()` を
呼ぶ。再開ハンドルを捨てて新しいセッションにつなぎ直す（撤回した人の発言が残った文脈を
使い続けないため）。

SDK の接続口（`client.aio.live.connect`）は差し替えられる（テストでは模擬の接続を渡す）。
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from google.genai import types

from persona_bot.config.constants import (
    LIVE_COMPRESSION_TARGET_TOKENS,
    LIVE_COMPRESSION_TRIGGER_TOKENS,
    LIVE_CONNECT_TIMEOUT_SECONDS,
    LIVE_INPUT_MIME_TYPE,
    LIVE_MODEL_NAME,
    LIVE_RECONNECT_BACKOFF_SECONDS,
    LIVE_RECONNECT_MAX_ATTEMPTS,
)
from persona_bot.core.background import spawn, wait_for_cleanup
from persona_bot.services.genai_client import QUOTA_EXHAUSTED_STATUS, error_status_code, is_quota_exhausted


logger = logging.getLogger(__name__)

# 接続の設定そのものを受け付けなかったことを示す WebSocket の終了コード・HTTP の状態。
# ボイス名や検索ツールに未対応のモデルでは、ここで拒否される。
_CONFIG_ERROR_CODES = frozenset({1007, 1008, 400, 403, 404})
# WebSocket の終了コード（4桁）を含めて読む上限。
_MAX_ERROR_CODE = 4999
_QUOTA_ERROR_WORDS = ("quota", "resource_exhausted", "resource exhausted", "rate limit", "exceeded")

CLOSED_QUOTA = "quota"
CLOSED_FAILED = "failed"


class LiveUnavailableError(Exception):
    """Live 会話を始められない（利用枠の超過・設定の誤り・接続できない）。"""

    def __init__(self, *, quota: bool) -> None:
        super().__init__("Gemini Live に接続できません。")
        self.quota = quota


@dataclass(frozen=True)
class LiveOptions:
    """接続ごとに変わらない設定（接続した時点のペルソナ・声・検索の有無）。"""

    system_instruction: str
    voice_name: str
    grounding: bool


def _error_code(error: BaseException) -> int | None:
    # WebSocket の終了コード（1007・1011 など）も含めて読むため、上限を HTTP の範囲より広げる。
    return error_status_code(error, max_code=_MAX_ERROR_CODE)


def is_quota_error(error: BaseException) -> bool:
    """利用枠の超過か。超過なら再接続しない（無制限に叩き続けない）。"""
    code = _error_code(error)
    if code == QUOTA_EXHAUSTED_STATUS or (code is None and is_quota_exhausted(error)):
        return True
    text = " ".join(str(part) for part in (getattr(error, "status", ""), getattr(error, "message", ""), error))
    return any(word in text.lower() for word in _QUOTA_ERROR_WORDS)


def is_config_error(error: BaseException) -> bool:
    """接続の設定が受け付けられなかったか（利用枠の超過は含めない）。"""
    return _error_code(error) in _CONFIG_ERROR_CODES and not is_quota_error(error)


def build_live_config(
    options: LiveOptions, *, handle: str | None, minimal: bool, search: bool = True,
) -> types.LiveConnectConfig:
    """Live の接続設定を組み立てる。

    発話の区切りはボット側（ローカルの VAD）が `activity_start` / `activity_end` で伝えるため、
    サーバー側の自動検出は切る（複数人のボイスチャンネルで、誰の発言をいつ渡すかをボット側で
    決めるため）。話し始めた時点でボットの返答を止める（割り込み）のは既定のまま使う。

    minimal が真なら、ボイス名と検索を外す（モデルが対応していないときの退避）。
    search が偽なら、検索だけを外す（検索ツール付きの接続だけが利用枠の超過で拒否されるときの退避）。
    """
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        system_instruction=options.system_instruction,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(disabled=True),
            activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=LIVE_COMPRESSION_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(target_tokens=LIVE_COMPRESSION_TARGET_TOKENS),
        ),
    )
    if not minimal:
        if options.voice_name:
            config.speech_config = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=options.voice_name),
                ),
            )
        if options.grounding and search:
            config.tools = [types.Tool(google_search=types.GoogleSearch())]
    return config


class LiveSession:
    """1つのボイスチャンネル分の Gemini Live 接続。"""

    def __init__(
        self,
        genai_client: Any,
        options: LiveOptions,
        *,
        on_message: Callable[[Any], Awaitable[None]],
        on_reconnected: Callable[[], None],
        on_closed: Callable[[str], Awaitable[None]],
        model: str = LIVE_MODEL_NAME,
        connect: Callable[..., Any] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._options = options
        self._on_message = on_message
        self._on_reconnected = on_reconnected
        self._on_closed = on_closed
        self._model = model
        self._connect_live = connect if connect is not None else genai_client.aio.live.connect
        self._sleep = sleep
        self._context: Any = None
        self._session: Any = None
        self._resume_handle: str | None = None
        self._minimal = False
        # 検索ツール付きの接続だけが利用枠の超過で拒否されたとき、以後は検索なしでつなぐ。
        self._search_off = False
        self._closing = False
        self._go_away = False
        self._restart_requested = False
        self._runner: asyncio.Task[Any] | None = None
        # 接続ごとの世代。呼び出し側は、古い接続の応答を新しい接続のものと取り違えない。
        self.generation = 0
        # 文脈の破棄（同意の撤回）のたびに進める世代と、いまの接続を開いたときの値。
        # 撤回前の文脈で開いた接続や、撤回前の接続から届いた再開ハンドルを採用しない。
        self._context_epoch = 0
        self._session_epoch = 0

    @property
    def connected(self) -> bool:
        return self._session is not None and not self._closing

    # ------------------------------------------------------------------
    # 開始・終了
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """最初の接続を確立し、受信と再接続を裏で始める。

        Raises:
            LiveUnavailableError: 接続できなかった（quota が真なら利用枠の超過）。
        """
        try:
            while True:
                try:
                    await self._open()
                    break
                except Exception as error:
                    if self._drop_search_on_quota(error):
                        continue
                    if not self._minimal and is_config_error(error):
                        logger.warning("Gemini Live: 設定を外して接続し直します。", exc_info=error)
                        self._minimal = True
                        continue
                    raise
        except asyncio.CancelledError:
            await self._close_current()
            raise
        except Exception as error:
            await self._close_current()
            logger.error("Gemini Live: 接続できませんでした。", exc_info=error)
            raise LiveUnavailableError(quota=is_quota_error(error)) from error
        self._runner = spawn(self._run(), label="Gemini Live の受信")

    def _drop_search_on_quota(self, error: BaseException) -> bool:
        """検索ツール付きの接続が利用枠の超過で拒否されたなら、検索を外して試し直す（真を返す）。

        Live の Google 検索は無料枠に含まれないことがあり、その場合は音声だけなら接続できるのに、
        検索ツールを付けた接続だけが「quota を超えた」と拒否される。検索を外して会話を優先する。
        検索を外した後の超過や、もともと検索なしの接続の超過は本物の超過として扱う（1回だけ試す）。
        """
        if self._search_off or self._minimal or not self._options.grounding or not is_quota_error(error):
            return False
        logger.warning("Gemini Live: 検索ツール付きの接続が利用枠の超過で拒否されたため、検索を外して接続し直します。")
        self._search_off = True
        return True

    async def close(self) -> None:
        """接続を閉じる。以後は再接続しない（何度呼んでもよい）。"""
        self._closing = True
        runner, self._runner = self._runner, None
        if runner is not None and runner is not asyncio.current_task():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        await wait_for_cleanup(self._close_current(), label="Gemini Live の切断")

    async def restart_fresh(self) -> None:
        """これまでの文脈を引き継がずに、新しいセッションへつなぎ直す。

        再接続の途中（接続を待っている間）に呼ばれた場合は、`_open` が世代の食い違いを見て、
        撤回前のハンドルで開いた接続を採用せずに新しいセッションでつなぎ直す。
        """
        self.forget_context()
        if self._closing or self._session is None:
            return
        self._restart_requested = True
        # 受信待ちを抜けさせる。受信ループが張り替えを行う。
        await self._close_current()

    def forget_context(self) -> None:
        """次に再接続するとき、これまでの文脈を引き継がないようにする。"""
        self._resume_handle = None
        self._context_epoch += 1

    # ------------------------------------------------------------------
    # 送信
    # ------------------------------------------------------------------

    async def send_audio(self, pcm: bytes) -> bool:
        return await self._send(audio=types.Blob(data=pcm, mime_type=LIVE_INPUT_MIME_TYPE))

    async def activity_start(self) -> bool:
        return await self._send(activity_start=types.ActivityStart())

    async def activity_end(self) -> bool:
        return await self._send(activity_end=types.ActivityEnd())

    async def _send(self, **payload: Any) -> bool:
        session = self._session
        if session is None or self._closing:
            return False
        try:
            await session.send_realtime_input(**payload)
            return True
        except Exception:
            # 切断中の送信。受信側が切断を検知して張り替えるので、ここでは捨てるだけ。
            logger.debug("Gemini Live: 送信できなかったため捨てました。", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # 受信と再接続
    # ------------------------------------------------------------------

    async def _open(self) -> None:
        while True:
            epoch = self._context_epoch
            handle = self._resume_handle
            config = build_live_config(
                self._options, handle=handle, minimal=self._minimal, search=not self._search_off,
            )
            context = self._connect_live(model=self._model, config=config)
            try:
                session = await asyncio.wait_for(context.__aenter__(), timeout=LIVE_CONNECT_TIMEOUT_SECONDS)
            except BaseException:
                try:
                    await wait_for_cleanup(context.__aexit__(None, None, None), label="Gemini Live の接続中断")
                except Exception:
                    logger.debug("Gemini Live: 接続中断後の回収に失敗しました。", exc_info=True)
                raise
            if handle is not None and epoch != self._context_epoch:
                # 接続を待つ間に文脈の破棄（同意の撤回）を求められた。撤回前のハンドルで再開した
                # セッションは使わず、新しいセッションでつなぎ直す。
                logger.info("Gemini Live: 接続中に文脈の破棄を求められたため、新しいセッションでつなぎ直します。")
                try:
                    await context.__aexit__(None, None, None)
                except Exception:
                    logger.debug("Gemini Live: 採用しない接続を閉じる途中で例外が出ました。", exc_info=True)
                continue
            break
        self._context, self._session = context, session
        self._session_epoch = self._context_epoch
        self._go_away = False
        self.generation += 1

    async def _close_current(self) -> None:
        context, self._context, self._session = self._context, None, None
        if context is None:
            return
        try:
            await context.__aexit__(None, None, None)
        except Exception:
            logger.debug("Gemini Live: 接続を閉じる途中で例外が出ました。", exc_info=True)

    async def _run(self) -> None:
        try:
            while not self._closing:
                error = await self._pump()
                await self._close_current()
                if self._closing:
                    return
                if error is not None and is_quota_error(error):
                    logger.warning("Gemini Live: 利用枠の上限に達したため、再接続せずに終了します。")
                    await self._on_closed(CLOSED_QUOTA)
                    return
                if not await self._reconnect():
                    return
        finally:
            await self._close_current()

    async def _pump(self) -> BaseException | None:
        """接続が切れるか張り替えが必要になるまで受信する。切断の原因を返す。"""
        session = self._session
        if session is None:
            return None
        try:
            while not self._closing and not self._restart_requested and session is self._session:
                received = False
                async for message in session.receive():
                    if self._closing or self._restart_requested or session is not self._session:
                        return None
                    received = True
                    await self._handle(message)
                    if self._go_away:
                        return None
                # SDK は通常のターン完了でもイテレーションを終えるので、次のターンを受ける。
                # 何も返さず閉じたストリームは再利用しない（空ループによる占有を防ぐ）。
                if not received:
                    return None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._closing and not self._restart_requested:
                logger.warning("Gemini Live: 接続が切れました。", exc_info=error)
            return error
        return None

    async def _handle(self, message: Any) -> None:
        update = getattr(message, "session_resumption_update", None)
        if (update is not None and update.resumable and update.new_handle
                and self._session_epoch == self._context_epoch):
            # 文脈を破棄した後に、古い接続から届いた再開ハンドルは受け取らない。
            self._resume_handle = update.new_handle
        if getattr(message, "go_away", None) is not None:
            # 予告された切断の前に、再開ハンドルで張り替える。
            logger.info("Gemini Live: サーバーから接続の終了予告を受けたため、つなぎ直します。")
            self._go_away = True
        await self._on_message(message)

    async def _reconnect(self) -> bool:
        immediate = self._go_away or self._restart_requested
        self._restart_requested = False
        for attempt in range(LIVE_RECONNECT_MAX_ATTEMPTS):
            if attempt or not immediate:
                await self._sleep(LIVE_RECONNECT_BACKOFF_SECONDS[min(attempt, len(LIVE_RECONNECT_BACKOFF_SECONDS) - 1)])
            if self._closing:
                return False
            try:
                try:
                    await self._open()
                except Exception as error:
                    if not self._drop_search_on_quota(error):
                        raise
                    # 検索だけの超過は同じ試行内で退避する。最終試行でも音声を試し、待ちを増やさない。
                    await self._close_current()
                    if self._closing:
                        return False
                    await self._open()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._close_current()
                if is_quota_error(error):
                    logger.warning("Gemini Live: 利用枠の上限に達したため、再接続を中止します。")
                    await self._on_closed(CLOSED_QUOTA)
                    return False
                # 再開ハンドルが使えない可能性がある。次は新しいセッションで試す。
                logger.warning("Gemini Live: 再接続に失敗しました（%d/%d 回目）。", attempt + 1, LIVE_RECONNECT_MAX_ATTEMPTS, exc_info=error)
                self._resume_handle = None
                continue
            logger.info("Gemini Live: 再接続しました。")
            self._on_reconnected()
            return True
        logger.error("Gemini Live: 再接続できなかったため、音声会話を終了します。")
        await self._on_closed(CLOSED_FAILED)
        return False
