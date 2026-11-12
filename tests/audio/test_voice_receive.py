"""音声受信（#268 の VoiceReceiveAdapter）を、discord.py と同じ暗号化で作ったパケットで確かめる。

★ discord.py を更新したら必ず回すこと。受信は discord.py の内部属性（`_connection`・
`secret_key`・`dave_session`）と音声 WebSocket のフックに依存している。
"""

import asyncio
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import nacl.secret
import nacl.utils
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from discord.voice_state import VoiceConnectionState
from persona_bot.audio import voice_receive
from persona_bot.audio.echo_canceller import EchoReference
from persona_bot.audio.voice_receive import (
    OPUS_SILENCE_FRAME, LiveVoiceClient, VoiceReceiver, decrypt_transport, is_newer_sequence,
    parse_rtp_packet,
)

KEY = nacl.utils.random(nacl.secret.Aead.KEY_SIZE)
SSRC = 4321
USER_ID = 777


def encrypt_packet(payload: bytes, *, sequence: int = 1, extension: bytes | None = None,
                   csrcs: int = 0, nonce: int = 5, padding: bool = False,
                   extension_words: int | None = None, key: bytes = KEY) -> bytes:
    """discord.py の _encrypt_aead_xchacha20_poly1305_rtpsize と同じ形でパケットを作る。

    受信では拡張ヘッダー（4バイト）が追加認証データ、拡張の本体が暗号文の先頭に入る。
    """
    first = 0x80 | csrcs | (0x10 if extension is not None else 0) | (0x20 if padding else 0)
    header = bytearray(12)
    header[0] = first
    header[1] = 0x78
    struct.pack_into(">HII", header, 2, sequence, 960 * sequence, SSRC)
    header += bytes(4 * csrcs)
    body = payload
    if extension is not None:
        words = len(extension) // 4 if extension_words is None else extension_words
        header += struct.pack(">HH", 0xBEDE, words)
        body = extension + payload
    nonce_bytes = bytearray(24)
    nonce_bytes[:4] = struct.pack(">I", nonce)
    box = nacl.secret.Aead(key)
    return bytes(header) + box.encrypt(body, bytes(header), bytes(nonce_bytes)).ciphertext + bytes(nonce_bytes[:4])


class FakeDecoder:
    def __init__(self) -> None:
        self.decoded: list[bytes] = []

    def decode(self, data: bytes, *, fec: bool) -> bytes:
        self.decoded.append(data)
        # 20ms・48kHz・ステレオの一定値（実際の Opus のデコードの代わり）
        return np.full(960 * 2, 1200, dtype=np.int16).tobytes()


class RtpTests(unittest.TestCase):
    def test_packet_without_extension_round_trips(self):
        packet = parse_rtp_packet(encrypt_packet(b"opus-frame"))
        self.assertEqual((packet.ssrc, packet.sequence), (SSRC, 1))
        self.assertEqual(decrypt_transport(packet, nacl.secret.Aead(KEY)), b"opus-frame")

    def test_extension_body_and_csrcs_are_removed(self):
        raw = encrypt_packet(b"opus-frame", extension=b"\x10\xff\x00\x00" * 2, csrcs=2)
        packet = parse_rtp_packet(raw)
        self.assertTrue(packet.has_extension)
        self.assertEqual(decrypt_transport(packet, nacl.secret.Aead(KEY)), b"opus-frame")

    def test_tampered_header_fails_authentication(self):
        raw = bytearray(encrypt_packet(b"opus-frame"))
        raw[8] ^= 0x01  # SSRC を書き換える
        packet = parse_rtp_packet(bytes(raw))
        with self.assertRaises(Exception):
            decrypt_transport(packet, nacl.secret.Aead(KEY))

    def test_invalid_padding_lengths_are_rejected(self):
        for payload in (b"", b"opus\x00", b"opus\x06"):
            with self.subTest(payload=payload):
                packet = parse_rtp_packet(encrypt_packet(payload, padding=True))
                self.assertIsNone(decrypt_transport(packet, nacl.secret.Aead(KEY)))

    def test_valid_padding_is_removed_after_extension(self):
        for extension in (None, b"\x00" * 4):
            with self.subTest(extension=extension):
                raw = encrypt_packet(b"opus\x00\x02", extension=extension, padding=True)
                self.assertEqual(decrypt_transport(parse_rtp_packet(raw), nacl.secret.Aead(KEY)), b"opus")

    def test_extension_length_cannot_exceed_plaintext(self):
        raw = encrypt_packet(b"opus", extension=b"", extension_words=2)
        self.assertIsNone(decrypt_transport(parse_rtp_packet(raw), nacl.secret.Aead(KEY)))

    def test_padding_cannot_consume_extension_bytes(self):
        raw = encrypt_packet(b"opus\x06", extension=b"\x00" * 4, padding=True)
        self.assertIsNone(decrypt_transport(parse_rtp_packet(raw), nacl.secret.Aead(KEY)))

    def test_rtcp_and_short_packets_are_ignored(self):
        rtcp = bytes([0x80, 200]) + bytes(30)
        self.assertIsNone(parse_rtp_packet(rtcp))
        self.assertIsNone(parse_rtp_packet(b"\x80\x78\x00"))
        self.assertIsNone(parse_rtp_packet(bytes([0x40, 0x78]) + bytes(30)))

    def test_sequence_comparison_handles_wraparound(self):
        self.assertTrue(is_newer_sequence(5, None))
        self.assertTrue(is_newer_sequence(6, 5))
        self.assertFalse(is_newer_sequence(5, 5))
        self.assertFalse(is_newer_sequence(4, 5))
        self.assertTrue(is_newer_sequence(1, 0xFFFF))
        self.assertFalse(is_newer_sequence(0xFFFF, 1))


class LiveVoiceClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self) -> LiveVoiceClient:
        loop = asyncio.get_running_loop()
        client = SimpleNamespace(_connection=SimpleNamespace(loop=loop))
        voice_client = LiveVoiceClient(client, SimpleNamespace(id=1, guild=SimpleNamespace(id=2)))
        self.addCleanup(voice_client._connection._socket_reader.stop)
        return voice_client

    async def test_connection_state_uses_the_speaking_hook(self):
        voice_client = self.make_client()
        self.assertIsInstance(voice_client._connection, VoiceConnectionState)
        self.assertEqual(voice_client._connection.hook, voice_client._handle_voice_ws_message)

    async def test_speaking_maps_ssrc_and_disconnect_forgets_it(self):
        voice_client = self.make_client()
        await voice_client._handle_voice_ws_message(None, {"op": 5, "d": {"user_id": "77", "ssrc": 9, "speaking": 1}})
        self.assertEqual(voice_client.user_id_for_ssrc(9), 77)
        await voice_client._handle_voice_ws_message(None, {"op": 13, "d": {"user_id": "77"}})
        self.assertIsNone(voice_client.user_id_for_ssrc(9))

    async def test_malformed_messages_are_ignored(self):
        voice_client = self.make_client()
        for message in ({"op": 5, "d": {"user_id": "名前", "ssrc": 1}}, {"op": 5, "d": None}, {"op": 2}):
            await voice_client._handle_voice_ws_message(None, message)
        self.assertIsNone(voice_client.user_id_for_ssrc(1))


class ReceiverTests(unittest.IsolatedAsyncioTestCase):
    async def test_unready_key_drops_packet_without_exception(self):
        from discord.utils import MISSING
        receiver = self.make_receiver()
        self.connection.secret_key = MISSING
        self.assertIsNone(receiver._decode_packet(encrypt_packet(b"opus")))
        self.assertEqual(receiver._dropped, 0)

    def make_receiver(self, *, dave_session=None, dave_version=0, echo_reference=None):
        self.connection = SimpleNamespace(
            mode="aead_xchacha20_poly1305_rtpsize", secret_key=list(KEY),
            dave_session=dave_session, dave_protocol_version=dave_version,
            add_socket_listener=Mock(), remove_socket_listener=Mock(),
        )
        voice_client = LiveVoiceClientTests.make_client(self)
        voice_client._connection = self.connection
        voice_client._ssrc_users = {SSRC: (USER_ID, object())}
        self.decoders: list[FakeDecoder] = []

        def factory():
            decoder = FakeDecoder()
            self.decoders.append(decoder)
            return decoder

        self.received: list[tuple] = []
        receiver = VoiceReceiver(
            voice_client, lambda *args: self.received.append(args), decoder_factory=factory,
            echo_reference=echo_reference,
        )
        return receiver

    async def test_echo_canceller_passes_cleaned_audio_and_speech_level(self):
        """参照信号を渡すと、エコー除去の結果（除去後の音声と発話の強さ）を一緒に渡す（#409）。"""
        reference = EchoReference(clock=lambda: 10.0)
        receiver = self.make_receiver(echo_reference=reference)
        receiver.start()
        receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        user_id, pcm, level = self.received[0]
        self.assertEqual(user_id, USER_ID)
        self.assertEqual(len(pcm), 320 * 2)
        # ペルソナの声（参照信号）が無い間は、受け取った音声のまま・音量のとおり
        self.assertGreater(level, 0)
        self.assertEqual(receiver.echo_stats()[0].delay_ms, None)
        receiver.stop()
        self.assertEqual(receiver.echo_stats(), [])

    async def test_echo_canceller_failure_falls_back_to_the_received_audio(self):
        receiver = self.make_receiver(echo_reference=EchoReference(clock=lambda: 10.0))
        receiver.start()
        with unittest.mock.patch.object(voice_receive.EchoCanceller, "process", side_effect=RuntimeError("架空の失敗")):
            receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(len(self.received[0]), 2)  # 取り除かずに渡す（発話の強さは付けない）
        self.assertEqual(receiver.dropped_packets, 0)

    async def test_decrypted_frame_is_delivered_as_16khz_mono(self):
        receiver = self.make_receiver()
        receiver.start()
        self.connection.add_socket_listener.assert_called_once_with(receiver._on_packet)
        receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        user_id, pcm = self.received[0]
        self.assertEqual(user_id, USER_ID)
        self.assertEqual(len(pcm), 320 * 2)  # 20ms・16kHz・モノラル
        self.assertEqual(self.decoders[0].decoded, [b"opus"])
        receiver.stop()
        self.connection.remove_socket_listener.assert_called_once_with(receiver._on_packet)

    async def test_unknown_speaker_old_sequence_and_silence_are_dropped(self):
        receiver = self.make_receiver()
        receiver.start()
        unknown = bytearray(encrypt_packet(b"opus"))
        receiver._voice_client._ssrc_users = {}
        receiver._on_packet(bytes(unknown))
        receiver._voice_client._ssrc_users = {SSRC: (USER_ID, object())}
        receiver._on_packet(encrypt_packet(b"opus", sequence=10))
        receiver._on_packet(encrypt_packet(b"old", sequence=9))
        receiver._on_packet(encrypt_packet(OPUS_SILENCE_FRAME, sequence=11))
        await asyncio.sleep(0)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.decoders[0].decoded, [b"opus"])

    async def test_broken_packet_is_counted_without_raising(self):
        receiver = self.make_receiver()
        receiver.start()
        raw = bytearray(encrypt_packet(b"opus"))
        raw[-6] ^= 0xFF  # 暗号文を壊す
        receiver._on_packet(bytes(raw))
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])
        self.assertEqual(receiver.dropped_packets, 1)

    async def test_malformed_rtp_payload_never_reaches_dave_or_opus(self):
        session = SimpleNamespace(
            ready=True, decrypt=Mock(return_value=b"opus"), can_passthrough=Mock(return_value=False),
        )
        receiver = self.make_receiver(dave_session=session, dave_version=1)
        receiver.start()
        receiver._on_packet(encrypt_packet(b"opus\x00", padding=True))
        receiver._on_packet(encrypt_packet(b"opus", extension=b"", extension_words=2))
        await asyncio.sleep(0)
        self.assertEqual(receiver.dropped_packets, 2)
        session.decrypt.assert_not_called()
        self.assertEqual(self.decoders, [])

    async def test_disconnect_and_same_speaker_rejoin_reset_sequence_and_decoder(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"before", sequence=40000))
        await receiver._voice_client._handle_voice_ws_message(None, {"op": 13, "d": {"user_id": USER_ID}})
        await receiver._voice_client._handle_voice_ws_message(
            None, {"op": 5, "d": {"user_id": USER_ID, "ssrc": SSRC}},
        )
        receiver._on_packet(encrypt_packet(b"after", sequence=10000))
        await asyncio.sleep(0)
        self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"before"], [b"after"]])
        self.assertEqual(len(self.received), 2)

    async def test_ssrc_reassignment_to_another_speaker_resets_sequence(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"before", sequence=40000))
        await receiver._voice_client._handle_voice_ws_message(
            None, {"op": 5, "d": {"user_id": USER_ID + 1, "ssrc": SSRC}},
        )
        receiver._on_packet(encrypt_packet(b"after", sequence=10000))
        await asyncio.sleep(0)
        self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"before"], [b"after"]])
        self.assertEqual([user_id for user_id, _pcm in self.received], [USER_ID, USER_ID + 1])

    async def test_repeated_speaking_notification_preserves_sequence_filter(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"new", sequence=40000))
        await receiver._voice_client._handle_voice_ws_message(
            None, {"op": 5, "d": {"user_id": USER_ID, "ssrc": SSRC}},
        )
        receiver._on_packet(encrypt_packet(b"old", sequence=10000))
        self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"new"]])

    async def test_voice_reconnect_resets_stream_even_with_the_same_key(self):
        for op in (4, 9):
            with self.subTest(op=op):
                receiver = self.make_receiver()
                receiver.start()
                receiver._on_packet(encrypt_packet(b"before", sequence=40000))
                await receiver._voice_client._handle_voice_ws_message(None, {"op": op, "d": {}})
                receiver._on_packet(encrypt_packet(b"after", sequence=10000))
                self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"before"], [b"after"]])

    async def test_new_voice_connection_forgets_old_speaker_assignments(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"before", sequence=40000))
        await receiver._voice_client._handle_voice_ws_message(None, {"op": 2, "d": {}})
        receiver._on_packet(encrypt_packet(b"unknown", sequence=10000))
        self.assertIsNone(receiver._voice_client.user_id_for_ssrc(SSRC))
        self.assertNotIn(SSRC, receiver._decoders)
        self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"before"]])

    async def test_transport_key_change_resets_decoder_before_sequence_comparison(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"before", sequence=40000))
        new_key = nacl.utils.random(nacl.secret.Aead.KEY_SIZE)
        self.connection.secret_key = list(new_key)
        receiver._on_packet(encrypt_packet(b"after", sequence=10000, key=new_key))
        self.assertEqual([decoder.decoded for decoder in self.decoders], [[b"before"], [b"after"]])

    async def test_unsupported_mode_is_dropped(self):
        receiver = self.make_receiver()
        self.connection.mode = "xsalsa20_poly1305"
        receiver.start()
        with self.assertLogs("persona_bot.audio.voice_receive", level="WARNING"):
            receiver._on_packet(encrypt_packet(b"opus"))
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])

    async def test_dave_layer_is_decrypted_with_the_speaker_id(self):
        session = SimpleNamespace(
            ready=True, decrypt=Mock(return_value=b"plain-opus"), can_passthrough=Mock(return_value=False),
        )
        receiver = self.make_receiver(dave_session=session, dave_version=1)
        receiver.start()
        receiver._on_packet(encrypt_packet(b"dave-cipher"))
        await asyncio.sleep(0)
        session.decrypt.assert_called_once_with(USER_ID, voice_receive.davey.MediaType.audio, b"dave-cipher")
        self.assertEqual(self.decoders[0].decoded, [b"plain-opus"])

    async def test_undecryptable_dave_frame_is_dropped_unless_passthrough(self):
        for passthrough in (False, True):
            with self.subTest(passthrough=passthrough):
                session = SimpleNamespace(
                    ready=True, decrypt=Mock(side_effect=RuntimeError("架空の復号失敗")),
                    can_passthrough=Mock(return_value=passthrough),
                )
                receiver = self.make_receiver(dave_session=session, dave_version=1)
                receiver.start()
                receiver._on_packet(encrypt_packet(b"frame"))
                await asyncio.sleep(0)
                self.assertEqual(len(self.received), 1 if passthrough else 0)

    async def test_packets_after_stop_are_not_delivered(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"opus"))
        receiver.stop()
        await asyncio.sleep(0)
        self.assertEqual(self.received, [])


class ReceiverDecoderLifetimeTests(unittest.IsolatedAsyncioTestCase):
    """切断した話者のデコーダ回収と、停止後に入れ直さないこと（#432）。"""

    make_receiver = ReceiverTests.make_receiver  # 受信の組み立てを共有する（テストの重複実行は避ける）

    async def test_decoder_of_a_departed_speaker_is_released_when_others_keep_talking(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver._on_packet(encrypt_packet(b"first", sequence=10))
        departed_ssrc = SSRC + 1
        receiver._decoders[departed_ssrc] = receiver._decoders[SSRC]  # 切断済みの話者のデコーダ
        self.assertIn(departed_ssrc, receiver._decoders)
        receiver._on_packet(encrypt_packet(b"second", sequence=11))
        self.assertNotIn(departed_ssrc, receiver._decoders)
        self.assertIn(SSRC, receiver._decoders)

    async def test_active_ssrcs_follow_speaker_assignments(self):
        receiver = self.make_receiver()
        voice_client = receiver._voice_client
        self.assertEqual(voice_client.active_ssrcs(), frozenset({SSRC}))
        await voice_client._handle_voice_ws_message(None, {"op": 13, "d": {"user_id": USER_ID}})
        self.assertEqual(voice_client.active_ssrcs(), frozenset())

    async def test_decoder_is_not_inserted_after_stop(self):
        receiver = self.make_receiver()
        receiver.start()
        receiver.stop()
        # stop() と同時に UDP スレッドが走っていた場合を再現する（_running を見ずに _on_packet を迂回）。
        self.assertIsNone(receiver._decode_packet(encrypt_packet(b"late")))
        self.assertEqual(receiver._decoders, {})
        self.assertEqual(self.decoders, [])


class DiscordCompatibilityTests(unittest.TestCase):
    def test_discord_py_still_exposes_the_receive_entry_points(self):
        """受信が依存する discord.py の内部の入口が残っているか（版の更新で消えたら検出する）。"""
        self.assertTrue(hasattr(VoiceConnectionState, "add_socket_listener"))
        self.assertTrue(hasattr(VoiceConnectionState, "remove_socket_listener"))
        self.assertIn("aead_xchacha20_poly1305_rtpsize", discord.VoiceClient.supported_modes)
        self.assertTrue(hasattr(discord.VoiceClient, "create_connection_state"))
        self.assertTrue(hasattr(discord.opus, "Decoder"))


if __name__ == "__main__":
    unittest.main()
