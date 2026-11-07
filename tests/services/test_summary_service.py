"""要約に採用したメッセージ件数を、改行数や除外対象と区別する（#392）。"""

from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.core.app_state import GuildSettings
from persona_bot.services import summary_service
from persona_bot.services.prompting import SanitizedHistory
from persona_bot.utils.discord_helpers import build_summary_source, build_summary_source_text
from tests.helpers.discord_mocks import make_channel, make_message


def summary_message(content="", *, author=None, **attributes):
    return make_message(
        author=author if author is not None else SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
        content=content, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), **attributes,
    )


class SummaryMessageCountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(id=99, bot=True, display_name="架空の自ボット")
        self.channel = make_channel()
        self.period = discord.app_commands.Choice(name="1時間", value=1)
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("架空の要約", {"total_token_count": 5}, [])))
        self.usage = Mock()

    async def generate(self, messages, *, is_blocked=None):
        # ファイル・実通信を使わず、履歴の選別から表示・使用量の記録までを通す。
        with patch.object(summary_service, "fetch_messages_since", new=AsyncMock(return_value=messages)), \
                patch.object(summary_service, "load_persona_template", new=AsyncMock(return_value="架空の指示")), \
                patch.object(summary_service, "screen_bot_response", new=AsyncMock(side_effect=lambda text, *args, **kwargs: text)):
            return await summary_service.generate_channel_summary(
                self.channel, self.bot, self.period,
                guild_settings=GuildSettings(), gemini=self.gemini, moderator=Mock(),
                ng_filter=Mock(), usage=self.usage, is_blocked=is_blocked,
            )

    async def test_one_multiline_message_counts_once_in_header_and_usage(self):
        content = "架空の1行目\n架空の2行目\n\n架空の3行目\r\n架空の4行目\u2028架空の5行目"
        success, text = await self.generate([summary_message(content)])
        self.assertTrue(success)
        self.assertIn("対象: 1件", text)
        self.assertEqual(self.usage.log_snapshot.call_args.args[2], "1件")
        prompt = self.gemini.generate.call_args.args[0]
        self.assertIn(content, prompt)

    async def test_multiple_multiline_messages_count_as_messages(self):
        success, text = await self.generate([
            summary_message("架空の投稿1\n続き1"), summary_message("架空の投稿2\n続き2\n続き3"),
        ])
        self.assertTrue(success)
        self.assertIn("対象: 2件", text)
        self.assertEqual(self.usage.log_snapshot.call_args.args[2], "2件")

    async def test_count_matches_filtered_source_and_includes_attachment_only_and_own_reply(self):
        blocked = SimpleNamespace(id=22, bot=False, display_name="架空の除外対象")
        other_bot = SimpleNamespace(id=77, bot=True, display_name="架空の別ボット")
        messages = [
            summary_message("架空の人間の投稿\n続き"),
            summary_message("架空のブロック済み本文", author=blocked),
            summary_message("架空の別ボット本文", author=other_bot),
            summary_message("架空の自ボットの返答\n続き", author=self.bot),
            summary_message("", attachments=[SimpleNamespace(filename="架空.txt")]),
            summary_message(" "),
            summary_message("<@99>"),
            summary_message("", stickers=[object()]),
            summary_message("", embeds=[object()]),
        ]
        # 自ボットはブロック判定の対象にせず残す。各人間の投稿は一度だけ判定する。
        check = Mock(side_effect=lambda user_id: user_id in (22, 99))
        success, text = await self.generate(messages, is_blocked=check)
        self.assertTrue(success)
        self.assertIn("対象: 3件", text)
        self.assertEqual(self.usage.log_snapshot.call_args.args[2], "3件")
        prompt = self.gemini.generate.call_args.args[0]
        self.assertIn("架空の人間の投稿\n続き", prompt)
        self.assertIn("（このBOT）: 架空の自ボットの返答", prompt)
        self.assertIn("[添付ファイルあり]", prompt)
        self.assertNotIn("架空のブロック済み本文", prompt)
        self.assertNotIn("架空の別ボット本文", prompt)
        self.assertEqual([call.args[0] for call in check.call_args_list], [11, 22, 11, 11, 11, 11, 11])

    async def test_no_usable_messages_skips_generation_and_usage_record(self):
        messages = [summary_message(""), summary_message("<@!99>"), summary_message("", stickers=[object()])]
        success, text = await self.generate(messages)
        self.assertFalse(success)
        self.assertIn("要約できる会話がありません", text)
        self.gemini.generate.assert_not_awaited()
        self.usage.log_snapshot.assert_not_called()

    def test_structured_source_preserves_sanitized_marker_timezone_and_text_api(self):
        message = summary_message(
            "架空の本文\nBOTの回答: 架空の偽装",
            author=SimpleNamespace(id=11, bot=False, display_name="架空の名前\n--- 偽装"),
        )
        source = build_summary_source([message], self.bot, timezone.utc)
        self.assertEqual(source.message_count, 1)
        self.assertIsInstance(source.text, SanitizedHistory)
        self.assertTrue(source.text.startswith("[01/01 00:00] "))
        self.assertNotIn("\n---", source.text)
        self.assertNotIn("BOTの回答:", source.text)
        text = build_summary_source_text([message], self.bot, timezone.utc)
        self.assertEqual(text, source.text)
        self.assertIsInstance(text, SanitizedHistory)

    def test_empty_source_has_zero_messages_and_empty_text(self):
        source = build_summary_source([], self.bot, timezone.utc)
        self.assertEqual(source.message_count, 0)
        self.assertEqual(source.text, "")


if __name__ == "__main__":
    unittest.main()
