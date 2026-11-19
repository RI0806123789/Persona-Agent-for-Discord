"""🧠 記憶を見る（#484）: 押した本人の記憶だけを、本人だけに見える HTML ファイルで返すことを確かめる。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import discord

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import MEMORY_GRAPH_FILENAME
from persona_bot.core.app_state import GuildSettings
from persona_bot.services.memory_graph import MemorySnapshot
from persona_bot.views.functions_view import FunctionsView, StatsPrivacyButton
from persona_bot.views.memory_graph_view import (
    MEMORY_GRAPH_BUSY_NOTICE,
    MEMORY_GRAPH_FAILURE_NOTICE,
    MEMORY_GRAPH_SEND_FAILURE_NOTICE,
    MemoryGraphButton,
)
from persona_bot.views.stats_privacy_view import StatsPrivacyView
from tests.helpers.discord_mocks import make_http_response, make_interaction, make_response
from tests.helpers.member_helpers import make_member


def snapshot(**overrides) -> MemorySnapshot:
    values = {"profile": {}, "projects": {}, "document_summaries": [], "pending_count": 0}
    values.update(overrides)
    return MemorySnapshot(**values)


class MemoryGraphButtonTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stats = SimpleNamespace(is_opted_out=Mock(return_value=False))
        self.memory = SimpleNamespace(load_user_snapshot=AsyncMock(
            return_value=snapshot(profile={"呼び名": "架空の太郎"}, pending_count=2),
        ))
        self.sent_files: list[tuple[str, bytes]] = []

    def panel(self, *, memory=True) -> StatsPrivacyView:
        view = StatsPrivacyView(
            self.stats, viewer_id="11", viewer_name="架空の本人", memory=self.memory if memory else None,
        )
        self.addCleanup(view.stop)
        return view

    def interaction(self, *, user_id: int = 11) -> SimpleNamespace:
        interaction = make_interaction(user=make_member(admin=False, user_id=user_id), guild_id=1, extras={})

        async def send(*args, **kwargs):
            # 送信後に送信側がファイルを閉じるので、呼ばれた時点で中身を控える。
            picture = kwargs.get("file")
            if picture is not None:
                self.sent_files.append((picture.filename, picture.fp.read()))

        interaction.followup.send.side_effect = send
        return interaction

    @staticmethod
    def button(view: StatsPrivacyView) -> MemoryGraphButton:
        return next(item for item in view.children if isinstance(item, MemoryGraphButton))

    async def test_button_and_explanation_appear_only_with_memory_service(self):
        without = self.panel(memory=False)
        self.assertFalse(any(isinstance(item, MemoryGraphButton) for item in without.children))
        self.assertNotIn("記憶を見る", without.build_preview())

        view = self.panel()
        self.assertEqual(self.button(view).row, 2)
        self.assertIn("🧠 記憶を見る", view.build_preview())
        # トグル・確定の組み直しでも残る。
        view.refresh_items()
        self.assertEqual(sum(isinstance(item, MemoryGraphButton) for item in view.children), 1)

    async def test_sends_only_the_pressers_memory_as_ephemeral_html(self):
        view = self.panel()
        interaction = self.interaction()
        await self.button(view).callback(interaction)

        self.memory.load_user_snapshot.assert_awaited_once_with("11")
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.followup.send.assert_awaited_once()
        args, kwargs = interaction.followup.send.await_args
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("記憶の項目: 1 件", args[0])
        self.assertIn("まとめる前の会話が 2 件", args[0])
        self.assertIn("他の人に渡さないでください", args[0])
        self.assertEqual(len(self.sent_files), 1)
        filename, content = self.sent_files[0]
        self.assertEqual(filename, MEMORY_GRAPH_FILENAME)
        html = content.decode("utf-8")
        self.assertIn("架空の太郎", html)
        # ファイルに利用者 ID・表示名を入れない。
        self.assertNotIn("架空の本人", html)
        self.assertTrue(kwargs["file"].fp.closed)

    async def test_no_memory_sends_notice_without_file(self):
        self.memory.load_user_snapshot.return_value = snapshot(pending_count=4)
        interaction = self.interaction()
        await self.button(self.panel()).callback(interaction)

        interaction.followup.send.assert_awaited_once()
        args, kwargs = interaction.followup.send.await_args
        self.assertIn("まだあなたについて覚えている記憶はありません", args[0])
        self.assertIn("まとめる前の会話が 4 件", args[0])
        self.assertEqual(kwargs, {"ephemeral": True})
        self.assertEqual(self.sent_files, [])

    async def test_load_failure_is_reported_only_to_the_presser(self):
        self.memory.load_user_snapshot.side_effect = OSError("架空の読み込み失敗")
        interaction = self.interaction()
        with self.assertLogs("persona_bot.views.view_parts", "ERROR") as logs:
            await self.button(self.panel()).callback(interaction)

        interaction.followup.send.assert_awaited_once_with(MEMORY_GRAPH_FAILURE_NOTICE, ephemeral=True)
        self.assertEqual(self.sent_files, [])
        self.assertNotIn("11", "\n".join(record.getMessage() for record in logs.records))

    async def test_send_failure_is_reported(self):
        interaction = self.interaction()
        failure = discord.HTTPException(make_http_response(500), "架空の送信失敗")
        interaction.followup.send.side_effect = [failure, None]
        with self.assertLogs("persona_bot.views.view_parts", "ERROR"):
            await self.button(self.panel()).callback(interaction)

        self.assertEqual(interaction.followup.send.await_count, 2)
        self.assertEqual(interaction.followup.send.await_args.args, (MEMORY_GRAPH_SEND_FAILURE_NOTICE,))
        self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_second_press_while_running_is_declined(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def load(user_id):
            entered.set()
            await release.wait()
            return snapshot(profile={"呼び名": "架空の太郎"})

        self.memory.load_user_snapshot.side_effect = load
        view = self.panel()
        first = asyncio.create_task(self.button(view).callback(self.interaction()))
        await asyncio.wait_for(entered.wait(), 2)
        second = self.interaction()
        await self.button(view).callback(second)
        second.response.send_message.assert_awaited_once_with(MEMORY_GRAPH_BUSY_NOTICE, ephemeral=True)
        release.set()
        await asyncio.wait_for(first, 2)
        self.memory.load_user_snapshot.assert_awaited_once()

        # 終わったら、もう一度押せる。
        third = self.interaction()
        await self.button(view).callback(third)
        third.followup.send.assert_awaited_once()


class HubMemoryWiringTests(unittest.IsolatedAsyncioTestCase):
    def hub(self, memory) -> FunctionsView:
        blacklist = SimpleNamespace(is_blocked=Mock(return_value=False))
        stats = SimpleNamespace(is_opted_out=Mock(return_value=False))
        hub = FunctionsView(GuildSettings(), Mock(), Mock(), blacklist, Mock(), Mock(), stats, Mock(), memory=memory)
        self.addCleanup(hub.stop)
        return hub

    async def test_hub_passes_memory_to_privacy_panel_and_clone(self):
        memory = SimpleNamespace(load_user_snapshot=AsyncMock())
        hub = self.hub(memory)
        self.assertIn("🧠 記憶の確認", hub.build_preview())
        response = make_response("is_done", "send_message", "edit_message")
        message = SimpleNamespace(edit=AsyncMock())
        interaction = make_interaction(
            user=make_member(admin=False), guild_id=1, extras={}, response=response,
            original_response=AsyncMock(return_value=message),
        )
        clone = hub.clone(interaction)
        self.addCleanup(clone.stop)
        self.assertIs(clone.memory, memory)

        self.assertTrue(await hub.interaction_check(interaction))
        await next(item for item in hub.children if isinstance(item, StatsPrivacyButton)).callback(interaction)
        panel = interaction.response.edit_message.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        self.assertIsInstance(panel, StatsPrivacyView)
        self.assertIs(panel.memory, memory)
        self.assertTrue(any(isinstance(item, MemoryGraphButton) for item in panel.children))

    async def test_hub_without_memory_keeps_previous_text(self):
        hub = self.hub(None)
        self.assertNotIn("記憶の確認", hub.build_preview())
        self.assertIn("・🔒 統計プライバシー … 会話統計の収集停止・データ削除（あなた自身の分だけ）", hub.build_preview())


if __name__ == "__main__":
    unittest.main()
