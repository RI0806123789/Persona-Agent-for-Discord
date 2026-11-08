"""NG再読込が失敗した場合、操作した本人へ従来設定の保持を案内する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.views.functions_view import NgReloadButton
from tests.helpers.discord_mocks import make_interaction, make_response


class NgReloadFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_error_reports_failure_privately_without_closing_hub(self) -> None:
        for error in (PermissionError("架空の読取失敗"), UnicodeError("架空の文字コード不正")):
            with self.subTest(error=type(error).__name__):
                view = discord.ui.View()
                self.addCleanup(view.stop)
                view.ng_filter = SimpleNamespace(reload=Mock(side_effect=error), word_count=3)
                button = NgReloadButton()
                view.add_item(button)
                interaction = make_interaction(
                    response=make_response("defer"), without=("edit_original_response", "original_response"),
                )
                await button.callback(interaction)
                interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                interaction.followup.send.assert_awaited_once()
                notice = interaction.followup.send.call_args
                self.assertTrue(notice.kwargs["ephemeral"])
                self.assertIn("再読み込みに失敗", notice.args[0])
                self.assertIn("従来の設定を保持", notice.args[0])
                self.assertNotIn("✅", notice.args[0])
                self.assertFalse(view.is_finished())
