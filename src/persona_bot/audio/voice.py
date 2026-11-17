"""ボイスチャンネルでの読み上げ（TTS）。

ボットがボイスチャンネルに接続中に、AI の返信や自律行動の発話を音声で読み上げる。
接続は `/join` または自律行動が行う。未接続なら黙って何もしない（no-op）。

**声はペルソナごとに変わる。** 主エンジンの **edge-tts**（Microsoft Edge の読み上げが
使っているニューラル音声。APIキー不要）に、キャラクターごとの話者・声の高さ・話す速さを
指定して合成する。edge-tts には音色を変える機能が無いため、同じ話者を使い回すキャラの
差だけ `voice_dsp` の EQ で付ける。どのキャラをどう鳴らすかは、利用者がローカルで作る
`prompts/personas.json` の `voice`（読み込みと形式の点検は `config/personas.py`）。

**edge-tts が失敗したら gTTS へ退避する。** edge-tts は公式に third-party 利用が
認められた API ではなく、過去に Microsoft 側の仕様変更で一時的に使えなくなった経緯が
ある。退避時はキャラ差が失われるが、読み上げ機能ごと止まるよりはよいと判断している。

**FFmpeg には依存しない。** どちらのエンジンも mp3（24kHz モノラル）を返すが、
`ffmpeg.exe` のような外部プロセスを毎回起動する代わりに、miniaudio（C ライブラリを
同梱した pip パッケージ。exe 不要）でメモリ上のまま「48kHz / ステレオ / 16bit」の
生 PCM へデコードし、`discord.PCMAudio` に渡す。Discord のボイス送信はこの形式の
PCM をそのまま受け取れるため、変換用の外部バイナリも一時ファイルも要らない。
CPU 1コア / メモリ 1GB 級の小さなサーバーでも動かせるようにするための構成。
"""

import logging

import asyncio
from collections import deque
from dataclasses import dataclass
import io
import re
from typing import Any, Callable, Mapping

import discord
import edge_tts
import miniaudio
import numpy as np
from gtts import gTTS

from persona_bot.audio.playback import get_playback_controller
from persona_bot.core.background import run_in_thread, spawn
from persona_bot.config.constants import (
    EDGE_TTS_VOICE_FEMALE,
    PCM_INT16_MAX,
    PCM_INT16_MIN,
    TTS_CHANNELS,
    TTS_EDGE_TIMEOUT_SECONDS,
    TTS_GTTS_CONNECT_TIMEOUT_SECONDS,
    TTS_GTTS_READ_TIMEOUT_SECONDS,
    TTS_LANG,
    TTS_MAX_CHARS,
    TTS_PLAY_TIMEOUT_MARGIN_SECONDS,
    TTS_QUEUE_MAX,
    TTS_SAMPLE_RATE,
    TTS_TIMEOUT_SECONDS,
    TTS_TRUNCATION_NOTICE,
)
from persona_bot.audio.voice_dsp import apply_equalizer, needs_equalizer, resolve_profile
from persona_bot.services.affinity_service import strip_speech_tags


logger = logging.getLogger(__name__)

# 読み上げに不向きな要素を落とすためのパターン群。
# 音声で聞いて意味をなさないもの（URL・記号・コード）は読み上げる価値がないうえ、
# 音声エンジンは URL を1文字ずつ読み上げてしまい非常に長くなるため必ず除去する。
_CODE_BLOCK_PATTERN = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`]*`")
# URL の直後に空白なしで続く日本語や全角句読点を巻き込まない。
_URL_PATTERN = re.compile(r"https?://[\x21-\x7e]+")
# Discord のカスタム絵文字 <:name:123> / アニメ絵文字 <a:name:123>
_CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:\w+:\d+>")
# メンション <@123> / <@!123> / <@&123>（ロール） / <#123>（チャンネル）
_MENTION_PATTERN = re.compile(r"<[@#][!&]?\d+>")
# Markdown の装飾記号と引用・見出し記号（読み上げでは雑音にしかならない）
_MARKDOWN_SYMBOL_PATTERN = re.compile(r"[*_~>#|`]")
_WHITESPACE_PATTERN = re.compile(r"\s+")

# edge-tts が失敗し続けている間、返信のたびに同じログを出さないための状態。
# 「失敗し始めたとき」と「復旧したとき」の2回だけ記録する。
_edge_tts_failing = False


def build_speech_text(response_text: str) -> str:
    """返信テキストを読み上げ向けに整形する。

    テキストチャンネルには元の全文が既に投稿されているため、ここでは
    「声で聞いて分かる部分」だけを残す。長すぎる返信は途中で切り、
    ボイスチャンネルを何分も占有しないようにする。
    """
    text = strip_speech_tags(response_text)
    text = _CODE_BLOCK_PATTERN.sub(" ", text)
    text = _INLINE_CODE_PATTERN.sub(" ", text)
    text = _URL_PATTERN.sub(" ", text)
    text = _CUSTOM_EMOJI_PATTERN.sub(" ", text)
    text = _MENTION_PATTERN.sub(" ", text)
    text = _MARKDOWN_SYMBOL_PATTERN.sub(" ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if not any(character.isalnum() for character in text):
        return ""
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS].rstrip() + TTS_TRUNCATION_NOTICE
    return text


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


async def _play_pcm(
    voice_client: discord.VoiceClient, pcm: bytes, *, is_valid: Callable[[], bool] | None = None,
) -> None:
    """同じ接続の停止・再生・解放を直列化する。"""
    controller = get_playback_controller(voice_client)
    async with controller.lock:
        if is_valid is not None and not is_valid():
            return
        await controller.prepare(voice_client)
        # 終了済みスレッドでも executor の順番待ちで接続や配信が変わりうる。
        # ここから play() までは await による処理の切り替えを挟まない。
        if is_valid is not None and not is_valid():
            return
        await _play_pcm_locked(voice_client, pcm)


async def _play_pcm_locked(voice_client: discord.VoiceClient, pcm: bytes) -> None:
    """PCM を再生し、再生が終わる（または失敗する）まで待つ。

    完了待ちには必ず上限を設ける。`after` コールバックはボイス接続が正常な
    `disconnect()` を経ずに切れる（回線の瞬断など）と呼ばれないことがあり、
    無期限に待つとそのサーバーの読み上げワーカーが永久に返らなくなる
    （キューの登録も消せないため、再起動まで読み上げが止まる）。
    """
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()

    def after_playing(error: Exception | None) -> None:
        # このコールバックは discord.py の再生スレッドから呼ばれるため、
        # イベントループ側のオブジェクトへは call_soon_threadsafe 経由で触る。
        if error:
            logger.error(f'読み上げの再生エラー: {type(error).__name__}', exc_info=error)
        loop.call_soon_threadsafe(finished.set)

    # 待ち時間の上限は「音声の実再生秒数 + 余裕」。16bit なので 1 サンプル 2 バイト、
    # 1 秒あたり (サンプリング周波数 × チャンネル数 × 2) バイトになる。
    play_seconds = len(pcm) / float(TTS_SAMPLE_RATE * TTS_CHANNELS * 2)
    timeout_seconds = play_seconds + TTS_PLAY_TIMEOUT_MARGIN_SECONDS

    pcm_stream = io.BytesIO(pcm)
    audio_source = discord.PCMAudio(pcm_stream)
    controller = get_playback_controller(voice_client)
    try:
        controller.play(voice_client, audio_source, after=after_playing)
        await asyncio.wait_for(finished.wait(), timeout=timeout_seconds)
    except asyncio.CancelledError:
        try:
            controller.stop(voice_client, source=audio_source)
        except Exception as error:
            logger.error(f'TTS: キャンセル時の再生停止に失敗しました: {type(error).__name__}', exc_info=error)
        raise
    except asyncio.TimeoutError:
        logger.warning(f'TTS: 再生の完了通知が {timeout_seconds:.1f} 秒以内に届かなかったため、この読み上げを打ち切って次へ進みます。ボイス接続が正常な切断を経ずに切れた可能性があります。', exc_info=True)
        try:
            # まだ鳴っている場合に次の読み上げと音が重ならないよう止めておく。
            # 既に切断されていると例外になりうるが、打ち切り自体は続行する。
            controller.stop(voice_client, source=audio_source)
        except Exception:
            # 完了通知の途絶は既に警告済み。停止済み・切断済みでも次の読み上げへ進める。
            logger.debug('TTS: 打ち切り時の再生停止に失敗しました。後始末を続行します。', exc_info=True)
    finally:
        try:
            await controller.wait_for_players()
        finally:
            controller.release(audio_source)
            try:
                audio_source.cleanup()
            except Exception as error:
                logger.error(f'TTS: 音声ストリームの解放に失敗しました: {type(error).__name__}', exc_info=error)
            finally:
                # PCMAudio.cleanup() は入力ストリームを閉じないため、所有する側で解放する。
                pcm_stream.close()


# ギルドIDごとの読み上げ待ちキュー。再生中に届いた返信を捨てずに順番に読むために持つ。
# ワーカーは最初の依頼で起動し、キューが空になったら自分を片付けて終了するので、
# 参加していないサーバーの分までタスクが常駐することはない。
#
# 投入時の接続・チャンネルも記録し、切替前の返信を別の通話へ持ち越さない。
# プロファイルを積む時点で確定させておくと、待っている間に /functions でペルソナを
# 切り替えられても、既に積まれた分は依頼時のキャラクターの声のまま読み上げられる。
@dataclass(frozen=True)
class _SpeechRequest:
    text: str
    profile: Mapping[str, Any]
    voice_client: discord.VoiceClient
    channel_id: int | None
    is_valid: Callable[[], bool] | None

    def matches_connection(self, guild: discord.Guild) -> bool:
        return (
            guild.voice_client is self.voice_client
            and self.voice_client.is_connected()
            and getattr(self.voice_client.channel, "id", None) == self.channel_id
        )


_speech_queues: dict[int, deque[_SpeechRequest]] = {}


def _discard_speech_queue(guild_id: int, queue: deque[_SpeechRequest]) -> None:
    if _speech_queues.get(guild_id) is queue:
        _speech_queues.pop(guild_id, None)


async def _run_speech_worker(guild: discord.Guild) -> None:
    """1サーバー分の読み上げキューを順に処理する。"""
    queue = _speech_queues[guild.id]
    try:
        while True:
            if not queue:
                # ここから return までに await を挟まないこと。挟むと、キューが空だと
                # 判断した後・登録を消す前に新しい依頼が積まれ、その分が
                # 誰にも処理されないまま取り残される。
                return
            request = queue.popleft()

            # 読み上げている間に /leave された場合に備え、毎回その時点の接続を見る。
            if not request.matches_connection(guild):
                continue
            voice_client = request.voice_client
            is_valid = request.is_valid

            try:
                # 配信が終了した依頼は、キュー待機後にも捨てる。
                if is_valid is not None and not is_valid():
                    continue
                pcm = await asyncio.wait_for(
                    _synthesize_pcm(request.text, request.profile),
                    timeout=TTS_TIMEOUT_SECONDS,
                )
                # 合成待機中の終了・移動・接続差し替えも、再生の直前に確かめる。
                if (not request.matches_connection(guild)
                        or (is_valid is not None and not is_valid())):
                    continue
                await _play_pcm(
                    voice_client, pcm,
                    is_valid=lambda: request.matches_connection(guild) and (
                        request.is_valid is None or request.is_valid()
                    ),
                )
            except asyncio.TimeoutError:
                logger.warning(f'TTS: 音声合成が {TTS_TIMEOUT_SECONDS} 秒で完了しなかったため、この返信の読み上げを見送ります。', exc_info=True)
            except Exception as error:
                # 読み上げはチャット本体の付随機能なので、失敗しても次の返信へ進む。
                logger.error('TTSの音声合成に失敗しました。', exc_info=error)
    finally:
        # 予期しない例外で抜けた場合も登録を残さない（次の返信で再起動できるように）。
        _discard_speech_queue(guild.id, queue)


async def play_tts_response(
    message: discord.Message,
    response_text: str | None,
    prompt_file: str | None = None,
    *,
    is_valid: Callable[[], bool] | None = None,
) -> None:
    """AI の返信をボイスチャンネルで読み上げる。

    ボットがボイスチャンネルに接続しているときだけ動作し、それ以外は
    何もせずに戻る（テキストチャットは読み上げの成否に影響されない）。

    `prompt_file` は今回の応答に使ったペルソナのパス。通常のチャットでは
    `MessageHandler` が生成開始時の `ResponseSettings.prompt_file` を渡し、
    途中で設定が変わっても同じキャラクターの話者・高さ・速さ・音色を使う。
    省略・未登録のペルソナの場合は既定の声で読み上げる。

    DM にはギルドが無く、ボットが参加できるボイスチャンネルも存在しないため、
    `message.guild` が None のときは `speak_text` 側で no-op になる。
    """
    await speak_text(message.guild, response_text, prompt_file, is_valid=is_valid)


async def speak_text(
    guild: discord.Guild | None,
    response_text: str | None,
    prompt_file: str | None = None,
    *,
    is_valid: Callable[[], bool] | None = None,
) -> None:
    """テキストを、そのサーバーのボイスチャンネルで読み上げる。

    `play_tts_response` から「元メッセージ」への依存を外した版。自律行動の
    独り言のように、どの発言への返信でもないテキストを読み上げるために使う。
    接続していない・本文が空・整形後に読み上げる中身が残らない場合は
    黙って何もしない（no-op）。
    `is_valid` は自律配信の有効性判定。投入前・合成前・再生直前に照合する。
    """
    if not response_text or response_text.isspace():
        return

    if guild is None:
        return

    voice_client = guild.voice_client
    if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
        return
    if is_valid is not None and not is_valid():
        return

    speech_text = build_speech_text(response_text)
    if not speech_text:
        # 装飾やURLだけの返信を除去した結果、読み上げる中身が残らなかった場合。
        return

    # 待ち行列に並ぶ前に、いま選ばれているキャラクターの声を確定させておく。
    request = _SpeechRequest(
        speech_text, resolve_profile(prompt_file), voice_client,
        getattr(voice_client.channel, "id", None), is_valid,
    )

    queue = _speech_queues.get(guild.id)
    if queue is None:
        queue = deque([request])
        _speech_queues[guild.id] = queue
        worker = spawn(_run_speech_worker(guild), label="ボイス読み上げ")
        if worker is not None:
            # 終了と競合して開始前にキャンセルされた場合は finally が走らない。
            worker.add_done_callback(lambda _: _discard_speech_queue(guild.id, queue))
        return

    # 旧接続や終了済み配信の待機分で、新しい読み上げの枠を塞がない。
    pending = [
        queued for queued in queue
        if queued.matches_connection(guild) and (queued.is_valid is None or queued.is_valid())
    ]
    # ワーカーも同じdequeを参照するため、登録を差し替えず中身だけ整理する。
    queue.clear()
    queue.extend(pending)
    if len(queue) >= TTS_QUEUE_MAX:
        # 読み上げが追いつかないほど返信が続いた場合。古い返信を延々と喋り続けるより、
        # 遅れた分は捨てて会話の現在地に追従する方が自然なため、この返信は見送る。
        logger.warning('TTS: 読み上げの待ち行列が上限に達したため、この返信の読み上げを見送ります。')
        return
    queue.append(request)
