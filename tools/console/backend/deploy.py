"""デプロイの管理。1つずつだけ走らせ、最後の記録を持ち、状態不明の記録を保つ（バックエンドを再起動すると消える）。

実際の実行（git pull → 依存の更新 → 再起動 → ヘルスチェック → 失敗時の巻き戻し）は deploy_runner.py の
DeployRunner が行う（#482 で分けた）。記録の形・手順の定義は deploy_models.py にある。

従来の `from deploy import DeployRunner, HEALTH_LOG_TAIL, OUTPUT_MAX_CHARS, ...` を保つため、実行部の名前と
定数・記録の名前をここから再公開する（画面側のテストと app.py が、この入口から読む）。

★ 事前確認で「サーバーに未コミットの変更が無い」ことを確かめる・巻き戻しに `git reset --hard` を使わない・
  bot.db は戻さない、といった実行時の約束は deploy_runner.py の docstring にある。
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Callable

from async_utils import CancellationState, await_shielded
from deploy_models import (  # noqa: F401  （外部へ再公開する名前を含む）
    DEPLOY_STEPS, ROLLBACK_STEPS, Check, DeployRecord, Step, StepFailed, new_record,
)
from deploy_runner import (  # noqa: F401  （外部へ再公開する名前を含む）
    FETCH_TIMEOUT, HEALTH_LOG_TAIL, HEALTH_TIMEOUT, OUTPUT_MAX_CHARS, PIP_TIMEOUT, POLL_INTERVAL,
    RESTART_SETTLE_TIMEOUT, RESTART_TIMEOUT, UNEXPECTED_ERROR_MESSAGE, UNEXPECTED_ERROR_STEP_MESSAGE,
    DeployRunner,
)
from profiles import Profile
from ssh_session import SshSession


class DeployManager:
    """デプロイを1つずつだけ走らせ、最後の記録を持っておく（バックエンドを再起動すると消える）。"""

    def __init__(self) -> None:
        self.current: DeployRecord | None = None
        self._task: asyncio.Task | None = None
        self._ids = itertools.count(1)

    @property
    def active(self) -> bool:
        """遠隔処理・巻き戻しを待っている間は接続を変更させない。"""
        return self._task is not None and not self._task.done()

    @property
    def running(self) -> bool:
        """画面へ返す実行状態は、デプロイタスクの生存期間だけに合わせる。"""
        return self.active

    @property
    def state_unknown(self) -> bool:
        """タスク完了後・再接続後も、状態不明の記録がある間は更新操作を拒否する。"""
        return self.current is not None and self.current.status == "unknown"

    def start(self, session: SshSession, profile: Profile, *, on_finished: Callable[[], None] | None = None, **runner_options) -> DeployRecord:
        if self.state_unknown:
            raise RuntimeError("デプロイの状態不明のため、更新操作を停止しています。サーバーの状態を確認してください。")
        if self.running:
            raise RuntimeError("デプロイを実行中です。終わるまで待ってください。")
        session.require_command_completion()
        record = new_record(next(self._ids), profile)
        self.current = record
        runner = DeployRunner(session, profile, record, **runner_options)
        # タスクへの強い参照を持ち続ける（捨てると実行途中で回収されうる）
        self._task = asyncio.get_running_loop().create_task(runner.run())
        if on_finished is not None:
            self._task.add_done_callback(lambda _task: on_finished())
        return record

    async def wait(self) -> DeployRecord | None:
        """実行中のデプロイが終わるまで待つ（テストと終了処理用）。"""
        task = self._task
        state = CancellationState()
        if task is not None:
            await await_shielded(task, state)
        if state.canceled:
            raise asyncio.CancelledError
        return self.current
