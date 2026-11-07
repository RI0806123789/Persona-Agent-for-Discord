"""設定読込を起動せず、SDKの一括削除イベントとボットの委譲を確かめる。"""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord
from discord.state import ConnectionState


class FeedbackEventWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_bulk_clear_events_reach_feedback_callbacks(self) -> None:
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / "src/bot.py").read_text(encoding="utf-8-sig"))
        names = {"on_raw_reaction_clear", "on_raw_reaction_clear_emoji"}
        handlers = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
        self.assertEqual({node.name for node in handlers}, names)
        registered = {}

        def register(callback):
            registered[callback.__name__] = callback
            return callback

        feedback = SimpleNamespace(handle_reaction_clear=AsyncMock(), handle_reaction_clear_emoji=AsyncMock())
        namespace = {"client": SimpleNamespace(event=register), "feedback": feedback, "discord": discord}
        # イベント関数だけを定義し、bot.pyの設定読込・DB初期化・起動を実行しない。
        exec(compile(ast.Module(body=handlers, type_ignores=[]), "bot-events", "exec"), namespace)
        pending = []

        def dispatch(name, payload):
            pending.append(asyncio.create_task(registered["on_" + name](payload)))

        state = SimpleNamespace(dispatch=dispatch, _get_message=lambda _: None)
        data = {"message_id": "500", "channel_id": "10", "guild_id": "1"}
        ConnectionState.parse_message_reaction_remove_all(state, data)
        ConnectionState.parse_message_reaction_remove_emoji(state, {**data, "emoji": {"id": None, "name": "👎"}})
        await asyncio.gather(*pending)
        feedback.handle_reaction_clear.assert_awaited_once()
        feedback.handle_reaction_clear_emoji.assert_awaited_once()
        self.assertEqual(feedback.handle_reaction_clear.await_args.args[0].message_id, 500)
        self.assertEqual(str(feedback.handle_reaction_clear_emoji.await_args.args[0].emoji), "👎")


if __name__ == "__main__":
    unittest.main()
