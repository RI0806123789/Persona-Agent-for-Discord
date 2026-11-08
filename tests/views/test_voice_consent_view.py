"""音声会話（#268）の同意パネル。押した本人の同意だけを切り替え、本人だけに結果を返す。"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import discord
from tests.helpers.discord_mocks import (
    ONLY_RESPONSE, make_http_response, make_interaction as make_discord_interaction, make_response,
)
from persona_bot.services.voice_conversation import ConsentState
from persona_bot.views.voice_consent_view import VoiceConsentView, build_consent_panel_text


class FakeConversation:
    def __init__(self):
        self.consent = ConsentState()
        self.stopped = False
        self.is_open = False
        self.waiting_count = 2

    def grant_voice(self, user_id):
        self.consent.voice.add(user_id)

    def withdraw(self, user_id):
        self.consent.voice.discard(user_id)
        self.consent.memory.discard(user_id)

    def toggle_memory(self, user_id):
        self.consent.memory.symmetric_difference_update({user_id})
        return user_id in self.consent.memory

    def toggle_affinity(self, user_id):
        self.consent.affinity.symmetric_difference_update({user_id})
        return user_id in self.consent.affinity


def make_interaction(user_id=11):
    return make_discord_interaction(user_id=user_id, response=make_response("send_message"), without=ONLY_RESPONSE)


class PanelTextTests(unittest.TestCase):
    def test_states_show_counts_without_names(self):
        conversation = FakeConversation()
        self.assertIn("同意待ち: 2人", build_consent_panel_text(conversation))
        conversation.is_open = True
        self.assertIn("会話中", build_consent_panel_text(conversation))
        conversation.stopped = True
        self.assertIn("終了しました", build_consent_panel_text(conversation))

    def test_explains_what_is_sent_and_kept(self):
        text = build_consent_panel_text(FakeConversation())
        for phrase in ("Google の Gemini Live", "保存しません", "初期はオフ", "撤回"):
            self.assertIn(phrase, text)
        self.assertLessEqual(len(text), 2000)


class ConsentViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conversation = FakeConversation()
        self.blocked = set()
        self.view = VoiceConsentView(self.conversation, is_blocked=lambda user_id: user_id in self.blocked)

    async def press(self, name, interaction):
        button = getattr(self.view, name)
        await button.callback(interaction)

    async def test_buttons_change_only_the_pressers_consent_and_reply_privately(self):
        interaction = make_interaction()
        await self.press("grant", interaction)
        self.assertEqual(self.conversation.consent.voice, {11})
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("音声会話 同意済み", interaction.response.send_message.await_args.args[0])
        await self.press("memory", interaction)
        self.assertEqual(self.conversation.consent.memory, {11})
        await self.press("affinity", interaction)
        self.assertEqual(self.conversation.consent.affinity, {11})
        await self.press("withdraw", interaction)
        self.assertEqual(self.conversation.consent.voice, set())
        self.assertEqual(self.conversation.consent.memory, set())

    async def test_blacklisted_user_is_rejected(self):
        self.blocked.add(11)
        interaction = make_interaction()
        await self.press("grant", interaction)
        self.assertEqual(self.conversation.consent.voice, set())
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_finished_conversation_does_not_accept_consent(self):
        self.conversation.stopped = True
        interaction = make_interaction()
        await self.press("grant", interaction)
        self.assertEqual(self.conversation.consent.voice, set())
        self.assertIn("終了", interaction.response.send_message.await_args.args[0])

    async def test_reply_failure_keeps_the_accepted_change(self):
        interaction = make_interaction()
        interaction.response.send_message.side_effect = discord.HTTPException(
            make_http_response(500, "架空"), "架空の失敗",
        )
        with self.assertLogs("persona_bot.views.voice_consent_view", level="WARNING"):
            await self.press("grant", interaction)
        self.assertEqual(self.conversation.consent.voice, {11})

    def test_view_does_not_time_out(self):
        self.assertIsNone(self.view.timeout)


if __name__ == "__main__":
    unittest.main()
