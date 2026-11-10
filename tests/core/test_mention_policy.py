"""本文由来の通知を既定と個別の送信境界で制限する（#278）。"""

import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import discord
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.voice_commands import announce_voice_join


class MentionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_defaults_block_body_mentions_but_keep_reply_notification(self) -> None:
        with patch("persona_bot.core.bot_client.close_genai_client", new=AsyncMock()):
            client = PersonaClient(genai_client=object(), intents=discord.Intents.none())
            try:
                self.assertEqual(client.allowed_mentions.to_dict(), {"parse": [], "replied_user": True})
                # 登録済み通知は明示した本人だけを許可し、既定より優先できる。
                notification = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=11)])
                merged = client.allowed_mentions.merge(notification).to_dict()
                self.assertEqual(merged["users"], [11])
                self.assertEqual(merged["parse"], [])
            finally:
                await client.close()

    async def test_explicit_client_mention_policy_is_preserved(self) -> None:
        policy = discord.AllowedMentions.none()
        with patch("persona_bot.core.bot_client.close_genai_client", new=AsyncMock()):
            client = PersonaClient(genai_client=object(), intents=discord.Intents.none(), allowed_mentions=policy)
            try:
                self.assertIs(client.allowed_mentions, policy)
            finally:
                await client.close()

    async def test_stage_failure_and_success_suppress_channel_name_mentions(self) -> None:
        channel = Mock(spec=discord.StageChannel)
        channel.name = "@everyone @here <@&22> <@33>"
        interaction = SimpleNamespace(guild=object(), followup=SimpleNamespace(send=AsyncMock()))
        for error in (None, RuntimeError("架空の昇格失敗")):
            with self.subTest(error=error):
                interaction.followup.send.reset_mock()
                with patch("persona_bot.core.voice_commands.enable_stage_speaker", new=AsyncMock(side_effect=error)):
                    await announce_voice_join(interaction, channel)
                interaction.followup.send.assert_awaited_once()
                self.assertIn(channel.name, interaction.followup.send.await_args.args[0])
                self.assertEqual(
                    interaction.followup.send.await_args.kwargs["allowed_mentions"].to_dict(), {"parse": []},
                )


class DirectSendPolicyTests(unittest.TestCase):
    def test_handler_replies_and_dynamic_public_command_sends_declare_mentions(self) -> None:
        """helperを経由しない送信のうち、入力を載せうる経路の指定漏れを検出する。"""
        for filename in ("message_handler.py", "message_admission.py", "message_routing.py", "message_response.py", "commands.py"):
            path = ROOT / "src/persona_bot/core" / filename
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                method = node.func.attr
                if method not in {"reply", "send", "send_message"}:
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords}
                ephemeral = keywords.get("ephemeral")
                if isinstance(ephemeral, ast.Constant) and ephemeral.value is True:
                    continue
                content = node.args[0] if node.args else keywords.get("content")
                if filename == "commands.py" and isinstance(content, ast.Constant):
                    # 固定文言には利用者の文字列が入らず、クライアントの既定も適用される。
                    continue
                with self.subTest(file=filename, line=node.lineno):
                    self.assertIn("allowed_mentions", keywords)


if __name__ == "__main__":
    unittest.main()
