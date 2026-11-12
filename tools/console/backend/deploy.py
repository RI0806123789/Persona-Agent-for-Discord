"""デプロイ（git pull → 依存の更新 → 再起動 → ヘルスチェック）を、1ステップずつ確かめながら進める。

各ステップは「実行したコマンド」「出力」「確かめた事実（チェック）」を記録し、画面はそれをそのまま見せる。
チェックが1つでも通らなければ、そのステップを失敗として止める（次のステップへ進まない）。

途中（依存の更新・再起動・ヘルスチェック）で失敗したら、自動で前の版へ戻す（利用者の指定）:
  コードを戻す（git reset --keep デプロイ前のコミット）→ 依存関係を戻す（変えていたら）→ 再起動 → ヘルスチェック

★ 事前確認で「サーバーに未コミットの変更が無い」ことを必ず確かめること。変更があるサーバーでは、
  デプロイ中の編集と見分けがつかず、巻き戻しの前提（変更が無いこと）を確かめられないため、デプロイ自体を始めない。
★ 巻き戻しは git reset --hard を使わない（#381）。事前確認の後に、サーバーで別の操作（手動の編集・ブランチの切り替え・
  コミット）が行われうるため、戻す直前に remote_commands.deploy_rollback_command が1回の遠隔コマンドの中で
  「ブランチが想定どおり」「HEAD がデプロイ前か取り込み後」「追跡ファイルに変更なし」を確かめ、外れていたら
  何も変更せずに止まる（rollback_failed。変更は消さず、安全な復旧手順を案内する）。確認の後に編集されても、
  git reset --keep が衝突するファイルを守って中止する。
★ bot.db は戻さない。新しい版がスキーマを上げていても、ボットの移行は列や表を足すだけなので、前の版は
  知らない列・表を読まないだけで動く（戻すと、デプロイ後に保存された会話の記憶などを失う）。
★ 実行するコマンドは remote_commands.py のホワイトリストで組み立てたものだけ。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from service_jobs import last_line as _last_line, wait_for_service_job
import remote_commands as rc
from async_utils import CancellationState, await_shielded
from profiles import Profile
from ssh_session import CommandResult, CommandStateUnknownError, SshError, SshSession

logger = logging.getLogger(__name__)

# 想定外の例外は本文を画面へ出さない（パス・ホスト名などを含みうるため。型名だけをログへ残す）
UNEXPECTED_ERROR_STEP_MESSAGE = "想定外のエラーが発生しました。詳細は管理コンソールのログを確認してください。"
UNEXPECTED_ERROR_MESSAGE = "想定外のエラーでデプロイを止めました。サーバーの状態を確かめてください。"

# 1つのステップに残す出力の長さ（長いと画面と通信が重くなる。末尾を残す）
OUTPUT_MAX_CHARS = 8000
# 各コマンドの待ち時間（秒）
FETCH_TIMEOUT = 90
PIP_TIMEOUT = 900
RESTART_TIMEOUT = 90
# 再起動のあと、稼働中になり PID が変わるまで待つ時間
RESTART_SETTLE_TIMEOUT = 30
# 「ログイン成功」のログが出るまで待つ時間（Discord への接続とコマンドの同期を見込む）
HEALTH_TIMEOUT = 120
# 状態やログを取り直す間隔
POLL_INTERVAL = 2.0
# ヘルスチェックが失敗したとき、出力に残す再起動後のログの行数
HEALTH_LOG_TAIL = 40

DEPLOY_STEPS = (
    ("precheck", "事前確認", "ブランチ・未コミットの変更・GitHub の最新を確かめる"),
    ("pull", "git pull", "{branch} の最新を早送りで取り込む"),
    ("deps", "pip install -r requirements.txt", "requirements.txt か constraints.txt が変わったときだけ、依存関係を入れ直す"),
    ("restart", "sudo systemctl restart {service}", "新しいコードで起動し、PID が変わったことを確かめる"),
    ("health", "ヘルスチェック", "「ログイン成功」のログが出て、落ちずに動いていることを確かめる"),
)
ROLLBACK_STEPS = (
    (
        "rollback_code", "コードを戻す",
        "ブランチ・コミット・未コミットの変更が想定どおりか確かめ、git reset --keep でデプロイ前のコミットへ戻す（取り込んだコミットは GitHub に残る）",
    ),
    ("rollback_deps", "依存関係を戻す", "依存関係を入れ直していたら、前の requirements.txt・constraints.txt で入れ直す"),
    ("rollback_restart", "再起動", "前の版で起動し直し、PID が変わったことを確かめる"),
    ("rollback_health", "ヘルスチェック", "前の版で「ログイン成功」まで戻ったことを確かめる"),
)


@dataclass
class Check:
    """ステップの中で確かめた事実1つ（画面に ✓ / ✗ で出す）。"""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class Step:
    key: str
    title: str
    description: str
    phase: str = "deploy"  # deploy / rollback
    status: str = "pending"  # pending / running / succeeded / failed / skipped
    started_at: float | None = None
    finished_at: float | None = None
    commands: list[str] = field(default_factory=list)
    output: str = ""
    # 再起動ログは本文の境界が不明。切り詰めや追記後も画面で隠すために保持する。
    output_contains_logs: bool = False
    checks: list[Check] = field(default_factory=list)
    message: str = ""


@dataclass
class DeployRecord:
    id: int
    profile_id: str
    environment: str
    service: str
    branch: str
    started_at: float
    steps: list[Step]
    # running / succeeded / up_to_date / failed / rolled_back / rollback_failed / unknown
    status: str = "running"
    finished_at: float | None = None
    before_commit: str | None = None
    target_commit: str | None = None
    after_commit: str | None = None
    commits: list[dict] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class StepFailed(Exception):
    """ステップの確認が通らなかった。メッセージは画面にそのまま出す日本語。"""


def _short(commit: str | None) -> str:
    return commit[:7] if commit else "—"


class DeployRunner:
    """1回のデプロイを実行する。進み具合は record を書き換えて伝える（画面は record を取り直して表示する）。"""

    def __init__(
        self,
        session: SshSession,
        profile: Profile,
        record: DeployRecord,
        *,
        restart_settle_timeout: float = RESTART_SETTLE_TIMEOUT,
        health_timeout: float = HEALTH_TIMEOUT,
        poll_interval: float = POLL_INTERVAL,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.profile = profile
        self.record = record
        self._restart_settle_timeout = restart_settle_timeout
        self._health_timeout = health_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock
        # 巻き戻しで何を戻す必要があるかの記録
        self._code_changed = False
        self._deps_changed = False
        self._restart_attempted = False
        self._rollback_started = False
        # 巻き戻しが、サーバーの状態が想定と違うために何も変えずに止まった理由（画面と手動の案内に使う）
        self._rollback_refusal: str | None = None
        # 直前の再起動の時刻（サーバーの UNIX 秒）と、そのときの自動再起動の回数。ヘルスチェックが使う
        self._since = 0
        self._restarts_after: int | None = None
        self._pid_after: int | None = None

    # ------------------------------------------------------------ 全体の流れ

    async def run(self) -> DeployRecord:
        try:
            await self._run_all()
        except CommandStateUnknownError as error:
            self._finish_unknown(str(error))
        except asyncio.CancelledError:
            # SSH実行の終了を待った後で巻き戻す。追加キャンセルでも復旧を先に打ち切らない。
            recovery = asyncio.create_task(self._recover_canceled())
            try:
                await await_shielded(recovery, CancellationState(canceled=True))
            except Exception as error:  # noqa: BLE001 — 復旧の失敗で元のキャンセルと終了記録を失わない
                detail = f"中断後の復旧で想定外のエラーが発生しました（{type(error).__name__}: {error}）。サーバーの状態を確かめてください。"
                if self.session.command_state_unknown or isinstance(error, CommandStateUnknownError):
                    self._finish_unknown(detail)
                else:
                    self._finish("failed", detail)
            raise
        except Exception as error:  # noqa: BLE001 — 想定外の失敗でも、画面に「動いたまま」と見せないため記録して終える
            logger.error("デプロイ中に想定外のエラーが発生しました（%s）", type(error).__name__)
            # 実行中のステップを失敗、未実行のステップを見送りにそろえてから終える
            for step in self.record.steps:
                if step.status == "running":
                    step.status = "failed"
                    step.message = UNEXPECTED_ERROR_STEP_MESSAGE
                    step.finished_at = step.finished_at or time.time()
            self._skip_rest("想定外のエラーで止めたため、実行しません。")
            self._finish("failed", UNEXPECTED_ERROR_MESSAGE)
        return self.record

    def _finish_unknown(self, detail: str) -> None:
        self._skip_rest("遠隔処理の終了を確認できないため、実行しません。")
        self._finish("unknown", "遠隔処理の終了を確認できず、状態不明です。デプロイ・巻き戻し・サービス操作を停止します。読み取りと再接続は利用できます。" + detail)

    async def _recover_canceled(self) -> None:
        if self._rollback_started:
            return
        for step in self.record.steps:
            if step.status == "running":
                step.status = "failed"
                step.message = "中断されました。遠隔コマンドの終了を待ってから復旧します。"
        self._skip_rest("デプロイが中断されたため、実行しません。")
        try:
            self.session.require_command_completion()
            if self._code_changed:
                await self._rollback()
            else:
                self._finish("failed", "デプロイが中断されました。実行済みの遠隔コマンドは終了しています。")
        except CommandStateUnknownError as error:
            self._finish_unknown(str(error))

    async def _run_all(self) -> None:
        if not await self._step("precheck", self._precheck):
            self._skip_rest("事前確認で止めたため、実行しません。")
            self._finish("failed", "事前確認で止めました。コードもボットも変えていません。")
            return
        if self.record.target_commit == self.record.before_commit:
            self._skip_rest("最新のため、実行しません。")
            self._finish("up_to_date", f"{self.profile.deploy_branch} は最新です（{_short(self.record.before_commit)}）。何も変えていません。")
            return

        for key, action in (("pull", self._pull), ("deps", self._deps), ("restart", self._restart), ("health", self._health)):
            if await self._step(key, action):
                continue
            self._skip_rest("前のステップで止めたため、実行しません。")
            if not self._code_changed:
                self._finish("failed", "コードの取り込みで止めました。コードもボットも変えていません。")
                return
            state = CancellationState()
            task = asyncio.create_task(self._rollback())
            try:
                await await_shielded(task, state)
            except Exception as error:
                if not state.canceled:
                    raise
                if isinstance(error, CommandStateUnknownError) or self.session.command_state_unknown:
                    self._finish_unknown(str(error))
                else:
                    self._finish("failed", f"中断中の巻き戻しに失敗しました（{type(error).__name__}）。")
                raise asyncio.CancelledError from error
            if state.canceled:
                raise asyncio.CancelledError
            return

        count = len(self.record.commits)
        self._finish(
            "succeeded",
            f"デプロイしました（{_short(self.record.before_commit)} → {_short(self.record.after_commit)}、コミット {count} 件）。",
        )

    def _finish(self, status: str, message: str) -> None:
        self.record.status = status
        self.record.message = message
        self.record.finished_at = time.time()

    def _skip_rest(self, message: str) -> None:
        for step in self.record.steps:
            if step.status == "pending":
                step.status = "skipped"
                step.message = message

    def _find(self, key: str) -> Step:
        return next(step for step in self.record.steps if step.key == key)

    async def _step(self, key: str, action: Callable[[Step], Awaitable[None]]) -> bool:
        """ステップを1つ実行し、成功（または不要で飛ばした）なら True を返す。"""
        step = self._find(key)
        step.status = "running"
        step.started_at = time.time()
        try:
            await action(step)
        except (StepFailed, SshError, ValueError) as error:
            step.status = "failed"
            step.message = str(error)
            if isinstance(error, CommandStateUnknownError):
                raise
        except Exception as error:  # noqa: BLE001 — 想定外でもステップを「実行中」のまま残さない
            logger.error("デプロイのステップで想定外のエラーが発生しました（%s: %s）", key, type(error).__name__)
            step.status = "failed"
            step.message = UNEXPECTED_ERROR_STEP_MESSAGE
        else:
            if step.status == "running":
                step.status = "succeeded"
        finally:
            step.finished_at = time.time()
        return step.status in ("succeeded", "skipped")

    # ------------------------------------------------------------ コマンドの実行と記録

    async def _run(self, step: Step, command: str, *, label: str, timeout: float = 30, quiet: bool = False) -> CommandResult:
        """コマンドを実行し、ステップにコマンドと出力を残す（quiet なら残さない。状態の取り直しに使う）。"""
        if not quiet:
            step.commands.append(command)
        def waiting() -> None:
            step.message = "応答期限または中断の要求を受けました。遠隔処理の状態は不明のため、終了確認を待っています。"
            self.record.message = step.message

        result = await self.session.run(
            command, label=f"デプロイ: {label}", timeout=timeout, wait_for_exit=True, on_waiting=waiting,
        )
        if not quiet:
            parts = [f"$ {command}"]
            if result.stdout.strip():
                parts.append(result.stdout.rstrip())
            if result.stderr.strip():
                parts.append(result.stderr.rstrip())
            parts.append(f"（終了コード {result.exit_status}・{result.duration_ms} ms）")
            self._append_output(step, "\n".join(parts))
        return result

    def _append_output(self, step: Step, text: str) -> None:
        combined = f"{step.output}\n{text}" if step.output else text
        step.output = combined[-OUTPUT_MAX_CHARS:]

    def _check(self, step: Step, label: str, ok: bool, detail: str = "", failure: str | None = None) -> None:
        """事実を1つ記録する。通らなければ failure（無ければ label）でステップを止める。"""
        step.checks.append(Check(label, ok, detail))
        if not ok:
            raise StepFailed(failure or f"確認が通りませんでした: {label}（{detail}）")

    async def _service(self, step: Step, *, quiet: bool) -> dict:
        result = await self._run(step, rc.service_show_command(self.profile.service), label="サービスの状態", quiet=quiet)
        if result.exit_status != 0:
            raise StepFailed(f"サービスの状態を読めませんでした: {_last_line(result.stderr) or result.exit_status}")
        return rc.parse_service_show(result.stdout, self.profile.service, None)

    async def _head(self, step: Step) -> str:
        result = await self._run(step, rc.deploy_state_command(self.profile.bot_dir), label="今のコミット", quiet=True)
        self._check(step, "現在のコミットの取得", result.exit_status == 0, failure="現在のコミットを取得できませんでした。")
        return rc.check_commit(rc.parse_lines(result.stdout, 2)[1])

    # ------------------------------------------------------------ デプロイのステップ

    async def _precheck(self, step: Step) -> None:
        bot_dir, branch = self.profile.bot_dir, self.profile.deploy_branch
        result = await self._run(step, rc.deploy_state_command(bot_dir), label="事前確認（ブランチ・変更）")
        if result.exit_status != 0:
            raise StepFailed(f"ボットのフォルダ（{bot_dir}）で git を実行できませんでした: {_last_line(result.stderr) or result.exit_status}")
        current_branch, head, dirty = rc.parse_lines(result.stdout, 3)
        self.record.before_commit = rc.check_commit(head)
        self._check(
            step,
            f"ブランチが {branch}",
            current_branch == branch,
            f"今のブランチ: {current_branch}",
            f"サーバーのボットは {branch} ではなく {current_branch} にいます。サーバーで git switch {branch} してから、もう一度デプロイしてください。",
        )
        dirty_count = int(dirty) if dirty.isdigit() else -1
        self._check(
            step,
            "サーバーに未コミットの変更が無い",
            dirty_count == 0,
            f"{dirty_count} 件" if dirty_count >= 0 else f"読めない出力: {dirty}",
            "サーバーのボットのフォルダに未コミットの変更があります。失敗時に安全に巻き戻せなくなるため、デプロイしません"
            "（サーバーで git status を確かめ、変更を片付けてからデプロイしてください）。",
        )

        result = await self._run(step, rc.deploy_fetch_command(bot_dir, branch), label="GitHub の最新を取得", timeout=FETCH_TIMEOUT)
        if result.exit_status != 0:
            raise StepFailed(f"GitHub から {branch} の最新を取り寄せられませんでした: {_last_line(result.stderr) or result.exit_status}")
        target, behind, ahead = rc.parse_lines(result.stdout, 3)
        self.record.target_commit = rc.check_commit(target)
        self._check(
            step,
            "サーバーにだけあるコミットが無い（早送りで取り込める）",
            ahead == "0",
            f"{ahead} 件",
            f"サーバーに、GitHub の {branch} に無いコミットが {ahead} 件あります。早送りで取り込めないため、デプロイしません。",
        )
        step.message = (
            f"新しいコミットが {behind} 件あります（{_short(self.record.before_commit)} → {_short(target)}）。"
            if behind != "0"
            else f"新しいコミットはありません（{_short(target)}）。"
        )

    async def _pull(self, step: Step) -> None:
        bot_dir, branch = self.profile.bot_dir, self.profile.deploy_branch
        before = self.record.before_commit or ""
        # 送信後は応答が失われても、遠隔側だけ更新されている可能性がある。
        self._code_changed = True
        try:
            result = await self._run(step, rc.deploy_merge_command(bot_dir, branch), label="git pull（早送り）", timeout=FETCH_TIMEOUT)
        except asyncio.CancelledError:
            if not self.session.command_state_unknown:
                task = asyncio.create_task(self._check_pull_result(step, before))
                await await_shielded(task, CancellationState(canceled=True))
            raise
        except CommandStateUnknownError:
            raise  # 未終了の更新とHEAD確認・巻き戻しを並行させない。
        except SshError:
            await self._check_pull_result(step, before)
            raise
        if result.exit_status != 0:
            # 失敗しても、途中まで進んでいたら巻き戻しが要るので、今のコミットを読み直して確かめる
            await self._check_pull_result(step, before)
            raise StepFailed(f"{branch} を早送りで取り込めませんでした: {_last_line(result.stderr) or result.exit_status}")
        self._code_changed = True
        after = rc.check_commit(_last_line(result.stdout))
        self.record.after_commit = after
        self._check(
            step,
            f"取り込んだ後のコミットが origin/{branch} と一致",
            after == self.record.target_commit,
            f"{_short(after)}（origin/{branch} は {_short(self.record.target_commit)}）",
        )
        log = await self._run(step, rc.deploy_log_command(bot_dir, before, after), label="取り込んだコミット")
        self.record.commits = rc.parse_commit_list(log.stdout) if log.exit_status == 0 else []
        step.message = f"コミット {len(self.record.commits)} 件を取り込みました（{_short(before)} → {_short(after)}）。"

    async def _check_pull_result(self, step: Step, before: str) -> None:
        """通信失敗・非ゼロ終了のどちらでも、実際のHEADから変更を追跡する。"""
        try:
            head = await self._head(step)
        except (SshError, ValueError, StepFailed):
            # 状態不明を未変更と扱わず、既存の巻き戻しで復旧の可否を記録する。
            self._code_changed = True
            self._append_output(step, "取り込み後のコミットを確認できません。変更の有無は不明のため、巻き戻しを試みます。")
        else:
            self.record.after_commit = head
            self._code_changed = head != before
            self._append_output(step, f"取り込み結果を再確認しました（現在のコミット: {_short(head)}）。")

    async def _deps(self, step: Step) -> None:
        bot_dir = self.profile.bot_dir
        before, after = self.record.before_commit or "", self.record.after_commit or ""
        diff = await self._run(step, rc.deploy_requirements_diff_command(bot_dir, before, after), label="依存関係の定義の変更")
        if diff.exit_status != 0:
            raise StepFailed(f"依存関係の定義の変更を調べられませんでした: {_last_line(diff.stderr) or diff.exit_status}")
        if not diff.stdout.strip():
            step.checks.append(Check("requirements.txt・constraints.txt に変更なし", True, f"{_short(before)} → {_short(after)}"))
            step.status = "skipped"
            step.message = "依存関係は変わっていないため、入れ直しません。"
            return
        self._deps_changed = True  # 途中で失敗しても、前の requirements.txt・constraints.txt で入れ直す必要がある
        await self._install_requirements(step)
        # 何か出力があれば入れ直す（安全側）。表示には分かったファイル名だけを使う
        changed = [name for name in rc.DEPENDENCY_FILES if name in diff.stdout.split()] or ["依存関係の定義"]
        step.message = f"{'・'.join(changed)} が変わったため、依存関係を入れ直しました。"

    async def _install_requirements(self, step: Step) -> None:
        bot_dir, python = self.profile.bot_dir, self.profile.python
        result = await self._run(step, rc.deploy_pip_install_command(bot_dir, python), label="pip install", timeout=PIP_TIMEOUT)
        self._check(step, "pip install が成功（終了コード 0）", result.exit_status == 0, _last_line(result.stderr) or _last_line(result.stdout))
        result = await self._run(step, rc.deploy_pip_check_command(bot_dir, python), label="pip check")
        self._check(step, "pip check で依存関係の食い違いなし", result.exit_status == 0, _last_line(result.stdout) or _last_line(result.stderr))

    async def _restart(self, step: Step) -> None:
        await self._restart_and_verify(step)

    async def _restart_and_verify(self, step: Step) -> None:
        before = await self._service(step, quiet=False)
        old_pid = before["main_pid"]
        clock = await self._run(step, rc.SERVER_TIME_COMMAND, label="サーバーの時刻", quiet=True)
        if clock.exit_status != 0:
            raise StepFailed("サーバーの時刻を取得できませんでした。")
        try:
            # 時計の秒の切り替わりで取りこぼさないよう、1秒前から読む
            step_since = int(clock.stdout.strip()) - 1
        except ValueError as error:
            raise StepFailed(f"サーバーの時刻を読めませんでした: {clock.stdout.strip()!r}") from error

        self._restart_attempted = True
        result = await self._run(
            step, rc.service_control_command(self.profile.service, "restart"), label="再起動", timeout=RESTART_TIMEOUT
        )
        needs_password = "password is required" in result.stderr or "a terminal is required" in result.stderr
        self._check(
            step,
            "systemctl restart が成功（終了コード 0）",
            result.exit_status == 0,
            _last_line(result.stderr) or f"終了コード {result.exit_status}",
            "sudo にパスワードが必要なため、再起動できませんでした（tools/console/examples/sudoers.example を参照）。"
            if needs_password
            else None,
        )

        deadline = self._clock() + self._restart_settle_timeout
        polls = 0
        while True:
            state = await self._service(step, quiet=True)
            polls += 1
            if (state["active_state"] in ("failed", "inactive") or state["sub_state"] == "auto-restart"
                    or (state["restarts"] is not None and before["restarts"] is not None
                        and state["restarts"] > before["restarts"])):
                raise StepFailed("ボットが起動の途中で停止または自動再起動しました。")
            running = state["active_state"] == "active" and state["sub_state"] == "running"
            if running and state["main_pid"] and state["main_pid"] != old_pid:
                break
            if self._clock() >= deadline:
                self._append_output(step, f"（状態を {polls} 回確かめました。最後の状態: {state['active_state']} / {state['sub_state']}）")
                raise StepFailed(
                    f"{self._restart_settle_timeout:g} 秒たっても新しいプロセスで稼働中になりませんでした"
                    f"（状態: {state['active_state']} / {state['sub_state']}、PID: {state['main_pid'] or 'なし'}）。"
                )
            await self._sleep(self._poll_interval)
        self._append_output(step, f"（状態を {polls} 回確かめ、稼働中になったことを確認しました）")
        step.checks.append(Check("稼働中（active / running）", True, f"{state['active_state']} / {state['sub_state']}"))
        step.checks.append(Check("PID が変わった（新しいプロセスで起動した）", True, f"{old_pid or 'なし'} → {state['main_pid']}"))
        step.message = f"新しいプロセス（PID {state['main_pid']}）で起動しました。"
        self._since = step_since
        self._restarts_after = state["restarts"]
        self._pid_after = state["main_pid"]

    async def _health(self, step: Step) -> None:
        await self._health_check(step)

    async def _health_check(self, step: Step) -> None:
        since, restarts_after = self._since, self._restarts_after
        pid_after = self._pid_after
        command = rc.journal_since_command(self.profile.service, since, use_sudo=self.profile.journal_sudo)
        step.commands.append(command)
        started = self._clock()
        deadline = started + self._health_timeout
        entries: list[dict] = []
        found: dict | None = None
        while True:
            entries = await self._logs_since(step, command)
            found = next((entry for entry in entries
                          if pid_after is not None and entry.get("pid") == pid_after
                          and rc.LOGIN_SUCCESS_TEXT in entry["message"]), None)
            state = await self._service(step, quiet=True)
            # 起動に失敗して止まった・落ちて再起動を待っているなら、待たずに失敗にする
            if state["active_state"] in ("failed", "inactive") or state["sub_state"] == "auto-restart":
                # ログを読んだ後・状態を読む前に止まったときは、止まった原因（例外など）がまだ手元に無い。
                # 原因を画面に残すため、止まったと分かってから読み直す
                self._append_log_tail(step, await self._logs_since(step, command))
                raise StepFailed(f"ボットが起動の途中で止まりました（状態: {state['active_state']} / {state['sub_state']}）。")
            await self._check_startup_process(step, state, pid_after, restarts_after, command)
            if found:
                break
            if self._clock() >= deadline:
                self._append_log_tail(step, entries)
                raise StepFailed(f"{self._health_timeout:g} 秒たっても「{rc.LOGIN_SUCCESS_TEXT}」のログが出ませんでした。")
            await self._sleep(self._poll_interval)

        waited = max(0.0, self._clock() - started)
        self._append_log_tail(step, entries)
        step.checks.append(Check(f"起動したPIDの「{rc.LOGIN_SUCCESS_TEXT}」のログが出た", True,
                                 f"PID {pid_after}: {found['message']}"))
        state = await self._service(step, quiet=False)
        await self._check_startup_process(step, state, pid_after, restarts_after, command)
        self._check(
            step,
            "サービスが稼働中のまま",
            state["active_state"] == "active" and state["sub_state"] == "running",
            f"{state['active_state']} / {state['sub_state']}",
        )
        self._check(
            step,
            "落ちて自動再起動していない",
            state["restarts"] == restarts_after,
            f"自動再起動の回数: {restarts_after} → {state['restarts']}",
        )
        step.message = f"「{rc.LOGIN_SUCCESS_TEXT}」まで確認しました（再起動から約 {waited:.0f} 秒）。"

    async def _check_startup_process(self, step: Step, state: dict, pid: int | None, restarts: int | None, command: str) -> None:
        """ログ待機中と成功確定直前に、再起動で確認したプロセスが続いているかを照合する。

        落ちて別のPIDで再起動済みだった場合も、原因を追えるよう失敗の前に直近のログを添える。
        """
        if (pid is None or state["main_pid"] != pid) or state["restarts"] != restarts:
            self._append_log_tail(step, await self._logs_since(step, command))
        if pid is None or state["main_pid"] != pid:
            self._check(step, "起動確認中のPIDが変わっていない", False,
                        f"確認対象: {pid or 'なし'} → 現在: {state['main_pid'] or 'なし'}",
                        failure="起動確認中にボットのプロセスが変わりました。サーバーの状態を確認してください。")
        if state["restarts"] != restarts:
            self._check(step, "落ちて自動再起動していない", False,
                        f"自動再起動の回数: {restarts} → {state['restarts']}",
                        failure="起動確認中にボットが自動再起動しました。サーバーの状態を確認してください。")

    async def _logs_since(self, step: Step, command: str) -> list[dict]:
        result = await self._run(step, command, label="再起動後のログ", quiet=True)
        if result.exit_status != 0:
            self._check(
                step, "再起動後のログを取得できた", False, f"終了コード {result.exit_status}",
                failure="再起動後のログを取得できませんでした。journal_sudo とログの読み取り権限を確認してください。",
            )
        return [entry for line in result.stdout.splitlines() if (entry := rc.parse_journal_line(line))]

    def _append_log_tail(self, step: Step, entries: list[dict]) -> None:
        tail = entries[-HEALTH_LOG_TAIL:]
        if tail:
            step.output_contains_logs = True
            self._append_output(step, "再起動後のログ（古い順）:\n" + "\n".join(entry["message"] for entry in tail))
        else:
            self._append_output(step, "再起動後のログはまだありません。")

    # ------------------------------------------------------------ 巻き戻し

    async def _wait_for_service_job(self) -> None:
        """再起動に伴う systemd の処理が終わる前にコードを戻さない。"""
        self.session.require_command_completion()
        await wait_for_service_job(
            self.session, self.profile.service, timeout=RESTART_TIMEOUT, poll=0.5,
            unknown_on_cancel=False,
            unknown_message="systemd ジョブの終了を確認できないため、巻き戻しを停止しました。サーバーの状態を確認してください。",
        )

    async def _rollback(self) -> None:
        self._rollback_started = True
        if self._restart_attempted:
            await self._wait_for_service_job()
        for key, title, description in ROLLBACK_STEPS:
            if not any(step.key == key for step in self.record.steps):
                self.record.steps.append(Step(key, title, description, phase="rollback"))

        if not await self._step("rollback_code", self._rollback_code):
            self._skip_rest("コードを戻せなかったため、実行しません。")
            self._finish_rollback_failed()
            return
        ok = await self._step("rollback_deps", self._rollback_deps)
        if self._restart_attempted and ok:
            ok = await self._step("rollback_restart", self._restart_and_verify) and await self._step("rollback_health", self._health_check)
        elif not self._restart_attempted:
            for key in ("rollback_restart", "rollback_health"):
                step = self._find(key)
                step.status = "skipped"
                step.message = "再起動する前に止めたため不要です（ボットは前の版のまま動いています）。"
        self._skip_rest("前のステップで止めたため、実行しません。")
        if ok:
            self._finish("rolled_back", f"デプロイに失敗したため、前の版（{_short(self.record.before_commit)}）へ戻しました。")
        else:
            self._finish_rollback_failed()

    def _rollback_expected_heads(self) -> list[str]:
        """巻き戻しの前に HEAD として現れてよい、デプロイ前以外のコミット（取り込み先と取り込み後）。

        これ以外のコミットに HEAD があるなら、デプロイの外で動かされたので、その参照は書き換えない。
        """
        return [commit for commit in (self.record.after_commit, self.record.target_commit) if commit]

    def _rollback_refusal_reason(self, result: CommandResult) -> str | None:
        """巻き戻しコマンドが、サーバーの状態が想定と違うためコードに触れずに止まったなら、その理由を返す。"""
        observed = (result.stdout.strip().splitlines() or [""])[0][:100]
        branch = self.profile.deploy_branch
        status = result.exit_status
        if status == rc.ROLLBACK_EXIT_BRANCH_CHANGED:
            return f"デプロイ中にサーバーのブランチが {branch} から {observed or '不明'} に変わっていました。"
        if status == rc.ROLLBACK_EXIT_UNEXPECTED_HEAD:
            return f"デプロイ中にサーバーのコミットが、デプロイ前でも取り込み後でもない {_short(observed)} に動いていました。"
        if status == rc.ROLLBACK_EXIT_TREE_CHANGED:
            detail = f"（{observed} 件）" if observed.isdigit() else ""
            return f"デプロイ中にサーバーの追跡ファイルが変更されていました{detail}。"
        if status == rc.ROLLBACK_EXIT_UNREADABLE:
            return "巻き戻しの前に、サーバーのブランチ・コミット・変更の有無を読めませんでした。"
        return None

    async def _rollback_code(self, step: Step) -> None:
        before = self.record.before_commit or ""
        result = await self._run(
            step,
            rc.deploy_rollback_command(self.profile.bot_dir, self.profile.deploy_branch, before, self._rollback_expected_heads()),
            label="コードを戻す",
        )
        reason = self._rollback_refusal_reason(result)
        if reason is not None:
            # 何も変えずに止まった。変更や別のブランチを巻き込まないよう、ここで巻き戻しを打ち切る。
            self._rollback_refusal = reason
            step.checks.append(Check("巻き戻しの前提（ブランチ・コミット・未コミットの変更）が変わっていない", False, reason))
            raise StepFailed(f"安全のため巻き戻しを中止しました。{reason}サーバーのコードと変更には触れていません。")
        head = _last_line(result.stdout)
        self._check(
            step,
            "コードがデプロイ前のコミットに戻った",
            result.exit_status == 0 and head == before,
            f"今のコミット: {_short(head) if head else '不明'}（デプロイ前: {_short(before)}）",
        )
        step.message = f"コードを {_short(before)} に戻しました。"

    async def _rollback_deps(self, step: Step) -> None:
        if not self._deps_changed:
            step.status = "skipped"
            step.message = "依存関係は入れ直していないため不要です。"
            return
        await self._install_requirements(step)
        step.message = "前の requirements.txt・constraints.txt で依存関係を入れ直しました。"

    def _finish_rollback_failed(self) -> None:
        bot_dir, service = self.profile.bot_dir, self.profile.service
        branch, before = self.profile.deploy_branch, self.record.before_commit
        restart = (
            f"依存関係を入れ直してから sudo systemctl restart {service} で起動し、"
            f"journalctl -u {service} -n 50 で「{rc.LOGIN_SUCCESS_TEXT}」を確かめてください。"
        )
        # --keep は未コミットの変更を消さず、戻すコミットと衝突するファイルがあれば中止する（--hard は使わない）。
        keep = "（--keep は未コミットの変更を消さず、衝突するときは中止します）"
        if self._rollback_refusal is not None:
            self._finish(
                "rollback_failed",
                f"安全のため巻き戻しを中止しました。{self._rollback_refusal}"
                "サーバーのコード・ブランチ・未コミットの変更には何も手を加えていません。サーバーで手動の対応が必要です: "
                f"cd {bot_dir} && git status && git log -3 で今の状態を確かめ、変更を片付けてから、"
                f"ボットのコードを {before} に戻す必要があれば git switch {branch} && git reset --keep {before} を実行し{keep}、{restart}",
            )
            return
        self._finish(
            "rollback_failed",
            "巻き戻しにも失敗しました。サーバーで手動の対応が必要です: "
            f"cd {bot_dir} && git reset --keep {before} を実行し{keep}、{restart}",
        )


def new_record(record_id: int, profile: Profile) -> DeployRecord:
    steps = [
        Step(key, title.format(branch=profile.deploy_branch, service=profile.service), description.format(branch=profile.deploy_branch))
        for key, title, description in DEPLOY_STEPS
    ]
    return DeployRecord(
        id=record_id,
        profile_id=profile.id,
        environment=profile.environment,
        service=profile.service,
        branch=profile.deploy_branch,
        started_at=time.time(),
        steps=steps,
    )


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
