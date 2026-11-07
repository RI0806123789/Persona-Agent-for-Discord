"""Discord終了時に、共有するGeminiクライアントの接続とDBも閉じる。"""

import logging

import asyncio
from typing import Any, Awaitable, Callable

import discord

from persona_bot.core.background import ShutdownTimeoutError, TaskOwner, run_in_thread, spawn, task_scope, wait_for_task
from persona_bot.config.constants import BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS
from persona_bot.services.genai_client import close_genai_client
from persona_bot.utils.discord_helpers import REPLY_ALLOWED_MENTIONS


logger = logging.getLogger(__name__)


class PersonaClient(discord.Client):
    def __init__(self, *, genai_client: Any, database: Any = None, **options: Any) -> None:
        # 個別の通知先を明示しない送信でも、本文中のメンションでは通知しない。
        options.setdefault("allowed_mentions", REPLY_ALLOWED_MENTIONS)
        super().__init__(**options)
        self._genai_client = genai_client
        self._database = database
        self._shutdown_callbacks: list[Callable[[], Awaitable[None]]] = []
        self.background_tasks = TaskOwner()
        self._close_task: asyncio.Task[Any] | None = None
        self._service_stop_task: asyncio.Task[Any] | None = None

    async def start(self, token: str, *, reconnect: bool = True) -> None:
        # Discordが生成するコマンド・UI処理の子タスクにも所有者を引き継ぐ。
        with self.background_tasks.scope():
            await super().start(token, reconnect=reconnect)

    async def _run_event(self, coro, event_name, *args, **kwargs) -> None:
        await self.background_tasks.run(super()._run_event(coro, event_name, *args, **kwargs))

    def add_shutdown_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """共有クライアントの切断前に終了させる処理を明示的に登録する。"""
        self._shutdown_callbacks.append(callback)

    async def close(self) -> None:
        self.background_tasks.release_current()
        if (self._close_task is not None and self._close_task.done()
                and not self._close_task.cancelled()
                and isinstance(self._close_task.exception(), ShutdownTimeoutError)):
            # 期限超過でリソースを残した場合は、I/O復旧後の再終了を許す。
            self._close_task = None
        if self._close_task is None:
            # 終了タスク自身を回収対象に入れると、自分自身の終了を待ってしまう。
            with task_scope(None):
                self._close_task = spawn(self._close_resources(), label="ボットクライアントの終了")
        await wait_for_task(self._close_task)

    async def _close_resources(self) -> None:
        with self.background_tasks.scope():
            await self._shutdown()

    async def _shutdown(self) -> None:
        self.background_tasks.stop_accepting()

        async def stop_services() -> None:
            for callback in self._shutdown_callbacks:
                try:
                    await callback()
                except (Exception, asyncio.CancelledError) as error:
                    logger.error(f'終了前のバックグラウンド処理の回収に失敗しました: {type(error).__name__}', exc_info=error)

        # 新規受付停止後もサービスの後片付けは保護して実行する。
        deadline = asyncio.get_running_loop().time() + BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS
        if self._service_stop_task is None:
            self._service_stop_task = spawn(
                stop_services(), label="サービスの終了通知", owner=self.background_tasks, _protected=True,
            )
        stopping = self._service_stop_task
        await asyncio.wait({stopping}, timeout=BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS)
        # サービスが期限内に終わらない場合も、通常処理の中断と追跡は行う。
        # 超過時は例外を返し、まだ使われているDiscord・Gemini・DBを閉じない。
        await self.background_tasks.shutdown(timeout=max(0.0, deadline - asyncio.get_running_loop().time()))
        if not stopping.done():
            # 所有者の回収方法にかかわらず、期限後に無期限の待機へ進まない。
            raise ShutdownTimeoutError("サービスの終了通知が期限を超えました。")
        try:
            await stopping
        finally:
            try:
                await super().close()
            finally:
                try:
                    await close_genai_client(self._genai_client)
                finally:
                    # DB は共有クライアントと文書処理の終了後、最後に閉じる。
                    if self._database is not None:
                        await run_in_thread(self._database.close)
