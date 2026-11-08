"""統計とフィードバックの初期応答、長文分割、本人限定の送信を確認する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.views.stats_view import FeedbackButton, RunStatsButton, StatsView
from tests.helpers.discord_mocks import make_interaction, make_response
from tests.helpers.member_helpers import make_member


class StatsPaginationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stats = SimpleNamespace(
            get_user_options=Mock(return_value=[("11", "架空の本人", 1)]),
            display_name=Mock(return_value="架空の本人"), build_dashboard=AsyncMock(),
        )
        self.feedback = SimpleNamespace(build_summary_text=AsyncMock(return_value="架空の集計"))
        self.view = StatsView(self.stats, viewer_id="11", is_admin=True, feedback=self.feedback)
        self.addCleanup(self.view.stop)
        self.interaction = make_interaction(
            user=make_member(admin=True), guild_id=1,
            response=make_response("defer", "edit_message", "send_message"), without=("original_response",),
        )

    def button(self, kind):
        return next(item for item in self.view.children if isinstance(item, kind))

    async def test_long_caption_is_split_without_losing_images_or_buffers(self):
        for with_image in (False, True):
            with self.subTest(with_image=with_image):
                text = "架" * 4001
                buffer = io.BytesIO(b"fictional-png")
                self.addCleanup(buffer.close)
                self.stats.build_dashboard.return_value = (text, [("stats.png", buffer)] if with_image else [])
                self.interaction.followup.send.reset_mock()
                await self.button(RunStatsButton).callback(self.interaction)
                first = self.interaction.edit_original_response.call_args.kwargs["content"]
                calls = self.interaction.followup.send.call_args_list
                text_calls = [call for call in calls if call.args]
                self.assertEqual([len(first), *[len(call.args[0]) for call in text_calls]], [2000, 2000, 1])
                self.assertEqual(first + "".join(call.args[0] for call in text_calls), text)
                self.assertTrue(all(call.kwargs["ephemeral"] for call in calls))
                image_calls = [call for call in calls if "files" in call.kwargs]
                self.assertEqual(len(image_calls), int(with_image))
                if with_image:
                    self.assertTrue(buffer.closed)
                    self.assertTrue(image_calls[0].kwargs["files"][0].fp.closed)

    async def test_caption_send_failure_still_releases_all_image_buffers(self):
        buffer = io.BytesIO(b"fictional-png")
        self.stats.build_dashboard.return_value = ("架" * 4001, [("stats.png", buffer)])
        self.interaction.followup.send.side_effect = OSError("架空の通信障害")
        await self.button(RunStatsButton).callback(self.interaction)
        self.assertTrue(buffer.closed)
        self.interaction.edit_original_response.assert_awaited_once_with(content="架" * 2000, view=None)
        self.assertIn("失敗", self.interaction.followup.send.call_args.args[0])
        self.assertTrue(self.interaction.followup.send.call_args.kwargs["ephemeral"])

    async def test_blank_caption_still_delivers_images_and_finishes_panel(self):
        buffer = io.BytesIO("架空の画像".encode())
        self.stats.build_dashboard.return_value = (" \n\t", [("stats.png", buffer)])
        await self.button(RunStatsButton).callback(self.interaction)
        self.assertEqual(self.interaction.edit_original_response.call_args.kwargs["content"], "表示できる統計の本文がありません。")
        self.interaction.followup.send.assert_awaited_once()
        self.assertIn("files", self.interaction.followup.send.call_args.kwargs)
        self.assertTrue(buffer.closed)

    async def test_blank_feedback_finishes_deferred_response_with_notice(self):
        self.feedback.build_summary_text.return_value = " \n\t"
        await self.button(FeedbackButton).callback(self.interaction)
        self.interaction.followup.send.assert_awaited_once_with("表示できるフィードバックの集計がありません。", ephemeral=True)

    async def test_feedback_defers_before_aggregation_and_splits_private_messages(self):
        text = "架" * 4001

        async def summary(guild_id):
            self.interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            self.assertEqual(guild_id, 1)
            await asyncio.sleep(0)
            return text

        self.feedback.build_summary_text.side_effect = summary
        await self.button(FeedbackButton).callback(self.interaction)
        calls = self.interaction.followup.send.call_args_list
        self.assertEqual([len(call.args[0]) for call in calls], [2000, 2000, 1])
        self.assertEqual("".join(call.args[0] for call in calls), text)
        self.assertTrue(all(call.kwargs["ephemeral"] for call in calls))
        self.interaction.response.send_message.assert_not_awaited()
        self.assertFalse(self.view.is_finished())

    async def test_feedback_rejects_non_admin_dm_and_disabled_service_without_aggregation(self):
        for condition in ("non-admin", "dm", "disabled"):
            with self.subTest(condition=condition):
                self.interaction.user.guild_permissions.administrator = condition != "non-admin"
                self.interaction.guild_id = None if condition == "dm" else 1
                self.view.feedback = None if condition == "disabled" else self.feedback
                await self.button(FeedbackButton).callback(self.interaction)
        self.feedback.build_summary_text.assert_not_awaited()
        self.interaction.response.defer.assert_not_awaited()
        self.assertTrue(all(call.kwargs["ephemeral"] for call in self.interaction.response.send_message.call_args_list))

    async def test_feedback_revoked_permission_during_query_does_not_send_result(self):
        async def summary(guild_id):
            self.interaction.user.fresh.guild_permissions.administrator = False
            return "公開してはいけない架空の集計"

        self.feedback.build_summary_text.side_effect = summary
        await self.button(FeedbackButton).callback(self.interaction)
        self.interaction.followup.send.assert_awaited_once_with("この機能はサーバー管理者のみ利用できます。", ephemeral=True)

    async def test_feedback_query_failure_finishes_deferred_response(self):
        self.feedback.build_summary_text.side_effect = OSError("架空の集計障害")
        await self.button(FeedbackButton).callback(self.interaction)
        self.interaction.response.defer.assert_awaited_once()
        self.assertIn("失敗", self.interaction.followup.send.call_args.args[0])
        self.assertTrue(self.interaction.followup.send.call_args.kwargs["ephemeral"])
