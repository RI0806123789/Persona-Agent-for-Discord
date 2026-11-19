"""SSH 接続の管理（asyncssh を使用）。

ダッシュボードはサーバーへの SSH 接続を 1 本だけ持ち、状態の取得・ログの追跡・サービス操作を
すべてその接続の上で行う（コマンドのたびにログインし直さない）。

★ ホスト鍵の検証は必ず行う（known_hosts=None で検証を切らないこと）。
  検証を切ると、通信の途中で別のサーバーにすり替えられても気づけない。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import asyncssh

from async_utils import CancellationState, await_shielded
from profiles import Profile, resolve_local_path
# エラーの型と、診断文・伏せ字用の安全な文面の組み立ては ssh_errors.py にある（#482）。
# 従来の `from ssh_session import SshError, CommandStateUnknownError, ...` を保つため、ここから再公開する。
from ssh_errors import (  # noqa: F401  （外部へ再公開する名前を含む）
    CONNECTION_LOST_SAFE_MESSAGE, COMMAND_STATE_UNKNOWN_MESSAGE, NOT_CONNECTED_MESSAGE, STATE_UNKNOWN_SUFFIX,
    CommandStateUnknownError, NotConnectedError, SshError,
    communication_error, completion_unknown_from_transport, completion_unknown_from_unexpected,
    connection_lost_message, describe_connect_error, exit_status_unknown_error,
    missing_default_known_hosts_error, missing_key_error, missing_known_hosts_error,
    start_error, timeout_error, with_unknown_state_suffix,
)

CONNECT_TIMEOUT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 20
KEEPALIVE_SECONDS = 30
HISTORY_MAX = 200
@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int | None
    duration_ms: int


@dataclass
class CommandRecord:
    """画面の「SSH コマンド履歴」に出す1件分の記録。"""

    at: float
    label: str
    command: str
    exit_status: int | None
    duration_ms: int
    ok: bool
    error: str | None = None
    error_safe: str | None = None


class _ConnectionWatcher(asyncssh.SSHClient):
    """接続が切れたことを SshSession に知らせるための受け口。"""

    def __init__(self, session: SshSession) -> None:
        self._session = session
        self._conn: asyncssh.SSHClientConnection | None = None

    def connection_made(self, conn: asyncssh.SSHClientConnection) -> None:
        self._conn = conn

    def connection_lost(self, exc: Exception | None) -> None:
        self._session._handle_connection_lost(self._conn, exc)


class SshSession:
    def __init__(self) -> None:
        self._conn: asyncssh.SSHClientConnection | None = None
        self._lock = asyncio.Lock()
        self.profile: Profile | None = None
        self.connected_at: float | None = None
        self.fingerprint: str | None = None
        self.server_info: dict | None = None
        self.last_error: str | None = None
        self.last_error_safe: str | None = None
        self.history: deque[CommandRecord] = deque(maxlen=HISTORY_MAX)
        self.generation = 0  # 接続し直す前の集計結果を使い回さないための世代
        self.command_state_unknown = False

    # ------------------------------------------------------------ 状態

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def require_profile(self) -> Profile:
        """接続中のプロファイルを返す。未接続なら NotConnectedError。"""
        if self._conn is None or self.profile is None:
            raise NotConnectedError(NOT_CONNECTED_MESSAGE)
        return self.profile

    def snapshot(self) -> dict:
        return {
            "connected": self.connected,
            "profile": self.profile.public_dict() if self.profile and self.connected else None,
            "connected_at": self.connected_at if self.connected else None,
            "fingerprint": self.fingerprint if self.connected else None,
            "server": self.server_info if self.connected else None,
            "last_error": self.last_error,
            "last_error_safe": self.last_error_safe,
        }

    def history_list(self) -> list[dict]:
        """新しい順のコマンド履歴。"""
        return [asdict(record) for record in reversed(self.history)]

    # ------------------------------------------------------------ 接続・切断

    async def connect(self, profile: Profile) -> None:
        async with self._lock:
            await self._close_current()
            self.last_error = None
            self.last_error_safe = None
            target = f"{profile.username + '@' if profile.username else ''}{profile.host}"
            if profile.port:
                target += f":{profile.port}"

            started = time.perf_counter()
            try:
                options = self._build_options(profile)
                conn = await asyncssh.connect(**options)
            except Exception as error:  # noqa: BLE001 — どんな失敗も画面向けの文言に変える
                message = describe_connect_error(error, profile.host, CONNECT_TIMEOUT_SECONDS)
                safe_message = error.safe_message if isinstance(error, SshError) else None
                self.last_error = message
                self.last_error_safe = safe_message
                self._record("SSH 接続", f"ssh {target}", None, started, ok=False, error=message, error_safe=safe_message)
                raise SshError(message, safe_message=safe_message) from error

            self._conn = conn
            self.profile = profile
            self.connected_at = time.time()
            self.server_info = None
            host_key = conn.get_server_host_key()
            self.fingerprint = host_key.get_fingerprint() if host_key else None
            self._record("SSH 接続", f"ssh {target}", 0, started, ok=True)

    async def disconnect(self) -> None:
        async with self._lock:
            await self._close_current()

    def _reset_connection_state(self) -> asyncssh.SSHClientConnection | None:
        """接続に結びつく状態をまとめて消し、世代を進めて、切り離した接続を返す（#461）。

        自分で閉じるとき（`_close_current`）と、外から切れたとき（`_handle_connection_lost`）で共有する。
        接続に結びつく項目を増やしたら、ここに足す（片側だけ消し忘れないため）。
        `last_error` 系は経路ごとに値が違うので、呼び出し側が決める。
        """
        self.generation += 1
        conn, self._conn = self._conn, None
        self.profile = None
        self.connected_at = None
        self.fingerprint = None
        self.server_info = None
        return conn

    async def _close_current(self) -> None:
        conn = self._reset_connection_state()
        self.last_error = None
        self.last_error_safe = None
        if conn is None:
            return
        # 先に self._conn を空にしておくので、自分で閉じた場合は「切れた」扱いにならない
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), timeout=5)
        except (asyncio.TimeoutError, asyncssh.Error, OSError):
            pass

    def _build_options(self, profile: Profile) -> dict:
        options: dict = {
            "host": profile.host,
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "keepalive_interval": KEEPALIVE_SECONDS,
            "client_factory": lambda: _ConnectionWatcher(self),
            "config": [],  # use_ssh_config=False の場合は既定の設定ファイルも読まない
        }
        if profile.port:
            options["port"] = profile.port
        if profile.username:
            options["username"] = profile.username

        if profile.key_path:
            key_path = resolve_local_path(profile.key_path)
            if not key_path.is_file():
                raise missing_key_error(key_path)
            options["client_keys"] = [str(key_path)]

        if profile.known_hosts:
            known_hosts = resolve_local_path(profile.known_hosts)
            if not known_hosts.is_file():
                raise missing_known_hosts_error(known_hosts)
            options["known_hosts"] = str(known_hosts)
        else:
            # 指定が無ければ ~/.ssh/known_hosts を使う。ファイル自体が無いと asyncssh が
            # 読み込みで失敗するので、分かりやすい案内を先に出す
            default_known_hosts = Path.home() / ".ssh" / "known_hosts"
            if not default_known_hosts.is_file():
                raise missing_default_known_hosts_error(default_known_hosts, profile.host)
            options["known_hosts"] = str(default_known_hosts)

        ssh_config = Path.home() / ".ssh" / "config"
        if profile.use_ssh_config and ssh_config.is_file():
            # Host の別名・HostName・User・IdentityFile などを ~/.ssh/config から読む
            options["config"] = [str(ssh_config)]
        return options

    def _handle_connection_lost(self, conn: asyncssh.SSHClientConnection | None, exc: Exception | None) -> None:
        if conn is None or conn is not self._conn:
            return  # 自分で閉じた接続、または古い接続
        self._reset_connection_state()
        self.last_error = connection_lost_message(exc)
        # 切断理由（exc）には接続先のホスト名・IP アドレス・ローカルのパスなど、画面が秘密として
        # 知らない値が入りうる。伏せ字モードでは理由を含まない定型文を出す（#399）。
        self.last_error_safe = CONNECTION_LOST_SAFE_MESSAGE
        self.history.append(
            CommandRecord(
                time.time(), "接続断", "-", None, 0, ok=False,
                error=self.last_error, error_safe=CONNECTION_LOST_SAFE_MESSAGE,
            )
        )

    # ------------------------------------------------------------ コマンド実行

    async def run(
        self,
        command: str,
        *,
        label: str,
        input: str | None = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
        wait_for_exit: bool = False,
        on_waiting: Callable[[], None] | None = None,
    ) -> CommandResult:
        """単発実行。更新操作は wait_for_exit を指定し、期限・中断後も終了を待つ。"""
        if wait_for_exit:
            return await self._run_waiting_for_exit(
                command, label=label, input=input, timeout=timeout, on_waiting=on_waiting,
            )
        conn = self._require_conn()
        started = time.perf_counter()
        try:
            result = await conn.run(command, input=input, check=False, timeout=timeout, errors="replace")
        except asyncio.TimeoutError as error:
            failure = timeout_error(label, timeout)
            self._record(label, command, None, started, ok=False, error=str(failure))
            raise failure from error
        except (asyncssh.Error, OSError) as error:
            failure = communication_error(label, error)
            self._record(label, command, None, started, ok=False, error=str(failure), error_safe=failure.safe_message)
            raise failure from error

        exit_status = result.exit_status
        duration_ms = self._record(label, command, exit_status, started, ok=exit_status == 0)
        return CommandResult(
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            exit_status=exit_status,
            duration_ms=duration_ms,
        )

    async def _run_waiting_for_exit(
        self,
        command: str,
        *,
        label: str,
        input: str | None,
        timeout: float,
        on_waiting: Callable[[], None] | None,
    ) -> CommandResult:
        """更新操作の実行は終了確認と回収まで所有し、次の更新との競合を防ぐ。"""
        self.require_command_completion()
        conn = self._require_conn()
        started = time.perf_counter()

        async def execute():
            process = None
            try:
                try:
                    process = await conn.create_process(command, input=input, errors="replace")
                    result = await process.wait(check=False)
                    # チャネル切断やシェルへのシグナルだけでは、子の更新処理の終了を保証できない。
                    if result.exit_status is None or result.exit_status < 0:
                        raise exit_status_unknown_error(label)
                    return result
                finally:
                    if process is not None:
                        process.close()
                        await process.wait_closed()
            except (asyncssh.Error, OSError) as error:
                raise completion_unknown_from_transport(label, error) from error
            except CommandStateUnknownError:
                raise
            except Exception as error:
                raise completion_unknown_from_unexpected(label, error) from error

        # 開始途中も保護する。create_process のキャンセルで参照を失う経路を作らない。
        task = asyncio.create_task(execute())
        canceled = False
        finished_exit_status = None
        try:
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as error:
                canceled = isinstance(error, asyncio.CancelledError)
                if on_waiting is not None:
                    on_waiting()
                # 終了要求は送らず、フォアグラウンドの処理が自然に終わるのを待つ。
                # シェルだけを止めると git/pip が残り、巻き戻しと競合しうる。
                state = CancellationState(canceled=canceled)
                try:
                    completed = await await_shielded(task, state)
                    finished_exit_status = completed.exit_status
                except asyncio.CancelledError:
                    self.command_state_unknown = True
                    self._record(
                        label, command, None, started, ok=False,
                        error=f"{label}: 遠隔コマンドの終了確認が中断されました。状態不明のため、サーバーで確認してください。",
                    )
                    raise
                finally:
                    canceled = state.canceled
                if canceled:
                    message = f"{label}: 中断されました。遠隔コマンドの終了とチャネルの回収を確認しました。"
                    self._record(label, command, finished_exit_status, started, ok=False, error=message)
                    raise asyncio.CancelledError
                raise SshError(f"{label}: {timeout:g} 秒以内に応答がありませんでした。遠隔コマンドの終了を確認しました。") from error
        except CommandStateUnknownError as error:
            self.command_state_unknown = True
            # 作り直す例外にも安全な文面を引き継ぐ（落とすと、伏せ字中に生のエラーへ戻ってしまう）。
            enriched = with_unknown_state_suffix(error)
            self._record(label, command, None, started, ok=False, error=str(enriched), error_safe=enriched.safe_message)
            if canceled:
                raise asyncio.CancelledError from error
            raise enriched from error
        except SshError as error:
            message = str(error)
            self._record(label, command, finished_exit_status, started, ok=False, error=message)
            raise

        exit_status = result.exit_status
        duration_ms = self._record(label, command, exit_status, started, ok=exit_status == 0)
        return CommandResult(
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            exit_status=exit_status,
            duration_ms=duration_ms,
        )

    async def open_process(self, command: str, *, label: str) -> asyncssh.SSHClientProcess:
        """終わらないコマンド（journalctl -f など）を開始して、プロセスをそのまま返す。

        呼び出し側は使い終わったら必ず process.close() すること（チャンネルが残り続けるため）。
        """
        conn = self._require_conn()
        started = time.perf_counter()
        try:
            process = await conn.create_process(command, errors="replace")
        except (asyncssh.Error, OSError) as error:
            failure = start_error(label, error)
            self._record(label, command, None, started, ok=False, error=str(failure), error_safe=failure.safe_message)
            raise failure from error
        self._record(label, command, None, started, ok=True)
        return process

    def _require_conn(self) -> asyncssh.SSHClientConnection:
        if self._conn is None:
            raise NotConnectedError(NOT_CONNECTED_MESSAGE)
        return self._conn

    def require_command_completion(self) -> None:
        """再接続後も、終了不明の操作と次の更新・サービス操作を並行させない。"""
        if self.command_state_unknown:
            raise CommandStateUnknownError(COMMAND_STATE_UNKNOWN_MESSAGE)

    def _record(
        self,
        label: str,
        command: str,
        exit_status: int | None,
        started: float,
        *,
        ok: bool,
        error: str | None = None,
        error_safe: str | None = None,
    ) -> int:
        duration_ms = round((time.perf_counter() - started) * 1000)
        self.history.append(CommandRecord(time.time(), label, command, exit_status, duration_ms, ok, error, error_safe))
        return duration_ms
