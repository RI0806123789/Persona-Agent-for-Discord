"""NGワードファイルのBOM除去と、検出したNGワードの返却順を検証する（#55）。"""

import codecs
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import ng_word_service
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.utils.normalization import fold_kana


class NgWordFileTestCase(unittest.TestCase):
    """実際の data/ng/ を読まないよう、両方のNGワードファイルを一時ディレクトリへ差し替える。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.public = root / "NG_WORD.txt"
        self.private = root / "NG_WORD_private.txt"
        paths = patch.multiple(ng_word_service, NG_WORD_PATH=self.public, NG_WORD_PRIVATE_PATH=self.private)
        paths.start()
        self.addCleanup(paths.stop)
        # 読込件数のログでテスト出力を埋めない
        quiet = patch.object(ng_word_service.logger, "info")
        quiet.start()
        self.addCleanup(quiet.stop)

    @staticmethod
    def write(path: Path, text: str, *, bom: bool) -> None:
        # メモ帳の「UTF-8 (BOM 付き)」保存と同じバイト列を作る
        path.write_bytes((codecs.BOM_UTF8 if bom else b"") + text.encode("utf-8"))


class NgWordBomTests(NgWordFileTestCase):
    def test_comment_on_first_line_after_bom_is_skipped(self) -> None:
        self.write(self.public, "# 架空のコメント\n架空語A\n", bom=True)
        self.assertEqual(ng_word_service.load_ng_words(), ["架空語A"])

    def test_word_on_first_line_after_bom_is_detected_and_masked(self) -> None:
        self.write(self.public, "架空語A\n架空語B\n", bom=True)
        ng_filter = NgWordFilter()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語B"])
        self.assertTrue(ng_filter.contains_ng_word("これは架空語Aです"))
        self.assertEqual(ng_filter.mask_ng_words("これは架空語Aです"), "これは●●●●です")

    def test_private_file_with_bom_is_merged_without_duplicates(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.write(self.private, "架空語A\n架空語C\n", bom=True)
        ng_filter = NgWordFilter()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語C"])
        self.assertEqual(ng_filter.find_ng_words("架空語Cと架空語A"), ["架空語C", "架空語A"])

    def test_file_without_bom_keeps_previous_behavior(self) -> None:
        self.write(self.public, "# 架空のコメント\n\n  架空語A  \n", bom=False)
        self.assertEqual(ng_word_service.load_ng_words(), ["架空語A"])
        self.assertTrue(all("﻿" not in word for word in ng_word_service.load_ng_words()))


class NgWordOrderTests(NgWordFileTestCase):
    def test_find_ng_words_returns_unique_words_in_order_of_first_appearance(self) -> None:
        self.write(self.public, "\n".join(f"架空語{c}" for c in "ABCDE") + "\n", bom=False)
        ng_filter = NgWordFilter()
        text = "架空語E、架空語C、架空語A、架空語E、架空語D、架空語B、架空語C"
        expected = ["架空語E", "架空語C", "架空語A", "架空語D", "架空語B"]
        # set では文字列のハッシュ順（プロセスごとに変わる）になり、5語が一致する確率は1/120
        for _ in range(3):
            self.assertEqual(ng_filter.find_ng_words(text), expected)

    def test_no_match_returns_empty_list(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.assertEqual(NgWordFilter().find_ng_words("問題のない文"), [])


class NgWordReloadTests(NgWordFileTestCase):
    def test_invalid_encoding_preserves_both_previous_files_and_reports_failure(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        self.write(self.private, "架空語B\n", bom=False)
        ng_filter = NgWordFilter()
        self.write(self.public, "架空語C\n", bom=False)
        self.private.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語A", "架空語B"])
        self.assertTrue(ng_filter.contains_ng_word("架空語B"))
        self.assertFalse(ng_filter.contains_ng_word("架空語C"))

    def test_read_error_preserves_previous_snapshot_then_valid_reload_succeeds(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        ng_filter = NgWordFilter()
        with patch.object(Path, "read_text", side_effect=PermissionError("架空のロック")):
            with self.assertRaises(PermissionError):
                ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語A"])
        self.write(self.public, "架空語B\n", bom=False)
        ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語B"])

    def test_reload_publishes_words_and_pattern_only_after_compilation(self) -> None:
        self.write(self.public, "架空語A\n", bom=False)
        ng_filter = NgWordFilter()
        self.write(self.public, "架空語B\n架空語C\n", bom=False)
        original = ng_word_service._build_pattern

        def compile_words(words):
            self.assertEqual(ng_filter.words, ["架空語A"])
            self.assertEqual(ng_filter.word_count, 1)
            self.assertEqual(ng_filter.mask_ng_words("架空語Aと架空語B"), "●●●●と架空語B")
            return original(words)

        with patch.object(ng_word_service, "_build_pattern", side_effect=compile_words):
            ng_filter.reload()
        self.assertEqual(ng_filter.words, ["架空語B", "架空語C"])
        self.assertEqual(ng_filter.mask_ng_words("架空語Aと架空語B"), "架空語Aと●●●●")


class NgWordNormalizationTests(NgWordFileTestCase):
    def make_filter(self, words: str) -> NgWordFilter:
        self.write(self.public, words + "\n", bom=False)
        return NgWordFilter()

    def assert_detected_span(self, ng_filter: NgWordFilter, value: str) -> None:
        text = "前" + value + "後"
        self.assertTrue(ng_filter.contains_ng_word(text))
        self.assertEqual(ng_filter.find_ng_words(text), [value])
        self.assertEqual(ng_filter.mask_ng_words(text), "前" + "●" * len(value) + "後")

    def test_kana_width_and_combining_dakuten_share_one_matching_form(self) -> None:
        ng_filter = self.make_filter("がくう")
        for value in ("がくう", "ガクウ", "ｶﾞｸｳ", "か\u3099くう", "ｶ\u200bﾞｸｳ", "カ\u200b\u3099クウ"):
            with self.subTest(value=repr(value)):
                self.assert_detected_span(ng_filter, value)

    def test_width_casefold_and_ligature_expansion_map_back_to_original_length(self) -> None:
        ng_filter = self.make_filter("架空paz\n架空strasse\n架空ffi語")
        for value in ("架空ＰＡＺ", "架空PAZ", "架空Straße", "架空STRAẞE", "架空ﬃ語"):
            with self.subTest(value=value):
                self.assert_detected_span(ng_filter, value)

    def test_zero_width_and_non_whitespace_controls_are_removed_before_composition(self) -> None:
        ng_filter = self.make_filter("がくう")
        for separator in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u2060", "\u034f", "\ufe0f", "\U000e0100", "\x00", "\x7f"):
            with self.subTest(separator=repr(separator)):
                self.assert_detected_span(ng_filter, "か" + separator + "\u3099くう")

    def test_spaces_symbols_and_leet_substitution_are_not_skipped(self) -> None:
        ng_filter = self.make_filter("がくう\n架空paz")
        for text in ("が くう", "が.くう", "がーくう", "が\nくう", "が\tくう", "が\r\nくう", "架空p4z"):
            with self.subTest(text=repr(text)):
                self.assertFalse(ng_filter.contains_ng_word(text))
                self.assertEqual(ng_filter.find_ng_words(text), [])
                self.assertEqual(ng_filter.mask_ng_words(text), text)

    def test_registered_words_are_normalized_and_deduplicated_in_first_seen_order(self) -> None:
        self.write(self.public, "架空ＰＡＺ\nがくう\n架空ＳＳ\n\u200b\n\x00\n", bom=False)
        self.write(self.private, "架空paz\nｶﾞｸｳ\n架空ß\n", bom=False)
        ng_filter = NgWordFilter()
        self.assertEqual(ng_filter.words, ["架空ＰＡＺ", "がくう", "架空ＳＳ"])
        self.assertEqual(ng_filter.word_count, 3)
        self.assertEqual(ng_filter.find_ng_words("架空ß がくう 架空PAZ 架空ß"), ["架空ß", "がくう", "架空PAZ"])
        ng_filter.words.clear()
        self.assertEqual(ng_filter.word_count, 3)

    def test_longest_normalized_match_and_duplicate_source_spans_are_fully_masked(self) -> None:
        ng_filter = self.make_filter("架空s\n架空ß")
        self.assert_detected_span(ng_filter, "架空SS")
        self.assert_detected_span(ng_filter, "架空ß")
        # 1文字が展開した複数の位置で一致しても、元の文字は1回・1文字分だけ伏せる。
        ng_filter = self.make_filter("s")
        self.assertEqual(ng_filter.mask_ng_words("前ß後"), "前●後")
        self.assertEqual(ng_filter.find_ng_words("前ß後"), ["ß"])

    def test_no_normalized_word_does_not_create_empty_pattern_matches(self) -> None:
        ng_filter = self.make_filter("\u200b\n\x00")
        text = "通常の会話です。\n😀 👩\u200d💻 ＡＢＣ カナ"
        self.assertEqual(ng_filter.word_count, 0)
        self.assertFalse(ng_filter.contains_ng_word(text))
        self.assertEqual(ng_filter.find_ng_words(text), [])
        self.assertEqual(ng_filter.mask_with_log(text, label="架空の文脈"), text)

    def test_matching_units_agree_with_stdlib_nfkc_for_composition_and_reordering(self) -> None:
        examples = (
            "か\u3099\u200bくう", "ｶ\u200bﾞｸｳ", "각", "ᄀ\u200bᅡᆨ", "\u09c7\u09be",
            "a\u0315\u0300", "Kßﬃ①ﾊﾞｶ", "ワ\u3099ヷわ\u3099", "Σςẞİ", "Ａ\u034fＢ",
        )
        for text in examples:
            with self.subTest(text=repr(text)):
                cleaned = "".join(character for character in text if not ng_word_service._is_ignored_character(character))
                expected = fold_kana(unicodedata.normalize("NFKC", cleaned))
                normalized, spans = ng_word_service._normalize_for_matching(text)
                self.assertEqual(normalized, expected)
                self.assertEqual(len(normalized), len(spans))
                self.assertTrue(all(0 <= start < end <= len(text) for start, end in spans))

    def test_compilation_is_not_repeated_for_each_message(self) -> None:
        with patch.object(ng_word_service.re, "compile", wraps=ng_word_service.re.compile) as compile_pattern:
            ng_filter = self.make_filter("架空paz\nがくう")
            for _ in range(3):
                ng_filter.contains_ng_word("架空ＰＡＺ")
                ng_filter.find_ng_words("ガクウ")
                ng_filter.mask_ng_words("ｶﾞｸｳ")
                ng_filter.mask_with_log("架空PAZ", label="架空の文脈")
            compile_pattern.assert_called_once()

    def test_log_and_mask_keep_one_snapshot_if_reload_happens_during_matching(self) -> None:
        ng_filter = self.make_filter("架空Ａ")
        original_ranges = ng_word_service._source_ranges

        def matching_with_reload(text, pattern):
            ranges = original_ranges(text, pattern)
            self.write(self.public, "架空Ｂ\n", bom=False)
            ng_filter.reload()
            return ranges

        with patch.object(ng_word_service, "_source_ranges", side_effect=matching_with_reload), \
                self.assertLogs(ng_word_service.logger, level="WARNING") as logging:
            masked = ng_filter.mask_with_log("架空aと架空b", label="架空の文脈")
        self.assertEqual(masked, "●●●と架空b")
        self.assertEqual([record.getMessage() for record in logging.records], ["NGワード検出: 1件をマスクしました。"])
        self.assertTrue(ng_filter.contains_ng_word("架空b"))


if __name__ == "__main__":
    unittest.main()
