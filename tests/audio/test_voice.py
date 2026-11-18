"""音声の整形、合成エンジンの退避、PCM変換、読み上げキューを外部通信なしで検証する。"""

import asyncio
from collections import deque
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from requests.exceptions import ConnectTimeout, ReadTimeout

from persona_bot.audio import speech_text, tts_synthesis, voice
from persona_bot.core.background import spawn
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class SpeechTextTests(unittest.TestCase):
    def test_emotion_and_affinity_tags_are_removed_anywhere(self):
        tags = (
            "[V:10, A:-5]", "[v: +5 , a: -10 ]",
            "[happy]", "[SAD]", "[ angry ]", "[arousal:+5]",
            "[ valence : -2 ]", "[affinity:1]",
        )
        for tag in tags:
            for text in (tag + "架空の本文", "架空の" + tag + "本文", "架空の本文" + tag):
                with self.subTest(tag=tag, text=text):
                    self.assertEqual(voice.build_speech_text(text), "架空の本文")
        self.assertEqual(voice.build_speech_text("[happy][V:0, A:0] [arousal:+5]"), "")

    def test_bracketed_body_text_is_preserved(self):
        text = "[Python] [API v2] [日本語の補足] [1, 2] [arousal] [happy birthday]"
        self.assertEqual(voice.build_speech_text(text), text)

    def test_tags_are_removed_before_applying_length_limit(self):
        text = "あ" * speech_text.TTS_MAX_CHARS
        self.assertEqual(voice.build_speech_text("[happy]" + text + "[V:0, A:0]"), text)

    def test_markup_urls_code_mentions_and_custom_emoji_are_removed(self):
        text = '**架空の説明** https://example.com/path `内部コード` ```py\nprint(1)\n``` <@11> <@!22> <@&33> <#44> <:smile:55> <a:wave:66>\n続き'
        self.assertEqual(voice.build_speech_text(text), "架空の説明 続き")
        self.assertEqual(voice.build_speech_text('https://example.com `code` <:smile:55>'), "")

    def test_exact_limit_and_truncation_notice(self):
        for length in (speech_text.TTS_MAX_CHARS - 1, speech_text.TTS_MAX_CHARS):
            with self.subTest(length=length):
                self.assertEqual(voice.build_speech_text("あ" * length), "あ" * length)
        self.assertEqual(voice.build_speech_text("あ" * (speech_text.TTS_MAX_CHARS + 1)),
                         "あ" * speech_text.TTS_MAX_CHARS + speech_text.TTS_TRUNCATION_NOTICE)

    def test_url_removal_preserves_adjacent_japanese_text(self):
        cases = (
            ("https://example.com/testを見てね！", "を見てね！"),
            ("詳細はhttps://example.com/path?q=a%20b&n=1#partを確認してください。", "詳細は を確認してください。"),
            ("参照先http://example.com/path。次の説明です。", "参照先 。次の説明です。"),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(voice.build_speech_text(text), expected)

    def test_unclosed_code_block_is_not_read_aloud(self):
        self.assertEqual(voice.build_speech_text("架空の説明\n```python\nprint('読まない')"), "架空の説明")
        self.assertEqual(voice.build_speech_text("```未完了のコード"), "")
        self.assertEqual(voice.build_speech_text("前```code```後```unclosed"), "前 後")


class PcmDecodeTests(unittest.TestCase):
    def test_flat_profile_decodes_stereo_without_equalizer(self):
        samples = np.array([-100, -100, 200, 200], dtype=np.int16)
        with patch.object(tts_synthesis.miniaudio, "decode", return_value=SimpleNamespace(samples=samples)) as decode, patch.object(
            tts_synthesis, "apply_equalizer", side_effect=AssertionError("加工なしの経路でEQを呼んではいけません。"),
        ):
            result = tts_synthesis._decode_to_pcm(b"fictional-mp3", {"eq": "flat"})
        self.assertEqual(result, samples.tobytes())
        self.assertEqual(decode.call_args.kwargs["nchannels"], tts_synthesis.TTS_CHANNELS)
        self.assertEqual(decode.call_args.kwargs["sample_rate"], tts_synthesis.TTS_SAMPLE_RATE)

    def test_equalized_mono_is_clipped_and_interleaved_for_both_channels(self):
        mono = np.array([-100, 0, 100], dtype=np.int16)
        with patch.object(tts_synthesis, "needs_equalizer", return_value=True), patch.object(
            tts_synthesis.miniaudio, "decode", return_value=SimpleNamespace(samples=mono),
        ) as decode, patch.object(tts_synthesis, "apply_equalizer", return_value=np.array([-40000, 0, 40000], dtype=float)):
            result = tts_synthesis._decode_to_pcm(b"fictional-mp3", {"eq": "fictional"})
        self.assertEqual(decode.call_args.kwargs["nchannels"], 1)
        np.testing.assert_array_equal(np.frombuffer(result, dtype=np.int16), [-32768, -32768, 0, 0, 32767, 32767])


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_gtts_receives_finite_network_timeouts_and_closes_buffer(self):
        buffers = []

        def write(buffer):
            buffers.append(buffer)
            buffer.write(b"fictional-mp3")

        with patch.object(tts_synthesis, "gTTS", return_value=SimpleNamespace(write_to_fp=write)) as engine:
            self.assertEqual(tts_synthesis._fetch_gtts_mp3("架空の音声"), b"fictional-mp3")
        engine.assert_called_once_with(
            text="架空の音声", lang=tts_synthesis.TTS_LANG,
            timeout=(tts_synthesis.TTS_GTTS_CONNECT_TIMEOUT_SECONDS, tts_synthesis.TTS_GTTS_READ_TIMEOUT_SECONDS),
        )
        for seconds in engine.call_args.kwargs["timeout"]:
            self.assertGreater(seconds, 0)
            self.assertLess(seconds, float("inf"))
        self.assertTrue(buffers[0].closed)

    async def test_gtts_repeated_cancellation_recovers_sync_worker_and_buffer(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                entered, release = asyncio.Event(), threading.Event()
                finished = threading.Event()
                buffers = []
                loop = asyncio.get_running_loop()

                def write(buffer):
                    buffers.append(buffer)
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        if not release.wait(5):
                            raise TimeoutError("音声テストの待機が終了しませんでした")
                        if failure:
                            raise ReadTimeout("架空の読み取り期限超過")
                        buffer.write(b"fictional-mp3")
                    finally:
                        finished.set()

                with patch.object(tts_synthesis, "gTTS", return_value=SimpleNamespace(write_to_fp=write)), patch.object(
                    tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=OSError("架空の障害")),
                ), patch.object(tts_synthesis, "_edge_tts_failing", False):
                    task = spawn(tts_synthesis._fetch_mp3("架空の音声", {}), label="退避音声のキャンセルの確認")
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                            self.assertFalse(buffers[0].closed)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                    finally:
                        release.set()
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                self.assertTrue(finished.is_set())
                self.assertTrue(buffers[0].closed)

    async def test_edge_stream_ignores_metadata_and_keeps_audio_order(self):
        async def stream():
            for chunk in ({"type": "WordBoundary"}, {"type": "audio", "data": b"first"}, {"type": "audio", "data": b"second"}):
                yield chunk

        profile = {"voice": "fictional-voice", "rate": "+5%", "pitch": "+10Hz"}
        with patch.object(tts_synthesis.edge_tts, "Communicate", return_value=SimpleNamespace(stream=stream)) as engine:
            self.assertEqual(await tts_synthesis._fetch_edge_mp3("架空の音声", profile), b"firstsecond")
        engine.assert_called_once_with("架空の音声", "fictional-voice", rate="+5%", pitch="+10Hz")

    async def test_edge_failure_or_empty_response_falls_back_once_and_recovers(self):
        for failure in (OSError("架空の障害"), b""):
            with self.subTest(failure=type(failure).__name__):
                threads = []

                def fallback(text):
                    threads.append(threading.get_ident())
                    return b"fallback"

                with patch.object(tts_synthesis, "_edge_tts_failing", False), patch.object(
                    tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=[failure, failure, b"recovered"]),
                ), patch.object(tts_synthesis, "_fetch_gtts_mp3", side_effect=fallback) as backup, self.assertLogs("persona_bot.audio.voice", level="INFO") as logs:
                    self.assertEqual(await tts_synthesis._fetch_mp3("架空", {}), b"fallback")
                    self.assertEqual(await tts_synthesis._fetch_mp3("架空", {}), b"fallback")
                    self.assertEqual(await tts_synthesis._fetch_mp3("架空", {}), b"recovered")
                    self.assertFalse(tts_synthesis._edge_tts_failing)
                    self.assertEqual(backup.call_count, 2)
                    self.assertEqual(len(logs.records), 2)
                    self.assertEqual([record.levelname for record in logs.records], ["WARNING", "INFO"])
                    self.assertIn("gTTS", logs.records[0].getMessage())
                    self.assertIn("復旧", logs.records[1].getMessage())
                    if isinstance(failure, BaseException):
                        self.assertIs(logs.records[0].exc_info[1], failure)
                        self.assertNotIn("架空の障害", LOG_FORMATTER.format(logs.records[0]))
                self.assertNotIn(threading.get_ident(), threads)

    async def test_edge_success_skips_fallback_and_cancellation_does_not_trigger_it(self):
        for result in (b"audio", asyncio.CancelledError()):
            with self.subTest(result=type(result).__name__), patch.object(tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=[result])), patch.object(
                tts_synthesis, "_fetch_gtts_mp3",
            ) as backup:
                if isinstance(result, BaseException):
                    with self.assertRaises(asyncio.CancelledError):
                        await tts_synthesis._fetch_mp3("架空", {})
                else:
                    self.assertEqual(await tts_synthesis._fetch_mp3("架空", {}), result)
                backup.assert_not_called()

    async def test_edge_hang_times_out_and_fallback_completes_synthesis(self):
        cancelled = asyncio.Event()

        async def hang(*args):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(tts_synthesis, "_fetch_edge_mp3", side_effect=hang), patch.object(
            tts_synthesis, "_fetch_gtts_mp3", return_value=b"fallback",
        ) as backup, patch.object(tts_synthesis, "_decode_to_pcm", return_value=b"pcm") as decode, patch.object(
            tts_synthesis, "TTS_EDGE_TIMEOUT_SECONDS", 0.01,
        ), patch.object(tts_synthesis, "_edge_tts_failing", False):
            result = await asyncio.wait_for(voice._synthesize_pcm("架空の音声", {}), 2)

        self.assertEqual(result, b"pcm")
        self.assertTrue(cancelled.is_set())
        backup.assert_called_once_with("架空の音声")
        decode.assert_called_once_with(b"fallback", {})

    async def test_external_cancellation_during_edge_wait_does_not_fallback(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def hang(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch.object(tts_synthesis, "_fetch_edge_mp3", side_effect=hang), patch.object(
            tts_synthesis, "_fetch_gtts_mp3",
        ) as backup:
            task = asyncio.create_task(voice._synthesize_pcm("架空の音声", {}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(cancelled.is_set())
        backup.assert_not_called()

    async def test_pcm_decode_runs_in_worker_thread(self):
        threads = []

        def decode(*args):
            threads.append(threading.get_ident())
            return b"pcm"

        with patch.object(tts_synthesis, "_fetch_mp3", new=AsyncMock(return_value=b"audio")), patch.object(tts_synthesis, "_decode_to_pcm", side_effect=decode):
            self.assertEqual(await voice._synthesize_pcm("架空", {}), b"pcm")
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)


class SpeechQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = Mock(spec=discord.VoiceClient)
        self.client.is_connected.return_value = True
        self.client.is_playing.return_value = False
        self.client.is_paused.return_value = False
        self.client.channel = SimpleNamespace(id=10)
        self.guild = SimpleNamespace(id=1, voice_client=self.client)
        self.workers = []

        def start(coro, **kwargs):
            self.workers.append(coro)
            self.addCleanup(coro.close)

        for name, replacement in (("_speech_queues", {}), ("spawn", start)):
            patcher = patch.object(voice, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_empty_or_disconnected_requests_do_not_create_worker(self):
        for guild, text in ((None, "架空"), (self.guild, None), (self.guild, "  "), (self.guild, "https://example.com")):
            await voice.speak_text(guild, text)
        self.client.is_connected.return_value = False
        await voice.speak_text(self.guild, "架空")
        self.guild.voice_client = None
        await voice.speak_text(self.guild, "架空")
        self.assertEqual(self.workers, [])
        self.assertEqual(voice._speech_queues, {})

    async def test_both_entrypoints_synthesize_only_body_text(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(return_value=b"pcm")) as synthesize, patch.object(
            voice, "_play_pcm", new=AsyncMock(),
        ) as play:
            await voice.speak_text(self.guild, "[happy]架空の独り言[V:0, A:0]")
            await voice.play_tts_response(SimpleNamespace(guild=self.guild), "架空の返信[sad][arousal:+5]")
            await self.workers[0]
        self.assertEqual([call.args[0] for call in synthesize.call_args_list], ["架空の独り言", "架空の返信"])
        self.assertEqual(play.await_count, 2)
        self.assertEqual(voice._speech_queues, {})

    async def test_tag_only_requests_do_not_create_worker(self):
        await voice.speak_text(self.guild, "[happy][V:0, A:0]")
        await voice.play_tts_response(SimpleNamespace(guild=self.guild), "[sad][arousal:+5]")
        self.assertEqual(self.workers, [])
        self.assertEqual(voice._speech_queues, {})

    async def test_old_worker_cancellation_does_not_remove_replacement_queue(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def synthesize(*args):
            entered.set()
            await release.wait()
            return b"pcm"

        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(side_effect=synthesize)):
            await voice.speak_text(self.guild, "古い読み上げ")
            worker = asyncio.create_task(self.workers[0])
            try:
                await asyncio.wait_for(entered.wait(), 2)
                replacement = deque()
                voice._speech_queues[self.guild.id] = replacement
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await worker
                self.assertIs(voice._speech_queues[self.guild.id], replacement)
            finally:
                release.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    async def test_queue_overflow_drops_only_newest_request_and_keeps_requested_profiles(self):
        first, second = {"voice": "first"}, {"voice": "second"}
        with patch.object(voice, "TTS_QUEUE_MAX", 2), patch.object(voice, "resolve_profile", side_effect=[first, second, first]), patch.object(
            voice, "_synthesize_pcm", new=AsyncMock(return_value=b"pcm"),
        ) as synthesize, patch.object(voice, "_play_pcm", new=AsyncMock()) as play:
            await voice.speak_text(self.guild, "最初", "first.txt")
            await voice.speak_text(self.guild, "次", "second.txt")
            await voice.speak_text(self.guild, "溢れた分", "first.txt")
            self.assertEqual(len(self.workers), 1)
            await self.workers[0]
        self.assertEqual([call.args for call in synthesize.call_args_list], [("最初", first), ("次", second)])
        self.assertEqual(play.await_count, 2)
        self.assertEqual(voice._speech_queues, {})

    async def test_enqueues_during_playback_keep_waiting_order_and_drop_only_overflow(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def play(*args, **kwargs):
            entered.set()
            await release.wait()

        with patch.object(voice, "TTS_QUEUE_MAX", 2), patch.object(
            voice, "_synthesize_pcm", AsyncMock(return_value=b"pcm"),
        ) as synthesize, patch.object(voice, "_play_pcm", AsyncMock(side_effect=play)), patch("persona_bot.audio.voice.logger"):
            await voice.speak_text(self.guild, "再生中")
            worker = asyncio.create_task(self.workers[0])
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await voice.speak_text(self.guild, "待機一件目")
                await voice.speak_text(self.guild, "待機二件目")
                for _ in range(20):
                    await voice.speak_text(self.guild, "上限超過分")
                self.assertEqual(len(self.workers), 1)
                release.set()
                await asyncio.wait_for(worker, 2)
            finally:
                release.set()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual([call.args[0] for call in synthesize.await_args_list],
                         ["再生中", "待機一件目", "待機二件目"])
        self.assertEqual(voice._speech_queues, {})

    async def test_request_after_worker_drains_starts_new_worker(self):
        with patch.object(voice, "_synthesize_pcm", AsyncMock(return_value=b"pcm")) as synthesize, patch.object(
            voice, "_play_pcm", AsyncMock(),
        ):
            await voice.speak_text(self.guild, "最初の返信")
            await self.workers[0]
            self.assertEqual(voice._speech_queues, {})
            await voice.speak_text(self.guild, "空になった後の返信")
            self.assertEqual(len(self.workers), 2)
            await self.workers[1]
        self.assertEqual([call.args[0] for call in synthesize.await_args_list],
                         ["最初の返信", "空になった後の返信"])
        self.assertEqual(voice._speech_queues, {})

    async def test_synthesis_error_and_timeout_skip_to_next_request(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(side_effect=[asyncio.TimeoutError(), OSError("架空の合成障害"), b"pcm"])), patch.object(
            voice, "_play_pcm", new=AsyncMock(),
        ) as play:
            for text in ("時間切れ", "失敗", "正常"):
                await voice.speak_text(self.guild, text)
            await self.workers[0]
        play.assert_awaited_once_with(self.client, b"pcm", is_valid=ANY)
        self.assertEqual(voice._speech_queues, {})

    async def test_gtts_network_timeouts_skip_failed_requests_and_next_edge_request_plays(self):
        # gTTS本体を通し、HTTP境界だけで接続・読み取り期限の例外を注入する。
        threads = []
        timeouts = []
        failures = iter((ConnectTimeout("架空の接続期限超過"), ReadTimeout("架空の読み取り期限超過")))

        def send(*args, **kwargs):
            threads.append(threading.get_ident())
            timeouts.append(kwargs["timeout"])
            raise next(failures)

        with patch.object(tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=[
            OSError("架空の障害"), OSError("架空の障害"), b"fictional-mp3",
        ])), patch("gtts.tts.requests.Session.send", side_effect=send) as network, patch.object(
            tts_synthesis, "_decode_to_pcm", return_value=b"pcm",
        ) as decode, patch.object(voice, "_play_pcm", new=AsyncMock()) as play, patch.object(
            tts_synthesis, "_edge_tts_failing", False,
        ):
            for text in ("接続失敗", "読み取り失敗", "次の返信"):
                await voice.speak_text(self.guild, text)
            await asyncio.wait_for(self.workers[0], 2)
        self.assertEqual(network.call_count, 2)
        self.assertEqual(len(threads), 2)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(timeouts, [(
            tts_synthesis.TTS_GTTS_CONNECT_TIMEOUT_SECONDS, tts_synthesis.TTS_GTTS_READ_TIMEOUT_SECONDS,
        )] * 2)
        decode.assert_called_once()
        play.assert_awaited_once_with(self.client, b"pcm", is_valid=ANY)
        self.assertEqual(voice._speech_queues, {})

    async def test_synthesis_deadline_waits_for_gtts_cleanup_before_advancing_queue(self):
        entered, release = asyncio.Event(), threading.Event()
        buffers = []
        loop = asyncio.get_running_loop()
        normal_timeout = voice.TTS_TIMEOUT_SECONDS

        def write(buffer):
            buffers.append(buffer)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise TimeoutError("音声テストの待機が終了しませんでした")
            raise ReadTimeout("架空の読み取り期限超過")

        with patch.object(tts_synthesis, "gTTS", return_value=SimpleNamespace(write_to_fp=write)), patch.object(
            tts_synthesis, "_fetch_edge_mp3", new=AsyncMock(side_effect=[OSError("架空の障害"), b"fictional-mp3"]),
        ), patch.object(tts_synthesis, "_decode_to_pcm", return_value=b"pcm"), patch.object(
            voice, "_play_pcm", new=AsyncMock(),
        ) as play, patch.object(voice, "TTS_TIMEOUT_SECONDS", 0.02), patch.object(tts_synthesis, "_edge_tts_failing", False):
            await voice.speak_text(self.guild, "期限超過")
            await voice.speak_text(self.guild, "次の返信")
            task = spawn(self.workers[0], label="合成期限後の同期処理回収の確認")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                play.assert_not_awaited()
                self.assertFalse(buffers[0].closed)
                # 後続の正常な合成には既定の期限を戻し、CIのスレッド起動時間で失敗させない。
                voice.TTS_TIMEOUT_SECONDS = normal_timeout
                release.set()
                await asyncio.wait_for(task, 2)
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(buffers[0].closed)
        play.assert_awaited_once_with(self.client, b"pcm", is_valid=ANY)
        self.assertEqual(voice._speech_queues, {})

    async def test_worker_cancellation_clears_registration(self):
        with patch.object(voice, "_synthesize_pcm", new=AsyncMock(side_effect=asyncio.CancelledError())):
            await voice.speak_text(self.guild, "架空")
            with self.assertRaises(asyncio.CancelledError):
                await self.workers[0]
        self.assertEqual(voice._speech_queues, {})

    async def test_playback_completion_can_arrive_from_audio_thread(self):
        threads = []

        def play(source, *, after):
            thread = threading.Thread(target=lambda: after(None))
            threads.append(thread)
            thread.start()
            source.cleanup()

        self.client.play.side_effect = play
        await asyncio.wait_for(voice._play_pcm(self.client, b"\0" * 16), 2)
        for thread in threads:
            thread.join(1)
        self.client.stop.assert_not_called()

    async def test_cancelled_playback_stops_and_closes_stream_even_if_stop_fails(self):
        for stop_error in (None, RuntimeError("架空の停止失敗")):
            with self.subTest(stop_error=stop_error):
                started = asyncio.Event()
                sources = []

                def play(source, **kwargs):
                    sources.append(source)
                    started.set()

                self.client.play.side_effect = play
                self.client.stop.reset_mock(side_effect=True)
                self.client.stop.side_effect = stop_error
                task = asyncio.create_task(voice._play_pcm(self.client, b"\0" * 16))
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.client.stop.assert_called_once()
                self.assertTrue(sources[0].stream.closed)

    async def test_play_rejection_closes_stream(self):
        sources = []

        def reject(source, **kwargs):
            sources.append(source)
            raise RuntimeError("架空の再生失敗")

        self.client.play.side_effect = reject
        with self.assertRaises(RuntimeError):
            await voice._play_pcm(self.client, b"\0" * 16)
        self.assertTrue(sources[0].stream.closed)

    async def test_missing_completion_stops_playback_instead_of_hanging_worker(self):
        async def timeout(awaitable, *, timeout):
            awaitable.close()
            raise asyncio.TimeoutError

        self.client.play.side_effect = lambda source, **kwargs: source.cleanup()
        with patch.object(voice.asyncio, "wait_for", side_effect=timeout):
            await voice._play_pcm(self.client, b"\0" * 16)
        self.client.stop.assert_called_once()
