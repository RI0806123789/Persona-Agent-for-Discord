"""説明文・不正候補・JSON例が判定結果を隠さないことを検証する。"""

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services.content_moderator import ContentModerator
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.memory_service import MemoryService
from persona_bot.utils.storage import extract_json_object


class JsonExtractionTests(unittest.TestCase):
    def test_scans_past_invalid_or_unclosed_braces(self):
        expected = {"safe": False, "reason": "架空の判定"}
        for prefix in ('注記 {invalid} ', '説明 {閉じていない ', '{"壊れた文字列: ', '{bad} {bad} '):
            with self.subTest(prefix=prefix):
                self.assertEqual(extract_json_object(prefix + json.dumps(expected)), expected)

    def test_nested_strings_braces_and_escaped_quotes_are_preserved(self):
        expected = {"nested": {"text": '引用 " と {括弧} と \\', "values": [1, {"key": 2}]}}
        self.assertEqual(extract_json_object("説明 {invalid} " + json.dumps(expected) + " 後書き"), expected)

    def test_oversized_integer_does_not_hide_later_result(self):
        # Python 3.11以降は整数の桁数制限でJSONDecodeError以外のValueErrorも発生する。
        text = '{"dummy": ' + '1' * 5000 + '} {"safe": false}'
        self.assertEqual(extract_json_object(text, required_keys=("safe",)), {"safe": False})

    def test_fenced_output_takes_priority_over_inline_example(self):
        for fence in ("```json\n", "```JSON\r\n", "```\n"):
            with self.subTest(fence=fence):
                text = '例 {"dummy": 1}\n' + fence + '{"safe": false}\n```\n後書き'
                self.assertEqual(extract_json_object(text), {"safe": False})

    def test_required_keys_skip_unrelated_valid_objects_and_fences(self):
        text = '```json\n{"dummy": {"safe": true}}\n```\n{"other": 2}\n{"safe": false}'
        self.assertEqual(extract_json_object(text, required_keys=("safe",)), {"safe": False})
        self.assertIsNone(extract_json_object('{"dummy": 1}', required_keys=("safe",)))

    def test_empty_and_invalid_inputs_return_none(self):
        for text in ("", "説明だけ", "{invalid}", "[]", "null"):
            with self.subTest(text=text):
                self.assertIsNone(extract_json_object(text))
        self.assertEqual(extract_json_object("{}"), {})


class ModerationJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_later_unsafe_result_is_used_by_both_entry_points(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        for text in (
            '{invalid} {"safe": false, "reason": "架空の判定"}',
            '{"dummy": 1} {"safe": false, "reason": "架空の判定"}',
            '例 {"dummy": 1}\n```json\n{"safe": false, "reason": "架空の判定"}\n```',
        ):
            with self.subTest(text=text), patch(
                "persona_bot.services.content_moderator.generate_content", new=AsyncMock(return_value=SimpleNamespace(text=text)),
            ):
                self.assertEqual(await moderator.check("架空の入力"), (False, "架空の判定"))
                safe, reason, category, reminder = await moderator.check_and_route("架空の入力")
                self.assertFalse(safe)
                self.assertEqual(reason, "架空の判定")
                self.assertEqual(category, "none")
                self.assertIsNone(reminder)

    async def test_no_usable_json_keeps_existing_fail_open_behavior(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        with patch("persona_bot.services.content_moderator.generate_content", new=AsyncMock(return_value=SimpleNamespace(text="{invalid}"))):
            self.assertEqual(await moderator.check("架空の入力"), (True, ""))


class ServiceJsonTests(unittest.IsolatedAsyncioTestCase):
    async def test_document_summary_skips_unrelated_json(self):
        for prefix in ('{"dummy": 1}', '```json\n{"dummy": 1}\n```'):
            with self.subTest(prefix=prefix), patch(
                "persona_bot.services.gemini_service.generate_content", new=AsyncMock(return_value=SimpleNamespace(
                    text=prefix + '\n{"summary": "架空の要約"}',
                )),
            ):
                result = await GeminiService(None).summarize_document(object(), "fictional.txt", "test-model")
                self.assertEqual(result["summary"], "架空の要約")

    async def test_router_skips_unrelated_json_and_normalizes_category(self):
        service = MemoryService(None, None)
        for value, expected in ((" PROFILE ", "profile"), (" Projects ", "projects"), (" unknown ", "none")):
            with self.subTest(value=value), patch(
                "persona_bot.services.memory_service.generate_content", new=AsyncMock(return_value=SimpleNamespace(
                    text='{"dummy": 1}\n' + json.dumps({"category": value}),
                )),
            ):
                self.assertEqual(await service.route_category("前に話した架空の計画を教えてください"), expected)

    async def test_compaction_skips_json_missing_either_required_key(self):
        service = MemoryService(None, None)
        expected = {"profile": {"趣味": "架空の工作"}, "projects": {"計画": "架空の計画"}}
        for prefix in ('{"dummy": 1}', '{"profile": {}}', '{"projects": {}}'):
            with self.subTest(prefix=prefix), patch(
                "persona_bot.services.memory_service.generate_content", new=AsyncMock(return_value=SimpleNamespace(
                    text=prefix + '\n' + json.dumps(expected),
                )),
            ):
                self.assertEqual(await service._compact_memory("架空の会話", "{}", "{}"),
                                 (expected["profile"], expected["projects"]))

    async def test_missing_required_fields_keep_existing_failures(self):
        with patch("persona_bot.services.gemini_service.generate_content", new=AsyncMock(
            return_value=SimpleNamespace(text='{"dummy": 1}'),
        )):
            with self.assertRaises(ValueError):
                await GeminiService(None).summarize_document(object(), "fictional.txt", "test-model")
        with patch("persona_bot.services.memory_service.generate_content", new=AsyncMock(
            return_value=SimpleNamespace(text='{"profile": {}}'),
        )):
            self.assertEqual(await MemoryService(None, None)._compact_memory("架空の会話", "{}", "{}"),
                             (None, None))

    async def test_combined_moderation_normalizes_category_and_recurrence(self):
        moderator = ContentModerator(None, SimpleNamespace(words=[]))
        for repeat, expected in ((" DAILY ", "daily"), (" Weekly ", "weekly"), (" Monthly ", "monthly"), ("unknown", "none"), (None, "none")):
            response = {"safe": True, "category": " PROFILE ", "reminder": {
                "at": "2026-12-01 12:00", "message": "架空の予定", "repeat": repeat,
            }}
            with self.subTest(repeat=repeat), patch(
                "persona_bot.services.content_moderator.generate_content", new=AsyncMock(return_value=SimpleNamespace(
                    text=json.dumps(response),
                )),
            ):
                safe, reason, category, reminder = await moderator.check_and_route("架空の依頼")
                self.assertTrue(safe)
                self.assertEqual(category, "profile")
                self.assertEqual(reminder["repeat"], expected)
