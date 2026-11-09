"""共通化した読込・描画と分割の境界を、実データなしで確認する。"""
import asyncio
import io
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import matplotlib.pyplot as plt
from persona_bot.config import paths
from persona_bot.audio import voice, voice_dsp
from persona_bot.utils import graph_rendering
from persona_bot.utils.discord_helpers import can_bot_send_messages, split_message


class LifecycleHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_persona_resolves_and_reads_bom_in_worker_and_propagates_missing_file(self):
        main_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "persona.txt").write_text("架空のペルソナ", encoding="utf-8-sig")
            resolve = paths.resolve_prompt_path
            def worker_resolve(filename):
                self.assertNotEqual(threading.get_ident(), main_thread)
                return resolve(filename)
            with patch.object(paths, "BASE_DIR", root), patch.object(paths, "resolve_prompt_path", side_effect=worker_resolve):
                self.assertEqual(await paths.load_persona_template("persona.txt"), "架空のペルソナ")
                with self.assertRaises(FileNotFoundError):
                    await paths.load_persona_template("missing.txt")

    def test_png_buffer_starts_at_zero_and_figure_is_closed(self):
        figure, axis = plt.subplots()
        axis.plot([0, 1])
        buffer = graph_rendering.save_figure_to_buffer(figure, bbox_inches="tight")
        try:
            self.assertEqual(buffer.read(8), b"\x89PNG\r\n\x1a\n")
            self.assertFalse(plt.fignum_exists(figure.number))
        finally:
            buffer.close()

    def test_failed_save_closes_buffer_and_figure(self):
        figure = plt.figure()
        buffer = io.BytesIO()
        with patch.object(graph_rendering.io, "BytesIO", return_value=buffer), patch.object(figure, "savefig", side_effect=RuntimeError("架空の保存失敗")):
            with self.assertRaises(RuntimeError):
                graph_rendering.save_figure_to_buffer(figure)
        self.assertTrue(buffer.closed)
        self.assertFalse(plt.fignum_exists(figure.number))

    def test_split_consumes_one_separator_and_preserves_blank_line_and_indent(self):
        self.assertEqual(split_message("1234567\n\n  abcdef", limit=10), ["1234567\n", "  abcdef"])
        # text[limit] の改行も区切りとして使う（#411）。2つ目の改行を区切りとして1つだけ消費し、
        # 1つ目は前のチャンクの末尾に残る（連結し直すと原文に戻る）。
        self.assertEqual(split_message("123456789\n\n  abcdef", limit=10), ["123456789\n", "  abcdef"])
        self.assertEqual([len(chunk) for chunk in split_message("あ" * 4001)], [2000, 2000, 1])

    def test_split_finds_a_separator_right_after_the_limit(self):
        # 制限ちょうどの次の文字（text[limit]）が区切りなら、手前の空白で不要に分割しない（#411）。
        for separator in ("\n", " "):
            with self.subTest(separator=repr(separator)):
                head = "あ" * 4 + " " + "い" * 5  # 10文字（途中に空白がある）
                self.assertEqual(len(head), 10)
                self.assertEqual(split_message(head + separator + "う" * 6, limit=10), [head, "う" * 6])
        text = "あ" * 1000 + " " + "い" * 999 + "\n" + "う" * 1500
        self.assertEqual(split_message(text, limit=2000), ["あ" * 1000 + " " + "い" * 999, "う" * 1500])

    def test_split_discards_whitespace_only_chunks_at_boundaries_and_between_text(self):
        for whitespace in ("\n   ", " " * 4001, "\n" * 4001):
            with self.subTest(whitespace=repr(whitespace[:10])):
                self.assertEqual(split_message("あ" * 2000 + whitespace), ["あ" * 2000])
                chunks = split_message("あ" * 2000 + whitespace + "い" * 2000)
                self.assertTrue(all(chunk.strip() and len(chunk) <= 2000 for chunk in chunks))
                self.assertEqual("".join(chunks).replace(" ", "").replace("\n", ""), "あ" * 2000 + "い" * 2000)

    def test_split_returns_no_chunks_for_empty_or_whitespace_only_text(self):
        for text in ("", " \n\t\r　", " " * 4001, "\n" * 4001):
            with self.subTest(text=repr(text[:10])):
                self.assertEqual(split_message(text), [])

    def test_split_preserves_code_block_indentation_and_surrounding_whitespace(self):
        text = " \n```python\n    架空の処理()\n```\n "
        self.assertEqual(split_message(text), [text])
        # text[limit] の空白は区切りとして1つ消費される（コードブロックの字下げと改行は残る）。
        self.assertEqual(split_message("あ" * 2000 + text), ["あ" * 2000, text[1:]])

    def test_symbols_do_not_reach_speech_engines_but_letters_and_digits_survive(self):
        for text in ("！！！", "「……？」", "👋😊", "[V:1,A:0] ……"):
            with self.subTest(text=text):
                self.assertEqual(voice.build_speech_text(text), "")
        for text in ("こんにちは！", "123", "Hello"):
            self.assertEqual(voice.build_speech_text(text), text)

    def test_inverted_clamp_bounds_respect_upper_limit(self):
        self.assertEqual(voice_dsp._clamp(100, 20, 10), 10)
        for value in (-100, 5, 100):
            self.assertLessEqual(voice_dsp._clamp(value, 20, 10), 10)

    def test_send_permission_requires_member_view_and_matching_channel_permission(self):
        import discord
        member = object()
        for thread in (False, True):
            channel = Mock(spec=discord.Thread if thread else discord.TextChannel)
            for view, send, thread_send in ((False, True, True), (True, False, True), (True, True, False)):
                channel.permissions_for.return_value = Mock(view_channel=view, send_messages=send, send_messages_in_threads=thread_send)
                self.assertEqual(can_bot_send_messages(channel, member), view and (thread_send if thread else send))
            self.assertFalse(can_bot_send_messages(channel, None))
