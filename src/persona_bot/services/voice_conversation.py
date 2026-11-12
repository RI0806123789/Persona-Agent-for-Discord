"""Gemini Live による音声会話（/join。#268 の Persona Voice Controller）。

`/join` でボイスチャンネルへ入ったボットと、参加者が声で会話する。1つのボイスチャンネルに
つき1つの Live セッション（`live_session.LiveSession`）を使い、次の部品をここでつなぐ。

- **同意（ConsentState）**: ボイスチャンネルの人間の参加者（ブラックリスト登録者を除く）
  全員が音声処理に同意するまで、入出力を一時停止する。途中参加者も同じ。記憶への保存・
  親密度の更新への同意は音声処理とは別で、初期はオフ。同意は接続している間だけメモリに持つ。
- **話者の管理（Active Speaker Manager）**: Discord の user_id で話者を見分け、Live へは
  同時に1人分の音声だけを送る（複数人の音声を混ぜない）。話し始め・話し終わりはローカルの
  VAD（音量としきい値）で判定し、`activity_start` / `activity_end` で Live へ伝える。
  ほかの人が話している間の「話したい」は発話希望として短時間だけ覚える（音声は溜めない）。
- **割り込み（#409）**: ペルソナの声が出ている間も受信は止めない。受信した音声からは、再生した
  音声を参照信号にしてエコー（ペルソナ自身の声の回り込み）を取り除き（`audio/echo_canceller.py`）、
  除去後の音声で話し始めを判定する。声が出ている間の話し始めはすぐには Live へ送らず手元に留め、
  `LIVE_BARGE_IN_HOLD_SECONDS` 話し続けたら送る（短い相槌・言いよどみでは返答を止めない）。
  送った後、入力の文字起こしがフィラー・相槌を除いて複数の語になったら割り込みと確定し、
  ペルソナの声を強めにフェードアウトさせる（`voice_barge_in.py`）。確定するまでは届いている返答を
  流し続ける。文字起こしがペルソナ自身の直近の発話とほとんど同じなら、回り込みとみなしてその発言への
  返答を再生しない（音声ループの防止）。誤って割り込んだときは Live に確認させる（システム指示）。
  返答の生成を待っている間（まだ声が出ていない間）の話し始めは、従来どおり `LIVE_BARGE_IN_MS` で
  すぐに割り込む。会話の終了・違反の検出・一時停止では、フェードアウトせずに即座に止める。
- **モデレーション（並行検査＋短時間バッファ）**: 入力・出力の文字起こしをローカルの
  NG ワードで即座に、AI モデレーションで生成と並行して検査する。違反を見つけたら再生を
  止めて残りを捨てる（検出前に再生された部分は取り消せないことは仕様上許容する）。
  AI の判定不能は再生を止めないが、記憶・親密度の更新には使わない（条件付き fail-open）。
  同じ発言の違反は最大1回だけ数え、ボット自身の出力の違反は参加者に数えない。
- **記憶・親密度**: 個人の長期記憶・親密度は Live へ渡さない（共有の場で漏らさない）。
  記憶・親密度に同意した人の、入出力とも AI が安全と判定した発言だけを保存・反映する。

生の音声・録音・文字起こしの全文・会話ログは保存しない（処理中だけメモリに持つ）。
"""

from __future__ import annotations

import logging
import asyncio
from collections import Counter
from statistics import median, quantiles
from time import monotonic
from typing import Any, Callable

import discord

from persona_bot.audio.echo_canceller import EchoReference
from persona_bot.audio.live_audio import DISCORD_FRAME_BYTES, LivePlaybackSource, Upsampler, frame_rms
from persona_bot.audio.playback import get_playback_controller
from persona_bot.audio.voice_receive import LiveVoiceClient, VoiceReceiver
from persona_bot.config.constants import (
    LIVE_AEC_ENABLED,
    LIVE_BARGE_IN_FADE_MS,
    LIVE_ECHO_GUARD_SECONDS,
    LIVE_INPUT_CHUNK_MS,
    LIVE_INPUT_QUEUE_MAX_CHUNKS,
    LIVE_MAX_CONCURRENT_SESSIONS,
    LIVE_MAX_UTTERANCE_SECONDS,
    LIVE_OUTPUT_BUFFER_MS,
    LIVE_PLAYBACK_STALL_SECONDS,
    LIVE_TICK_SECONDS,
    LIVE_TRANSCRIPT_MAX_CHARS,
    LIVE_VAD_ENERGY_THRESHOLD,
    LIVE_VAD_SILENCE_MS,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.config.personas import current_persona_catalog
from persona_bot.core.background import spawn, wait_for_cleanup
from persona_bot.services.live_session import CLOSED_QUOTA, LiveOptions, LiveSession, LiveUnavailableError
from persona_bot.services.prompting import build_live_system_instruction
from persona_bot.services.voice_barge_in import (
    INTERRUPTION_SPEECH, RecentSpeech, classify_interruption, is_echo_text,
)
# 状態オブジェクトと、責務ごとに切り出したミックスイン（#415）。`_Utterance` などは
# 従来どおりこのモジュールから import できる。
from persona_bot.services.voice_conversation_consent import VoiceConsentMixin
from persona_bot.services.voice_conversation_moderation import VoiceModerationMixin
from persona_bot.services.voice_conversation_state import (
    _INPUT_BYTES_PER_MS, ConsentState, _Participant, _SpeakerState, _TranscriptState, _Utterance,
)
from persona_bot.utils.discord_helpers import can_bot_send_messages


logger = logging.getLogger(__name__)

# 送信 1 チャンクのバイト数（16kHz・モノラル・16bit）。
_INPUT_CHUNK_BYTES = _INPUT_BYTES_PER_MS * LIVE_INPUT_CHUNK_MS
# 再生を始めるまでに溜める応答音声（48kHz・ステレオ）のバイト数。
_OUTPUT_BUFFER_BYTES = DISCORD_FRAME_BYTES * max(1, LIVE_OUTPUT_BUFFER_MS // 20)
_PLAYBACK_STALL_FRAMES = int(LIVE_PLAYBACK_STALL_SECONDS * 50)
# 割り込みを確定したときに絞り切るまでのフレーム数（20ms 単位）。
_FADE_FRAMES = max(1, LIVE_BARGE_IN_FADE_MS // 20)

_NO_MENTIONS = discord.AllowedMentions.none()

# 利用者に見せる案内（接続の結果・終了の理由）。
NOTICE_LIVE_UNAVAILABLE = (
    "⚠️ 音声会話（Gemini Live）に接続できなかったため、ボイスチャンネルから退出しました。"
    "しばらく待ってからもう一度 /join してください。"
)
NOTICE_LIVE_QUOTA = (
    "⚠️ 音声会話（Gemini Live）の利用枠の上限に達したため、ボイスチャンネルから退出しました。"
    "時間をおいてからもう一度 /join してください。"
)
NOTICE_LIVE_FAILED = (
    "⚠️ 音声会話（Gemini Live）との接続が切れ、つなぎ直せなかったため退出しました。"
    "もう一度 /join してください。"
)
NOTICE_LIVE_CAPACITY = (
    f"⚠️ 同時に音声会話できるボイスチャンネルの上限（{LIVE_MAX_CONCURRENT_SESSIONS}か所）に達しています。"
    "ほかのボイスチャンネルでの会話が終わってから、もう一度 /join してください。"
)
NOTICE_CONSENT_PANEL_FAILED = (
    "⚠️ 同意パネルを投稿できなかったため、音声会話を始めずに退出しました。"
    "ボイスチャンネルのチャットか /join を実行したチャンネルで、ボットに"
    "「チャンネルを見る」「メッセージを送信」の権限があるか確認してください。"
)


class ConsentPanelError(Exception):
    """同意パネルを出せない。同意を集められないため、音声会話を始めない。"""


class VoiceConversation(VoiceConsentMixin, VoiceModerationMixin):
    """1つのボイスチャンネルの音声会話（Persona Voice Controller）。

    責務ごとに次のモジュールへ分けている（#415。動作は変えていない）:
      ・`voice_conversation_state.py`: 同意・参加者・発言・話者の管理・文字起こしの帰属の状態
      ・`voice_conversation_consent.py`: 同意の付与・撤回と同意パネルの更新（`VoiceConsentMixin`）
      ・`voice_conversation_moderation.py`: 入出力のモデレーション・違反記録・記憶と親密度への反映（`VoiceModerationMixin`）
    ここには、受信した音声の振り分け・Live への送信・応答の再生と、それらをつなぐ制御を残す。
    """

    def __init__(
        self,
        service: VoiceConversationService,
        guild: discord.Guild,
        voice_client: LiveVoiceClient,
        channel_id: int,
        notice_channel: Any,
        options: LiveOptions,
        *,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._service = service
        self.guild = guild
        self.voice_client = voice_client
        self.channel_id = channel_id
        self.notice_channel = notice_channel
        self._now = now
        self.consent = ConsentState()
        self._live = LiveSession(
            service.genai_client, options,
            on_message=self._on_live_message,
            on_reconnected=self._on_live_reconnected,
            on_closed=self._on_live_closed,
            connect=service.live_connect,
        )
        self._receiver: VoiceReceiver | None = None
        self._speaker_state = _SpeakerState()
        self._current: _Utterance | None = None
        self._turn_seq = 0
        self._awaiting_interrupt_ack = False
        self._interrupt_ack_deadline = float("-inf")
        self._playback: LivePlaybackSource | None = None
        self._playback_owner: _Utterance | None = None
        self._playback_started = False
        # 割り込みでフェードアウトさせている音源（絞り切るまで再生スレッドが流す）。
        self._fading: LivePlaybackSource | None = None
        # ペルソナの声が最後に止まった時刻（止まった直後の話し始めは回り込みを疑う）。
        self._last_audible_at = float("-inf")
        # エコー除去の参照信号（再生した音声。メモリ上の数秒分だけ）と、回り込みの照合に使う直近の発話。
        self._echo_reference = EchoReference() if LIVE_AEC_ENABLED else None
        self._recent_output = RecentSpeech()
        # 診断用の件数（終了時にログへ出す。本文や話者は含めない）。
        self._barge_in_stats: Counter[str] = Counter()
        self._playback_controller = get_playback_controller(voice_client)
        self._upsampler = Upsampler()
        self._send_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        # いまの Live の接続（世代）で activity_start を送ったか。送っていなければ None。
        self._open_generation: int | None = None
        self._pending_chunk = bytearray()
        self._queue_full_warned = False
        self._gate_open = False
        self._waiting_count = 0
        # 入力の文字起こしの帰属先。発話権（_current）とは別に持つ — 前の人の文字起こしが
        # 遅れて届いても、次に話し始めた人の発言として扱わない（違反・記憶を取り違えない）。
        self._transcript_state = _TranscriptState()
        self._stopped = False
        self._end_requested = False
        # Live の接続が再接続できずに終わった理由（CLOSED_QUOTA / CLOSED_FAILED）。終わっていなければ None。
        self.live_closed_reason: str | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._latencies: list[float] = []
        self.panel_message: Any = None
        self.panel_view: Any = None
        self._panel_update_task: asyncio.Task[Any] | None = None
        self._panel_updated_at = float("-inf")

    # ==================================================================
    # 開始・終了
    # ==================================================================

    @property
    def stopped(self) -> bool:
        return self._stopped

    async def start(self) -> None:
        """Live へ接続し、受信と見回りを始める。接続できなければ LiveUnavailableError。"""
        await self._live.start()
        self._receiver = self._service.receiver_factory(
            self.voice_client, self._on_audio, echo_reference=self._echo_reference,
        )
        self._receiver.start()
        self._tasks.append(spawn(self._run_sender(), label="音声会話の送信"))
        self._tasks.append(spawn(self._run_ticks(), label="音声会話の見回り"))
        self._update_gate()

    async def stop(self) -> None:
        """受信・再生・Live をまとめて止める（何度呼んでもよい）。切断はしない。"""
        if self._stopped:
            return
        self._stopped = True
        if self._current is not None:
            self._discard_response(self._current, "音声会話の停止")
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        self._log_echo_stats()
        try:
            if self._receiver is not None:
                self._receiver.stop()
        except Exception:
            logger.warning("音声会話: 受信の停止に失敗しました。", exc_info=True)
        self._abort_playback()
        await self._wait_previous_player()
        self._clear_send_queue()
        self._speaker_state.participants.clear()
        self._speaker_state.reservations.clear()
        self._recent_output.clear()
        await wait_for_cleanup(self._live.close(), label="音声会話の Live の切断")
        await asyncio.gather(*(task for task in self._tasks if task is not current), return_exceptions=True)
        panel_task = self._panel_update_task
        if panel_task is not None and panel_task is not current:
            # ★ キャンセルを要求するだけでは、実行中の「会話中です」の編集の通信が残り、
            # 直後の終了表示の編集と順序が入れ替わりうる。終わるまで待ってから終了表示にする。
            panel_task.cancel()
            await asyncio.gather(panel_task, return_exceptions=True)
        self._log_latency()
        self._log_barge_in_stats()
        await self._finish_panel()

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
    # 参加者の出入り（同意の付与・撤回とパネルの更新は VoiceConsentMixin）
    # ==================================================================

    def handle_self_update(self) -> None:
        """ボット自身の移動・切断。ゲートの開閉を見回り（tick）を待たずに更新する。"""
        self._update_gate()

    def handle_member_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入り。抜けた人の発話権・発話希望・プリロールを無効にする。"""
        before_id = getattr(getattr(before, "channel", None), "id", None)
        after_id = getattr(getattr(after, "channel", None), "id", None)
        if before_id == after_id:
            return
        if before_id == self.channel_id:
            if self._speaker_state.active_user == member.id:
                self._end_activity(discard=True)
            if self._current is not None and self._current.user_id == member.id:
                self._discard_response(self._current, "チャンネルから退出")
            self._speaker_state.participants.pop(member.id, None)
            self._remove_reservation(member.id)
        self._update_gate()
        self._schedule_panel_update()

    # ==================================================================
    # 開閉（全員の同意がそろっているか）
    # ==================================================================

    def _eligible_members(self) -> list[Any]:
        channel = getattr(self.voice_client, "channel", None)
        if channel is None or getattr(channel, "id", None) != self.channel_id:
            # 別のチャンネルへ移されている間（voice_guard が戻すまで）は誰の声も扱わない。
            return []
        return [
            member for member in getattr(channel, "members", [])
            if not member.bot and not self._service.is_blocked(member.id)
        ]

    def _update_gate(self) -> None:
        if self._stopped:
            return
        eligible = self._eligible_members()
        waiting = [member for member in eligible if member.id not in self.consent.voice]
        is_open = bool(eligible) and not waiting
        self._waiting_count = len(waiting)
        if is_open == self._gate_open:
            return
        self._gate_open = is_open
        if is_open:
            logger.info("音声会話: 参加者全員の同意がそろったため、会話を再開します。")
        else:
            logger.info("音声会話: 同意していない参加者がいるため、会話を一時停止します。")
            self._pause()
        self._schedule_panel_update()

    def _pause(self) -> None:
        if self._speaker_state.speaking:
            self._end_activity(discard=True)
        if self._current is not None:
            self._discard_response(self._current, "一時停止")
        self._abort_playback()
        self._clear_send_queue()
        self._speaker_state.reservations.clear()
        for participant in self._speaker_state.participants.values():
            participant.drop_pending_audio()

    @property
    def is_open(self) -> bool:
        return self._gate_open

    @property
    def waiting_count(self) -> int:
        return self._waiting_count

    # ==================================================================
    # 受信した音声（イベントループ上で呼ばれる）
    # ==================================================================

    def _on_audio(self, user_id: int, pcm: bytes, level: float | None = None) -> None:
        """受信した 1 フレーム（16kHz・モノラル）。level はエコー除去が判定した発話の強さ。

        エコー除去なし（level が None）なら、フレームの実効値で判定する。
        """
        if self._stopped or not self._gate_open or not self._live.connected:
            return
        if user_id not in self.consent.voice or self._service.is_blocked(user_id):
            # 未同意・ブラックリスト登録者の声は Live へ送らずに捨てる。
            return
        now = self._now()
        voiced = (frame_rms(pcm) if level is None else level) >= LIVE_VAD_ENERGY_THRESHOLD
        action, participant = self._speaker_state.receive(
            user_id, pcm, now, voiced=voiced, bot_speaking=self._bot_speaking, bot_audible=self._bot_audible,
        )
        if action == "send":
            self._queue_audio(pcm)
        elif action == "begin":
            self._begin_activity(user_id, participant, now)
        elif action == "barge_in":
            self._begin_activity(user_id, participant, now, barge_in=True)

    def _bot_audible(self) -> bool:
        """ペルソナの声がいま出ているか（再生を始めた音源がある）。割り込みは手元で確かめてから送る。"""
        source = self._playback
        return source is not None and not source.aborted and self._playback_started

    def _recently_audible(self, now: float) -> bool:
        """ペルソナの声が出ているか、止まってすぐか（回り込みがまだ届きうる）。"""
        return self._bot_audible() or now - self._last_audible_at <= LIVE_ECHO_GUARD_SECONDS

    def _bot_speaking(self) -> bool:
        """ボットが返答中か（生成中・再生中）。話し始めのしきい値を割り込み用に上げる。"""
        if self._playback is not None and not self._playback.aborted:
            return True
        current = self._current
        if current is None or current.ended_at is None or current.response_complete or current.discarded:
            return False
        if current.response_started:
            return True
        # 返答がまだ届かない間も返答待ちとみなす。ただし返答が来ないまま時間が過ぎたら
        # 待つのをやめる（いつまでも話し始めのしきい値を割り込み用のままにしない）。
        return self._now() - current.ended_at < LIVE_PLAYBACK_STALL_SECONDS

    def _begin_activity(self, user_id: int, participant: _Participant, now: float, *, barge_in: bool = False) -> None:
        previous = self._current
        echo_suspect = self._recently_audible(now)
        if barge_in:
            # ペルソナの声が出ている間に話し続けている。Live へ送って文字起こしで割り込みかを確かめる。
            # 確定するまでは、届いている返答を流し続ける（相槌・回り込みなら止めない）。
            logger.info("音声会話: 返答中に話し続けている人の声を送り、割り込みかを確かめます。")
            self._barge_in_stats["sent"] += 1
            if previous is not None and previous.ended_at is not None and not previous.response_complete:
                # サーバーはこの発話の開始で生成を打ち切る（START_OF_ACTIVITY_INTERRUPTS）。打ち切りの通知
                # （interrupted）までに届いた残りは、前の返答の音源へ流す（新しい発言の返答と取り違えない）。
                self._awaiting_interrupt_ack = True
                self._interrupt_ack_deadline = now + LIVE_PLAYBACK_STALL_SECONDS
                self._transcript_state.interrupted = previous
                previous.cut_off = True
        elif self._bot_speaking():
            logger.info("音声会話: 返答中の割り込みを検出したため、再生を止めます。")
            if previous is not None:
                if previous.ended_at is not None and not previous.response_complete:
                    # 生成中の返答の残りが届いても、新しい発言の返答と取り違えない。最初の返答の
                    # チャンクが届く前（response_started が偽の返答待ち）に割り込まれた場合も同じ。
                    # ここで印を付けないと、サーバーの interrupted で文字起こしの待ちが解けず、
                    # 前の人宛ての遅れた返答や turn_complete が新しい発言への返答として通ってしまう。
                    # サーバーが割り込みを知らせないまま終わっても、待ち続けないよう期限を設ける。
                    self._awaiting_interrupt_ack = True
                    self._interrupt_ack_deadline = now + LIVE_PLAYBACK_STALL_SECONDS
                    self._transcript_state.interrupted = previous
                self._discard_response(previous, "割り込み")
            self._abort_playback()
        self._turn_seq += 1
        self._current = _Utterance(self._turn_seq, user_id, now, echo_suspect=echo_suspect)
        if barge_in:
            self._current.barge_in = "pending"
            self._current.interrupts = previous
        self._assign_transcript_owner(self._current, now)
        self._speaker_state.active_user = user_id
        self._speaker_state.speaking = True
        self._remove_reservation(user_id)
        self._enqueue("start")
        for frame in participant.take_pending_audio():
            self._queue_audio(frame)

    # --- 入力の文字起こしの帰属（発話権とは別に管理する） ---------------------------

    def _assign_transcript_owner(self, utterance: _Utterance, now: float) -> None:
        """話し始めた発言を、入力の文字起こしの帰属先にする（前の人の分が閉じていれば）。"""
        self._transcript_state.assign(utterance, now)

    def _close_transcript(self, utterance: _Utterance | None) -> None:
        self._transcript_state.close(utterance)

    def _reset_transcripts(self) -> None:
        """接続の張り替えで、届きうる文字起こしが無くなったときに帰属の状態を戻す。"""
        self._transcript_state.reset()

    def _end_activity(self, *, discard: bool = False) -> None:
        if not self._speaker_state.speaking:
            return
        utterance = self._current
        self._flush_chunk()
        self._enqueue("end")
        self._speaker_state.speaking = False
        self._speaker_state.active_user = None
        now = self._now()
        if utterance is not None:
            utterance.ended_at = now
            if discard:
                utterance.discarded = True
                # 破棄した発言の文字起こしは使わない。閉じておかないと、次に話し始めた人の断片が
                # 破棄済みの発言の帰属待ちに阻まれて捨てられ、その発言が記憶から外れる。
                self._close_transcript(utterance)
            else:
                utterance.input_task = spawn(self._judge_input(utterance), label="音声会話の入力判定")
        # 待っていた人に、少しの間だけ優先して話す機会を渡す。
        self._speaker_state.extend_reservations(now)

    def _request_floor(self, user_id: int, now: float) -> None:
        self._speaker_state.request_floor(user_id, now)

    def _remove_reservation(self, user_id: int) -> None:
        self._speaker_state.remove_reservation(user_id)

    def _floor_available_for(self, user_id: int, now: float) -> bool:
        return self._speaker_state.floor_available(user_id, now)

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

    # ==================================================================
    # 見回り（話し終わりの判定・接続の確認）
    # ==================================================================

    async def _run_ticks(self) -> None:
        while not self._stopped:
            await asyncio.sleep(LIVE_TICK_SECONDS)
            self.tick()

    def tick(self) -> None:
        if self._stopped:
            return
        guild_voice = getattr(self.guild, "voice_client", None)
        if guild_voice is not self.voice_client or not self.voice_client.is_connected():
            # /leave 以外の切断（voice_guard の退出・外部からの切断）。
            if not self._end_requested:
                self._end_requested = True
                spawn(
                    self._service.end(self.guild.id, reason="ボイス接続が切れた", disconnect=False, conversation=self),
                    label="音声会話の終了（切断）",
                )
            return
        self._update_gate()
        self._speaker_state.expire_holds(self._now())
        if (self._transcript_state.blocked is not None
                and self._now() - self._transcript_state.blocked_since >= LIVE_PLAYBACK_STALL_SECONDS):
            # 前の人の文字起こしが閉じる合図が来ないまま時間が過ぎた。以後の断片は次の人の分とみなす
            # （次の人の冒頭は捨てた可能性があるため、その発言は記憶に使わない）。
            self._close_transcript(self._transcript_state.blocked)
        if self._speaker_state.speaking and self._speaker_state.active_user is not None:
            participant = self._speaker_state.participants.get(self._speaker_state.active_user)
            now = self._now()
            started = self._current.started_at if self._current is not None else now
            silent = participant is None or now - participant.last_voiced_at >= LIVE_VAD_SILENCE_MS / 1000
            if silent or now - started >= LIVE_MAX_UTTERANCE_SECONDS:
                self._end_activity()

    # ==================================================================
    # Live からの応答
    # ==================================================================

    async def _on_live_message(self, message: Any) -> None:
        if self._stopped:
            return
        usage = getattr(message, "usage_metadata", None)
        if usage:
            self._service.usage.log_tokens("音声会話", usage)
        content = getattr(message, "server_content", None)
        if content is None:
            return
        transcription = getattr(content, "input_transcription", None)
        if transcription is not None:
            self._on_input_transcript(transcription.text or "", bool(getattr(transcription, "finished", None)))
        if getattr(content, "interrupted", None):
            self._awaiting_interrupt_ack = False
            previous = self._transcript_state.interrupted
            if previous is not None and not previous.discarded and self._playback_owner is previous:
                # 割り込みを確かめている間。届いている分は流し切る（確定したらフェードアウトさせる）。
                self._finish_held_previous(previous)
            else:
                self._abort_playback()
            # 割り込まれた発言は、生成が始まった時点で入力を処理し終えている。
            self._close_transcript(previous)
            self._transcript_state.interrupted = None
        model_turn = getattr(content, "model_turn", None)
        if model_turn is not None:
            utterance = self._current
            for part in model_turn.parts or []:
                inline = getattr(part, "inline_data", None)
                if inline is not None and inline.data:
                    await self._on_model_audio(inline.data)
                    if self._stopped or self._current is not utterance:
                        return
        transcription = getattr(content, "output_transcription", None)
        if transcription is not None and transcription.text:
            self._on_output_transcript(transcription.text)
        if getattr(content, "generation_complete", None) or getattr(content, "turn_complete", None):
            await self._on_generation_complete()
        if getattr(content, "turn_complete", None):
            self._on_turn_complete()

    def _held_previous(self) -> _Utterance | None:
        """割り込みを確かめている間、再生を続けている前の返答（打ち切りの通知までに届いた残りもここへ流す）。"""
        previous = self._transcript_state.interrupted
        if (previous is None or not self._awaiting_interrupt_ack or previous.discarded
                or self._playback_owner is not previous or self._now() >= self._interrupt_ack_deadline):
            return None
        return previous

    def _finish_held_previous(self, previous: _Utterance) -> None:
        """割り込まれた前の返答の生成が終わった。届いた分を流し切り、出力の AI 判定に掛ける。"""
        if self._playback is not None and self._playback_owner is previous:
            self._playback.finish()
        if previous.response_started and not previous.response_complete:
            previous.response_complete = True
            # 打ち切られた返答は記憶に使わない（cut_off）が、流している分の安全性は確かめる。
            spawn(self._judge_output(previous), label="音声会話の出力判定（割り込まれた返答）")

    def _accepting_response(self) -> _Utterance | None:
        """いま届いた応答を受け取る発言。受け取らない（捨てる）なら None。"""
        if self._awaiting_interrupt_ack and self._now() >= self._interrupt_ack_deadline:
            self._awaiting_interrupt_ack = False
            previous = self._transcript_state.interrupted
            if previous is not None and not previous.discarded and self._playback_owner is previous:
                self._finish_held_previous(previous)
        if self._awaiting_interrupt_ack or self._speaker_state.speaking or not self._gate_open:
            return None
        current = self._current
        if current is None or current.ended_at is None or current.discarded:
            return None
        return current

    def _on_input_transcript(self, text: str, finished: bool) -> None:
        utterance = self._transcript_state.owner
        if utterance is None:
            blocked = self._transcript_state.blocked
            if blocked is None:
                return
            if text:
                # 前の人の残りか次の人の冒頭か区別できない断片は捨てる（別の人へ帰属させない）。
                blocked.transcript_incomplete = True
                if self._transcript_state.next is not None:
                    self._transcript_state.next.transcript_incomplete = True
                logger.debug("音声会話: 話者を確定できない文字起こしを捨てました。")
            if finished:
                self._close_transcript(blocked)
            return
        if text:
            utterance.input_text = (utterance.input_text + text)[:LIVE_TRANSCRIPT_MAX_CHARS]
            utterance.input_updated_at = self._now()
            if self._service.ng_filter.contains_ng_word(utterance.input_text):
                self._reject_input(utterance, "入力の NG ワード")
            else:
                self._check_barge_in(utterance)
                if (utterance.input_task is not None and utterance.input_task.done()
                        and not utterance.violation_recorded and not self._stopped):
                    # 入力の AI 判定が済んだ後に続きが届いた。増えた分も判定し直す。
                    utterance.input_task = spawn(self._judge_input(utterance), label="音声会話の入力の再判定")
        if finished:
            self._close_transcript(utterance)

    def _check_barge_in(self, utterance: _Utterance) -> None:
        """返答中に話し始めた人の文字起こしが、割り込みとして確定できる中身になったか確かめる。

        フィラー・相槌だけの間や、ペルソナ自身の発話の回り込みでは確定しない（返答を流し続ける）。
        """
        if utterance.barge_in != "pending" or utterance.discarded:
            return
        verdict = classify_interruption(utterance.input_text, self._recent_output.text(self._now()))
        if verdict != INTERRUPTION_SPEECH:
            return
        utterance.barge_in = "confirmed"
        self._barge_in_stats["confirmed"] += 1
        previous = utterance.interrupts
        if previous is not None and not previous.discarded:
            logger.info("音声会話: 割り込みを確定したため、返答をフェードアウトします。")
            self._discard_response(previous, "割り込み", fade=True)

    def _is_echo_reply(self, utterance: _Utterance) -> bool:
        """この発言への返答を再生しないか（発言がペルソナ自身の声の回り込みだった）。"""
        if not utterance.echo_suspect or utterance.barge_in == "confirmed":
            return False
        return is_echo_text(utterance.input_text, self._recent_output.text(self._now()))

    async def _on_model_audio(self, data: bytes) -> None:
        previous = self._held_previous()
        if previous is not None:
            # 割り込みを確かめている間に届いた、前の返答の残り（打ち切りの通知の前に生成された分）。
            if self._playback is not None and not self._playback.aborted:
                self._playback.feed(self._upsampler.process(data))
            return
        utterance = self._accepting_response()
        if utterance is None:
            return
        if utterance.first_audio_at is None:
            if self._is_echo_reply(utterance):
                # ★ 自分の声を参加者の発言として受け取った（エコー除去で取り切れなかった回り込み）。
                # その返答を流すと、また回り込んで返答し続ける音声ループになるため再生しない。
                self._barge_in_stats["echo"] += 1
                self._discard_response(utterance, "自分の声の回り込みへの返答")
                return
            if utterance.barge_in == "pending":
                # 割り込みと確定しないまま返答が来た（言いよどみ・相槌だった）。前の返答は差し替える。
                self._barge_in_stats["unconfirmed"] += 1
            utterance.first_audio_at = self._now()
        utterance.response_started = True
        if (self._playback is None or self._playback_owner is not utterance
                or self._playback.aborted):
            # 再生側が途絶えで打ち切った音源（aborted）にはもう流せない。後続のチャンクは
            # 新しい音源で再生し直す（同じ発話の再開）。まだ流れている前の返答（割り込みを確かめる間に
            # 流していた分）は、強めに絞ってから替える。
            if self._playback is not None and self._playback_owner is not utterance and not self._playback.aborted:
                self._fade_out_playback()
            else:
                self._abort_playback()
            self._playback = LivePlaybackSource(stall_frames=_PLAYBACK_STALL_FRAMES, reference=self._echo_reference)
            self._playback_owner = utterance
            self._playback_started = False
            self._upsampler = Upsampler()
        self._playback.feed(self._upsampler.process(data))
        if not self._playback_started and self._playback.buffered_bytes >= _OUTPUT_BUFFER_BYTES:
            await self._start_playback()

    def _on_output_transcript(self, text: str) -> None:
        # 回り込みの照合用に、ボット自身の発話は受け取るかどうかに関わらず覚える（メモリ上だけ）。
        self._recent_output.add(self._now(), text)
        previous = self._held_previous()
        if previous is not None:
            # 割り込みを確かめている間も流している前の返答。NG ワードの検査は続ける。
            previous.output_text = (previous.output_text + text)[:LIVE_TRANSCRIPT_MAX_CHARS]
            if self._service.ng_filter.contains_ng_word(previous.output_text):
                previous.output_ng = True
                self._discard_response(previous, "出力の NG ワード")
            return
        utterance = self._accepting_response()
        if utterance is None:
            return
        utterance.response_started = True
        utterance.output_text = (utterance.output_text + text)[:LIVE_TRANSCRIPT_MAX_CHARS]
        if self._service.ng_filter.contains_ng_word(utterance.output_text):
            # ボット自身の出力の違反は、直前の参加者に数えない（再生を止めるだけ）。
            utterance.output_ng = True
            self._discard_response(utterance, "出力の NG ワード")

    async def _on_generation_complete(self) -> None:
        if self._playback is None or self._playback_owner is not self._current:
            return
        self._playback.finish()
        if not self._playback_started:
            # バッファに満たない短い返答も、ここで流し始める。
            await self._start_playback()

    def _on_turn_complete(self) -> None:
        if self._awaiting_interrupt_ack:
            # 割り込まれた古い返答の終わり。新しい発言の返答はこの後に届く。
            self._awaiting_interrupt_ack = False
            previous = self._transcript_state.interrupted
            if previous is not None and not previous.discarded and self._playback_owner is previous:
                self._finish_held_previous(previous)
            self._close_transcript(previous)
            self._transcript_state.interrupted = None
            return
        utterance = self._current
        if utterance is None or utterance.ended_at is None or not utterance.response_started:
            return
        # 返答が終わった時点で、その発言の入力はサーバーが処理し終えている。
        self._close_transcript(utterance)
        if utterance.response_complete:
            return
        utterance.response_complete = True
        spawn(self._judge_output(utterance), label="音声会話の出力判定")

    def _on_live_reconnected(self) -> None:
        # 新しい接続は、切れる前の発話の区切りを知らない。途中の発言と返答は捨てる。
        if self._speaker_state.speaking:
            self._end_activity(discard=True)
        if self._current is not None:
            self._discard_response(self._current, "Live の再接続")
        # 割り込みを確かめる間に流していた前の返答も止める（打ち切りの通知はもう届かない）。
        self._abort_playback()
        # 古い接続の文字起こしはもう届かない。
        self._reset_transcripts()
        self._awaiting_interrupt_ack = False
        self._clear_send_queue()

    async def _on_live_closed(self, reason: str) -> None:
        # 開始の途中（同意パネルの投稿を待つ間）で、まだ登録されていない会話でも終了を取りこぼさない。
        # 開始処理がこの記録を見て、開始を失敗にする。
        self.live_closed_reason = reason
        notice = NOTICE_LIVE_QUOTA if reason == CLOSED_QUOTA else NOTICE_LIVE_FAILED
        # 受信ループの中から自分を止めないよう、終了は別タスクで行う。
        spawn(
            self._service.end(self.guild.id, reason="Live の終了", notice=notice, disconnect=True, conversation=self),
            label="音声会話の終了（Live）",
        )

    # ==================================================================
    # 再生
    # ==================================================================

    async def _wait_previous_player(self) -> None:
        async with self._playback_controller.lock:
            await self._playback_controller.wait_for_players()

    async def _start_playback(self) -> None:
        source = self._playback
        if source is None or self._playback_started:
            return
        self._playback_started = True
        owner = self._playback_owner
        loop = asyncio.get_running_loop()

        def after_playing(error: Exception | None) -> None:
            # discord.py の再生スレッドから呼ばれる。
            if error:
                logger.error("音声会話: 再生エラー。", exc_info=error)
            loop.call_soon_threadsafe(self._on_playback_finished, source)

        try:
            async with self._playback_controller.lock:
                if (self._stopped or self._playback is not source or source.aborted
                        or owner is None or owner.discarded):
                    self._release_unplayed(source)
                    return
                await self._playback_controller.prepare(self.voice_client)
                if (self._stopped or self._playback is not source or source.aborted
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
        if self._playback is source:
            source.abort()
            self._on_playback_finished(source)

    def _on_playback_finished(self, source: LivePlaybackSource) -> None:
        self._playback_controller.release(source)
        if self._fading is source:
            self._fading = None
        if self._playback is source:
            if self._playback_started:
                self._last_audible_at = self._now()
            self._playback = None
            self._playback_owner = None
            self._playback_started = False

    def _abort_playback(self) -> None:
        """再生を即座に止める（会話の終了・違反の検出・一時停止など）。フェードアウト中の音源も止める。"""
        fading, self._fading = self._fading, None
        if fading is not None:
            fading.abort()
            self._stop_player(fading)
        source = self._playback
        if source is None:
            return
        source.abort()
        self._playback = None
        self._playback_owner = None
        started, self._playback_started = self._playback_started, False
        if started:
            self._last_audible_at = self._now()
            self._stop_player(source)

    def _fade_out_playback(self) -> None:
        """再生中の返答を強めにフェードアウトさせてから止める（割り込みを確定したとき。#409）。

        絞り切るまで（LIVE_BARGE_IN_FADE_MS）は再生スレッドが流し、終わると after から後片付けされる。
        その間に会話が終わったら `_abort_playback` が即座に止める。
        """
        source = self._playback
        if source is None:
            return
        self._playback = None
        self._playback_owner = None
        started, self._playback_started = self._playback_started, False
        if started and not source.aborted:
            self._last_audible_at = self._now()
            self._fading = source
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
        if self._playback_owner is utterance:
            if fade:
                self._fade_out_playback()
            else:
                self._abort_playback()


class VoiceConversationService:
    """サーバーごとの音声会話を管理する（同時実行の上限・開始・終了・参加者の出入り）。"""

    def __init__(
        self,
        client: discord.Client,
        genai_client: Any,
        *,
        moderator: Any,
        ng_filter: Any,
        blacklist: Any,
        memory: Any,
        affinity: Any,
        usage: Any,
        voice_guard: Any = None,
        live_connect: Callable[..., Any] | None = None,
        receiver_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._client = client
        self.genai_client = genai_client
        self.moderator = moderator
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.memory = memory
        self.affinity = affinity
        self.usage = usage
        self.voice_guard = voice_guard
        self.live_connect = live_connect
        # 受信の生成口: (voice_client, sink, echo_reference=参照信号または None) -> 受信（start/stop を持つ）。
        self.receiver_factory = receiver_factory or (
            lambda voice_client, sink, echo_reference=None: VoiceReceiver(
                voice_client, sink, echo_reference=echo_reference,
            )
        )
        self._sessions: dict[int, VoiceConversation] = {}
        self._reserved: dict[int, object] = {}

    def is_blocked(self, user_id: int) -> bool:
        return self.blacklist.is_blocked(user_id)

    def get(self, guild_id: int) -> VoiceConversation | None:
        return self._sessions.get(guild_id)

    def reserve(self, guild_id: int) -> object | None:
        """開始の予約票を返す。上限なら None（判定と確保の間に await を挟まない）。"""
        if guild_id in self._reserved:
            return self._reserved[guild_id]
        # 置換中も枠を保持し、既存会話と予約は同じサーバーなら二重に数えない。
        if (guild_id not in self._sessions
                and len(self._sessions.keys() | self._reserved.keys()) >= LIVE_MAX_CONCURRENT_SESSIONS):
            return None
        reservation = self._reserved[guild_id] = object()
        return reservation

    def release_reservation(self, guild_id: int, *, reservation: object | None = None) -> None:
        # 古い開始処理の finally で、停止後に取り直した予約を消さない。
        if reservation is None or self._reserved.get(guild_id) is reservation:
            self._reserved.pop(guild_id, None)

    async def start(
        self,
        guild: discord.Guild,
        voice_client: LiveVoiceClient,
        voice_channel: Any,
        response_settings: Any,
        *,
        fallback_channel: Any = None,
        reservation: object | None = None,
    ) -> VoiceConversation:
        """音声会話を始める。先行する接続・設定読込があれば reserve() の予約票を渡す。

        Raises:
            LiveUnavailableError: Live へ接続できなかった。
            ConsentPanelError: 同意パネルを出せなかった（同意を集められないので始めない）。
            asyncio.CancelledError: 待機中に予約が取り消された。
        """
        reservation = reservation if reservation is not None else self.reserve(guild.id)
        if reservation is None:
            raise LiveUnavailableError(quota=False)

        def check_reservation() -> None:
            if self._reserved.get(guild.id) is not reservation:
                # /leave・サーバー退出後は、待機から戻っても会話を復活させない。
                raise asyncio.CancelledError

        try:
            check_reservation()
            # 予約票が枠を保持するので、旧会話の所有権は停止を待つ前に外す。
            # 遅れて来た旧会話の end() が置換用の予約と接続を片付けないようにする。
            existing = self._sessions.pop(guild.id, None)
            if existing is not None:
                await wait_for_cleanup(existing.stop(), label="音声会話の置換前の終了")
                check_reservation()
            persona_prompt = await load_persona_template(response_settings.prompt_file)
            check_reservation()
            voice_name = str(current_persona_catalog().voice_profile(response_settings.prompt_file).get("live_voice") or "")
            options = LiveOptions(
                system_instruction=build_live_system_instruction(
                    persona_prompt, grounding=response_settings.grounding_enabled,
                ),
                voice_name=voice_name,
                grounding=response_settings.grounding_enabled,
            )
            notice_channel = (
                voice_channel if can_bot_send_messages(voice_channel, guild.me) else fallback_channel
            )
            conversation = VoiceConversation(
                self, guild, voice_client, voice_channel.id, notice_channel, options,
            )
            try:
                await conversation.start()
                check_reservation()
                # 同意パネルを出せなければ誰も同意できず、Live の接続と同時実行の枠だけが残る。
                # パネルの投稿までを開始の成功の条件にする（登録はその後）。
                await self._post_panel(conversation)
                check_reservation()
                if conversation.live_closed_reason is not None:
                    # パネルの投稿を待つ間に Live が終わった。未登録のため end() は何もしないので、
                    # 声に応答しない会話を登録せず、ここで開始を失敗にする。
                    raise LiveUnavailableError(quota=conversation.live_closed_reason == CLOSED_QUOTA)
            except BaseException:
                await wait_for_cleanup(conversation.stop(), label="音声会話の開始失敗の後片付け")
                raise
            self._sessions[guild.id] = conversation
        finally:
            self.release_reservation(guild.id, reservation=reservation)
        logger.info("音声会話: 開始しました。")
        return conversation

    async def _post_panel(self, conversation: VoiceConversation) -> None:
        from persona_bot.views.voice_consent_view import VoiceConsentView, build_consent_panel_text

        channel = conversation.notice_channel
        if channel is None:
            logger.warning("音声会話: 同意パネルを出せるチャンネルがありません。")
            raise ConsentPanelError
        view = VoiceConsentView(conversation, is_blocked=self.is_blocked)
        try:
            conversation.panel_message = await channel.send(
                build_consent_panel_text(conversation), view=view, allowed_mentions=_NO_MENTIONS,
            )
        except discord.DiscordException as error:
            view.stop()
            logger.error("音声会話: 同意パネルを送信できませんでした。", exc_info=error)
            raise ConsentPanelError from error
        conversation.panel_view = view

    async def stop(self, guild_id: int, *, reason: str) -> bool:
        """そのサーバーの音声会話を止める（切断は呼び出し側）。止めたら True。"""
        conversation = self._sessions.pop(guild_id, None)
        self.release_reservation(guild_id)
        if conversation is None:
            return False
        logger.info("音声会話: 終了します（%s）。", reason)
        await wait_for_cleanup(conversation.stop(), label="音声会話の終了")
        return True

    async def end(
        self, guild_id: int, *, reason: str, notice: str | None = None, disconnect: bool,
        conversation: VoiceConversation | None = None,
    ) -> None:
        """会話側の事情（Live の終了・接続の喪失）で止める。必要なら案内して退出する。"""
        current = self._sessions.get(guild_id)
        if conversation is not None and current is not conversation:
            # 既に /leave・/join で入れ替わった会話には触らない。
            return
        if current is None:
            return
        voice_client = current.voice_client
        await self.stop(guild_id, reason=reason)
        if guild_id in self._sessions or guild_id in self._reserved:
            # 終了を待つ間に /join が同じ接続で新しい会話を始めた（始めようとしている）。
            # 新しい会話の接続には触らず、退出の案内も出さない。
            return
        if notice:
            await current._notify(notice)
        if guild_id in self._sessions or guild_id in self._reserved:
            # 案内の送信中にも /join は始まる。保護解除・切断の直前に所有権を再確認する。
            return
        guild = current.guild
        if disconnect and guild.voice_client is voice_client and voice_client.is_connected():
            if self.voice_guard is not None:
                self.voice_guard.release(guild_id)
            try:
                await wait_for_cleanup(voice_client.disconnect(force=True), label="音声会話の終了時の切断")
            except Exception:
                logger.warning("音声会話: 終了時の切断に失敗しました。", exc_info=True)

    async def stop_all(self) -> None:
        # まだ _sessions に登録されていない開始待ちも無効にする。
        self._reserved.clear()
        for guild_id in list(self._sessions):
            await self.stop(guild_id, reason="ボットの終了")

    def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入りを、そのサーバーの音声会話へ伝える（同期関数。届いた順に処理する）。"""
        guild = getattr(member, "guild", None)
        conversation = self._sessions.get(getattr(guild, "id", None))
        if conversation is None:
            return
        bot_user = self._client.user
        if bot_user is not None and member.id == bot_user.id:
            # 接続の終了・戻しは見回り（tick）と voice_guard が扱う。ただし別のチャンネルへ
            # 移された瞬間から、移動先の人の声を扱わないよう、ゲートだけはすぐ更新する。
            conversation.handle_self_update()
            return
        conversation.handle_member_update(member, before, after)
