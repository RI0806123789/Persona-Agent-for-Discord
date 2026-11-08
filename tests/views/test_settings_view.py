"""会話リセットの操作時点を境界にした履歴取得を検証する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core.app_state import BotState
from persona_bot.utils.discord_helpers import fetch_channel_history_text
from persona_bot.views.settings_view import ResetButton, SettingsView
from tests.helpers.discord_mocks import ONLY_RESPONSE, make_channel, make_interaction, make_response


class ConversationResetTests(unittest.IsolatedAsyncioTestCase):
    async def check_timeline(self, guild_id, *, has_panel_message=True) -> None:
        state = BotState()
        settings = state.get_guild(guild_id)
        settings.channel_reset_points.update({10: 50, 20: 80})
        other_guild = state.get_guild(999)
        other_guild.channel_reset_points[10] = 70
        view = SettingsView(settings, 0, "未登録")
        self.addCleanup(view.stop)
        button = next(item for item in view.children if isinstance(item, ResetButton))
        messages = [
            SimpleNamespace(id=message_id, content=content, author=None, attachments=[])
            for message_id, content in (
                (90, "パネル作成前の発言"),
                (200, "パネル作成後・リセット前の発言"),
                (299, "リセット直前の発言"),
                (301, "リセット後の発言1"),
                (400, "リセット後の発言2"),
            )
        ]

        async def history(*, after, limit, oldest_first):
            # Discord と同じく after より後だけを返し、取得順と件数制限も再現する。
            filtered = [message for message in messages if after is None or message.id > after.id]
            for message in sorted(filtered, key=lambda m: m.id, reverse=not oldest_first)[:limit]:
                yield message

        channel = make_channel(history=Mock(side_effect=history))
        interaction = make_interaction(
            id=300,
            guild_id=guild_id,
            channel=channel,
            message=SimpleNamespace(id=100) if has_panel_message else None,
            response=make_response("edit_message"), without=ONLY_RESPONSE,
        )
        await button.callback(interaction)
        text = await fetch_channel_history_text(
            channel, settings.channel_reset_points[10], SimpleNamespace(id=99), limit=50
        )
        self.assertEqual(
            text,
            "ユーザーの質問: リセット後の発言1\nユーザーの質問: リセット後の発言2",
        )
        self.assertEqual(settings.channel_reset_points, {10: 300, 20: 80})
        self.assertEqual(other_guild.channel_reset_points, {10: 70})
        self.assertEqual(channel.history.call_args.kwargs["after"].id, 300)
        self.assertFalse(channel.history.call_args.kwargs["oldest_first"])
        interaction.response.edit_message.assert_awaited_once_with(
            content="🔄 会話履歴をリセットしました。ここから新しい会話を始めます。",
            view=None,
        )
        self.assertTrue(view.is_finished())
        if guild_id is not None:
            self.assertEqual(state.dm_settings.channel_reset_points, {})

    async def test_guild_reset_excludes_messages_before_button_press(self) -> None:
        await self.check_timeline(1)

    async def test_dm_reset_uses_the_same_timeline_boundary(self) -> None:
        await self.check_timeline(None)

    async def test_reset_does_not_require_a_panel_message(self) -> None:
        await self.check_timeline(1, has_panel_message=False)

    async def test_missing_channel_reports_error_without_changing_boundaries(self) -> None:
        settings = BotState().get_guild(1)
        settings.channel_reset_points[10] = 50
        view = SettingsView(settings, 0, "未登録")
        self.addCleanup(view.stop)
        button = next(item for item in view.children if isinstance(item, ResetButton))
        interaction = make_interaction(
            id=300, channel=None, message=SimpleNamespace(id=100),
            response=make_response("edit_message"), without=ONLY_RESPONSE,
        )
        await button.callback(interaction)
        self.assertEqual(settings.channel_reset_points, {10: 50})
        interaction.response.edit_message.assert_awaited_once_with(
            content="エラー: チャンネルが取得できませんでした。", view=None
        )
        self.assertTrue(view.is_finished())


if __name__ == "__main__":
    unittest.main()
