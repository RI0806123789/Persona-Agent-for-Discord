"""テスト用 Discord モックのファクトリ（tests/helpers/discord_mocks.py）の挙動を確かめる。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.helpers.discord_mocks import (
    ONLY_RESPONSE,
    RESPONSE_METHODS,
    make_channel,
    make_followup,
    make_guild,
    make_history,
    make_interaction,
    make_message,
    make_response,
)


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_response_has_every_method_and_is_not_done(self) -> None:
        response = make_response()
        self.assertEqual(set(vars(response)), set(RESPONSE_METHODS))
        await response.defer(ephemeral=True)
        response.defer.assert_awaited_once_with(ephemeral=True)
        self.assertFalse(response.is_done())

    def test_selected_methods_make_the_fake_strict(self) -> None:
        response = make_response("defer")
        self.assertEqual(set(vars(response)), {"defer"})
        with self.assertRaises(AttributeError):
            response.send_message  # noqa: B018 — 持たないメソッドは AttributeError になる

    def test_overrides_replace_defaults_and_add_new_names(self) -> None:
        failure = AsyncMock(side_effect=RuntimeError("架空の失敗"))
        response = make_response("defer", defer=failure, extra=1)
        self.assertIs(response.defer, failure)
        self.assertEqual(response.extra, 1)

    def test_unknown_method_name_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_response("defers")


class InteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_defaults_are_awaitable_and_independent_between_calls(self) -> None:
        first, second = make_interaction(), make_interaction()
        await first.followup.send("本文")
        await first.edit_original_response(content="更新")
        first.followup.send.assert_awaited_once_with("本文")
        second.followup.send.assert_not_awaited()
        self.assertIsNot(first.response, second.response)
        self.assertFalse(hasattr(first, "user"))

    def test_user_can_be_given_as_an_object_none_or_an_id(self) -> None:
        member = object()
        self.assertIs(make_interaction(user=member).user, member)
        self.assertIsNone(make_interaction(user=None).user)
        self.assertEqual(make_interaction(user_id=42).user.id, 42)
        self.assertIs(make_interaction(user=member, user_id=42).user, member)

    def test_response_accepts_a_namespace_or_a_dict_of_overrides(self) -> None:
        custom = make_response("defer")
        self.assertIs(make_interaction(response=custom).response, custom)
        failing = AsyncMock(side_effect=ValueError)
        interaction = make_interaction(response={"send_message": failing})
        self.assertIs(interaction.response.send_message, failing)
        self.assertTrue(hasattr(interaction.response, "defer"))

    def test_without_removes_default_attributes_and_rejects_unknown_names(self) -> None:
        minimal = make_interaction(user_id=1, without=ONLY_RESPONSE)
        self.assertEqual(set(vars(minimal)), {"user", "response"})
        partial = make_interaction(without=("followup",))
        self.assertFalse(hasattr(partial, "followup"))
        self.assertTrue(hasattr(partial, "original_response"))
        with self.assertRaises(ValueError):
            make_interaction(without=("response",))

    def test_attributes_add_and_override(self) -> None:
        guild = make_guild(guild_id=7)
        interaction = make_interaction(guild=guild, guild_id=7, extras={}, edit_original_response="上書き")
        self.assertIs(interaction.guild, guild)
        self.assertEqual((interaction.guild_id, interaction.extras), (7, {}))
        self.assertEqual(interaction.edit_original_response, "上書き")

    async def test_followup_override(self) -> None:
        followup = make_followup(send=AsyncMock(side_effect=RuntimeError("架空の失敗")))
        interaction = make_interaction(followup=followup)
        with self.assertRaises(RuntimeError):
            await interaction.followup.send("本文")


class ChannelGuildMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_has_send_and_accepts_extra_attributes(self) -> None:
        channel = make_channel(channel_id=5, name="架空の部屋", permissions_for=lambda member: "権限")
        await channel.send("本文", suppress_embeds=True)
        channel.send.assert_awaited_once_with("本文", suppress_embeds=True)
        self.assertEqual((channel.id, channel.name, channel.permissions_for(None)), (5, "架空の部屋", "権限"))
        self.assertFalse(hasattr(make_channel(), "name"))

    def test_guild_defaults_and_overrides(self) -> None:
        text = make_channel(channel_id=2)
        guild = make_guild(guild_id=3, system_channel=text, text_channels=[text])
        self.assertEqual((guild.id, guild.system_channel, guild.text_channels), (3, text, [text]))
        self.assertIsNone(guild.voice_client)
        self.assertIsNot(make_guild().me, make_guild().me)
        self.assertEqual(make_guild(name="架空のサーバー").name, "架空のサーバー")

    async def test_channel_history_yields_the_given_messages_in_order_and_records_calls(self) -> None:
        messages = [SimpleNamespace(id=3), SimpleNamespace(id=2), SimpleNamespace(id=1)]
        channel = make_channel(history_messages=messages)
        yielded = [item async for item in channel.history(limit=20, after=None, oldest_first=False)]
        self.assertEqual(yielded, messages)
        channel.history.assert_called_once_with(limit=20, after=None, oldest_first=False)

    async def test_channel_history_can_be_iterated_again_and_follows_the_list(self) -> None:
        # 呼ぶたびに新しい generator なので、同じチャンネルを続けて読んでも空にならない。
        messages = [SimpleNamespace(id=1)]
        channel = make_channel(history_messages=messages)
        self.assertEqual([item async for item in channel.history()], messages)
        later = SimpleNamespace(id=2)
        messages.append(later)
        self.assertEqual([item async for item in channel.history()], [*messages])
        self.assertIs([item async for item in channel.history()][-1], later)

    async def test_empty_history_messages_gives_an_empty_history_not_a_missing_one(self) -> None:
        channel = make_channel(history_messages=[])
        self.assertEqual([item async for item in channel.history(limit=5)], [])

    async def test_history_can_stop_with_a_failure_after_some_messages(self) -> None:
        # 取得の途中で失敗する履歴は、make_history を土台に generator を包んで作れる。
        first = SimpleNamespace(id=1)

        async def failing(**kwargs):
            yield first
            raise RuntimeError("架空の取得失敗")

        channel = make_channel(history=Mock(side_effect=failing))
        seen = []
        with self.assertRaises(RuntimeError):
            async for item in channel.history(limit=5):
                seen.append(item)
        self.assertEqual(seen, [first])

    def test_history_is_absent_unless_requested_and_not_combined_with_an_explicit_one(self) -> None:
        self.assertFalse(hasattr(make_channel(), "history"))
        self.assertIsNot(make_channel(history_messages=[]).history, make_channel(history_messages=[]).history)
        explicit = Mock()
        self.assertIs(make_channel(history=explicit).history, explicit)
        with self.assertRaises(ValueError):
            make_channel(history_messages=[], history=explicit)

    async def test_make_history_can_be_attached_to_a_channel_built_another_way(self) -> None:
        history = make_history([1, 2])
        channel = make_channel(history=history)
        self.assertEqual([item async for item in channel.history(limit=2)], [1, 2])
        self.assertEqual(history.call_count, 1)

    async def test_message_defaults_and_overrides(self) -> None:
        channel = make_channel()
        message = make_message(message_id=9, author_id=12, channel=channel, content="本文", mentions=[])
        await message.reply("返信")
        message.reply.assert_awaited_once_with("返信")
        self.assertIs(message.channel, channel)
        self.assertEqual((message.id, message.author.id, message.content, message.mentions), (9, 12, "本文", []))
        author = SimpleNamespace(id=1, bot=True)
        self.assertIs(make_message(author=author).author, author)
        self.assertIsNot(make_message().channel, make_message().channel)


if __name__ == "__main__":
    unittest.main()
