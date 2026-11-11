"""音声会話（#268）のモデレーション: 安全・不適切・判定不能の3段階と、感情の推定の相乗り。"""

import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from google.genai import types
from persona_bot.config.constants import LIVE_EMOTION_DELTA_MAX
from persona_bot.services.content_moderator import (
    VERDICT_SAFE, VERDICT_UNKNOWN, VERDICT_UNSAFE, ContentModerator, _VOICE_TURN_SYSTEM_PROMPT,
    _response_schema,
)
from persona_bot.services.prompting import build_live_system_instruction

TARGET = "persona_bot.services.content_moderator.generate_content"


def reply(payload) -> SimpleNamespace:
    return SimpleNamespace(text=json.dumps(payload))


class VoiceInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_verdicts_distinguish_unknown_from_safe(self):
        moderator = ContentModerator(None, SimpleNamespace(words=["架空のNG"]))
        cases = [
            (reply({"safe": True}), VERDICT_SAFE),
            (reply({"safe": False, "reason": "架空"}), VERDICT_UNSAFE),
            (reply({"safe": "false"}), VERDICT_UNSAFE),
            (reply({"safe": "yes"}), VERDICT_UNKNOWN),  # 曖昧な値では安全と確定しない
            (SimpleNamespace(text="JSONではない"), VERDICT_UNKNOWN),
            (asyncio.TimeoutError(), VERDICT_UNKNOWN),
            (RuntimeError("架空の通信障害"), VERDICT_UNKNOWN),
        ]
        for response, expected in cases:
            mock = AsyncMock(side_effect=response) if isinstance(response, BaseException) else AsyncMock(return_value=response)
            with self.subTest(expected=expected, response=type(response).__name__), patch(TARGET, mock):
                with self.assertNoLogs("persona_bot.services.content_moderator", level="CRITICAL"):
                    self.assertEqual(await moderator.judge_voice_input("架空の発言"), expected)

    async def test_empty_transcript_is_not_sent(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch(TARGET, AsyncMock()) as generate:
            self.assertEqual(await moderator.judge_voice_input("  "), VERDICT_UNKNOWN)
        generate.assert_not_awaited()

    async def test_prompt_blocked_counts_as_unsafe(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        blocked = types.GenerateContentResponse(
            prompt_feedback=types.GenerateContentResponsePromptFeedback(block_reason="SAFETY"),
        )
        with patch(TARGET, AsyncMock(return_value=blocked)):
            with self.assertLogs("persona_bot.services.content_moderator", level="WARNING"):
                self.assertEqual(await moderator.judge_voice_input("架空の発言"), VERDICT_UNSAFE)

    async def test_existing_check_still_fails_open(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch(TARGET, AsyncMock(return_value=reply({"safe": "yes"}))):
            self.assertEqual(await moderator.check("架空"), (True, ""))


class VoiceTurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_emotion_is_estimated_in_the_same_call_and_clamped(self):
        moderator = ContentModerator(None, SimpleNamespace(words=["架空のNG"]))
        response = reply({"safe": True, "valence_change": 99, "arousal_change": -3.4})
        with patch(TARGET, AsyncMock(return_value=response)) as generate:
            verdict, emotion = await moderator.judge_voice_turn("架空の質問", "架空の返答")
        self.assertEqual(verdict, VERDICT_SAFE)
        self.assertEqual(emotion, (LIVE_EMOTION_DELTA_MAX, -3))
        generate.assert_awaited_once()
        kwargs = generate.await_args.kwargs
        data = json.loads(kwargs["contents"].splitlines()[1])
        # 判定対象はボットの返答、参加者の発言は推定の材料（history）
        self.assertEqual(data["text"], "架空の返答")
        self.assertEqual(data["history"], "架空の質問")
        self.assertNotIn("架空のNG", kwargs["system_instruction"])
        self.assertIn("valence_change", kwargs["response_schema"]["required"])

    async def test_no_emotion_unless_safe_and_numeric(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        cases = [
            (reply({"safe": False, "valence_change": 5, "arousal_change": 5}), VERDICT_UNSAFE),
            (reply({"safe": True, "valence_change": "5", "arousal_change": 1}), VERDICT_SAFE),
            (reply({"safe": True, "valence_change": True, "arousal_change": 1}), VERDICT_SAFE),
            (reply({"safe": True}), VERDICT_SAFE),
        ]
        for response, expected in cases:
            with self.subTest(payload=response.text), patch(TARGET, AsyncMock(return_value=response)):
                with self.assertNoLogs("persona_bot.services.content_moderator", level="ERROR"):
                    self.assertEqual(await moderator.judge_voice_turn("q", "a"), (expected, None))

    async def test_empty_reply_is_not_judged(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch(TARGET, AsyncMock()) as generate:
            self.assertEqual(await moderator.judge_voice_turn("q", ""), (VERDICT_UNKNOWN, None))
        generate.assert_not_awaited()

    def test_output_examples_follow_the_schema(self):
        schema = _response_schema(emotion=True)
        examples = [json.loads(line) for line in _VOICE_TURN_SYSTEM_PROMPT.splitlines() if line.startswith('{"safe":')]
        self.assertEqual(len(examples), 2)
        for example in examples:
            self.assertTrue(set(schema["required"]).issubset(example))
        types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema)


class LiveInstructionTests(unittest.TestCase):
    def test_persona_is_kept_and_speech_rules_override_tags(self):
        text = build_live_system_instruction("架空のペルソナ", grounding=False)
        self.assertTrue(text.startswith("架空のペルソナ"))
        self.assertIn("感情タグ", text)
        self.assertIn("複数の人", text)
        self.assertNotIn("検索", text)
        self.assertIn("検索", build_live_system_instruction("架空のペルソナ", grounding=True))

    def test_mistaken_interruption_is_answered_with_a_confirmation(self):
        """誤って割り込んだとき（言いよどみ・相槌だけ）は、続けてよいか確認させる（#409 の決定事項）。"""
        text = build_live_system_instruction("架空のペルソナ", grounding=False)
        self.assertIn("遮られた", text)
        self.assertIn("続きをしてよいか短く確認", text)


if __name__ == "__main__":
    unittest.main()
