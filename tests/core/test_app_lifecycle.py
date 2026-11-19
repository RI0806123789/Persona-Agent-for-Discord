"""起動時の状態復元と終了時の後片付けを確かめる（#68）。"""

import ast
import asyncio
from contextlib import closing as closing_connection
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core import bot_client
from persona_bot.core.app_state import BotState
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.background import make_guarded_loop, run_in_thread, spawn, wait_for_cleanup
from persona_bot.core.database import Database
from persona_bot.config.choices import MODEL_CHOICES


class StartBackgroundOnceTests(unittest.TestCase):
    """on_ready が再接続のたびに発火しても、見回りループを二重起動しない（#414）。"""

    def test_starts_once_and_keeps_the_existing_flag_names(self) -> None:
        state = BotState()
        start = Mock()
        self.assertTrue(state.start_background_once("memory_background_started", start))
        self.assertTrue(state.memory_background_started)
        self.assertFalse(state.start_background_once("memory_background_started", start))
        start.assert_called_once_with()
        # 別のフラグは独立している
        other = Mock()
        self.assertTrue(state.start_background_once("document_worker_started", other))
        self.assertTrue(state.document_worker_started)
        self.assertFalse(state.reminder_background_started)

    def test_failed_start_does_not_set_the_flag_so_the_next_ready_retries(self) -> None:
        state = BotState()
        with self.assertRaises(RuntimeError):
            state.start_background_once("autonomous_background_started", Mock(side_effect=RuntimeError("架空の起動失敗")))
        self.assertFalse(state.autonomous_background_started)
        self.assertTrue(state.start_background_once("autonomous_background_started", Mock()))

    def test_unknown_flag_name_is_not_silently_ignored(self) -> None:
        start = Mock()
        with self.assertRaises(AttributeError):
            BotState().start_background_once("memory_background_startd", start)
        start.assert_not_called()


class SyncCommandsOnceTests(unittest.IsolatedAsyncioTestCase):
    """スラッシュコマンドの同期は、on_ready が重複して届いても1回だけ行う（#477）。"""

    async def test_overlapping_calls_sync_only_once(self) -> None:
        state = BotState()
        started, gate = asyncio.Event(), asyncio.Event()

        async def slow_sync() -> None:
            started.set()
            await gate.wait()

        sync = AsyncMock(side_effect=slow_sync)
        first = asyncio.create_task(state.sync_commands_once(sync))
        await started.wait()
        # 1回目の通信待ちの間に、2回目の on_ready が届く
        second = asyncio.create_task(state.sync_commands_once(sync))
        await asyncio.sleep(0)
        self.assertFalse(state.commands_synced)
        gate.set()
        self.assertEqual(await asyncio.gather(first, second), [True, False])
        sync.assert_awaited_once()
        self.assertTrue(state.commands_synced)

    async def test_failed_sync_leaves_the_flag_unset_so_the_next_ready_retries(self) -> None:
        state = BotState()
        sync = AsyncMock(side_effect=[RuntimeError("架空の通信失敗"), None])
        with self.assertRaises(RuntimeError):
            await state.sync_commands_once(sync)
        self.assertFalse(state.commands_synced)
        self.assertTrue(await state.sync_commands_once(sync))
        self.assertTrue(state.commands_synced)
        self.assertEqual(sync.await_count, 2)
        # 済んだ後は何もしない
        self.assertFalse(await state.sync_commands_once(sync))
        self.assertEqual(sync.await_count, 2)

    async def test_stopping_prevents_a_sync_even_after_waiting_for_the_lock(self) -> None:
        state = BotState()
        stopping = [False]
        gate = asyncio.Event()

        async def failing_sync() -> None:
            await gate.wait()
            raise RuntimeError("架空の通信失敗")

        first_sync, second_sync = AsyncMock(side_effect=failing_sync), AsyncMock()
        first = asyncio.create_task(state.sync_commands_once(first_sync, is_stopping=lambda: stopping[0]))
        await asyncio.sleep(0)  # 1回目がロックを持って通信を待つ
        second = asyncio.create_task(state.sync_commands_once(second_sync, is_stopping=lambda: stopping[0]))
        await asyncio.sleep(0)
        stopping[0] = True  # ロックを待つ間に終了が始まる
        gate.set()
        with self.assertRaises(RuntimeError):
            await first
        self.assertFalse(await second)
        second_sync.assert_not_awaited()
        self.assertFalse(state.commands_synced)
        # 終了中なら、最初から同期を始めない
        self.assertFalse(await state.sync_commands_once(second_sync, is_stopping=lambda: True))
        second_sync.assert_not_awaited()


class OnReadyTests(unittest.IsolatedAsyncioTestCase):
    """bot.py の on_ready（#477）。bot.py は import せず、関数だけを取り出して差し替えた部品で動かす。"""

    def load_on_ready(self, *, stopping: bool = False, ensure_storage=None):
        root = Path(__file__).resolve().parents[2]
        tree = ast.parse((root / "src/bot.py").read_text(encoding="utf-8-sig"))
        node = next(item for item in tree.body if isinstance(item, ast.AsyncFunctionDef) and item.name == "on_ready")
        registered = {}

        def register(callback):
            registered[callback.__name__] = callback
            return callback

        self.client = SimpleNamespace(
            event=register, user="架空のボット", background_tasks=SimpleNamespace(stopping=stopping),
        )
        self.loop = SimpleNamespace(start=Mock())
        self.documents = SimpleNamespace(start_worker=Mock())
        self.tree = SimpleNamespace(sync=AsyncMock())
        self.state = BotState()
        self.memory = SimpleNamespace(io_lock=asyncio.Lock(), ensure_storage=ensure_storage or Mock())
        namespace = {
            "client": self.client, "logger": Mock(), "memory": self.memory, "run_in_thread": run_in_thread,
            "state": self.state, "documents": self.documents, "tree": self.tree,
            "_periodic_loops": [("memory_background_started", self.loop)],
        }
        # on_ready だけを定義し、bot.py の設定読込・DB初期化・起動を実行しない。
        exec(compile(ast.Module(body=[node], type_ignores=[]), "bot-on-ready", "exec"), namespace)
        return registered["on_ready"]

    async def test_ready_starts_background_work_and_syncs_commands_once(self) -> None:
        on_ready = self.load_on_ready()
        await on_ready()
        await on_ready()  # 再接続での重複発火
        self.memory.ensure_storage.assert_called()
        self.loop.start.assert_called_once_with()
        self.documents.start_worker.assert_called_once_with()
        self.tree.sync.assert_awaited_once()
        self.assertTrue(self.state.commands_synced)

    async def test_overlapping_ready_events_sync_commands_once(self) -> None:
        on_ready = self.load_on_ready()
        started, gate = asyncio.Event(), asyncio.Event()

        async def slow_sync() -> None:
            started.set()
            await gate.wait()

        self.tree.sync.side_effect = slow_sync
        first = asyncio.create_task(on_ready())
        await started.wait()
        second = asyncio.create_task(on_ready())
        # 2回目は保存先の準備を終えて、1回目の同期の完了を待つ
        for _ in range(300):
            if self.memory.ensure_storage.call_count >= 2:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        self.assertFalse(second.done())
        gate.set()
        await asyncio.gather(first, second)
        self.tree.sync.assert_awaited_once()

    async def test_failed_sync_is_retried_by_the_next_ready(self) -> None:
        on_ready = self.load_on_ready()
        self.tree.sync.side_effect = [RuntimeError("架空の通信失敗"), None]
        with self.assertRaises(RuntimeError):
            await on_ready()
        self.assertFalse(self.state.commands_synced)
        await on_ready()
        self.assertTrue(self.state.commands_synced)
        self.assertEqual(self.tree.sync.await_count, 2)
        self.loop.start.assert_called_once_with()

    async def test_shutdown_during_storage_preparation_stops_before_loops_and_sync(self) -> None:
        holder = {}

        def ensure_storage() -> None:
            # 保存先の準備を待つ間に、終了が始まる
            holder["client"].background_tasks.stopping = True

        on_ready = self.load_on_ready(ensure_storage=Mock(side_effect=ensure_storage))
        holder["client"] = self.client
        await on_ready()
        self.memory.ensure_storage.assert_called_once()
        self.loop.start.assert_not_called()
        self.documents.start_worker.assert_not_called()
        self.tree.sync.assert_not_awaited()
        self.assertFalse(self.state.commands_synced)
        self.assertFalse(self.state.memory_background_started)

    async def test_ready_after_shutdown_started_does_nothing(self) -> None:
        on_ready = self.load_on_ready(stopping=True)
        await on_ready()
        self.memory.ensure_storage.assert_not_called()
        self.loop.start.assert_not_called()
        self.tree.sync.assert_not_awaited()


class RestoreAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_settings_created_before_restore_are_persisted(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(db.close)
        with patch("persona_bot.core.database.logger"):
            db.initialize()
        state = BotState()
        early = state.get_guild(1)  # 復元より前に作られた設定
        state.restore_from_database(db)
        model_name = MODEL_CHOICES[0].value
        early.current_model_name = model_name
        await early.persist()
        restored = BotState()
        restored.restore_from_database(db)
        self.assertEqual(restored.get_guild(1).current_model_name, model_name)


class ClientCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_shutdown_has_a_deadline_without_inherited_owner(self) -> None:
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        release = asyncio.Event()
        stop = AsyncMock(side_effect=release.wait)
        client.add_shutdown_callback(stop)
        with patch.object(discord.Client, "close", new=AsyncMock()) as discord_close, \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as gemini_close, \
                patch.object(bot_client, "BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS", 0.01):
            task = asyncio.create_task(client._shutdown())
            try:
                with self.assertLogs("persona_bot.core.background", level="ERROR"):
                    done, _ = await asyncio.wait({task}, timeout=1)
                    self.assertIn(task, done)
                    with self.assertRaises(TimeoutError):
                        await task
                self.assertIn(client._service_stop_task, client.background_tasks._tasks)
                self.assertFalse(client._service_stop_task.cancelled())
                discord_close.assert_not_awaited()
                gemini_close.assert_not_awaited()
                client._database.close.assert_not_called()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await client.background_tasks.shutdown(timeout=1)
            await client.close()
            stop.assert_awaited_once()
            discord_close.assert_awaited_once()
            gemini_close.assert_awaited_once()
            client._database.close.assert_called_once()

    async def test_unfinished_service_is_not_awaited_after_background_drain(self) -> None:
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        release = asyncio.Event()
        stop = AsyncMock(side_effect=release.wait)
        client.add_shutdown_callback(stop)
        with patch.object(discord.Client, "close", new=AsyncMock()) as discord_close, \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as gemini_close, \
                patch.object(bot_client, "BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS", 0.01), \
                patch.object(client.background_tasks, "shutdown", new=AsyncMock()):
            # 通常処理の回収が戻った場合も、終了通知自身の完了を別に確認する。
            task = asyncio.create_task(client._shutdown())
            try:
                done, _ = await asyncio.wait({task}, timeout=1)
                self.assertIn(task, done)
                with self.assertRaises(TimeoutError):
                    await task
                discord_close.assert_not_awaited()
                gemini_close.assert_not_awaited()
                client._database.close.assert_not_called()
                self.assertFalse(client._service_stop_task.cancelled())
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
                await client._service_stop_task
            await client.close()
            stop.assert_awaited_once()
            discord_close.assert_awaited_once()
            gemini_close.assert_awaited_once()
            client._database.close.assert_called_once()

    async def test_service_stop_precedes_cancellation_of_background_tasks(self) -> None:
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        started = asyncio.Event()
        order = []

        async def work():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                order.append("task")

        async def stop():
            self.assertTrue(client.background_tasks.stopping)
            await run_in_thread(lambda: order.append("service"))

        client.add_shutdown_callback(stop)
        task = spawn(work(), label="架空の背景処理", owner=client.background_tasks)
        await started.wait()
        with patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()):
            await client.close()
        self.assertEqual(order, ["service", "task"])
        self.assertTrue(task.cancelled())

    async def test_shutdown_timeout_does_not_close_resources_in_use(self) -> None:
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        release = asyncio.Event()
        stop = AsyncMock(side_effect=release.wait)
        client.add_shutdown_callback(stop)
        with patch.object(discord.Client, "close", new=AsyncMock()) as discord_close, \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as gemini_close, \
                patch.object(bot_client, "BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS", 0.01):
            try:
                with self.assertLogs("persona_bot.core.background", level="ERROR"):
                    for _ in range(2):
                        with self.assertRaises(TimeoutError):
                            await client.close()
                stop.assert_awaited_once()
                discord_close.assert_not_awaited()
                gemini_close.assert_not_awaited()
                client._database.close.assert_not_called()
            finally:
                release.set()
                await client.background_tasks.shutdown(timeout=1)
            await client.close()
            discord_close.assert_awaited_once()
            gemini_close.assert_awaited_once()
            client._database.close.assert_called_once()

    async def test_owned_save_and_cleanup_finish_before_resources_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.db"
            db = Database(path)
            db.initialize()
            self.addCleanup(db.close)
            client = PersonaClient(genai_client=object(), database=db, intents=discord.Intents.none())
            started, cleaning, finish_cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()
            release = threading.Event()
            loop = asyncio.get_running_loop()
            order = []

            def save():
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise AssertionError("保存の待機が解除されませんでした。")
                db.pending_append("11", "fictional-time", "架空の質問", "架空の返信")
                order.append("save")

            async def cleanup():
                cleaning.set()
                await finish_cleanup.wait()
                # 新規受付停止後も、後片付けに必要なI/Oは完了させる。
                await run_in_thread(db.pending_append, "11", "fictional-time", "架空の後片付け", "完了")
                order.append("cleanup")

            async def compress():
                try:
                    await asyncio.Event().wait()
                finally:
                    await wait_for_cleanup(cleanup(), label="架空の圧縮の後片付け")

            unrelated = spawn(asyncio.Event().wait(), label="別の所有者の処理")
            with client.background_tasks.scope():
                saving = spawn(run_in_thread(save), label="架空の保存")
                compression = spawn(compress(), label="架空の圧縮")
            closing = None
            with patch.object(discord.Client, "close", new=AsyncMock(side_effect=lambda: order.append("discord"))), \
                    patch.object(bot_client, "close_genai_client", new=AsyncMock(side_effect=lambda *_: order.append("gemini"))):
                try:
                    await asyncio.wait_for(started.wait(), 2)
                    closing = asyncio.create_task(client.close())
                    await asyncio.wait_for(cleaning.wait(), 2)
                    for _ in range(2):
                        closing.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(closing.done())
                        self.assertEqual(order, [])
                    self.assertFalse(unrelated.done())
                    rejected = Mock()
                    with client.background_tasks.scope():
                        late = spawn(run_in_thread(rejected), label="停止後の新規保存")
                    await asyncio.gather(late, return_exceptions=True)
                    rejected.assert_not_called()
                    release.set()
                    finish_cleanup.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(closing, 3)
                    await client.close()
                finally:
                    release.set()
                    finish_cleanup.set()
                    unrelated.cancel()
                    await asyncio.gather(*[task for task in (closing, saving, compression, unrelated) if task is not None], return_exceptions=True)
            self.assertTrue(saving.done())
            self.assertTrue(compression.done())
            self.assertEqual(order[-2:], ["discord", "gemini"])
            self.assertCountEqual(order[:-2], ["save", "cleanup"])
            with closing_connection(sqlite3.connect(path)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM pending_log").fetchone()[0], 2)

    async def test_periodic_loop_is_stopped_and_cannot_resume_after_close(self) -> None:
        client = PersonaClient(genai_client=object(), intents=discord.Intents.none())
        started = asyncio.Event()
        ended = asyncio.Event()

        async def periodic():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        guarded = make_guarded_loop(periodic, label="架空の見回り", client=client,
                                    seconds=0.01, owner=client.background_tasks)
        with patch.object(client, "wait_until_ready", new=AsyncMock()), \
                patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()):
            task = guarded.start()
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(client.close(), 2)
                self.assertTrue(ended.is_set())
                self.assertTrue(task.done())
                started.clear()
                restarted = guarded.start()
                await asyncio.gather(restarted, return_exceptions=True)
                self.assertFalse(started.is_set())
            finally:
                guarded.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_simultaneous_close_and_callback_failure_close_resources_once(self) -> None:
        client = PersonaClient(genai_client=object(), database=Mock(), intents=discord.Intents.none())
        failed = AsyncMock(side_effect=OSError("架空の終了失敗"))
        successful = AsyncMock()
        client.add_shutdown_callback(failed)
        client.add_shutdown_callback(successful)
        with patch.object(discord.Client, "close", new=AsyncMock()) as discord_close, \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as gemini_close:
            await asyncio.wait_for(asyncio.gather(client.close(), client.close()), 2)
            await client.close()
        failed.assert_awaited_once()
        successful.assert_awaited_once()
        discord_close.assert_awaited_once()
        gemini_close.assert_awaited_once()
        client._database.close.assert_called_once()

    async def test_event_can_request_close_without_waiting_for_itself(self) -> None:
        client = PersonaClient(genai_client=object(), intents=discord.Intents.none())

        async def event():
            await client.close()

        with patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()):
            await asyncio.wait_for(client._run_event(event, "fictional_event"), 2)
            late_event = AsyncMock()
            with self.assertRaises(asyncio.CancelledError):
                await client._run_event(late_event, "fictional_event")
            late_event.assert_not_awaited()

    async def test_start_propagates_owner_to_background_tasks(self) -> None:
        client = PersonaClient(genai_client=object(), intents=discord.Intents.none())
        child = None

        async def start(*args, **kwargs):
            nonlocal child
            child = spawn(asyncio.Event().wait(), label="起動から引き継ぐ処理")

        with patch.object(discord.Client, "start", new=AsyncMock(side_effect=start)), \
                patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()):
            await client.start("YOUR_DISCORD_BOT_TOKEN")
            await asyncio.wait_for(client.close(), 2)
        self.assertTrue(child.cancelled())

    async def test_database_is_closed_last_even_when_earlier_steps_fail(self) -> None:
        cases = [(None, None), (OSError("架空のDiscord終了失敗"), None), (None, OSError("架空のGemini終了失敗")),
                 (TimeoutError("架空のDiscord終了期限超過"), None), (None, TimeoutError("架空のGemini終了期限超過"))]
        for discord_error, gemini_error in cases:
            with self.subTest(discord_error=discord_error, gemini_error=gemini_error):
                order = []

                def step(name, error):
                    def run(*args, **kwargs):
                        order.append(name)
                        if error is not None:
                            raise error
                    return run

                database = Mock(close=Mock(side_effect=step("db", None)))
                client = PersonaClient(genai_client=object(), database=database, intents=discord.Intents.none())
                with patch.object(discord.Client, "close", new=AsyncMock(side_effect=step("discord", discord_error))), \
                        patch.object(bot_client, "close_genai_client", new=AsyncMock(side_effect=step("gemini", gemini_error))):
                    if discord_error or gemini_error:
                        with self.assertRaises(OSError):
                            await client.close()
                        if isinstance(discord_error or gemini_error, TimeoutError):
                            with self.assertRaises(TimeoutError):
                                await client.close()
                    else:
                        await client.close()
                self.assertEqual(order, ["discord", "gemini", "db"])
                database.close.assert_called_once()

    async def test_client_without_database_still_closes(self) -> None:
        client = PersonaClient(genai_client=SimpleNamespace(), intents=discord.Intents.none())
        with patch.object(discord.Client, "close", new=AsyncMock()), \
                patch.object(bot_client, "close_genai_client", new=AsyncMock()) as closing:
            await client.close()
        closing.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
