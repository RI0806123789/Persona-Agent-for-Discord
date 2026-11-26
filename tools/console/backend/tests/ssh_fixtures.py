"""模擬 SSH サーバーを使うテストの共通の準備と後片付け（#416）。

`FakeSshEnvironment` が、模擬サーバーの起動・接続先（`Profile`）の生成・終了処理をまとめる。
終了では、ログ生成タスクのキャンセルを `gather` で待ち切ってから受け口を閉じる
（待たずに閉じると、キャンセル中のタスクが後続のテストへ漏れて警告になる）。

使い方（`IsolatedAsyncioTestCase` の中で）:

    async def asyncSetUp(self):
        self.env = await FakeSshEnvironment.start(live_interval=(0.2, 0.4))
        self.addAsyncCleanup(self.env.close)
        self.profile = self.env.profile
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

import asyncssh

from fake_ssh_server import FakeBot, start_fake_server
from profiles import DEFAULT_BOT_DIR, Profile


def make_test_profile(port: int, key_dir: Path, **overrides: Any) -> Profile:
    """模擬サーバー（127.0.0.1）へつなぐ接続先。環境・ユーザー名などは必要な項目だけ上書きする。"""
    values: dict[str, Any] = dict(
        id="test", label="test", environment="development", host="127.0.0.1", port=port,
        username="ubuntu", key_path=str(key_dir / "client_key"), known_hosts=str(key_dir / "known_hosts"),
        use_ssh_config=False, service="bot", bot_dir=DEFAULT_BOT_DIR, journal_sudo=False, note="",
    )
    values.update(overrides)
    return Profile(**values)


class FakeSshEnvironment:
    """起動済みの模擬サーバーと、そこへつなぐ接続先。"""

    def __init__(
        self, key_dir: Path, acceptor: asyncssh.SSHAcceptor, bot: FakeBot, port: int, live_task: asyncio.Task,
        profile: Profile, *, owns_key_dir: bool,
    ) -> None:
        self.key_dir = key_dir
        self.acceptor = acceptor
        self.bot = bot
        self.port = port
        self.live_task = live_task
        self.profile = profile
        self._owns_key_dir = owns_key_dir

    @classmethod
    async def start(
        cls, *, key_dir: Path | None = None, live_interval: tuple[float, float] = (0.2, 0.4),
        delay_scale: float = 1.0, **profile_overrides: Any,
    ) -> "FakeSshEnvironment":
        """模擬サーバーを起動する。`key_dir` を渡さなければ一時フォルダを作り、終了時に消す。"""
        owns = key_dir is None
        directory = Path(tempfile.mkdtemp(prefix="pbc-fake-ssh-")) if owns else key_dir
        acceptor, bot, port, live_task = await start_fake_server(
            "127.0.0.1", 0, directory, live_interval=live_interval, delay_scale=delay_scale,
        )
        return cls(directory, acceptor, bot, port, live_task, make_test_profile(port, directory, **profile_overrides),
                   owns_key_dir=owns)

    async def close(self) -> None:
        """ログ生成タスクの終了を待ってから受け口を閉じる。何度呼んでもよい。"""
        self.live_task.cancel()
        await asyncio.gather(self.live_task, return_exceptions=True)
        self.acceptor.close()
        await self.acceptor.wait_closed()
        if self._owns_key_dir:
            shutil.rmtree(self.key_dir, ignore_errors=True)
