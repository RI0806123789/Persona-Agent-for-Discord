"""要約の途中で例外が起きても、パネルが「要約中...」のまま残らないことを確かめる（#77）。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import discord
from persona_bot.core.app_state import GuildSettings
from tests.helpers.discord_mocks import make_http_response, make_interaction, make_response
from tests.helpers.persona_helpers import install_persona_catalog
from persona_bot.services import summary_service
from persona_bot.services.prompting import SanitizedHistory
from persona_bot.utils.discord_helpers import SummarySource
from persona_bot.views import summary_view
from persona_bot.views.summary_view import RunSummaryButton, SummaryView


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(make_http_response(403, "Forbidden"), "架空の権限エラー")


class RunSummaryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        quiet = patch("persona_bot.views.summary_view.logger")
        quiet.start()
        self.addCleanup(quiet.stop)
        supported = patch.object(summary_view, "is_supported_message_channel", return_value=True)
        supported.start()
        self.addCleanup(supported.stop)
        self.generate = AsyncMock(return_value=(True, "📋 架空の要約"))
        generator = patch.object(summary_view, "generate_channel_summary", self.generate)
        generator.start()
        self.addCleanup(generator.stop)
        self.send_long = AsyncMock()
        sender = patch.object(summary_view, "send_long_message", self.send_long)
        sender.start()
        self.addCleanup(sender.stop)

        self.view = SummaryView(GuildSettings(), None, None, None, None)
        self.addCleanup(self.view.stop)
        self.button = next(item for item in self.view.children if isinstance(item, RunSummaryButton))
        self.interaction = make_interaction(
            channel=object(), client=SimpleNamespace(user=object()),
            response=make_response("edit_message"), without=("original_response",),
        )

    def last_panel(self) -> str:
        return self.interaction.edit_original_response.await_args.kwargs["content"]

    async def test_summary_uses_the_block_check_inherited_from_the_hub(self) -> None:
        check = Mock(return_value=False)
        self.view.blocked_user_check = check
        await self.button.callback(self.interaction)
        self.assertIs(self.generate.await_args.kwargs["is_blocked"], check)

    async def test_blank_summary_finishes_panel_without_public_or_private_post(self) -> None:
        for private in (False, True):
            with self.subTest(private=private):
                self.view.temp_private = private
                self.generate.return_value = (True, " \n\t")
                await self.button.callback(self.interaction)
                self.assertEqual(self.last_panel(), summary_view._FAILURE_NOTICE)
                self.send_long.assert_not_awaited()
                self.interaction.followup.send.assert_not_awaited()

    async def test_unexpected_generation_error_replaces_busy_panel(self) -> None:
        self.generate.side_effect = RuntimeError("架空の内部エラー")
        await self.button.callback(self.interaction)
        self.assertTrue(self.view.is_finished())
        self.assertEqual(self.last_panel(), summary_view._FAILURE_NOTICE)
        # 例外の文面（内部の情報）は利用者へ見せない。
        self.assertNotIn("架空の内部エラー", self.last_panel())
        self.send_long.assert_not_awaited()

    async def test_public_post_without_permission_suggests_private_display(self) -> None:
        self.send_long.side_effect = forbidden()
        await self.button.callback(self.interaction)
        self.assertEqual(self.last_panel(), summary_view._FORBIDDEN_NOTICE)

    async def test_every_public_summary_chunk_disables_all_mentions(self) -> None:
        from persona_bot.utils.discord_helpers import send_long_message

        text = "架空の要約 @everyone @here <@11> <@&22>" * 100
        channel = SimpleNamespace(send=AsyncMock())
        self.interaction.channel = channel
        self.generate.return_value = (True, text)
        with patch.object(summary_view, "send_long_message", send_long_message):
            await self.button.callback(self.interaction)
        self.assertGreater(channel.send.await_count, 1)
        self.assertEqual([call.args[0] for call in channel.send.await_args_list], summary_view.split_message(text))
        for call in channel.send.await_args_list:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(self.last_panel(), "✅ 要約をチャンネルに投稿しました。")

    async def test_private_display_keeps_shown_summary_and_reports_by_followup(self) -> None:
        self.view.temp_private = True
        self.generate.return_value = (True, "あ" * 2500)
        self.interaction.followup.send.side_effect = [RuntimeError("架空の送信失敗"), None]
        await self.button.callback(self.interaction)
        # 表示済みの1通目は上書きせず、失敗は本人だけの followup で伝える。
        self.interaction.edit_original_response.assert_awaited_once()
        self.assertEqual(len(self.last_panel()), 2000)
        self.assertEqual(self.interaction.followup.send.await_args.args, (summary_view._FAILURE_NOTICE,))
        self.assertTrue(self.interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_notice_falls_back_to_followup_when_panel_cannot_be_edited(self) -> None:
        self.generate.side_effect = RuntimeError("架空の内部エラー")
        self.interaction.edit_original_response.side_effect = forbidden()
        await self.button.callback(self.interaction)
        self.interaction.followup.send.assert_awaited_once_with(summary_view._FAILURE_NOTICE, ephemeral=True)

    async def test_notice_failure_does_not_escape(self) -> None:
        self.generate.side_effect = RuntimeError("架空の内部エラー")
        self.interaction.edit_original_response.side_effect = forbidden()
        self.interaction.followup.send.side_effect = forbidden()
        await self.button.callback(self.interaction)

    async def test_successful_paths_are_unchanged(self) -> None:
        await self.button.callback(self.interaction)
        self.send_long.assert_awaited_once()
        self.assertEqual(self.last_panel(), "✅ 要約をチャンネルに投稿しました。")
        self.generate.return_value = (False, "架空の案内")
        await self.button.callback(self.interaction)
        self.assertEqual(self.last_panel(), "架空の案内")


class SummaryGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        install_persona_catalog(self)

    async def test_initial_persona_and_model_are_kept_across_history_and_prompt_waits(self):
        for stage in ("history", "prompt"):
            with self.subTest(stage=stage):
                settings = GuildSettings()
                settings.current_prompt_file = "../prompts/prompt_default.txt"
                settings.current_model_name = "old-model"

                def change():
                    settings.current_prompt_file = "../prompts/prompt_maid.txt"
                    settings.current_model_name = "new-model"

                async def history(*args):
                    if stage == "history":
                        change()
                    return [object()]

                def prompt(path):
                    if stage == "prompt":
                        change()
                    return "架空のペルソナ"

                gemini = Mock(generate=AsyncMock(return_value=("架空の要約", None, [])))
                with patch.object(summary_service, "fetch_messages_since", AsyncMock(side_effect=history)), patch.object(
                    summary_service, "build_summary_source", return_value=SummarySource(SanitizedHistory("架空の発言"), 1),
                ), patch.object(summary_service, "load_persona_template", AsyncMock(side_effect=prompt)) as loader, patch.object(
                    summary_service, "screen_bot_response", AsyncMock(side_effect=lambda text, *args, **kwargs: text),
                ):
                    async def summarize():
                        return await summary_view.generate_channel_summary(
                            object(), object(), SimpleNamespace(value=1, name="1時間"), guild_settings=settings,
                            gemini=gemini, moderator=None, ng_filter=None, usage=Mock(),
                        )
                    self.assertTrue((await summarize())[0])
                    self.assertEqual(Path(loader.call_args.args[0]).name, "prompt_default.txt")
                    self.assertEqual(gemini.generate.await_args.args[1], "old-model")
                    self.assertTrue((await summarize())[0])
                    self.assertEqual(Path(loader.call_args.args[0]).name, "prompt_maid.txt")
                    self.assertEqual(gemini.generate.await_args.args[1], "new-model")

    async def test_empty_summary_after_tag_and_heading_removal_is_rejected(self):
        for text in (" \n\t", "  \n[V:+1, A:+1]", "### \n[V:0, A:0]"):
            with self.subTest(text=text), patch.object(summary_service, "fetch_messages_since", AsyncMock(return_value=[object()])), patch.object(
                summary_service, "build_summary_source", return_value=SummarySource(SanitizedHistory("架空の発言"), 1),
            ), patch.object(summary_service, "load_persona_template", AsyncMock(return_value="架空のペルソナ")), patch.object(
                summary_service, "screen_bot_response", AsyncMock(),
            ) as screen:
                result, _ = await summary_view.generate_channel_summary(
                    object(), object(), SimpleNamespace(value=1, name="1時間"), guild_settings=GuildSettings(),
                    gemini=Mock(generate=AsyncMock(return_value=(text, None, []))), moderator=None, ng_filter=None, usage=Mock(),
                )
                self.assertFalse(result)
                screen.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
