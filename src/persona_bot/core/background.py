"""fire-and-forget なバックグラウンドタスクの起動をまとめたヘルパー。

asyncio のイベントループは実行中のタスクを**弱参照**でしか保持しない。そのため
`asyncio.create_task(...)` の戻り値をどこにも保存しないと、そのタスクは
実行の途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが
「タスクが実行途中で消えるのを避けるため、戻り値への参照を保持すること」と
明記している既知の落とし穴。運が良ければ "Task was destroyed but it is pending!"
という警告が出るが、出ないまま静かに消えることもある）。

このモジュールの `spawn()` は生成したタスクへの強参照をモジュール内の集合で保持し、
完了時に自動で取り除く。加えて、タスク内で発生した例外をその場でログに出す
（`create_task` に投げっぱなしにすると、例外はタスクが回収されるまで報告されず、
最悪の場合まったく表示されないまま失われる）。

**fire-and-forget でコルーチンを起動するときは必ず `spawn()` を使うこと。**
`asyncio.create_task()` を直接呼ばないこと。

もう一つの `make_guarded_loop()` は、一定間隔で走る見回りタスク
（記憶の圧縮・自発的な会話・記念日・リマインダー）の定型部分をまとめたもの。
どのループも「tasks.loop で間隔を指定 / before_loop で接続完了を待つ /
本体を try-except で包んでログを出す」という同じ3点セットを書く必要があり、
以前は同じ形が4組コピーされていた。
"""

import logging

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Coroutine

import discord
from discord.ext import tasks

from persona_bot.config.constants import BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS


logger = logging.getLogger(__name__)

# 実行中タスクへの強参照。完了時にコールバックで取り除くため溜まり続けることはない。
_background_tasks: set[asyncio.Task[Any]] = set()
_current_owner: ContextVar["TaskOwner | None"] = ContextVar("background_owner", default=None)


@contextmanager
def task_scope(owner: "TaskOwner | None"):
    """子タスクへ引き継ぐ所有者を指定し、Noneなら管理対象から分離する。"""
    token = _current_owner.set(owner)
    try:
        yield
    finally:
        _current_owner.reset(token)


class ShutdownTimeoutError(TimeoutError):
    """共有リソースを閉じる前のタスク回収が期限を超えた。"""


class TaskOwner:
    """ボット単位で受付・周期処理・背景処理の寿命を管理する。"""

    def __init__(self) -> None:
        self.stopping = False
        self._tasks: dict[asyncio.Task[Any], bool] = {}
        self._loops: list[tasks.Loop[Any]] = []

    def scope(self):
        """この範囲と子タスクからの spawn に所有者を引き継ぐ。"""
        return task_scope(self)

    def release_current(self) -> None:
        """終了を要求したタスク自身を、終了待機の対象から外す。"""
        self._tasks.pop(asyncio.current_task(), None)

    def add_loop(self, loop: tasks.Loop[Any]) -> None:
        self._loops.append(loop)

    def _track(self, task: asyncio.Task[Any], *, protected: bool = False) -> None:
        self._tasks[task] = protected
        task.add_done_callback(lambda finished: self._tasks.pop(finished, None))

    async def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Discordイベント本体も受付停止と終了待機の対象にする。"""
        if self.stopping:
            coro.close()
            raise asyncio.CancelledError
        task = asyncio.current_task()
        if task is None:
            # 手動で駆動されたコルーチンも、実行本体を所有タスクとして回収できるようにする。
            return await spawn(coro, label="イベント処理", owner=self)
        self._track(task)
        try:
            with self.scope():
                return await coro
        finally:
            self._tasks.pop(task, None)

    def stop_accepting(self) -> None:
        """新規受付と周期処理を止め、サービスの終了通知を先行できるようにする。"""
        if self.stopping:
            return
        self.stopping = True
        for loop in self._loops:
            task = loop.get_task()
            loop.cancel()
            if task is not None and task not in self._tasks:
                self._track(task)

    async def shutdown(self, *, timeout: float = BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS) -> None:
        """通常処理を中断して回収する。期限超過でも保護タスクを強制中断しない。"""
        self.stop_accepting()
        loop_tasks = {loop.get_task() for loop in self._loops}
        current = asyncio.current_task()
        for task, protected in list(self._tasks.items()):
            if not protected and task not in loop_tasks and task is not current:
                task.cancel()
        deadline = asyncio.get_running_loop().time() + timeout
        while pending := {task for task in self._tasks if task is not current and not task.done()}:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.error("バックグラウンド処理の終了待機が期限を超えました（残り %d 件）。共有リソースは閉じません。", len(pending))
                raise ShutdownTimeoutError("バックグラウンド処理の終了待機が期限を超えました。")
            # wait_for/gatherの中断では、同期I/Oを保護する親まで再キャンセルしてしまう。
            await asyncio.wait(pending, timeout=remaining)
            # 完了通知の実行順に依存せず、回収済みの参照を外す。
            self._tasks = {task: protected for task, protected in self._tasks.items() if not task.done()}


def spawn(
    coro: Coroutine[Any, Any, Any], *, label: str,
    owner: TaskOwner | None = None, _protected: bool = False,
) -> asyncio.Task[Any]:
    """コルーチンをバックグラウンドで実行する。完了までタスクへの強参照を保つ。

    Args:
        coro: 実行するコルーチン。
        label: 失敗時のログに出す処理名（例: "会話統計の記録"）。
            どの処理が落ちたのか追えるよう、呼び出し側で必ず指定すること。
        owner: 明示する所有者。省略時は呼出元のコンテキストを引き継ぐ。
        _protected: 同期ワーカーと後片付け専用。通常の背景処理では指定しない。
    """
    owner = owner if owner is not None else _current_owner.get()
    if owner is None:
        task = asyncio.create_task(coro)
    else:
        with owner.scope():
            task = asyncio.create_task(coro)
        owner._track(task, protected=_protected)
        if owner.stopping and not _protected:
            # 開始前に中断するので、渡されたコルーチンの本体は実行されない。
            task.cancel()
    _background_tasks.add(task)
    task.add_done_callback(lambda finished: _on_task_done(finished, label))
    return task


def _on_task_done(task: asyncio.Task[Any], label: str) -> None:
    """タスク完了時に強参照を外し、例外があればログに出す。"""
    _background_tasks.discard(task)

    if task.cancelled():
        # ボット停止時などの正常なキャンセル。エラーとして騒がない。
        return

    error = task.exception()
    if error is not None:
        logger.error(f'バックグラウンド処理エラー ({label}): {type(error).__name__}', exc_info=error)


async def run_in_thread(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """同期処理を別スレッドで実行し、キャンセル時も完了まで呼出元のロックを保つ。"""
    async def invoke():
        try:
            return await asyncio.to_thread(function, *args, **kwargs), None
        except Exception as error:
            return None, error

    worker = spawn(invoke(), label="同期I/O・描画処理", _protected=True)
    canceled = False
    while True:
        try:
            result, error = await asyncio.shield(worker)
            break
        except asyncio.CancelledError:
            if worker.cancelled():
                raise
            # スレッド自体はキャンセルできない。繰り返しのキャンセルでも待機を続ける。
            canceled = True
    if canceled:
        # 生成済みの画像バッファは受け取る側が無いので、この場で回収する。
        try:
            close = getattr(result, "close", None)
            if callable(close):
                close()
        except Exception as cleanup_error:
            # 回収の失敗で、本来のキャンセルを別の例外に置き換えない。
            logger.error(f'キャンセル時の同期処理結果の回収に失敗しました: {type(cleanup_error).__name__}', exc_info=cleanup_error)
        raise asyncio.CancelledError
    if error is not None:
        raise error
    return result


async def wait_for_cleanup(coro: Coroutine[Any, Any, Any], *, label: str) -> Any:
    """後片付けを別タスクで完了させ、待機中のキャンセルは完了後に伝播する。"""
    return await wait_for_task(spawn(coro, label=label, _protected=True))


async def wait_for_task(cleanup: asyncio.Task[Any]) -> Any:
    """共有する終了タスクを、繰り返しのキャンセルから保護して待つ。"""
    canceled = False
    while True:
        try:
            result = await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            # 呼出元に繰り返しキャンセルが届いても、所有リソースを残さない。
            canceled = True
    if canceled:
        raise asyncio.CancelledError
    return result


def make_guarded_loop(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    *,
    label: str,
    client: discord.Client,
    minutes: float | None = None,
    seconds: float | None = None,
    owner: TaskOwner | None = None,
) -> tasks.Loop[Any]:
    """例外で止まらない定期実行ループ（tasks.Loop）を組み立てて返す。

    次の3点をまとめて引き受ける（呼び出し側は間隔と処理を渡すだけでよい）:

    1. `discord.ext.tasks.loop` で指定間隔のループにする。
    2. `before_loop` で `client.wait_until_ready()` を待つ。接続完了前に走らせると、
       チャンネルの取得やメッセージ送信が失敗するため。
    3. 本体を `try/except Exception` で包み、固定ラベル・例外型と発生箇所をログへ出す。
       tasks.Loop は本体が送出した例外でループ自体が停止するため、握りつぶさないと
       一度きりの失敗（API の一時エラーなど）で以降の見回りが二度と走らなくなる。

    Args:
        coro_factory: 毎回の実行で呼ぶ非同期関数（引数なし）。`memory.flush_if_needed`
            のようにバウンドメソッドをそのまま渡す。**コルーチンオブジェクトではなく
            「呼ぶとコルーチンを返すもの」を渡すこと**（コルーチンは1度しか await
            できず、2回目の実行で RuntimeError になるため）。
        label: エラーログでコロンの前に出す固定の文字列。"リマインダー確認エラー" のように**末尾まで含めた
            文言**を渡すこと（"リマインダー" だけを渡すとログの文言が変わる）。
        client: 接続完了を待つ対象の Discord クライアント。
        minutes: 実行間隔（分）。`seconds` とはどちらか一方だけを指定する。
        seconds: 実行間隔（秒）。分未満の間隔で見回りたい場合に使う。
        owner: 終了時に停止・待機する所有者。

    Returns:
        `tasks.Loop`。呼び出し側は `on_ready` などで `.start()` すること
        （この関数は生成するだけで開始はしない）。
    """
    if (minutes is None) == (seconds is None):
        raise ValueError("make_guarded_loop には minutes か seconds のどちらか一方だけを指定してください。")

    # tasks.loop は未指定の間隔を専用の番兵値（MISSING）で判定するため、
    # 指定された側だけを渡す（None や 0 を渡すと間隔の指定として解釈されてしまう）。
    interval: dict[str, float] = (
        {"minutes": minutes} if minutes is not None else {"seconds": seconds}  # type: ignore[dict-item]
    )

    @tasks.loop(**interval)
    async def guarded_loop() -> None:
        if owner is not None and owner.stopping:
            raise asyncio.CancelledError
        try:
            if owner is None:
                await coro_factory()
            else:
                with owner.scope():
                    await coro_factory()
        except Exception as error:
            logger.error(f'{label}: {type(error).__name__}', exc_info=error)

    @guarded_loop.before_loop
    async def before_guarded_loop() -> None:
        await client.wait_until_ready()
        if owner is not None and owner.stopping:
            raise asyncio.CancelledError

    if owner is not None:
        owner.add_loop(guarded_loop)
    return guarded_loop
