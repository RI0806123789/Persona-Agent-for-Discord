"""Windows の開発用コンソールを非表示で起動し、画面の寿命に合わせて終了する。"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


BACKEND_URL = "http://127.0.0.1:8765/api/connection"
FRONTEND_URL = "http://127.0.0.1:5173"
STARTUP_TIMEOUT = 30
WINDOW_TIMEOUT = 10
POLL_INTERVAL = 0.2
# 依存更新と巻き戻しの各900秒の待機も収まる猶予。超過時だけ強制終了する。
BACKEND_SHUTDOWN_TIMEOUT = 3600
# 専用画面に使うブラウザと専用プロファイルのフォルダ名。上のものほど優先する（既定は Chrome）。
# プロファイルを分けるのは、Chrome と Edge が別の版番号でプロファイルを書き換えるため。
# 共有して交互に使うと、相手の版を新しい版とみなして読み込めなくなるおそれがある。
# Edge は従来のフォルダ名のままにし、これまでの画面の設定を引き継ぐ。
BROWSER_CANDIDATES = (
    ("Google/Chrome/Application/chrome.exe", "browser-profile-chrome"),
    ("Microsoft/Edge/Application/msedge.exe", "browser-profile"),
)
_LIFETIME_RESOURCES = []


def attach_lifetime_job() -> None:
    """監視プロセスが強制終了された場合も、自分の子孫だけを OS に回収させる。"""
    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "read_operations", "write_operations", "other_operations",
            "read_bytes", "write_bytes", "other_bytes",
        )]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", IoCounters),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(job)
        raise ctypes.WinError(error)
    # ハンドルを子へ継承せず、監視プロセスの終了まで開いておく。
    # OS が最後のハンドルを閉じることで、Python・Vite・ブラウザの子孫も終了する。
    _LIFETIME_RESOURCES.append(job)


def find_browser_window(process_id: int) -> int | None:
    """空の補助ウィンドウを除き、コンソール本体のウィンドウを取得する。"""
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = None

    @callback_type
    def visit(window, _parameter):
        nonlocal found
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == process_id and user.IsWindowVisible(window):
            title = ctypes.create_unicode_buffer(1024)
            user.GetWindowTextW(window, title, len(title))
            # index.html の title。取得後はハンドルで追い、再読込中のタイトル変更を無視する。
            if title.value == "Persona Bot Console":
                found = window
                return False
        return True

    user.EnumWindows(visit, 0)
    return found


def browser_window_is_open(window: int) -> bool:
    user = ctypes.WinDLL("user32", use_last_error=True)
    user.IsWindow.argtypes = [wintypes.HWND]
    return bool(user.IsWindow(window))


def find_browser() -> tuple[Path, str]:
    """起動するブラウザと、その専用プロファイルのフォルダ名を返す。"""
    bases = [base for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                               os.environ.get("LOCALAPPDATA")) if base]
    # 1つのブラウザを全ての場所で探してから次へ進む。場所ごとに両方を試すと、
    # Program Files (x86) の Edge が Program Files の Chrome より先に見つかってしまう。
    for suffix, profile in BROWSER_CANDIDATES:
        for base in bases:
            candidate = Path(base) / suffix
            if candidate.is_file():
                return candidate, profile
    raise RuntimeError("Google Chrome または Microsoft Edge が必要です。インストール後に起動してください。")


def check_port_available(port: int) -> None:
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(f"ポート {port} は使用中です。既存のコンソールなどを閉じてから起動してください。") from error


def check_services(services: list[subprocess.Popen]) -> None:
    for process, label in zip(services, ("バックエンド", "画面配信")):
        if process.poll() is not None:
            raise RuntimeError(f"{label}が終了しました。.run 内のログを確認してください。")


def wait_until_ready(services: list[subprocess.Popen]) -> None:
    # 環境変数の HTTP プロキシを経由せず、手元の読み取り専用 API だけを確認する。
    opener = build_opener(ProxyHandler({}))
    pending = {BACKEND_URL, FRONTEND_URL}
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while pending:
        check_services(services)
        if time.monotonic() >= deadline:
            raise RuntimeError("起動確認がタイムアウトしました。.run 内のログを確認してください。")
        for url in tuple(pending):
            request = Request(url, headers={"X-Requested-By": "persona-bot-console"})
            try:
                with opener.open(request, timeout=0.5) as response:
                    if response.status == 200:
                        pending.remove(url)
            except (OSError, URLError):
                pass
        time.sleep(POLL_INTERVAL)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def stop_backend(process: subprocess.Popen, *, timeout: float = BACKEND_SHUTDOWN_TIMEOUT) -> bool:
    """バックエンドへ正常終了を要求し、デプロイの完了・巻き戻しを待つ。"""
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            logging.exception("バックエンドへの終了通知パイプを閉じられませんでした。")
    if process.poll() is not None:
        return process.returncode == 0
    logging.info("バックエンドの正常終了を待っています（最大 %s 秒）。", timeout)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.error("バックエンドが %s 秒以内に正常終了しなかったため強制終了します。サーバーのデプロイ状態を確認してください。", timeout)
        stop_process(process)
        return False
    if code != 0:
        logging.error("バックエンドが終了コード %s で終了しました。サーバーのデプロイ状態を確認してください。", code)
        return False
    logging.info("バックエンドの正常終了が完了しました。")
    return True


def _stop_backend_checked(process: subprocess.Popen) -> None:
    if not stop_backend(process):
        raise RuntimeError("バックエンドを正常終了できませんでした。.run のログとサーバーのデプロイ状態を確認してください。")


def run_console(root: Path, notify_ready) -> None:
    python = root / "backend/.venv/Scripts/python.exe"
    vite = root / "frontend/node_modules/vite/bin/vite.js"
    node = shutil.which("node.exe")
    if not python.is_file() or not vite.is_file() or not node:
        raise RuntimeError("初回セットアップが完了していません。Python・Node.js と依存関係を確認してください。")
    browser, profile = find_browser()
    for port in (8765, 5173):
        check_port_available(port)
    run = root / ".run"
    run.mkdir(exist_ok=True)
    base_environment = {
        key: value for key, value in os.environ.items() if key != "PERSONA_CONSOLE_MANAGED"
    }
    base_environment.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    backend_environment = {**base_environment, "PERSONA_CONSOLE_MANAGED": "1"}
    commands = (
        ([str(python), "-B", "app.py"], root / "backend", "backend.log", backend_environment),
        ([node, str(vite)], root / "frontend", "frontend.log", base_environment),
    )
    with ExitStack() as resources:
        services = []
        for command, directory, log_name, environment in commands:
            log = resources.enter_context((run / log_name).open("wb"))
            process = subprocess.Popen(
                command, cwd=directory, env=environment,
                stdin=subprocess.PIPE if log_name == "backend.log" else subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            resources.callback(_stop_backend_checked if log_name == "backend.log" else stop_process, process)
            services.append(process)
        wait_until_ready(services)
        # 普段のブラウザへ合流しない専用プロファイルで、画面の終了を追跡する。
        window = subprocess.Popen([
            str(browser), f"--app={FRONTEND_URL}", f"--user-data-dir={run / profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
        ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        resources.callback(stop_process, window)
        deadline = time.monotonic() + WINDOW_TIMEOUT
        console_window = find_browser_window(window.pid)
        while console_window is None:
            check_services(services)
            if window.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("コンソール画面を開けませんでした。.run 内のログを確認してください。")
            time.sleep(POLL_INTERVAL)
            console_window = find_browser_window(window.pid)
        notify_ready()
        while window.poll() is None and browser_window_is_open(console_window):
            check_services(services)
            time.sleep(POLL_INTERVAL)
        logging.info("コンソール画面が閉じられたため、起動したプロセスを終了します。")
        # ブラウザがプロファイルを書き戻す時間を確保してから、残るプロセスを回収する。
        try:
            window.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def write_status(path: Path, ok: bool, message: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"ok": ok, "message": message}, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    run = root / ".run"
    run.mkdir(exist_ok=True)
    if os.name != "nt":
        write_status(args.status_file, False, "この起動スクリプトは Windows 専用です。")
        return 1
    import msvcrt

    # PID に頼らず、監視プロセスが生存している間だけ排他する。
    lock = (run / "launcher.lock").open("a+b")
    try:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        lock.close()
        write_status(args.status_file, False, "コンソールは既に起動しています。開いている専用画面を使用してください。")
        return 1
    _LIFETIME_RESOURCES.append(lock)
    logging.basicConfig(filename=run / "launcher.log", filemode="w", encoding="utf-8", level=logging.INFO)
    ready_sent = False

    def notify_ready():
        nonlocal ready_sent
        write_status(args.status_file, True, "コンソールを起動しました。専用画面を閉じるとバックエンドも終了します。")
        ready_sent = True

    try:
        attach_lifetime_job()
        run_console(root, notify_ready)
        return 0
    except Exception as error:
        logging.exception("コンソールの起動・監視でエラーが発生しました。")
        if not ready_sent:
            write_status(args.status_file, False, str(error))
        return 1
    finally:
        # lock は終了まで保持し、Job のハンドルとともに OS に閉じてもらう。
        # PID や使用中ポートから、既存の別プロセスを停止することはない。
        logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
