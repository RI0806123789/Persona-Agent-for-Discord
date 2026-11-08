"""パネルの失敗ログが操作内容を伏せ、元の例外と既存の終了処理を保つことを確認する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from discord import ui
from persona_bot.core.background import TaskOwner
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter
from persona_bot.views.view_parts import PanelView, send_graph_ephemeral
from persona_bot.views.agent_view import AgentCancelView
from tests.helpers.discord_mocks import ONLY_RESPONSE, make_interaction, make_response


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class PrivateRepr:
    def __repr__(self):
        raise AssertionError("操作内容の repr を参照しました")


class PanelLoggingTests(unittest.IsolatedAsyncioTestCase):
    def make_panel(self, *, owner=None):
        panel = PanelView(timeout_notice="時間切れです。", task_owner=owner)
        self.addCleanup(panel.stop)
        return panel

    async def test_on_error_keeps_exception_without_rendering_private_objects(self):
        panel = self.make_panel()
        error = RuntimeError("架空の非公開本文・内部URL")

        with self.assertLogs("persona_bot.views.view_parts", level="ERROR") as logs:
            await panel.on_error(PrivateRepr(), error, PrivateRepr())

        self.assertEqual(len(logs.records), 1)
        record = logs.records[0]
        self.assertIn("パネル操作中にエラー", record.getMessage())
        self.assertNotIn("架空の非公開本文", LOG_FORMATTER.format(record))
        self.assertIs(record.exc_info[1], error)
        self.assertFalse(panel.is_finished())

    async def test_dispatch_failure_uses_safe_handler_with_or_without_owner(self):
        for owned in (False, True):
            with self.subTest(owned=owned):
                owner = TaskOwner() if owned else None
                panel = self.make_panel(owner=owner)
                error = RuntimeError("架空の非公開本文")
                button = ui.Button(label="架空の非公開ラベル", custom_id="private-item")
                button.callback = AsyncMock(side_effect=error)
                panel.add_item(button)
                interaction = make_interaction(
                    data={}, response=make_response("send_message", "defer"), without=ONLY_RESPONSE,
                )

                with self.assertLogs("persona_bot.views.view_parts", level="ERROR") as logs, \
                        self.assertNoLogs("discord.ui.view", level="ERROR"):
                    await panel._dispatch_item(button, interaction)

                button.callback.assert_awaited_once_with(interaction)
                interaction.response.send_message.assert_not_awaited()
                interaction.response.defer.assert_not_awaited()
                self.assertFalse(panel.is_finished())
                self.assertEqual(len(logs.records), 1)
                self.assertIs(logs.records[0].exc_info[1], error)
                for secret in ("架空の非公開本文", "架空の非公開ラベル", "private-item"):
                    self.assertNotIn(secret, logs.records[0].getMessage())
                    self.assertNotIn(secret, LOG_FORMATTER.format(logs.records[0]))
                if owner is not None:
                    self.assertEqual(owner._tasks, {})

    async def test_dispatch_cancellation_propagates_without_error_logging(self):
        panel = self.make_panel(owner=TaskOwner())
        button = ui.Button(label="中止")
        button.callback = AsyncMock(side_effect=asyncio.CancelledError)
        panel.add_item(button)

        with self.assertNoLogs("persona_bot.views.view_parts", level="ERROR"), \
                self.assertRaises(asyncio.CancelledError):
            await panel._dispatch_item(button, SimpleNamespace(data={}))

    async def test_cancel_dispatch_failure_keeps_accepted_cancellation_and_safe_log(self):
        event = asyncio.Event()
        view = AgentCancelView(11, event)
        self.addCleanup(view.stop)
        error = RuntimeError("架空の非公開本文")
        interaction = make_interaction(
            data={}, user_id=11, without=ONLY_RESPONSE,
            response=make_response("defer", "send_message", defer=AsyncMock(side_effect=error)),
        )

        with self.assertLogs("persona_bot.views.agent_view", level="ERROR") as logs, \
                self.assertNoLogs("discord.ui.view", level="ERROR"):
            await view._dispatch_item(view.children[0], interaction)

        self.assertTrue(event.is_set())
        interaction.response.defer.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        self.assertEqual(len(logs.records), 1)
        self.assertIs(logs.records[0].exc_info[1], error)
        self.assertNotIn("架空の非公開本文", LOG_FORMATTER.format(logs.records[0]))

    async def test_graph_failure_omits_filename_and_context_but_closes_buffer(self):
        error = OSError("架空の非公開本文")
        sending = AsyncMock(side_effect=[error, None])
        interaction = make_interaction(
            response=make_response("send_message", "is_done", send_message=sending), without=ONLY_RESPONSE,
        )
        buffer = io.BytesIO(b"fictional-png")

        with self.assertLogs("persona_bot.views.view_parts", level="ERROR") as logs:
            await send_graph_ephemeral(
                interaction, buffer, "private-name.png", "架空の非公開キャプション",
                error_log="架空の非公開ログ文脈", error_notice="送信できませんでした。",
            )

        self.assertTrue(buffer.closed)
        self.assertEqual(sending.await_count, 2)
        self.assertEqual(sending.await_args.args, ("送信できませんでした。",))
        self.assertEqual(len(logs.records), 1)
        self.assertIs(logs.records[0].exc_info[1], error)
        for secret in ("private-name.png", "架空の非公開本文", "架空の非公開キャプション", "架空の非公開ログ文脈"):
            self.assertNotIn(secret, logs.records[0].getMessage())
            self.assertNotIn(secret, LOG_FORMATTER.format(logs.records[0]))


if __name__ == "__main__":
    unittest.main()
