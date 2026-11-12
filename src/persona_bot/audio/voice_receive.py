"""ボイスチャンネルの音声受信（#268 の VoiceReceiveAdapter）。

discord.py 本体は音声の受信に対応していないため、Gemini Live の音声会話で人の声を
聞き取る部分をここで組む。**依存は増やさない**（どれも discord.py[voice] が既に入れている）:

1. UDP の受信 …………… `VoiceConnectionState.add_socket_listener`（discord.py が IP の検出に
   使っている購読口）。コールバックは discord.py のソケット読み取りスレッドで呼ばれる。
2. 転送層の復号 ………… PyNaCl の `Aead`（`aead_xchacha20_poly1305_rtpsize`。discord.py が
   送信に使うのと同じ方式で、Discord は 2024-11 から他の方式を受け付けない）。
3. DAVE（E2E暗号化）の復号 … discord.py が保持する `dave_session.decrypt`（davey）。
   Discord は 2026-03-02 から全ボイスチャンネルで DAVE を必須化している。
4. Opus のデコード ……… discord.py 同梱の `discord.opus.Decoder`（48kHz・ステレオ）。
5. 話者の識別 …………… 音声 WebSocket の SPEAKING（op 5）が知らせる「SSRC → user_id」の対応。
6. エコー除去（#409）…… 参照信号（`EchoReference`）を渡されたら、話者ごとの `EchoCanceller` で
   ペルソナの声の回り込みを取り除き、発話の強さ（回り込みと判定したら 0）を一緒に渡す。

★ discord.py の内部属性（`_connection`・`secret_key`・`dave_session`）と音声 WebSocket の
フックに依存する。discord.py の版を上げるときは `tests/audio/test_voice_receive.py` を
必ず回すこと（requirements.txt で版を固定しているのはこのため）。
受信の依存はこのモジュールに閉じ込めてあり、別の受信基盤へ置き換えるときもここだけを差し替える。

受信は再生（`VoiceClient.play` / `stop`）と独立している。`voice_guard` が再生を止めても
受信は止まらず、受信を止めても再生は止まらない。
"""

from __future__ import annotations

import logging
import asyncio
from dataclasses import dataclass
import struct
import threading
from typing import Any, Callable

import discord
from discord.voice_state import VoiceConnectionState

from persona_bot.audio.echo_canceller import EchoCanceller, EchoReference, EchoStats
from persona_bot.audio.live_audio import Downsampler

try:
    import davey
except ImportError:  # pragma: no cover - discord.py[voice] が入れるため通常は通らない
    davey = None

try:
    import nacl.secret
except ImportError:  # pragma: no cover - 同上
    nacl = None


logger = logging.getLogger(__name__)

_RTP_HEADER_SIZE = 12
_RTP_VERSION = 2
_OPUS_PAYLOAD_TYPE = 0x78
_RTPSIZE_NONCE_SIZE = 4
_SUPPORTED_MODE = "aead_xchacha20_poly1305_rtpsize"
# Discord のクライアントが話し終わりに送る無音のフレーム。
OPUS_SILENCE_FRAME = b"\xf8\xff\xfe"
# 音声 WebSocket の op コード（discord.py の DiscordVoiceWebSocket と同じ値）。
_OP_SPEAKING = 5
_OP_CLIENT_DISCONNECT = 13
_OP_READY = 2
_OP_SESSION_DESCRIPTION = 4
_OP_RESUMED = 9
# 16bit の通番の比較で「古い」とみなす幅（ラップアラウンドを考慮する）。
_SEQUENCE_HALF_RANGE = 0x8000

# 1人分の音声を受け取るコールバック。イベントループ上で呼ばれる。
# エコー除去なしなら (user_id, 16kHz モノラル PCM)、ありなら (user_id, 除去後の PCM, 発話の強さ)。
AudioSink = Callable[..., None]


@dataclass(frozen=True)
class RtpPacket:
    """復号前の RTP パケットの、受信に要る部分。"""

    sequence: int
    timestamp: int
    ssrc: int
    has_extension: bool
    has_padding: bool
    # 転送層の暗号の追加認証データ（rtpsize では RTP ヘッダーと拡張ヘッダーの4バイト）。
    authenticated: bytes
    ciphertext: bytes
    nonce: bytes


def parse_rtp_packet(data: bytes) -> RtpPacket | None:
    """Opus の RTP パケットなら分解して返す。RTCP・その他のパケットは None。"""
    if len(data) < _RTP_HEADER_SIZE + _RTPSIZE_NONCE_SIZE:
        return None
    first, second = data[0], data[1]
    if first >> 6 != _RTP_VERSION or second & 0x7F != _OPUS_PAYLOAD_TYPE:
        # RTCP（送信者レポートなど）は 2 バイト目が 200〜204 で、ここで除かれる。
        return None
    csrc_count = first & 0x0F
    has_extension = bool(first & 0x10)
    header_size = _RTP_HEADER_SIZE + 4 * csrc_count
    authenticated_size = header_size + (4 if has_extension else 0)
    if len(data) < authenticated_size + _RTPSIZE_NONCE_SIZE:
        return None
    sequence, timestamp, ssrc = struct.unpack_from(">HII", data, 2)
    return RtpPacket(
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        has_extension=has_extension,
        has_padding=bool(first & 0x20),
        authenticated=bytes(data[:authenticated_size]),
        ciphertext=bytes(data[authenticated_size:-_RTPSIZE_NONCE_SIZE]),
        nonce=bytes(data[-_RTPSIZE_NONCE_SIZE:]),
    )


def decrypt_transport(packet: RtpPacket, box: Any) -> bytes | None:
    """転送層の暗号を外し、RTP 拡張の本体とパディングを取り除いた Opus（または DAVE 暗号文）を返す。

    rtpsize では、拡張ヘッダー（4バイト）は追加認証データ、拡張の本体は暗号文の先頭に入る。
    """
    nonce = packet.nonce + bytes(nacl.secret.Aead.NONCE_SIZE - _RTPSIZE_NONCE_SIZE)
    plaintext = box.decrypt(packet.ciphertext, packet.authenticated, nonce)
    if packet.has_extension:
        (extension_words,) = struct.unpack_from(">H", packet.authenticated, len(packet.authenticated) - 2)
        if extension_words * 4 > len(plaintext):
            return None
        plaintext = plaintext[extension_words * 4:]
    if packet.has_padding:
        if not plaintext or not 0 < plaintext[-1] <= len(plaintext):
            return None
        plaintext = plaintext[:-plaintext[-1]]
    return plaintext


def is_newer_sequence(sequence: int, last: int | None) -> bool:
    """16bit の通番が前回より新しいか（遅れて届いた古いパケットを捨てるため）。"""
    if last is None:
        return True
    difference = (sequence - last) & 0xFFFF
    return 0 < difference < _SEQUENCE_HALF_RANGE


class _UserDecoder:
    """1人分の Opus デコーダと、16kHz モノラルへの変換の状態。"""

    def __init__(self, decoder: Any, stream: tuple[int, object], reference: EchoReference | None = None) -> None:
        self.decoder = decoder
        self.stream = stream
        self.downsampler = Downsampler()
        self.last_sequence: int | None = None
        self.canceller = EchoCanceller(reference) if reference is not None else None


class LiveVoiceClient(discord.VoiceClient):
    """音声 WebSocket の SPEAKING を見て、SSRC と話者の対応を覚える VoiceClient。

    `/join` はこのクラスで接続する（受信に必要な情報は接続の最初から流れてくるため、
    後から差し替えられない）。再生は通常の VoiceClient と同じ。
    """

    def __init__(self, client: discord.Client, channel: discord.abc.Connectable) -> None:
        # 話者と割当世代を1つの不変値として渡す。WS と UDP のスレッド間で混ぜない。
        self._ssrc_users: dict[int, tuple[int, object]] = {}
        super().__init__(client, channel)

    def create_connection_state(self) -> VoiceConnectionState:
        return VoiceConnectionState(self, hook=self._handle_voice_ws_message)

    async def _handle_voice_ws_message(self, _ws: Any, message: dict[str, Any]) -> None:
        """音声 WebSocket のフック。discord.py が処理した後に、全メッセージで呼ばれる。"""
        try:
            op = message.get("op")
            data = message.get("d") or {}
            if op == _OP_SPEAKING and "ssrc" in data and "user_id" in data:
                ssrc, user_id = int(data["ssrc"]), int(data["user_id"])
                current = self._ssrc_users.get(ssrc)
                if current is None or current[0] != user_id:
                    # 同じ話者の再通知では継続するが、再割当では通番とデコーダを引き継がない。
                    self._ssrc_users = {
                        key: value for key, value in self._ssrc_users.items()
                        if value[0] != user_id and key != ssrc
                    } | {ssrc: (user_id, object())}
            elif op == _OP_CLIENT_DISCONNECT and "user_id" in data:
                user_id = int(data["user_id"])
                self._ssrc_users = {
                    key: value for key, value in self._ssrc_users.items() if value[0] != user_id
                }
            elif op == _OP_READY:
                self._ssrc_users = {}
            elif op in (_OP_SESSION_DESCRIPTION, _OP_RESUMED):
                # 同じ SSRC・話者でも、再接続前の通番を新しいストリームへ持ち越さない。
                self._ssrc_users = {
                    key: (value[0], object()) for key, value in self._ssrc_users.items()
                }
        except (TypeError, ValueError):
            # 形式の違うメッセージで接続処理を落とさない（話者が分からない音声は捨てるだけ）。
            logger.debug("音声受信: 話者の対応を読めないメッセージを無視しました。", exc_info=True)

    def user_id_for_ssrc(self, ssrc: int) -> int | None:
        stream = self.stream_for_ssrc(ssrc)
        return stream[0] if stream is not None else None

    def stream_for_ssrc(self, ssrc: int) -> tuple[int, object] | None:
        """話者と割当世代のスナップショット。切断後の再割当も別の世代になる。"""
        return self._ssrc_users.get(ssrc)

    def active_ssrcs(self) -> frozenset[int]:
        """いま話者の対応がある SSRC。切断した話者のデコーダを回収する目安（#432）。"""
        return frozenset(self._ssrc_users)


class VoiceReceiver:
    """1つの接続の受信を管理する。`start()` で購読を始め、`stop()` で外す。

    パケットの処理（復号・デコード・16kHz への変換）はソケットの読み取りスレッドで行い、
    結果だけを `loop.call_soon_threadsafe` でイベントループへ渡す（イベントループを
    1パケットごとの CPU 処理で塞がないため）。1パケットの失敗は捨てて次へ進む。
    """

    def __init__(
        self,
        voice_client: LiveVoiceClient,
        sink: AudioSink,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        decoder_factory: Callable[[], Any] | None = None,
        echo_reference: EchoReference | None = None,
    ) -> None:
        self._voice_client = voice_client
        self._sink = sink
        self._loop = loop or asyncio.get_running_loop()
        self._decoder_factory = decoder_factory or discord.opus.Decoder
        self._echo_reference = echo_reference
        self._decoders: dict[int, _UserDecoder] = {}
        # デコーダの追加と stop() の全消去を直列化する。停止の直後に UDP スレッドが
        # デコーダを入れ直して残さないため。
        self._decoders_lock = threading.Lock()
        self._box: Any = None
        self._box_key: bytes | None = None
        self._running = False
        self._warned_mode = False
        self._dropped = 0

    @property
    def dropped_packets(self) -> int:
        """復号・デコードできずに捨てたパケット数（診断用）。"""
        return self._dropped

    def echo_stats(self) -> list[EchoStats]:
        """話者ごとのエコー除去の診断値（話者は含めない）。stop() の前に読むこと。"""
        return [state.canceller.stats() for state in list(self._decoders.values()) if state.canceller is not None]

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._voice_client._connection.add_socket_listener(self._on_packet)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._voice_client._connection.remove_socket_listener(self._on_packet)
        finally:
            with self._decoders_lock:
                self._decoders.clear()

    # --- ソケットの読み取りスレッド ------------------------------------------------

    def _on_packet(self, data: bytes) -> None:
        if not self._running:
            return
        # エコー除去の時間合わせに使う到着時刻（参照信号と同じ時計）。届いた直後に読む。
        arrival = self._echo_reference.now_index() if self._echo_reference is not None else 0
        try:
            result = self._decode_packet(data, arrival=arrival)
        except Exception:
            # 1パケットの失敗で読み取りスレッドのログを埋めない（件数だけ数える）。
            self._dropped += 1
            logger.debug("音声受信: パケットを処理できなかったため捨てました。", exc_info=True)
            return
        if result is None:
            return
        user_id, pcm, level = result
        try:
            self._loop.call_soon_threadsafe(self._deliver, user_id, pcm, level)
        except RuntimeError:
            # イベントループが閉じた後（終了処理中）に届いたパケット。
            return

    def _deliver(self, user_id: int, pcm: bytes, level: float | None = None) -> None:
        if not self._running:
            return
        if level is None:
            self._sink(user_id, pcm)
        else:
            self._sink(user_id, pcm, level)

    def _decode_packet(self, data: bytes, *, arrival: int = 0) -> tuple[int, bytes, float | None] | None:
        packet = parse_rtp_packet(data)
        if packet is None:
            return None
        connection = self._voice_client._connection
        if connection.mode != _SUPPORTED_MODE:
            if not self._warned_mode:
                self._warned_mode = True
                logger.warning("音声受信: 未対応の暗号方式で接続されたため、音声を受信できません。")
            return None
        stream = self._voice_client.stream_for_ssrc(packet.ssrc)
        if stream is None:
            # SPEAKING の通知より先に届いた音声。話者が分からないので捨てる。
            self._decoders.pop(packet.ssrc, None)
            return None
        user_id = stream[0]
        self._prune_departed_decoders()

        # 接続鍵の変更も通番判定より先に検知する。デコーダは UDP のスレッドでだけ入れ替える。
        box = self._transport_box(connection)
        if box is None:
            return None
        state = self._decoders.get(packet.ssrc)
        if state is not None and state.stream != stream:
            self._decoders.pop(packet.ssrc, None)
            state = None
        if state is not None and not is_newer_sequence(packet.sequence, state.last_sequence):
            return None

        opus = decrypt_transport(packet, box)
        if opus is None:
            self._dropped += 1
            return None
        opus = self._decrypt_dave(connection, user_id, opus)
        if opus is None:
            self._dropped += 1
            return None
        if state is None:
            with self._decoders_lock:
                if not self._running:
                    # stop() の後に届いたパケット。デコーダを残さない。
                    return None
                state = self._decoders[packet.ssrc] = _UserDecoder(
                    self._decoder_factory(), stream, self._echo_reference,
                )
        state.last_sequence = packet.sequence
        if opus == OPUS_SILENCE_FRAME:
            return None
        pcm = state.decoder.decode(opus, fec=False)
        converted = state.downsampler.process(pcm)
        if not converted:
            return None
        if state.canceller is None:
            return (user_id, converted, None)
        try:
            echo = state.canceller.process(converted, rtp_timestamp=packet.timestamp, arrival=arrival)
        except Exception:
            # エコー除去の失敗で受信を止めない（取り除かずに渡し、割り込みの判定は文字起こしに任せる）。
            logger.debug("音声受信: エコー除去に失敗したため、そのまま渡しました。", exc_info=True)
            return (user_id, converted, None)
        return (user_id, echo.pcm, echo.level)

    def _prune_departed_decoders(self) -> None:
        """話者の対応が消えた（切断した）SSRC のデコーダを捨てる。UDP スレッドでだけ呼ぶ。

        切断した話者のパケットはもう届かないため、通常の経路では回収されない。
        他の話者のパケットが届くたびに照合する（SSRC は数人分なので軽い）。
        """
        if not self._decoders:
            return
        active = self._voice_client.active_ssrcs()
        for ssrc in [ssrc for ssrc in list(self._decoders) if ssrc not in active]:
            self._decoders.pop(ssrc, None)

    def _transport_box(self, connection: Any) -> Any:
        raw_key = getattr(connection, "secret_key", None)
        if nacl is None or not isinstance(raw_key, (bytes, bytearray, list, tuple)) or len(raw_key) != 32:
            return None
        key = bytes(raw_key)
        if self._box is None or self._box_key != key:
            # 再接続で鍵が変わったら作り直す。
            self._box = nacl.secret.Aead(key)
            self._box_key = key
            self._decoders.clear()
        return self._box

    @staticmethod
    def _decrypt_dave(connection: Any, user_id: int, payload: bytes) -> bytes | None:
        """DAVE の暗号を外す。DAVE を使っていない接続ならそのまま返す。

        MLS のエポックの切り替わりの前後では一時的に復号できないパケットが届く。
        そのときは捨てる（パススルー中の相手なら暗号化されていないのでそのまま使う）。
        """
        session = getattr(connection, "dave_session", None)
        if session is None or getattr(connection, "dave_protocol_version", 0) == 0 or davey is None:
            return payload
        try:
            if session.ready:
                decrypted = session.decrypt(user_id, davey.MediaType.audio, payload)
                if decrypted:
                    return bytes(decrypted)
        except Exception:
            logger.debug("音声受信: DAVE の復号に失敗しました。", exc_info=True)
        try:
            if session.can_passthrough(user_id):
                return payload
        except Exception:
            logger.debug("音声受信: DAVE のパススルー状態を確認できませんでした。", exc_info=True)
        return None
