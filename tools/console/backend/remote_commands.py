"""SSH で実行するコマンドの組み立てと、その出力の読み取り（パース）。

★ ここに書いたコマンドだけがサーバーで実行される（ホワイトリスト方式）。
  ブラウザから届いた文字列をそのままコマンドに埋め込まないこと。
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Iterable
from pathlib import Path

# systemctl show で取り出す項目
SERVICE_PROPERTIES = (
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "UnitFileState",
    "MainPID",
    "NRestarts",
    "MemoryCurrent",
    "CPUUsageNSec",
    "ExecMainStartTimestampMonotonic",
)

# サービス操作として許可する動作。これ以外は実行しない
SERVICE_ACTIONS = ("start", "stop", "restart")

# 一度に取得するログの最大行数
MAX_LOG_LINES = 1000

# systemd が「値なし」を表すときに使う数値（2^64 - 1）
_SYSTEMD_UNSET = 18446744073709551615
# サービス名に使ってよい文字（シェルに渡すため、記号を厳しく制限する）。profiles.py の検証と共有する。
SAFE_SERVICE_NAME = re.compile(r"[A-Za-z0-9@._][A-Za-z0-9@._-]*")

# 疎通確認（何もしないコマンド。往復時間の計測に使う）
PING_COMMAND = "true"

# 接続直後に1回だけ取るサーバー情報（ホスト名 / カーネル / OS）
SERVER_INFO_COMMAND = (
    "hostname; uname -sr; "
    "grep '^PRETTY_NAME=' /etc/os-release | cut -d= -f2- | tr -d '\"'"
)

# CPU 負荷・メモリ・ディスクを1回のコマンドでまとめて取る
RESOURCES_COMMAND = (
    "cat /proc/uptime /proc/loadavg; nproc; free -b; "
    "df -B1 --output=size,used,avail / | tail -n 1"
)


def _check_safe_name(value: str) -> str:
    if not SAFE_SERVICE_NAME.fullmatch(value):
        raise ValueError(f"使えない文字を含む名前です: {value!r}")
    return value


# ---------------------------------------------------------------- コマンドの組み立て


def service_show_command(service: str) -> str:
    properties = ",".join(SERVICE_PROPERTIES)
    return f"systemctl show {_check_safe_name(service)} --no-pager --property={properties}"


def service_control_command(service: str, action: str) -> str:
    """サービスの起動・停止・再起動。

    sudo -n は「パスワードを聞かずに失敗する」指定。付けないと、パスワードの入力待ちで
    コマンドが返ってこなくなる。
    """
    if action not in SERVICE_ACTIONS:
        raise ValueError(f"許可されていない操作です: {action!r}")
    return f"sudo -n systemctl {action} {_check_safe_name(service)}"


def service_job_command(service: str) -> str:
    """値のないJobも明示行で取得し、応答欠落とジョブなしを区別する。"""
    return f"systemctl show {_check_safe_name(service)} --no-pager --all --property=Job"


def parse_service_job(text: str) -> int:
    """systemctlの空のJob表示と、版による0表示をジョブなしとして扱う。"""
    match = re.fullmatch(r"Job=([0-9]*)", text.strip())
    if match is None:
        raise ValueError("systemdジョブの応答形式を確認できませんでした。")
    return int(match.group(1) or "0")


def journal_command(service: str, *, lines: int, follow: bool, use_sudo: bool) -> str:
    """journald からボットのログを JSON 形式で取り出す（1行が1件）。"""
    lines = max(1, min(int(lines), MAX_LOG_LINES))
    prefix = "sudo -n " if use_sudo else ""
    follow_flag = " -f" if follow else ""
    return f"{prefix}journalctl -u {_check_safe_name(service)} -n {lines} -o json --no-pager{follow_flag}"


def git_command(bot_dir: str) -> str:
    """ブランチ / 最新コミット / タグ / 未コミットのファイル数 を1行ずつ出す。"""
    directory = shlex.quote(bot_dir)
    return (
        f"cd {directory} && git rev-parse --abbrev-ref HEAD"
        " && git log -1 --format=%h%x1f%s%x1f%cI"
        " && (git describe --tags --always 2>/dev/null || echo -)"
        " && git status --porcelain | wc -l"
    )


# ---------------------------------------------------------------- デプロイ（git pull → 依存の更新 → 再起動 → ヘルスチェック）
#
# デプロイは deploy.py が、ここで組み立てたコマンドを1つずつ実行して結果を確かめながら進める。
# ブランチ名・コミット・Python の場所は画面からは受け取らず、接続先の設定（profiles.json）と
# サーバーから読んだ値だけを使う。それでも埋め込む前に、使ってよい文字かを必ず確かめる。

# 取り込む元のリモート（GitHub の既定の名前）
DEPLOY_REMOTE = "origin"
# 起動できたと判断するログ（ボットの on_ready が出す「ログイン成功: <ボット名>」）
LOGIN_SUCCESS_TEXT = "ログイン成功"

_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SAFE_PYTHON = re.compile(r"^[A-Za-z0-9._/@+-]+$")

# サーバーの現在時刻（UNIX 秒）。再起動より後のログだけを読むための基準にする
SERVER_TIME_COMMAND = "date +%s"


def check_branch(branch: str) -> str:
    if not _SAFE_BRANCH.match(branch) or ".." in branch or branch.startswith(("-", "/")):
        raise ValueError(f"使えない文字を含むブランチ名です: {branch!r}")
    return branch


def check_commit(commit: str) -> str:
    """コミットは 40 桁の16進数（git rev-parse の出力）だけを受け付ける。"""
    if not _COMMIT.match(commit):
        raise ValueError(f"コミットの形ではありません: {commit!r}")
    return commit


def check_python(python: str) -> str:
    if not _SAFE_PYTHON.match(python) or ".." in python.split("/"):
        raise ValueError(f"使えない文字を含む Python の場所です: {python!r}")
    return python


def _in_bot_dir(bot_dir: str, command: str) -> str:
    return f"cd {shlex.quote(bot_dir)} && {command}"


def deploy_state_command(bot_dir: str) -> str:
    """今のブランチ / コミット / 未コミットの変更の数（追跡していないファイルは数えない）を1行ずつ出す。"""
    return _in_bot_dir(
        bot_dir,
        "git rev-parse --abbrev-ref HEAD && git rev-parse HEAD && git status --porcelain --untracked-files=no | wc -l",
    )


def deploy_fetch_command(bot_dir: str, branch: str) -> str:
    """リモートの最新を取り寄せ、取り込む先のコミット / 新しいコミットの数 / サーバーにだけあるコミットの数を出す。"""
    branch = check_branch(branch)
    target = f"refs/remotes/{DEPLOY_REMOTE}/{branch}"
    # single-branch clone の追跡範囲外でも、判定・取り込みに使う参照を更新する。
    refspec = f"+refs/heads/{branch}:{target}"
    return _in_bot_dir(
        bot_dir,
        f"git fetch --quiet {DEPLOY_REMOTE} {refspec}"
        f" && git rev-parse {target}"
        f" && git rev-list --count HEAD..{target}"
        f" && git rev-list --count {target}..HEAD",
    )


def deploy_merge_command(bot_dir: str, branch: str) -> str:
    """早送りだけで取り込む（git pull --ff-only と同じ）。最後の行に取り込んだ後のコミットを出す。"""
    return _in_bot_dir(bot_dir, f"git merge --ff-only refs/remotes/{DEPLOY_REMOTE}/{check_branch(branch)} && git rev-parse HEAD")


def deploy_log_command(bot_dir: str, before: str, after: str) -> str:
    """取り込んだコミットの一覧（短い ID と件名）。"""
    return _in_bot_dir(bot_dir, f"git log --format=%h%x1f%s {check_commit(before)}..{check_commit(after)}")


# 変わったら依存関係を入れ直すファイル。requirements.txt は -c で constraints.txt（推移依存の固定版）を読むので、
# constraints.txt だけが変わったときも入れ直さないと、新しい固定版が本番に入らない（Issue #285）
DEPENDENCY_FILES = ("requirements.txt", "constraints.txt")


def deploy_requirements_diff_command(bot_dir: str, before: str, after: str) -> str:
    """依存関係の定義（DEPENDENCY_FILES）のうち変わったもののファイル名を出す（変わっていなければ何も出ない）。"""
    files = " ".join(DEPENDENCY_FILES)
    return _in_bot_dir(bot_dir, f"git diff --name-only {check_commit(before)} {check_commit(after)} -- {files}")


def deploy_pip_install_command(bot_dir: str, python: str) -> str:
    """ボットの仮想環境に、requirements.txt の依存関係を入れる。"""
    py = shlex.quote(check_python(python))
    return _in_bot_dir(bot_dir, f"{py} -m pip install --disable-pip-version-check --no-input -r requirements.txt")


def deploy_pip_check_command(bot_dir: str, python: str) -> str:
    """入れた依存関係どうしが食い違っていないか確かめる。"""
    return _in_bot_dir(bot_dir, f"{shlex.quote(check_python(python))} -m pip check")


# 巻き戻しコマンドが、コードに触れずに止まったときの終了コード（理由は deploy.py が画面に出す）。
# git 自身の失敗（1・128 など）とは重ならない番号にしてある。
ROLLBACK_EXIT_UNREADABLE = 3  # ブランチ・HEAD・変更の有無を読めなかった
ROLLBACK_EXIT_BRANCH_CHANGED = 10  # デプロイ中に別のブランチへ移っていた
ROLLBACK_EXIT_UNEXPECTED_HEAD = 11  # HEAD がデプロイ前でも取り込み後でもない
ROLLBACK_EXIT_TREE_CHANGED = 12  # 追跡ファイルに未コミットの変更がある


def deploy_rollback_command(bot_dir: str, branch: str, commit: str, expected_heads: Iterable[str] = ()) -> str:
    """巻き戻し: デプロイ前のコミット（commit）へ戻す。取り込んだコミットは GitHub に残るので失われない。

    **1回の遠隔コマンドの中で**、戻す直前に次の3つを確かめ、1つでも外れていたらコードに触れずに止まる
    （終了コードは ROLLBACK_EXIT_*。標準出力の先頭行に見つけた値を出す）:
      - 今のブランチが branch である（別のブランチへ移っていたら、その参照を書き換えない）
      - HEAD が commit か expected_heads（取り込み先・取り込み後）のどれかである
      - 追跡ファイルに未コミットの変更が無い
    HEAD がすでに commit なら戻す操作は要らないので、何もせずに成功とする。
    戻すのは `git reset --hard` ではなく `--keep`。確認から実行までのわずかな間に編集されても、
    戻すコミットと違うファイルの変更は残り、衝突するファイルがあれば git が中止する（変更を消さない）。
    """
    branch = check_branch(branch)
    commit = check_commit(commit)
    heads = [head for head in dict.fromkeys(check_commit(item) for item in expected_heads) if head != commit]
    cases = [f'{commit}) echo "$head"; exit 0;;']
    if heads:
        cases.append(f"{'|'.join(heads)}) ;;")
    cases.append(f'*) echo "$head"; exit {ROLLBACK_EXIT_UNEXPECTED_HEAD};;')
    script = (
        f"branch=$(git rev-parse --abbrev-ref HEAD) || exit {ROLLBACK_EXIT_UNREADABLE}; "
        f"head=$(git rev-parse HEAD) || exit {ROLLBACK_EXIT_UNREADABLE}; "
        f'[ "$branch" = {shlex.quote(branch)} ] || {{ echo "$branch"; exit {ROLLBACK_EXIT_BRANCH_CHANGED}; }}; '
        f'case "$head" in {" ".join(cases)} esac; '
        f"changes=$(git status --porcelain --untracked-files=no) || exit {ROLLBACK_EXIT_UNREADABLE}; "
        f'[ -z "$changes" ] || {{ printf \'%s\\n\' "$changes" | wc -l; exit {ROLLBACK_EXIT_TREE_CHANGED}; }}; '
        f"git reset --keep {commit} && git rev-parse HEAD"
    )
    return _in_bot_dir(bot_dir, f"sh -c {shlex.quote(script)}")


def journal_since_command(service: str, since: int, *, use_sudo: bool) -> str:
    """指定した時刻（UNIX 秒）より後のログを JSON で出す（再起動後のログだけを見るため）。"""
    prefix = "sudo -n " if use_sudo else ""
    return f"{prefix}journalctl -u {_check_safe_name(service)} --since @{int(since)} -o json --no-pager"


def insights_command(bot_dir: str, service: str, *, use_sudo: bool = False) -> str:
    """remote_scripts/insights.py（INSIGHTS_SCRIPT）を標準入力から python3 に渡して実行する。"""
    sudo_flag = " --journal-sudo" if use_sudo else ""
    return f"python3 - --insights {shlex.quote(bot_dir)} {_check_safe_name(service)}{sudo_flag}"


def db_summary_command(bot_dir: str) -> str:
    """DB の集計スクリプト（DB_SUMMARY_SCRIPT）を標準入力から python3 に渡して実行する。"""
    base = bot_dir.rstrip("/")
    db_path = shlex.quote(f"{base}/data/memory/bot.db")
    blacklist_path = shlex.quote(f"{base}/data/ng/blacklist.json")
    return f"python3 - {db_path} {blacklist_path}"


# 中身を表示できる bot.db のテーブル（remote_scripts/db_table.py の TABLES と同じ。テストで照合する）
DB_TABLES = (
    "affinity",
    "user_stats",
    "queue_tasks",
    "pending_log",
    "legacy_imports",
    "profile",
    "guild_settings",
    "anniversary_posts",
    "reminders",
    "feedback",
)
# テーブルの中身を一度に取り寄せる最大の行数（発言サンプルを持つ user_stats でも数百 KB に収まる量）
DB_TABLE_PAGE_MAX = 200


def db_table_command(bot_dir: str, table: str, offset: int, limit: int) -> str:
    """テーブルの行を取り出すスクリプト（DB_TABLE_SCRIPT）を標準入力から python3 に渡して実行する。

    テーブル名は DB_TABLES にあるものだけを受け付ける（画面から届いた文字列を、そのままコマンドに埋め込まない）。
    """
    if table not in DB_TABLES:
        raise ValueError(f"表示できないテーブルです: {table!r}")
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), DB_TABLE_PAGE_MAX))
    db_path = shlex.quote(f"{bot_dir.rstrip('/')}/data/memory/bot.db")
    return f"python3 - --db-table {db_path} {table} {offset} {limit}"


# ボットの中の状態（Discord API・サーバーごとの設定・直近24時間のログ）を集めるスクリプト。
# 中身は remote_scripts/insights.py（サーバー側で標準入力から実行する）。
INSIGHTS_SCRIPT = (Path(__file__).resolve().parent / "remote_scripts" / "insights.py").read_text(encoding="utf-8")

# bot.db の1つのテーブルの行（会話・記憶の本文や ID を含む）を読み取り専用で取り出すスクリプト。
# 中身は remote_scripts/db_table.py（データページで、テーブルの中身を開いたときだけ使う）。
DB_TABLE_SCRIPT = (Path(__file__).resolve().parent / "remote_scripts" / "db_table.py").read_text(encoding="utf-8")

# サーバー側で動かす集計スクリプト（標準ライブラリだけで動く）。
# ★ DB は mode=ro（読み取り専用）で開き、件数だけを返す。会話や記憶の中身は持ち出さない
#   （中身は、テーブルを開いたときだけ DB_TABLE_SCRIPT で取り寄せる）。
DB_SUMMARY_SCRIPT = (Path(__file__).resolve().parent / "remote_scripts" / "db_summary.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 出力の読み取り


def _to_int(value: object) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if 0 <= number < _SYSTEMD_UNSET else None


def _to_float(value: str) -> float | None:
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_key_values(text: str) -> dict[str, str]:
    """`キー=値` の行を辞書にする（systemctl show の出力形式）。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()
    return values


def parse_service_show(text: str, service: str, host_uptime_seconds: float | None) -> dict:
    values = parse_key_values(text)
    active_state = values.get("ActiveState", "unknown")
    cpu_ns = _to_int(values.get("CPUUsageNSec"))
    start_us = _to_int(values.get("ExecMainStartTimestampMonotonic"))

    # 起動からの経過時間 = サーバーの稼働時間 - 「起動時点の稼働時間」
    uptime = None
    if active_state == "active" and start_us and host_uptime_seconds is not None:
        uptime = max(0.0, host_uptime_seconds - start_us / 1_000_000)

    return {
        "name": service,
        "description": values.get("Description", ""),
        "load_state": values.get("LoadState", ""),
        "active_state": active_state,
        "sub_state": values.get("SubState", ""),
        "result": values.get("Result", ""),
        "unit_file_state": values.get("UnitFileState", ""),
        "main_pid": _to_int(values.get("MainPID")) or None,
        "restarts": _to_int(values.get("NRestarts")),
        "memory_bytes": _to_int(values.get("MemoryCurrent")),
        "cpu_seconds": None if cpu_ns is None else cpu_ns / 1_000_000_000,
        "uptime_seconds": uptime,
    }


def parse_resources(text: str) -> dict:
    """RESOURCES_COMMAND の出力を読む。読めなかった項目は None のまま返す。"""
    lines = [line for line in text.splitlines() if line.strip()]
    result: dict = {
        "uptime_seconds": None,
        "load": None,
        "cpu_count": None,
        "memory": None,
        "swap": None,
        "disk": None,
    }
    try:
        result["uptime_seconds"] = _to_float(lines[0].split()[0])
    except (IndexError, ValueError):
        pass
    try:
        load = lines[1].split()
        values = [_to_float(load[index]) for index in range(3)]
        if all(value is not None for value in values):
            result["load"] = values
    except (IndexError, ValueError):
        pass
    try:
        result["cpu_count"] = _to_int(lines[2]) or None
    except (IndexError, ValueError):
        pass

    for line in lines:
        parts = line.split()
        try:
            if parts[0] == "Mem:":
                total, available = int(parts[1]), int(parts[6])
                # 「使用中」は total - available で数える（キャッシュは解放できるので使用中に含めない）
                result["memory"] = {"total": total, "used": total - available, "available": available}
            elif parts[0] == "Swap:":
                total, used = int(parts[1]), int(parts[2])
                result["swap"] = {"total": total, "used": used, "available": total - used}
        except (IndexError, ValueError):
            continue

    try:
        size, used, available = (int(value) for value in lines[-1].split())
        result["disk"] = {"total": size, "used": used, "available": available}
    except (IndexError, ValueError):
        pass
    return result


def parse_git(text: str) -> dict:
    lines = text.splitlines()
    if len(lines) < 4:
        raise ValueError("Git の出力が想定より短いです。")
    commit, subject, committed_at = (lines[1].split("\x1f") + ["", ""])[:3]
    return {
        "branch": lines[0].strip(),
        "commit": commit.strip(),
        "subject": subject.strip(),
        "committed_at": committed_at.strip(),
        "describe": lines[2].strip(),
        "dirty_files": _to_int(lines[3]) or 0,
    }


def parse_journal_line(line: str) -> dict | None:
    """journalctl -o json の1行を読む。JSON でない行（区切りの表示など）は None。"""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    message = data.get("MESSAGE", "")
    if isinstance(message, list):
        # 不正な UTF-8 を含むと、journald は本文を「バイトの数値の配列」で返す
        try:
            message = bytes(message).decode("utf-8", errors="replace")
        except (TypeError, ValueError):
            message = ""
    elif message is None:
        message = ""

    timestamp_us = _to_int(data.get("__REALTIME_TIMESTAMP")) or 0
    return {
        "ts": timestamp_us // 1000,
        "message": str(message),
        "priority": _to_int(data.get("PRIORITY")),
        "pid": _to_int(data.get("_PID")),
        "source": str(data.get("SYSLOG_IDENTIFIER") or data.get("_COMM") or ""),
    }


def parse_lines(text: str, count: int) -> list[str]:
    """先頭から count 行を取り出す（足りなければ ValueError）。git の出力の読み取りに使う。"""
    lines = [line.strip() for line in text.strip().splitlines()]
    if len(lines) < count:
        raise ValueError(f"出力が想定より短いです（{len(lines)} 行。{count} 行を想定）。")
    return lines[:count]


def parse_commit_list(text: str) -> list[dict]:
    """deploy_log_command の出力を [{"hash": 短い ID, "subject": 件名}] にする。"""
    commits = []
    for line in text.splitlines():
        if not line.strip():
            continue
        short, separator, subject = line.partition("\x1f")
        if not separator or not re.fullmatch(r"[0-9a-fA-F]{7,40}", short.strip()) or not subject.strip() or "\x1f" in subject:
            raise ValueError("コミット一覧の形式が不正です。ハッシュと件名を確認してください。")
        commits.append({"hash": short.strip(), "subject": subject.strip()})
    return commits


def parse_server_info(text: str) -> dict:
    lines = text.splitlines() + ["", "", ""]
    return {"hostname": lines[0].strip(), "kernel": lines[1].strip(), "os": lines[2].strip()}
