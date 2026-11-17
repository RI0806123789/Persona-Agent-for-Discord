"""サーバー参加時の案内の投稿先と、WELCOME_MESSAGE_ENABLED に従うことを架空のサーバーで検証する。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.helpers.discord_mocks import make_channel as make_text_channel, make_guild as make_server
from persona_bot.utils import discord_channels
from persona_bot.utils.discord_helpers import pick_announcement_channel, post_welcome_message


def make_channel(channel_id: int, *, view: bool = True, send: bool = True) -> SimpleNamespace:
    return make_text_channel(
        channel_id=channel_id,
        permissions_for=lambda member: SimpleNamespace(view_channel=view, send_messages=send),
    )


def make_guild() -> SimpleNamespace:
    channel = make_channel(2)
    return make_server(me=object(), system_channel=channel, text_channels=[channel])


class AnnouncementChannelTests(unittest.TestCase):
    def test_channels_that_cannot_be_viewed_are_skipped(self) -> None:
        # #78: 送信の許可だけでは選ばず、閲覧もできるチャンネルを選ぶ。
        hidden_system = make_channel(2, view=False)
        hidden_text = make_channel(3, view=False)
        mute_text = make_channel(4, send=False)
        usable = make_channel(5)
        guild = SimpleNamespace(
            id=1, me=object(), system_channel=hidden_system,
            text_channels=[hidden_system, hidden_text, mute_text, usable],
        )
        self.assertIs(pick_announcement_channel(guild), usable)
        guild.text_channels = [hidden_system, hidden_text, mute_text]
        self.assertIsNone(pick_announcement_channel(guild))

    def test_usable_system_channel_is_preferred(self) -> None:
        system = make_channel(2)
        guild = SimpleNamespace(id=1, me=object(), system_channel=system, text_channels=[make_channel(3), system])
        self.assertIs(pick_announcement_channel(guild), system)


class WelcomeMessageSettingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        quiet = patch("persona_bot.utils.discord_channels.logger")
        quiet.start()
        self.addCleanup(quiet.stop)

    async def test_disabled_setting_skips_everything(self) -> None:
        # #74: 無効なら送信せず、投稿先も探さず、投稿済みにも数えない。
        guild = make_guild()
        posted: set[int] = set()
        with patch.object(discord_channels, "WELCOME_MESSAGE_ENABLED", False), \
                patch.object(discord_channels, "pick_announcement_channel") as pick:
            self.assertFalse(await post_welcome_message(guild, "架空の案内", posted))
        pick.assert_not_called()
        guild.system_channel.send.assert_not_awaited()
        self.assertEqual(posted, set())

    async def test_enabled_setting_posts_once_without_mentions(self) -> None:
        guild = make_guild()
        posted: set[int] = set()
        with patch.object(discord_channels, "WELCOME_MESSAGE_ENABLED", True):
            self.assertTrue(await post_welcome_message(guild, "架空の案内", posted))
            # 同じサーバーへの2回目（イベントの重複）は投稿しない。
            self.assertFalse(await post_welcome_message(guild, "架空の案内", posted))
        guild.system_channel.send.assert_awaited_once()
        args, kwargs = guild.system_channel.send.call_args
        self.assertEqual(args, ("架空の案内",))
        mentions = kwargs["allowed_mentions"]
        self.assertIsInstance(mentions, discord.AllowedMentions)
        self.assertEqual(mentions.to_dict(), discord.AllowedMentions.none().to_dict())
        self.assertEqual(posted, {1})


if __name__ == "__main__":
    unittest.main()
