"""設定・NG再読込・要約の同期I/Oと公開投稿の失敗通知を確認する。"""

from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config import personas
from persona_bot.core.app_state import GuildSettings
from persona_bot.services import summary_service
from persona_bot.views import functions_view, settings_view, summary_view
from tests.helpers.discord_mocks import make_http_response, make_interaction, make_response
from tests.helpers.thread_probe import make_thread_probe
from tests.helpers.persona_helpers import install_persona_catalog


class ViewIOTests(unittest.IsolatedAsyncioTestCase):
    def interaction(self):
        return make_interaction(response=make_response("defer", "edit_message"), without=("original_response",))

    async def test_confirm_checks_path_in_worker_and_preserves_settings_when_missing(self):
        for exists in (True, False):
            with self.subTest(exists=exists):
                settings = GuildSettings()
                view = settings_view.SettingsView(settings, 0, "未登録")
                self.addCleanup(view.stop)
                view.temp_persona, view.temp_model = "../prompts/fictional-persona.txt", "fictional-model"
                interaction = self.interaction()
                operation, threads = make_thread_probe(exists)

                def resolve(*args):
                    # パス解決と存在確認の両方を、応答保留後にワーカーで行う。
                    self.assertTrue(interaction.response.defer.await_count)
                    operation()
                    return SimpleNamespace(is_file=lambda: exists)

                button = next(item for item in view.children if isinstance(item, settings_view.ConfirmButton))
                with patch.object(personas, "resolve_prompt_path", side_effect=resolve), patch.object(
                    settings, "persist", new_callable=AsyncMock,
                ) as persist:
                    await button.callback(interaction)
                self.assertTrue(threads)
                self.assertNotIn(threading.get_ident(), threads)
                interaction.edit_original_response.assert_awaited_once()
                if exists:
                    self.assertEqual(settings.current_prompt_file, "../prompts/fictional-persona.txt")
                    persist.assert_awaited_once()
                else:
                    self.assertNotEqual(settings.current_prompt_file, "../prompts/fictional-persona.txt")
                    persist.assert_not_awaited()
                    self.assertIn("見つかりません", interaction.edit_original_response.call_args.kwargs["content"])
                self.assertTrue(view.is_finished())

    async def test_ng_reload_defers_before_worker_and_sends_private_result(self):
        operation, threads = make_thread_probe(None)
        interaction = self.interaction()

        def reload():
            interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            operation()

        view = discord.ui.View()
        self.addCleanup(view.stop)
        view.ng_filter = SimpleNamespace(reload=Mock(side_effect=reload), word_count=7)
        button = functions_view.NgReloadButton()
        view.add_item(button)
        await button.callback(interaction)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)
        interaction.followup.send.assert_awaited_once()
        self.assertTrue(interaction.followup.send.call_args.kwargs["ephemeral"])
        self.assertIn("7件", interaction.followup.send.call_args.args[0])
        self.assertFalse(view.is_finished())

    async def test_summary_prompt_loading_keeps_loop_responsive_and_missing_file_falls_back(self):
        author = SimpleNamespace(bot=False, display_name="架空の利用者")
        message = SimpleNamespace(author=author, content="架空の会話", attachments=[], created_at=datetime.now(timezone.utc))
        install_persona_catalog(self)
        for missing in (False, True):
            with self.subTest(missing=missing):
                operation, threads = make_thread_probe("架空のペルソナ")
                loader = Mock(side_effect=FileNotFoundError() if missing else operation)
                gemini = SimpleNamespace(generate=AsyncMock(return_value=("架空の要約", None, [])))
                with patch.object(summary_service, "fetch_messages_since", new=AsyncMock(return_value=[message])), patch(
                    "persona_bot.config.paths.load_prompt_template", loader,
                ), patch.object(summary_service, "screen_bot_response", new=AsyncMock(side_effect=lambda text, *a, **k: text)):
                    ok, text = await summary_view.generate_channel_summary(
                        object(), None, summary_view._DEFAULT_PERIOD,
                        guild_settings=GuildSettings(current_prompt_file="../prompts/prompt_default.txt"),
                        gemini=gemini, moderator=None, ng_filter=None, usage=SimpleNamespace(log_snapshot=Mock()),
                    )
                self.assertTrue(ok)
                self.assertIn("架空の要約", text)
                if not missing:
                    self.assertTrue(threads)
                    self.assertNotIn(threading.get_ident(), threads)

    async def test_public_summary_reports_forbidden_and_partial_send_failure(self):
        errors = (
            (discord.Forbidden(make_http_response(403, "禁止"), "架空の権限不足"), "権限"),
            (discord.HTTPException(make_http_response(503, "障害"), "架空の障害"), "一部"),
            (None, "投稿しました"),
        )
        for error, notice in errors:
            with self.subTest(error=type(error).__name__):
                view = summary_view.SummaryView(GuildSettings(), None, None, None, None)
                self.addCleanup(view.stop)
                button = next(item for item in view.children if isinstance(item, summary_view.RunSummaryButton))
                interaction = self.interaction()
                interaction.channel = Mock(spec=discord.TextChannel)
                interaction.client = SimpleNamespace(user=None)
                with patch.object(summary_view, "generate_channel_summary", new=AsyncMock(return_value=(True, "架空の要約"))), patch.object(
                    summary_view, "send_long_message", new=AsyncMock(side_effect=error),
                ) as send:
                    await button.callback(interaction)
                send.assert_awaited_once()
                self.assertIn(notice, interaction.edit_original_response.call_args.kwargs["content"])
                self.assertTrue(view.is_finished())
