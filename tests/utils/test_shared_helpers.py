"""共通化した送信・権限・マスク処理と、調整定数の反映を検証する。"""

import asyncio
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.services import auto_topic_service
from persona_bot.services.auto_topic_service import AutoTopicService
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.core import commands
from persona_bot.services import content_moderator
from persona_bot.utils import discord_helpers
from persona_bot.utils.discord_helpers import (
    REPLY_ALLOWED_MENTIONS, build_history_text, fetch_channel_history_text, is_administrator, send_long_message,
    send_long_reply, split_message,
)
from persona_bot.views import functions_view
from persona_bot.views import help_view
from persona_bot.core.message_handler import MessageHandler
from persona_bot.services import ng_word_service
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services import reminder_service
from persona_bot.services.reminder_service import ReminderService
from persona_bot.views import reminder_view
from persona_bot.views import stats_view
from persona_bot.views import summary_view
from persona_bot.views import unblock_view
from persona_bot.views import view_parts
from tests.helpers.discord_mocks import make_channel, make_interaction as make_discord_interaction, make_message
from tests.helpers.member_helpers import make_member


def make_filter() -> NgWordFilter:
    # 実際の公開・非公開NGワードファイルは読まない。
    with patch.object(ng_word_service, "load_ng_words", return_value=["架空NG"]):
        return NgWordFilter()


class GroundingSourceMaskTests(unittest.TestCase):
    def test_source_titles_and_urls_are_masked_without_mutating_original(self):
        source = {"title": "架空NG[の資料]", "uri": "https://example.invalid/架空NG"}
        result = discord_helpers.format_grounding_sources([source], make_filter())
        self.assertNotIn("架空NG", result)
        self.assertIn("［の資料］", result)
        self.assertIn("https://example.invalid/", result)
        self.assertEqual(source["title"], "架空NG[の資料]")


class ChannelDisplayNameTests(unittest.TestCase):
    def test_missing_name_and_id_have_a_safe_label(self):
        for channel in (None, object(), SimpleNamespace(), SimpleNamespace(name=None, id=None)):
            with self.subTest(channel=channel):
                self.assertEqual(discord_helpers.channel_display_name(channel), "channel 不明")

    def test_existing_names_and_ids_keep_their_label(self):
        self.assertEqual(discord_helpers.channel_display_name(SimpleNamespace(name="架空の会場", id=12)), "架空の会場")
        self.assertEqual(discord_helpers.channel_display_name(SimpleNamespace(id=12)), "channel 12")


def make_interaction(*, user=None):
    # user を省略したテストは、従来どおり `interaction.user` が None の Interaction を使う。
    return make_discord_interaction(user=user)




class HistoryExclusionTests(unittest.IsolatedAsyncioTestCase):
    async def test_excluded_message_is_left_out_of_history(self) -> None:
        # 文書の元の投稿は、受付後に編集された本文がプロンプトへ入らないよう履歴から除く（#203）。
        messages = [
            SimpleNamespace(id=message_id, content=content, author=None, attachments=[])
            for message_id, content in ((3, "後の発言"), (2, "受付後に編集した架空の本文"), (1, "前の発言"))
        ]

        channel = make_channel(history_messages=messages)
        text = await fetch_channel_history_text(channel, None, SimpleNamespace(id=99), 50, exclude_message_id=2)
        self.assertEqual(text, "ユーザーの質問: 前の発言\nユーザーの質問: 後の発言")
        self.assertIn("受付後に編集した架空の本文", await fetch_channel_history_text(channel, None, SimpleNamespace(id=99), 50))


class LongMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_senders_skip_whitespace_only_text_and_trailing_chunks(self) -> None:
        for text in ("", " \n\t", "\n" * 4001, "あ" * 2000 + "\n   "):
            with self.subTest(text=repr(text[:10])):
                first = object()
                channel = make_channel()
                message = make_message(channel=channel, reply=AsyncMock(return_value=first))
                result = await send_long_reply(message, text)
                channel.send.assert_not_awaited()
                if text.strip():
                    self.assertIs(result, first)
                    message.reply.assert_awaited_once_with(
                        "あ" * 2000, suppress_embeds=False, allowed_mentions=REPLY_ALLOWED_MENTIONS,
                    )
                else:
                    self.assertIsNone(result)
                    message.reply.assert_not_awaited()
                await send_long_message(channel, text)
                if text.strip():
                    channel.send.assert_awaited_once_with("あ" * 2000, suppress_embeds=False, allowed_mentions=None)
                else:
                    channel.send.assert_not_awaited()

    async def test_senders_defend_against_whitespace_chunks_from_splitter(self) -> None:
        for chunks in (["\n ", "  架空の先頭\n", "\t", "\n架空の続き  ", " "], [" ", "\n"], []):
            with self.subTest(chunks=chunks):
                first = object()
                channel = make_channel()
                message = make_message(channel=channel, reply=AsyncMock(return_value=first))
                valid = [chunk for chunk in chunks if chunk.strip()]
                with patch.object(discord_helpers, "split_message", return_value=chunks):
                    result = await send_long_reply(message, "架空の本文", suppress_embeds=True)
                    if valid:
                        self.assertIs(result, first)
                        message.reply.assert_awaited_once_with(
                            valid[0], suppress_embeds=True, allowed_mentions=REPLY_ALLOWED_MENTIONS,
                        )
                    else:
                        self.assertIsNone(result)
                        message.reply.assert_not_awaited()
                    self.assertEqual([call.args[0] for call in channel.send.await_args_list], valid[1:])
                    channel.send.reset_mock()
                    mentions = discord.AllowedMentions.none()
                    await send_long_message(channel, "架空の本文", suppress_embeds=True, allowed_mentions=mentions)
                self.assertEqual([call.args[0] for call in channel.send.await_args_list], valid)
                for call in channel.send.await_args_list:
                    self.assertEqual(call.kwargs, {"suppress_embeds": True, "allowed_mentions": mentions})

    async def test_long_message_propagates_options_to_every_chunk(self) -> None:
        channel = make_channel()
        mentions = discord.AllowedMentions.none()
        await send_long_message(channel, "あ" * 4001, suppress_embeds=True, allowed_mentions=mentions)
        self.assertEqual([len(call.args[0]) for call in channel.send.call_args_list], [2000, 2000, 1])
        for call in channel.send.call_args_list:
            self.assertEqual(call.kwargs, {"suppress_embeds": True, "allowed_mentions": mentions})

    async def test_reply_returns_first_message_and_sends_remaining_chunks_in_order(self) -> None:
        first = object()
        message = make_message(reply=AsyncMock(return_value=first))
        result = await send_long_reply(message, "あ" * 4001, suppress_embeds=True)
        self.assertIs(result, first)
        message.reply.assert_awaited_once_with("あ" * 2000, suppress_embeds=True, allowed_mentions=REPLY_ALLOWED_MENTIONS)
        self.assertEqual([len(call.args[0]) for call in message.channel.send.call_args_list], [2000, 1])

    async def test_reply_restricts_mentions_in_every_chunk_except_replied_user(self) -> None:
        # AI の本文に全体・ロール・第三者のメンションが混ざっても、通知は返信先本人だけに限る。
        text = "@everyone @here <@&22> <@33> " + "あ" * 4001
        message = make_message(reply=AsyncMock(return_value=object()))
        await send_long_reply(message, text)
        calls = [message.reply.await_args, *message.channel.send.await_args_list]
        self.assertGreater(len(calls), 1)
        for call in calls:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"parse": [], "replied_user": True})

    async def test_topic_and_anniversary_restrict_mentions_in_every_chunk(self) -> None:
        text = "あ" * 4001
        channel = SimpleNamespace(
            id=10, name="架空のチャンネル", send=AsyncMock(), guild=SimpleNamespace(me=object()),
            permissions_for=Mock(return_value=SimpleNamespace(view_channel=True, send_messages=True)),
        )
        settings = auto_topic_service.GuildSettings(
            current_prompt_file="架空のパス", current_model_name="架空モデル",
            auto_topic_enabled=True, auto_topic_interval_hours=1,
            auto_topic_channels={10: datetime.now(timezone.utc) - timedelta(hours=2)},
        )
        service = AutoTopicService(
            SimpleNamespace(user=None), None, None,
            SimpleNamespace(generate=AsyncMock(return_value=(text, None, []))), None, None,
            SimpleNamespace(log_snapshot=Mock()), None,
        )
        service._screen_auto_post = AsyncMock(return_value=(text, True))
        member = SimpleNamespace(id=11, display_name="架空の利用者", mention="<@11>")
        with patch("persona_bot.config.paths.load_prompt_template", return_value="架空のペルソナ"), \
                patch.object(auto_topic_service, "fetch_channel_history_text", new=AsyncMock(return_value="")), \
                patch.object(auto_topic_service, "send_long_message", wraps=send_long_message, create=True) as sending:
            self.assertTrue(await service._post_topic(channel, settings))
            self.assertTrue(await service._post_anniversary(channel, settings, member, "誕生日"))
        self.assertEqual([call.args[1] for call in sending.call_args_list], [text, f"<@11> {text}"])
        for call in channel.send.call_args_list[:3]:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"parse": []})
        for call in channel.send.call_args_list[3:]:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"users": [11], "parse": []})

    async def test_reminder_keeps_mention_next_date_and_chunk_limits(self) -> None:
        channel = make_channel()
        db = SimpleNamespace(reminder_is_pending=Mock(return_value=True))
        service = ReminderService(SimpleNamespace(get_channel=Mock(return_value=channel)), db, make_filter())
        with patch.object(reminder_service, "send_long_message", wraps=send_long_message) as sending:
            await service._notify({
                "reminder_id": 1, "channel_id": 10, "user_id": "11", "message": "あ" * 4001,
                "next_remind_at": "2026-01-02T01:00:00+00:00",
            })
        sending.assert_awaited_once()
        body = sending.call_args.args[1]
        self.assertIn("⏰ <@11>", body)
        self.assertIn("🔁 次回は", body)
        self.assertTrue(all(len(call.args[0]) <= 2000 for call in channel.send.call_args_list))
        # 分割した全チャンクで、通知を許すのは依頼者本人だけ（#64）
        for call in channel.send.call_args_list:
            self.assertEqual(call.kwargs["allowed_mentions"].to_dict(), {"users": [11], "parse": []})

    async def test_reminder_body_cannot_ping_everyone_roles_or_other_users(self) -> None:
        channel = make_channel()
        db = SimpleNamespace(reminder_is_pending=Mock(return_value=True))
        service = ReminderService(SimpleNamespace(get_channel=Mock(return_value=channel)), db, make_filter())
        await service._notify({
            "reminder_id": 1, "channel_id": 10, "user_id": "11",
            "message": "@everyone @here <@&22> <@33> 架空の集合",
            "next_remind_at": "2026-01-02T01:00:00+00:00",
        })
        channel.send.assert_awaited_once()
        self.assertTrue(channel.send.call_args.args[0].startswith("⏰ <@11> "))
        self.assertEqual(channel.send.call_args.kwargs["allowed_mentions"].to_dict(), {"users": [11], "parse": []})

    async def test_summary_keeps_public_channel_and_private_followup_paths_separate(self) -> None:
        text = "あ" * 4001
        for private in (False, True):
            with self.subTest(private=private):
                view = view_parts.PanelView(timeout_notice="時間切れ")
                self.addCleanup(view.stop)
                for key, value in {
                    "temp_private": private, "temp_period": 1, "guild_settings": None,
                    "gemini": None, "moderator": None, "ng_filter": None, "usage": None,
                    "blocked_user_check": None,
                }.items():
                    setattr(view, key, value)
                button = summary_view.RunSummaryButton()
                view.add_item(button)
                interaction = make_interaction()
                interaction.channel = Mock(spec=discord.TextChannel, send=AsyncMock())
                interaction.client = SimpleNamespace(user=None)
                with patch.object(summary_view, "generate_channel_summary", new=AsyncMock(return_value=(True, text))), \
                        patch.object(summary_view, "send_long_message", wraps=send_long_message) as sending:
                    await button.callback(interaction)
                if private:
                    sending.assert_not_awaited()
                    interaction.channel.send.assert_not_awaited()
                    interaction.edit_original_response.assert_awaited_once_with(content=text[:2000], view=None)
                    self.assertEqual([len(call.args[0]) for call in interaction.followup.send.call_args_list], [2000, 1])
                    self.assertTrue(all(call.kwargs == {"ephemeral": True} for call in interaction.followup.send.call_args_list))
                else:
                    sending.assert_awaited_once()
                    self.assertEqual(sending.await_args.args, (interaction.channel, text))
                    self.assertEqual(sending.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []})
                    self.assertEqual(interaction.channel.send.await_count, 3)
                    self.assertTrue(all(
                        call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
                        for call in interaction.channel.send.call_args_list
                    ))
                    interaction.followup.send.assert_not_awaited()


class GraphSendingTests(unittest.IsolatedAsyncioTestCase):
    async def test_borrowed_discord_file_closes_underlying_buffer_once_even_after_sender_close(self):
        class CountingBuffer(io.BytesIO):
            def __init__(self):
                super().__init__(b"fictional-png")
                self.closes = 0

            def close(self):
                self.closes += 1
                super().close()

        for outcome in ("success", "failure", "cancel"):
            with self.subTest(outcome=outcome):
                buffer = CountingBuffer()
                interaction = make_interaction()

                async def send(*args, **kwargs):
                    # 実際のdiscord.Fileを借用する。discord.py側の終了処理も模擬する。
                    picture = kwargs.get("file")
                    if picture is None:
                        return
                    picture.close()
                    if outcome == "failure":
                        raise OSError("架空の送信失敗")
                    if outcome == "cancel":
                        raise asyncio.CancelledError

                interaction.response.send_message.side_effect = send
                if outcome == "cancel":
                    with self.assertRaises(asyncio.CancelledError):
                        await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空")
                else:
                    await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空")
                self.assertTrue(buffer.closed)
                self.assertEqual(buffer.closes, 1)

    async def test_initial_response_and_deferred_followup_both_close_buffer(self) -> None:
        for followup in (False, True):
            with self.subTest(followup=followup):
                interaction = make_interaction()
                buffer = io.BytesIO(b"fictional-png")
                options = {"followup": True} if followup else {}
                await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空のグラフ", **options)
                sender = interaction.followup.send if followup else interaction.response.send_message
                other = interaction.response.send_message if followup else interaction.followup.send
                sender.assert_awaited_once()
                self.assertEqual(sender.call_args.args, ("架空のグラフ",))
                self.assertTrue(sender.call_args.kwargs["ephemeral"])
                self.assertEqual(sender.call_args.kwargs["file"].filename, "fictional.png")
                other.assert_not_awaited()
                self.assertTrue(buffer.closed)

    async def test_send_failure_keeps_contextual_notice_and_closes_buffer(self) -> None:
        for followup in (False, True):
            with self.subTest(followup=followup):
                interaction = make_interaction()
                sender = interaction.followup.send if followup else interaction.response.send_message
                error = OSError("架空の送信失敗")
                sender.side_effect = [error, None]
                buffer = io.BytesIO(b"fictional-png")
                with self.assertLogs("persona_bot.views.view_parts", level="ERROR") as logging:
                    await view_parts.send_graph_ephemeral(
                        interaction, buffer, "fictional.png", "架空のグラフ", followup=followup,
                        error_log="架空の文脈", error_notice="架空のエラー案内",
                    )
                self.assertEqual(len(logging.records), 1)
                self.assertEqual(logging.records[0].getMessage(), "グラフの送信中にエラーが発生しました。")
                self.assertIs(logging.records[0].exc_info[1], error)
                self.assertNotIn("架空の文脈", logging.records[0].getMessage())
                self.assertNotIn("fictional.png", logging.records[0].getMessage())
                self.assertEqual(sender.await_args.args, ("架空のエラー案内",))
                self.assertEqual(sender.await_args.kwargs, {"ephemeral": True})
                self.assertTrue(buffer.closed)

    async def test_file_construction_failure_still_closes_buffer(self) -> None:
        buffer = io.BytesIO(b"fictional-png")
        interaction = make_interaction()
        with patch.object(view_parts.discord, "File", side_effect=OSError("架空の画像失敗")):
            await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空")
        self.assertTrue(buffer.closed)
        interaction.response.send_message.assert_awaited_once()
        self.assertNotIn("架空の画像失敗", interaction.response.send_message.call_args.args[0])

    async def test_cancellation_propagates_after_closing_buffer(self) -> None:
        interaction = make_interaction()
        interaction.followup.send.side_effect = asyncio.CancelledError()
        buffer = io.BytesIO(b"fictional-png")
        with self.assertRaises(asyncio.CancelledError):
            await view_parts.send_graph_ephemeral(interaction, buffer, "fictional.png", "架空", followup=True)
        self.assertTrue(buffer.closed)
        interaction.followup.send.assert_awaited_once()

    async def test_both_buttons_use_helper_and_keep_error_text(self) -> None:
        for module, button_class, notice in (
            (functions_view, functions_view.GraphButton, "エラー: グラフの送信に失敗しました。"),
            (stats_view, stats_view.RankingButton, "エラー: ランキングの送信に失敗しました。"),
        ):
            for failed in (False, True):
                with self.subTest(button=button_class.__name__, failed=failed):
                    buffer = io.BytesIO(b"fictional-png")
                    if button_class is stats_view.RankingButton:
                        # 全体比較の実行中の印は StatsView が持つ（#398）ので、実際のパネルに載せる
                        view = stats_view.StatsView(SimpleNamespace(
                            get_user_options=Mock(return_value=[]), build_ranking_graph=AsyncMock(return_value=buffer),
                        ), viewer_id="11", is_admin=True, feedback=None)
                        button = next(item for item in view.children if isinstance(item, button_class))
                    else:
                        view = discord.ui.View()
                        view.usage = SimpleNamespace(has_data=Mock(return_value=True), build_graph_buffer=AsyncMock(return_value=buffer))
                        button = button_class()
                        view.add_item(button)
                    self.addCleanup(view.stop)
                    interaction = make_interaction(user=make_member(admin=True))
                    if failed:
                        interaction.followup.send.side_effect = [OSError("架空の送信失敗"), None]
                    with patch.object(module, "send_graph_ephemeral", wraps=view_parts.send_graph_ephemeral) as sending:
                        await button.callback(interaction)
                    interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
                    interaction.response.send_message.assert_not_awaited()
                    sending.assert_awaited_once()
                    self.assertTrue(sending.call_args.kwargs["followup"])
                    self.assertTrue(buffer.closed)
                    if failed:
                        self.assertEqual(interaction.followup.send.await_args.args, (notice,))


class SharedMaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_output_paths_share_masking_and_keep_log_labels(self) -> None:
        ng_filter = make_filter()
        moderator = SimpleNamespace(check=AsyncMock(return_value=(True, "")))
        auto = AutoTopicService(None, None, None, None, moderator, ng_filter, None, None)
        autonomous = AutonomousService.__new__(AutonomousService)
        autonomous._ng_filter = ng_filter
        channel = make_channel()
        reminder = ReminderService(SimpleNamespace(get_channel=Mock(return_value=channel)), None, ng_filter)
        with patch.object(ng_filter, "mask_with_log", wraps=ng_filter.mask_with_log) as masking, \
                self.assertLogs("persona_bot.services.ng_word_service", level="WARNING") as logging:
            text = await content_moderator.screen_bot_response("架空NGの本文", ng_filter, moderator)
            automatic, _ = await auto._screen_auto_post("架空NGの本文[V:1, A:2]", "自発的な会話")
            speech = autonomous._screen_speech("架空NGの本文[V:1, A:2]", "自律行動の独り言")
            await reminder._notify({"channel_id": 10, "user_id": "11", "message": "架空NGの本文"})
        self.assertEqual((text, automatic, speech), ("●●●●の本文",) * 3)
        self.assertNotIn("架空NG", channel.send.call_args.args[0])
        labels = [call.kwargs["label"] for call in masking.call_args_list]
        self.assertEqual(labels, ["Bot応答", "自発的な会話", "自律行動の独り言", "リマインダー"])
        self.assertEqual([record.getMessage() for record in logging.records],
                         ["NGワード検出: 1件をマスクしました。"] * len(labels))
        self.assertTrue(all(call.args == ("●●●●の本文",) for call in moderator.check.call_args_list))

    async def test_safe_text_is_unchanged_and_does_not_log(self) -> None:
        ng_filter = make_filter()
        with patch("persona_bot.services.ng_word_service.logger.warning") as logging:
            self.assertEqual(ng_filter.mask_with_log("安全な本文", label="架空"), "安全な本文")
        logging.assert_not_called()


class AdministratorAndConstantsTests(unittest.IsolatedAsyncioTestCase):
    async def test_administrator_requires_a_member_with_server_permission(self) -> None:
        self.assertTrue(is_administrator(make_member(admin=True)))
        self.assertFalse(is_administrator(make_member(admin=False)))
        self.assertFalse(is_administrator(Mock(spec=discord.User)))
        self.assertFalse(is_administrator(SimpleNamespace(guild_permissions=SimpleNamespace(administrator=True))))

    async def test_ranking_and_unblock_recheck_permission_at_button_press(self) -> None:
        for module, button_class, helper, denied in (
            (functions_view, functions_view.UnblockButton, "is_bot_owner", "ボット所有者のみ"),
            (stats_view, stats_view.RankingButton, "is_administrator", "管理者のみ"),
        ):
            for user in (make_member(admin=False), Mock(spec=discord.User)):
                with self.subTest(button=button_class.__name__, user=type(user).__name__):
                    view = discord.ui.View()
                    self.addCleanup(view.stop)
                    view.blacklist = SimpleNamespace(get_all_blocked_users=Mock())
                    view.stats = SimpleNamespace(build_ranking_graph=AsyncMock())
                    button = button_class()
                    view.add_item(button)
                    interaction = make_interaction(user=user)
                    with patch.object(module, helper, return_value=False) as check:
                        await button.callback(interaction)
                    check.assert_called_once_with(interaction if helper == "is_bot_owner" else user)
                    interaction.response.defer.assert_not_awaited()
                    self.assertIn(denied, interaction.response.send_message.call_args.args[0])
                    view.blacklist.get_all_blocked_users.assert_not_called()
                    view.stats.build_ranking_graph.assert_not_awaited()

    async def test_functions_command_and_stats_panel_share_administrator_policy(self) -> None:
        callbacks = {}
        tree = SimpleNamespace(command=lambda **kwargs: lambda function: callbacks.setdefault(kwargs["name"], function))
        state = SimpleNamespace(get_guild=Mock(return_value=object()))
        commands.register_commands(tree, state, None, None,
                                   SimpleNamespace(is_blocked=Mock(return_value=False)),
                                   None, None, None, None, None)
        user = make_member(admin=True)
        interaction = make_interaction(user=user)
        interaction.guild_id = 1
        panel = SimpleNamespace(build_preview=Mock(return_value="架空"))
        with patch.object(commands, "FunctionsView", return_value=panel) as factory, \
                patch.object(commands, "is_administrator", wraps=is_administrator) as check:
            await callbacks["functions"](interaction)
        check.assert_called_once_with(user)
        self.assertTrue(factory.call_args.kwargs["is_admin"])
        view = discord.ui.View()
        self.addCleanup(view.stop)
        view.stats = SimpleNamespace(has_data=Mock(return_value=True))
        view.clone = Mock()
        button = functions_view.StatsButton()
        view.add_item(button)
        with patch.object(functions_view, "StatsView", return_value=panel) as factory, \
                patch.object(functions_view, "swap_panel", new=AsyncMock()), \
                patch.object(functions_view, "is_administrator", wraps=is_administrator) as check:
            await button.callback(interaction)
        check.assert_called_once_with(user)
        self.assertTrue(factory.call_args.kwargs["is_admin"])

    async def test_select_limits_and_labels_follow_constants(self) -> None:
        for module in (unblock_view, reminder_view, stats_view):
            with self.subTest(module=module.__name__), \
                    patch.object(module, "DISCORD_SELECT_MAX_OPTIONS", 2), \
                    patch.object(view_parts, "DISCORD_SELECT_LABEL_MAX_LENGTH", 4):
                if module is unblock_view:
                    select = module.UnblockSelect([{"user_id": i, "user_name": "あ" * 110} for i in range(3)])
                elif module is reminder_view:
                    select = module.ReminderCancelSelect(
                        [{"reminder_id": i, "message": "あ" * 110} for i in range(3)],
                        SimpleNamespace(describe=Mock(return_value="い" * 110)),
                    )
                    self.assertTrue(all(len(option.description) == 4 for option in select.options))
                else:
                    select = module.StatsUserSelect([(str(i), "あ" * 110, 1) for i in range(3)], "0")
                self.assertEqual(len(select.options), 2)
                self.assertTrue(all(len(option.label) == 4 for option in select.options))

    async def test_moderation_sampling_and_notice_use_the_same_limit(self) -> None:
        with patch.object(content_moderator, "MODERATION_NG_WORD_SAMPLE_LIMIT", 2):
            text, _contents = content_moderator._build_prompt("架空の質問", "Bot応答", ["語句A", "語句B", "語句C"])
            self.assertIn("語句A, 語句B", text)
            self.assertNotIn("語句C", text)
            self.assertIn("他にもあります", text)
            text, _contents = content_moderator._build_prompt("架空", "Bot応答", ["語句A", "語句B"])
            self.assertNotIn("他にもあります", text)

    async def test_help_and_block_notice_follow_configured_threshold(self) -> None:
        with patch.object(help_view, "BLACKLIST_MAX_VIOLATIONS", 5), patch.object(help_view, "VIEW_TIMEOUT_SECONDS", 30):
            text = help_view.build_help_text("架空のボット")
        self.assertIn("5回でボットが応答しなくなります", text)
        self.assertIn("30秒で時間切れ", text)
        handler = MessageHandler.__new__(MessageHandler)
        handler.blacklist = SimpleNamespace(record_violation=AsyncMock(return_value=(5, True)))
        message = SimpleNamespace(author=SimpleNamespace(id=11), reply=AsyncMock())
        with patch("persona_bot.services.violation_notice.BLACKLIST_MAX_VIOLATIONS", 5):
            await handler.notify_violation(message, "架空の警告")
        self.assertIn("違反が5回に達したため", message.reply.call_args.args[0])


class AttachmentLabelTests(unittest.TestCase):
    """会話履歴の添付表記と、対応形式の案内を確認する（#65）。"""

    @staticmethod
    def attachment(filename: str, content_type: str | None = None) -> SimpleNamespace:
        return SimpleNamespace(filename=filename, content_type=content_type)

    def history_line(self, *attachments) -> str:
        bot = SimpleNamespace(id=99)
        message = SimpleNamespace(
            author=SimpleNamespace(id=11), content="架空の発言", attachments=list(attachments),
        )
        return build_history_text([message], bot)

    def test_history_distinguishes_images_from_other_files(self) -> None:
        cases = {
            "画像のみ": ((self.attachment("a.png", "image/png"),), "ユーザーの質問: 架空の発言 [画像添付あり]"),
            "拡張子だけの画像": ((self.attachment("a.webp"),), "ユーザーの質問: 架空の発言 [画像添付あり]"),
            "文書のみ": ((self.attachment("a.pdf", "application/pdf"),), "ユーザーの質問: 架空の発言 [添付ファイルあり]"),
            "両方": ((self.attachment("a.png", "image/png"), self.attachment("a.txt")),
                     "ユーザーの質問: 架空の発言 [画像添付あり] [添付ファイルあり]"),
            "添付なし": ((), "ユーザーの質問: 架空の発言"),
        }
        for name, (attachments, expected) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.history_line(*attachments), expected)

    def test_supported_attachment_guide_lists_images_and_documents_from_constants(self) -> None:
        describe = discord_helpers.describe_supported_attachments
        self.assertEqual(describe(), "画像（gif / jpeg / jpg / png / webp）と文書（csv / md / pdf / txt）")
        self.assertEqual(describe(documents=False), "画像（gif / jpeg / jpg / png / webp）")


if __name__ == "__main__":
    unittest.main()
