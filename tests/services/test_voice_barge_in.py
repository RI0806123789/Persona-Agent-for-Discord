"""割り込み（バージイン）の文字起こしによる判定（#409）を確かめる。文字起こしは架空の文。"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services.voice_barge_in import (
    INTERRUPTION_ECHO, INTERRUPTION_FILLER, INTERRUPTION_SPEECH, RecentSpeech, classify_interruption,
    echo_overlap, estimate_word_count, is_echo_text, meaningful_tokens, normalize_speech_text,
)


class NormalizeTests(unittest.TestCase):
    def test_katakana_width_case_and_long_vowels_are_unified(self):
        self.assertEqual(normalize_speech_text("エーーット"), normalize_speech_text("えーっと"))
        self.assertEqual(normalize_speech_text("ｱｰ"), "あー")
        self.assertEqual(normalize_speech_text("あ〜〜"), "あー")
        self.assertEqual(normalize_speech_text("OK"), "ok")


class FillerTests(unittest.TestCase):
    def test_fillers_and_backchannels_are_not_words(self):
        for text in ("あー", "えーと", "えーーっと、あのー", "うんうん", "うーん…", "なるほどなるほど",
                     "はい、はい", "Uh, um", "あっ", "エート"):
            with self.subTest(text=text):
                self.assertEqual(meaningful_tokens(text), [])
                self.assertEqual(estimate_word_count(text), 0)
                self.assertEqual(classify_interruption(text, ""), INTERRUPTION_FILLER)

    def test_leading_filler_is_removed_but_the_content_counts(self):
        self.assertEqual(meaningful_tokens("えーと明日の予定"), ["明日の予定"])
        self.assertEqual(classify_interruption("えーと明日の予定", ""), INTERRUPTION_SPEECH)

    def test_word_like_fillers_are_kept_inside_words(self):
        # 「あの」「その」は語の一部でもあるので、語の頭からは取り除かない。
        self.assertEqual(meaningful_tokens("あの人"), ["あの人"])

    def test_multiple_words_are_needed(self):
        self.assertEqual(classify_interruption("ちょっと待って", ""), INTERRUPTION_SPEECH)
        self.assertEqual(classify_interruption("あの、すみません", ""), INTERRUPTION_SPEECH)
        self.assertEqual(classify_interruption("wait a second", ""), INTERRUPTION_SPEECH)
        self.assertEqual(classify_interruption("wait", ""), INTERRUPTION_FILLER)
        self.assertEqual(classify_interruption("ね", ""), INTERRUPTION_FILLER)
        self.assertEqual(classify_interruption("", ""), INTERRUPTION_FILLER)

    def test_word_count_mixes_scripts(self):
        self.assertEqual(estimate_word_count("明日"), 1)
        self.assertEqual(estimate_word_count("明日の天気"), 3)
        self.assertEqual(estimate_word_count("hello there"), 2)
        self.assertEqual(estimate_word_count("LINEで"), 2)


class EchoTests(unittest.TestCase):
    def test_own_words_coming_back_are_echo(self):
        bot = "今日はいい天気ですね。散歩に出かけるのはどうでしょう。"
        self.assertTrue(is_echo_text("散歩に出かけるのはどうでしょう", bot))
        self.assertEqual(classify_interruption("散歩に出かけるのはどうでしょう", bot), INTERRUPTION_ECHO)
        # カタカナ・ひらがなの違いも吸収する
        self.assertTrue(is_echo_text("サンポに出かけるのは", "さんぽに出かけるのは楽しい"))

    def test_different_words_and_short_answers_are_not_echo(self):
        bot = "今日はいい天気ですね。散歩に出かけるのはどうでしょう。"
        self.assertFalse(is_echo_text("それより明日の予定を教えて", bot))
        # 短い発言は回り込みかどうか判定しない（相手がボットの言葉を引用しただけのことがある）
        self.assertEqual(echo_overlap("天気", bot), 0.0)
        self.assertFalse(is_echo_text("散歩に出かける", ""))


class RecentSpeechTests(unittest.TestCase):
    def test_old_output_is_forgotten(self):
        recent = RecentSpeech(window_seconds=10)
        recent.add(0.0, "古い発話")
        recent.add(5.0, "新しい発話")
        self.assertEqual(recent.text(5.0), "古い発話新しい発話")
        self.assertEqual(recent.text(12.0), "新しい発話")
        recent.clear()
        self.assertEqual(recent.text(12.0), "")


if __name__ == "__main__":
    unittest.main()
