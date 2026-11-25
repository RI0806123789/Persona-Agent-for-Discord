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
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable

import discord

from persona_bot.audio.echo_canceller import EchoReference
from persona_bot.audio.live_audio import DISCORD_FRAME_BYTES, LivePlaybackSource, Upsampler, frame_rms
from persona_bot.audio.playback import get_playback_controller
from persona_bot.audio.voice_receive import LiveVoiceClient, VoiceReceiver
from persona_bot.config.constants import (
    LIVE_AEC_ENABLED,
    LIVE_MAX_CONCURRENT_SESSIONS,
    LIVE_MAX_UTTERANCE_SECONDS,
    LIVE_OUTPUT_BUFFER_MS,
    LIVE_PLAYBACK_STALL_SECONDS,
    LIVE_TICK_SECONDS,
    LIVE_TRANSCRIPT_MAX_CHARS,
    LIVE_VAD_ENERGY_THRESHOLD,
    LIVE_VAD_SILENCE_MS,
)
from persona_bot.core.background import spawn, wait_for_cleanup, wait_for_task
from persona_bot.services.live_session import CLOSED_QUOTA, LiveOptions, LiveSession
from persona_bot.services.voice_barge_in import (
    INTERRUPTION_SPEECH, RecentSpeech, classify_interruption, is_echo_text,
)
# 状態オブジェクトと、責務ごとに切り出したミックスイン（#415）。`_Utterance` などは
# 従来どおりこのモジュールから import できる。
from persona_bot.services.voice_conversation_consent import VoiceConsentMixin
from persona_bot.services.voice_conversation_diagnostics import VoiceDiagnosticsMixin
from persona_bot.services.voice_conversation_moderation import VoiceModerationMixin
from persona_bot.services.voice_conversation_playback import VoicePlaybackMixin
from persona_bot.services.voice_conversation_state import (
    ConsentState, _Participant, _PlaybackState, _SpeakerState, _TranscriptState, _Utterance,
)
from persona_bot.utils.usage_graph import log_conversation_safely, log_tokens_safely

if TYPE_CHECKING:
    from persona_bot.services.voice_conversation_service import VoiceConversationService

logger = logging.getLogger(__name__)

# 再生を始めるまでに溜める応答音声（48kHz・ステレオ）のバイト数。
_OUTPUT_BUFFER_BYTES = DISCORD_FRAME_BYTES * max(1, LIVE_OUTPUT_BUFFER_MS // 20)
_PLAYBACK_STALL_FRAMES = int(LIVE_PLAYBACK_STALL_SECONDS * 50)


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


class VoiceConversation(VoiceDiagnosticsMixin, VoicePlaybackMixin, VoiceConsentMixin, VoiceModerationMixin):
    """1つのボイスチャンネルの音声会話（Persona Voice Controller）。

    責務ごとに次のモジュールへ分けている（#415・#495。動作は変えていない）:
      ・`voice_conversation_state.py`: 同意・参加者・発言・話者の管理・文字起こしの帰属の状態
      ・`voice_conversation_consent.py`: 同意の付与・撤回と同意パネルの更新（`VoiceConsentMixin`）
      ・`voice_conversation_moderation.py`: 入出力のモデレーション・違反記録・記憶と親密度への反映（`VoiceModerationMixin`）
      ・`voice_conversation_playback.py`: 返答の再生・フェードアウト・破棄の調停（`VoicePlaybackMixin`。#470。
        状態は `_PlaybackState` にまとめ、`self._playback_state` を通して読み書きする）
      ・`voice_conversation_diagnostics.py`: Live への送信キューと終了時の診断統計ログ（`VoiceDiagnosticsMixin`。#495）
    サーバーごとの会話の管理（`VoiceConversationService`）は `voice_conversation_service.py`（#470）。
    ここには、受信した音声の振り分け・応答の受け取りと、それらをつなぐ制御を残す。
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
        # 返答の再生・フェードアウトの状態は1か所に集める（読み書きは voice_conversation_playback.py）。
        self._playback_state = _PlaybackState()
        # エコー除去の参照信号（再生した音声。メモリ上の数秒分だけ）と、回り込みの照合に使う直近の発話。
        self._echo_reference = EchoReference() if LIVE_AEC_ENABLED else None
        self._recent_output = RecentSpeech()
        # 診断用の件数（終了時にログへ出す。本文や話者は含めない）。
        self._barge_in_stats: Counter[str] = Counter()
        self._playback_controller = get_playback_controller(voice_client)
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
        # 入出力の AI 判定のタスク（#465）。終わったものは自分で外れ、stop() が残りを止めて回収する。
        self._judge_tasks: set[asyncio.Task[Any]] = set()
        # 後片付けの共有タスク。最初の stop() が起動し、以後の stop() も同じ完了を待つ（#465）。
        self._stop_task: asyncio.Task[Any] | None = None
        self._latencies: list[float] = []
        self.panel_message: Any = None
        self.panel_view: Any = None
        self._panel_update_task: asyncio.Task[Any] | None = None
        self._panel_updated_at = float("-inf")
        # 更新の実行中（編集の通信を待つ間）に、パネルの表示が変わる変化があったか（#465）。
        self._panel_dirty = False

    # ==================================================================
    # 開始・終了
    # ==================================================================

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def end_requested(self) -> bool:
        """ボイス接続の喪失（見回りが検知）で、会話の終了を求めたか。

        開始の途中（同意パネルの投稿を待つ間）はまだサービスへ登録されておらず、見回りが予約した
        `end()` は何もせずに戻る。開始処理がこの値を見て、開始を失敗にする（#476）。
        """
        return self._end_requested

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
        """受信・再生・Live をまとめて止める（何度呼んでもよい）。切断はしない。

        後片付けは最初の呼び出しが起動する1つの共有タスクで行い、どの呼び出しもその完了を待つ（#465）。
        呼び出し側が途中でキャンセルされても、後片付け（Live の切断・同意パネルの終了表示）は最後まで進み、
        キャンセルは完了後に伝わる。後から呼んだ側も同じ完了を待つ。
        ★ 以前は先に `_stopped` を立ててから本体を await していたため、途中で止まると二度目の呼び出しが
        すぐ戻り、Live の接続と同意パネルが片付かないまま残った。
        """
        task = self._stop_task
        if task is None:
            # 入出力の受け付けは、後片付けの完了を待たずにこの時点で止める。
            self._stopped = True
            task = self._stop_task = spawn(
                self._finish_stop(asyncio.current_task()), label="音声会話の停止", _protected=True,
            )
        if task is asyncio.current_task():
            # 後片付けの中から呼ばれた（自分の完了を待つと止まる）。
            return
        await wait_for_task(task)

    async def _finish_stop(self, caller: asyncio.Task[Any] | None) -> None:
        """stop() の後片付けの本体（共有タスクで1回だけ実行する）。"""
        # stop() を呼んだタスク自身は止めない・待たない（自分を待って止まらないため）。
        excluded = {caller, asyncio.current_task()}
        if self._current is not None:
            self._discard_response(self._current, "音声会話の停止")
        # 見回り・送信に加えて、入出力の AI 判定（過去の発言の分も）を止めて回収する。
        owned = [task for task in [*self._tasks, *self._judge_tasks] if task not in excluded]
        for task in owned:
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
        await asyncio.gather(*owned, return_exceptions=True)
        panel_task = self._panel_update_task
        if panel_task is not None and panel_task not in excluded:
            # ★ キャンセルを要求するだけでは、実行中の「会話中です」の編集の通信が残り、
            # 直後の終了表示の編集と順序が入れ替わりうる。終わるまで待ってから終了表示にする。
            panel_task.cancel()
            await asyncio.gather(panel_task, return_exceptions=True)
        self._log_latency()
        self._log_barge_in_stats()
        await self._finish_panel()

    def _spawn_judge(self, coro: Any, *, label: str) -> asyncio.Task[Any] | None:
        """入出力の AI 判定を起動し、stop() で止めて回収できるよう追跡する（#465）。

        追跡しないと、次の発言へ移った後の過去の発言の判定や出力判定が、会話の終了後も Gemini の応答を
        待って残る。停止後は起動しない（None を返す）。
        """
        if self._stopped:
            coro.close()
            return None
        task = spawn(coro, label=label)
        self._judge_tasks.add(task)
        task.add_done_callback(self._judge_tasks.discard)
        return task

    # ==================================================================
    # 参加者の出入り（同意の付与・撤回とパネルの更新は VoiceConsentMixin）
    # ==================================================================

    def handle_self_update(self) -> None:
        """ボット自身の移動・切断。ゲートの開閉を見回り（tick）を待たずに更新する。"""
        self._update_gate()

    def _clear_participant_state(self, user_id: int, reason: str) -> None:
        """退出・同意撤回・ブロック登録で対象外になった参加者の発話権・進行中ターン・予約を回収する。"""
        if self._speaker_state.active_user == user_id:
            self._end_activity(discard=True)
        if self._current is not None and self._current.user_id == user_id:
            self._discard_response(self._current, reason)
        self._speaker_state.participants.pop(user_id, None)
        self._remove_reservation(user_id)

    def handle_member_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入り。抜けた人の発話権・発話希望・プリロールを無効にする。"""
        before_id = getattr(getattr(before, "channel", None), "id", None)
        after_id = getattr(getattr(after, "channel", None), "id", None)
        if before_id == after_id:
            return
        if before_id == self.channel_id:
            self._clear_participant_state(member.id, "チャンネルから退出")
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
        waiting_count = len(waiting)
        waiting_changed = waiting_count != self._waiting_count
        self._waiting_count = waiting_count
        if is_open == self._gate_open:
            if waiting_changed:
                self._schedule_panel_update()
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
        # 割り込みを確かめている最中で一時停止した場合も、待ちを解消しておく。
        # 残したままだと、再開後の新しい発話の返答が前の割り込み待ちで破棄されてしまう。
        self._awaiting_interrupt_ack = False
        # ★ ただし、文字起こしを遮断している割り込まれた発言（blocked）への参照は残す（#465）。
        # 消すと、後から届く打ち切りの通知・返答の終わりで遮断を解けず、再開後の発話の文字起こしが
        # 見回りの期限まで捨てられる。遮断そのものは解かない（一時停止の前の遅れた断片を、
        # 再開後に話し始めた人へ付けない）。
        if self._transcript_state.interrupted is not self._transcript_state.blocked:
            self._transcript_state.interrupted = None

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

    def _bot_speaking(self) -> bool:
        """ボットが返答中か（生成中・再生中）。話し始めのしきい値を割り込み用に上げる。"""
        if self._playback_state.source is not None and not self._playback_state.source.aborted:
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
            # 未確定の短い発話を挟んだ後でも、まだ鳴っている返答を割り込みの対象にする
            # （挟んだ発話の返答はまだ届いていない。割り込みを確定したときにフェードアウト
            # されるのは、いま鳴っている返答でなければならない）。
            playing = self._playback_state.owner
            if (playing is not None and playing is not previous and not playing.discarded
                    and self._playback_state.started):
                previous = playing
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
            if discard or utterance.discarded:
                utterance.discarded = True
                # 破棄した発言の文字起こしは使わない。閉じておかないと、次に話し始めた人の断片が
                # 破棄済みの発言の帰属待ちに阻まれて捨てられ、その発言が記憶から外れる。
                self._close_transcript(utterance)
            else:
                utterance.input_task = self._spawn_judge(self._judge_input(utterance), label="音声会話の入力判定")
        # 待っていた人に、少しの間だけ優先して話す機会を渡す。
        self._speaker_state.extend_reservations(now)

    def _remove_reservation(self, user_id: int) -> None:
        self._speaker_state.remove_reservation(user_id)

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
            # 記録の失敗が Live の受信処理へ伝わらないよう、内部の呼び出しと同じ安全な入口を通す（#496）。
            log_tokens_safely(self._service.usage, "音声会話", usage)
        content = getattr(message, "server_content", None)
        if content is None:
            return
        transcription = getattr(content, "input_transcription", None)
        if transcription is not None:
            self._on_input_transcript(transcription.text or "", bool(getattr(transcription, "finished", None)))
        if getattr(content, "interrupted", None):
            self._awaiting_interrupt_ack = False
            previous = self._transcript_state.interrupted
            playing = self._playback_state.owner
            if previous is not None and not previous.discarded and playing is previous:
                # 割り込みを確かめている間。届いている分は流し切る（確定したらフェードアウトさせる）。
                self._finish_held_previous(previous)
            elif playing is not None and playing is self._current:
                # 確認の期限切れで待つのをやめた後に、打ち切りの通知が遅れて届いた（#476）。いま鳴っているのは
                # 最後に話し始めた発言（_current）への返答。発言が始まるたびに _current は差し替わるので、
                # その後に新しい発言は無く、この通知が打ち切ったのはこの返答ではない。後続の返答を止めない。
                logger.debug("音声会話: 遅れて届いた打ち切りの通知を、後続の返答には適用しませんでした。")
            else:
                # 文字起こしで割り込みを確定し、前の返答をすでにフェードアウトさせていることがある。
                # 遅れて届いたこの通知で絞り切る前に切らない（#465）。
                self._abort_playback(keep_fading=True)
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
            # 受信時点の状態を控える。短い返答では _on_generation_complete が再生の開始を待つため、
            # その間に停止や新しい発話（割り込み）が起きうる。
            awaiting_at_receipt = self._awaiting_interrupt_ack
            current_at_receipt = self._current
            await self._on_generation_complete()
            if self._stopped:
                return
            if (getattr(content, "turn_complete", None) and not awaiting_at_receipt
                    and (self._current is not current_at_receipt or self._awaiting_interrupt_ack)):
                # 待つ間に新しい発言へ交代した。古い turn_complete で、新しい割り込みの確認待ちを解かない。
                return
        if getattr(content, "turn_complete", None):
            self._on_turn_complete()

    def _held_previous(self) -> _Utterance | None:
        """割り込みを確かめている間、再生を続けている前の返答（打ち切りの通知までに届いた残りもここへ流す）。"""
        previous = self._transcript_state.interrupted
        if (previous is None or not self._awaiting_interrupt_ack or previous.discarded
                or self._playback_state.owner is not previous or self._now() >= self._interrupt_ack_deadline):
            return None
        return previous

    def _finish_held_previous(self, previous: _Utterance) -> None:
        """割り込まれた前の返答の生成が終わった。届いた分を流し切り、出力の AI 判定に掛ける。"""
        if self._playback_state.source is not None and self._playback_state.owner is previous:
            self._playback_state.source.finish()
        if previous.response_started and not previous.response_complete:
            previous.response_complete = True
            # 打ち切られた返答は記憶に使わない（cut_off）が、流している分の安全性は確かめる。
            self._spawn_judge(self._judge_output(previous), label="音声会話の出力判定（割り込まれた返答）")

    def _resolve_interrupt_ack(self) -> None:
        """割り込みの確認待ちを終える（返答の終わり・確認の期限切れ）。

        割り込まれた前の返答は流し切り、その発言の入力の文字起こしの帰属も閉じる。閉じないと、次の人の
        文字起こしが見回りの期限まで捨てられ、残った `interrupted` の返答の文字が回り込みの照合
        （`_recent_echo_text`）に居座る（#476）。
        """
        self._awaiting_interrupt_ack = False
        previous = self._transcript_state.interrupted
        if previous is not None and not previous.discarded and self._playback_state.owner is previous:
            self._finish_held_previous(previous)
        self._close_transcript(previous)
        self._transcript_state.interrupted = None

    def _accepting_response(self) -> _Utterance | None:
        """いま届いた応答を受け取る発言。受け取らない（捨てる）なら None。"""
        if self._awaiting_interrupt_ack and self._now() >= self._interrupt_ack_deadline:
            # 打ち切りの通知が来ないまま期限が過ぎた。待つのをやめる。
            self._resolve_interrupt_ack()
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
                # 初回音声の後に文字起こしが増えた場合も、回り込みへの返答を止められるように
                # 判定し直す（初回音声の時点では断片が足りず、エコーと分からないことがある）。
                if (utterance.first_audio_at is not None and not utterance.discarded
                        and self._is_echo_reply(utterance)):
                    self._barge_in_stats["echo"] += 1
                    self._discard_response(utterance, "自分の声の回り込みへの返答")
                elif (utterance.input_task is not None and utterance.input_task.done()
                        and not utterance.violation_recorded and not utterance.discarded
                        and not self._stopped):
                    # 入力の AI 判定が済んだ後に続きが届いた。増えた分も判定し直す。
                    utterance.input_task = self._spawn_judge(self._judge_input(utterance), label="音声会話の入力の再判定")
        if finished:
            self._close_transcript(utterance)

    def _recent_echo_text(self, now: float) -> str:
        """回り込みの照合に使う、ボット自身の直近の発話の全文。

        `_recent_output` は受信時刻基準の窓のため、長い返答の前半は窓からあふれる。
        いま鳴っている返答（再生・流し切り中）は、その発言の文字起こしの全文を
        窓の期限に関わらず参照に加える（再生が終わった分は窓で照合する）。
        """
        parts = []
        owner = self._playback_state.owner
        if owner is not None and owner.output_text:
            parts.append(owner.output_text)
        interrupted = self._transcript_state.interrupted
        if interrupted is not None and interrupted.output_text:
            parts.append(interrupted.output_text)
        recent = self._recent_output.text(now)
        if recent:
            parts.append(recent)
        return "\n".join(parts)

    def _check_barge_in(self, utterance: _Utterance) -> None:
        """返答中に話し始めた人の文字起こしが、割り込みとして確定できる中身になったか確かめる。

        フィラー・相槌だけの間や、ペルソナ自身の発話の回り込みでは確定しない（返答を流し続ける）。
        """
        if utterance.barge_in != "pending" or utterance.discarded:
            return
        verdict = classify_interruption(utterance.input_text, self._recent_echo_text(self._now()))
        if verdict != INTERRUPTION_SPEECH:
            return
        utterance.barge_in = "confirmed"
        self._barge_in_stats["confirmed"] += 1
        previous = utterance.interrupts
        if previous is not None and not previous.discarded:
            logger.info("音声会話: 割り込みを確定したため、返答をフェードアウトします。")
            self._discard_response(previous, "割り込み", fade=True)

    def _is_echo_reply(self, utterance: _Utterance) -> bool:
        """この発言への返答を再生しないか（発言がペルソナ自身の声の回り込みだった）。

        割り込みを確定させた後も照合する（短い断片での確定は、回り込みの可能性を消さないため）。
        """
        if not utterance.echo_suspect:
            return False
        return is_echo_text(utterance.input_text, self._recent_echo_text(self._now()))

    async def _on_model_audio(self, data: bytes) -> None:
        previous = self._held_previous()
        if previous is not None:
            # 割り込みを確かめている間に届いた、前の返答の残り（打ち切りの通知の前に生成された分）。
            if self._playback_state.source is not None and not self._playback_state.source.aborted:
                self._playback_state.source.feed(self._playback_state.upsampler.process(data))
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
        if (self._playback_state.source is None or self._playback_state.owner is not utterance
                or self._playback_state.source.aborted):
            # 再生側が途絶えで打ち切った音源（aborted）にはもう流せない。後続のチャンクは
            # 新しい音源で再生し直す（同じ発話の再開）。まだ流れている前の返答（割り込みを確かめる間に
            # 流していた分）は、強めに絞ってから替える。
            if self._playback_state.source is not None and self._playback_state.owner is not utterance and not self._playback_state.source.aborted:
                self._fade_out_playback()
            else:
                # 割り込みの確定でフェードアウト中の前の返答は、絞り切らせる（#465）。
                # 新しい音源の再生開始（_start_playback）が、フェードの終わりを上限付きで待つ。
                self._abort_playback(keep_fading=True)
            self._playback_state.source = LivePlaybackSource(stall_frames=_PLAYBACK_STALL_FRAMES, reference=self._echo_reference)
            self._playback_state.owner = utterance
            self._playback_state.started = False
            self._playback_state.upsampler = Upsampler()
        self._playback_state.source.feed(self._playback_state.upsampler.process(data))
        if not self._playback_state.started and self._playback_state.source.buffered_bytes >= _OUTPUT_BUFFER_BYTES:
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
        if self._playback_state.source is None or self._playback_state.owner is not self._current:
            return
        self._playback_state.source.finish()
        if not self._playback_state.started:
            # バッファに満たない短い返答も、ここで流し始める。
            await self._start_playback()

    def _on_turn_complete(self) -> None:
        if self._awaiting_interrupt_ack:
            # 割り込まれた古い返答の終わり。新しい発言の返答はこの後に届く。
            self._resolve_interrupt_ack()
            return
        stale = self._transcript_state.interrupted
        if stale is not None and stale is self._transcript_state.blocked:
            # 一時停止で割り込みの確認待ちを解いた後に届いた返答の終わり（#465）。返答が終わった時点で、
            # それより前に話し始めた割り込まれた発言の入力は処理し終えているので、遮断を解く。
            self._close_transcript(stale)
            self._transcript_state.interrupted = None
        utterance = self._current
        if utterance is None or utterance.ended_at is None or not utterance.response_started:
            return
        # 返答が終わった時点で、その発言の入力はサーバーが処理し終えている。
        self._close_transcript(utterance)
        if utterance.response_complete:
            return
        utterance.response_complete = True
        if not utterance.discarded:
            # 参加者の発話に対して返答が完了した1ターンを、管理コンソールの会話件数へ数える（#496）。
            log_conversation_safely(self._service.usage, "音声会話")
        self._spawn_judge(self._judge_output(utterance), label="音声会話の出力判定")

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


def __getattr__(name: str) -> Any:
    """`VoiceConversationService` は #470 で `voice_conversation_service` へ移した。

    あちらがこのモジュールの `VoiceConversation` を import するため、ここから先に import すると循環する。
    従来の `from persona_bot.services.voice_conversation import VoiceConversationService` を保つため、
    参照された時点で遅延して取り出す。
    """
    if name == "VoiceConversationService":
        from persona_bot.services import voice_conversation_service

        return voice_conversation_service.VoiceConversationService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
