"""調整定数を変更したとき、文書・記憶・統計・音声の利用側へ反映されるか確認する。"""

import importlib.util
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.utils.storage import write_json_file


def load_with_tuning(name: str, **values):
    """起動前の定数変更を再現し、既存のimport済みモジュールは変更しない。"""
    spec = importlib.util.spec_from_file_location(f"tuned_{name}", importlib.util.find_spec(name).origin)
    module = importlib.util.module_from_spec(spec)
    with patch.multiple("persona_bot.config.constants", **values):
        spec.loader.exec_module(module)
    return module


class DocumentTuningTests(unittest.TestCase):
    def test_preview_limit_accepts_split_utf8_and_rejects_invalid_preview(self) -> None:
        module = load_with_tuning("persona_bot.services.document_service", TEXT_VALIDATION_PREVIEW_BYTES=4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.txt"
            # プレビュー外のバイトは検証せず、境界の途中で切れた日本語は許容する。
            path.write_bytes(b"abc" + "あ".encode("utf-8") + b"\xff")
            module.validate_text_file(path)
            for content in (b"a\xffbc", b"\xe3"):
                with self.subTest(content=content):
                    path.write_bytes(content)
                    with self.assertRaises(module.DocumentUserError) as raised:
                        module.validate_text_file(path)
                    self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)


class ModerationAndChoiceTuningTests(unittest.TestCase):
    def test_moderation_defaults_use_configured_model_and_timeout(self) -> None:
        module = load_with_tuning("persona_bot.services.content_moderator",
                                 MODERATION_MODEL_NAME="test-moderation-model", MODERATION_TIMEOUT_SECONDS=23.0)
        moderator = module.ContentModerator(None, SimpleNamespace(words=[]))
        self.assertEqual((moderator._model_name, moderator._timeout), ("test-moderation-model", 23.0))

    def test_default_choices_follow_configured_mode(self) -> None:
        # ペルソナは固定の選択肢を持たない（prompts/ から作る）ため、応答モードだけを確かめる
        module = load_with_tuning("persona_bot.config.choices", DEFAULT_RESPOND_MODE="test-mode")
        self.assertIn("test-mode", [choice.value for choice in module.RESPOND_MODE_CHOICES])


class MemoryAndStatsTuningTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_context_default_uses_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = load_with_tuning(
                "persona_bot.services.memory_service", DOCUMENT_MEMORY_CONTEXT_LIMIT=1,
                MEMORY_DIR=root, PROFILE_MEMORY_PATH=root / "profile.json",
                PROJECTS_MEMORY_PATH=root / "projects.json",
            )
            wrapper = module.default_projects_memory()
            wrapper["users"]["fictional-user"] = {"document_summaries": [
                {"document_name": f"文書{i}", "summary": f"架空の要約{i}"} for i in range(3)
            ]}
            write_json_file(root / "projects.json", wrapper)
            service = module.MemoryService(None, None)
            _, combined = await service.build_contexts("none", "fictional-user")
            individual = await service.build_document_memory_context("fictional-user")
            self.assertEqual(combined, "- 文書2: 架空の要約2")
            self.assertEqual(individual, combined)
            explicit = await service.build_document_memory_context("fictional-user", limit=2)
            self.assertEqual(len(explicit.splitlines()), 2)
            for count in (0, -1):
                with self.subTest(limit=count):
                    self.assertEqual(await service.build_document_memory_context("fictional-user", limit=count), "")
            zero_default = load_with_tuning(
                "persona_bot.services.memory_service", DOCUMENT_MEMORY_CONTEXT_LIMIT=0,
                MEMORY_DIR=root, PROFILE_MEMORY_PATH=root / "profile.json",
                PROJECTS_MEMORY_PATH=root / "projects.json",
            )
            zero_service = zero_default.MemoryService(None, None)
            self.assertEqual(await zero_service.build_document_memory_context("fictional-user"), "")
            self.assertEqual((await zero_service.build_contexts("none", "fictional-user"))[1], "")

    async def test_ranking_default_uses_configured_count(self) -> None:
        module = load_with_tuning("persona_bot.services.stats_service", STATS_RANKING_TOP_N=2)
        entries = {}
        for i in range(4):
            entry = module.StatsService._new_entry(f"架空の利用者{i}")
            entry["total_count"] = i + 1
            entries[f"fictional-{i}"] = entry
        service = module.StatsService(SimpleNamespace(stats_load_all=lambda: entries), None)
        graph = io.BytesIO()
        service._render_ranking_graph = Mock(return_value=graph)
        try:
            self.assertIs(await service.build_ranking_graph(), graph)
            self.assertEqual(service._render_ranking_graph.call_args.args[1], 2)
        finally:
            graph.close()


class VoiceTuningTests(unittest.TestCase):
    def test_equalizer_defaults_and_limits_use_configured_values(self) -> None:
        module = load_with_tuning(
            "persona_bot.audio.voice_dsp", TTS_EQ_DEFAULT_FREQUENCY_HZ=800.0,
            TTS_EQ_MIN_FREQUENCY_HZ=20.0, TTS_EQ_NYQUIST_MARGIN=0.4,
            TTS_EQ_DEFAULT_GAIN_DB=3.0, TTS_EQ_DEFAULT_Q=0.8, TTS_EQ_MIN_Q=0.2,
            VOICE_EQ_PROFILES={"flat": {}, "test": {"kind": "low_pass"}},
        )
        with patch.object(module, "_biquad_coefficients", return_value=None) as coefficients:
            module.apply_equalizer(np.ones(4), {"eq": "test"}, sample_rate=10000)
            self.assertEqual(coefficients.call_args.args, ("low_pass", 800.0, 3.0, 0.8, 10000))
            for frequency, expected in ((-1, 20.0), (99999, 4000.0)):
                module.VOICE_EQ_PROFILES["test"].update(frequency=frequency, q=-1)
                module.apply_equalizer(np.ones(4), {"eq": "test"}, sample_rate=10000)
                self.assertEqual(coefficients.call_args.args,
                                 ("low_pass", expected, 3.0, 0.2, 10000))

    def test_default_equalizer_name_applies_to_missing_and_unknown_profiles(self) -> None:
        module = load_with_tuning(
            "persona_bot.audio.voice_dsp", DEFAULT_VOICE_EQ_NAME="test",
            VOICE_EQ_PROFILES={"flat": {}, "test": {"bit_crush": 8}},
        )
        for profile in ({}, {"eq": "unknown"}):
            with self.subTest(profile=profile):
                self.assertTrue(module.needs_equalizer(profile))
                result = module.apply_equalizer(np.array([100.0, 300.0]), profile, sample_rate=48000)
                np.testing.assert_array_equal(result, [0.0, 256.0])

    def test_small_biquad_blocks_keep_recursive_state(self) -> None:
        module = load_with_tuning("persona_bot.audio.voice_dsp", TTS_BIQUAD_BLOCK_SIZE=3)
        signal = np.zeros(11)
        signal[0] = 1
        # y[n] = x[n] + y[n-1]/2 の減衰がブロック境界をまたいでも続く。
        result = module._apply_biquad(signal, (1, 0, 0, -0.5, 0))
        np.testing.assert_array_equal(result, 0.5 ** np.arange(11))

    def test_pcm_clips_to_configured_limits_before_stereo_conversion(self) -> None:
        module = load_with_tuning("persona_bot.audio.voice", PCM_INT16_MIN=-100.0, PCM_INT16_MAX=100.0)
        decoded = SimpleNamespace(samples=np.zeros(3, dtype=np.int16))
        with patch.object(module, "needs_equalizer", return_value=True), \
                patch.object(module.miniaudio, "decode", return_value=decoded), \
                patch.object(module, "apply_equalizer", return_value=np.array([-500.0, 0.0, 500.0])):
            result = module._decode_to_pcm(b"fictional-audio", {})
        np.testing.assert_array_equal(np.frombuffer(result, dtype=np.int16),
                                      [-100, -100, 0, 0, 100, 100])


class LiveTuningTests(unittest.TestCase):
    """音声会話（#268）の調整値どうしの前提。値を変えても崩してはいけない関係を固定する。"""

    def test_sample_rates_convert_by_whole_ratios(self) -> None:
        from persona_bot.config import constants as c
        self.assertEqual(c.TTS_SAMPLE_RATE % c.LIVE_INPUT_SAMPLE_RATE, 0)
        self.assertEqual(c.TTS_SAMPLE_RATE % c.LIVE_OUTPUT_SAMPLE_RATE, 0)
        self.assertIn(str(c.LIVE_INPUT_SAMPLE_RATE), c.LIVE_INPUT_MIME_TYPE)
        # 間引きの前のローパスは、16kHz で表せる上限（8kHz）より低く切る。
        self.assertLess(c.LIVE_DOWNSAMPLE_CUTOFF_HZ, c.LIVE_INPUT_SAMPLE_RATE / 2)
        self.assertEqual(c.LIVE_DOWNSAMPLE_TAPS % 2, 1)

    def test_turn_taking_thresholds_keep_their_order(self) -> None:
        from persona_bot.config import constants as c
        # 割り込みは通常の話し始めより長い発言を要し、プリロールで話し始めの判定分を送れる。
        self.assertGreater(c.LIVE_BARGE_IN_MS, c.LIVE_VAD_START_MS)
        self.assertGreaterEqual(c.LIVE_VAD_PREROLL_MS, c.LIVE_VAD_START_MS)
        self.assertGreater(c.LIVE_VAD_SILENCE_MS, c.LIVE_VAD_START_GAP_MS)
        self.assertGreater(c.LIVE_MAX_UTTERANCE_SECONDS * 1000, c.LIVE_VAD_SILENCE_MS)
        self.assertGreaterEqual(c.LIVE_INPUT_CHUNK_MS, 20)

    def test_reconnect_and_compression_limits_are_consistent(self) -> None:
        from persona_bot.config import constants as c
        self.assertGreaterEqual(len(c.LIVE_RECONNECT_BACKOFF_SECONDS), 1)
        self.assertGreater(c.LIVE_RECONNECT_MAX_ATTEMPTS, 0)
        self.assertLess(c.LIVE_COMPRESSION_TARGET_TOKENS, c.LIVE_COMPRESSION_TRIGGER_TOKENS)
        self.assertGreater(c.LIVE_MAX_CONCURRENT_SESSIONS, 0)
        self.assertEqual(c.VOICE_PROFILE_FALLBACK["live_voice"], "")
