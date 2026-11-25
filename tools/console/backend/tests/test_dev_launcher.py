"""開発画面の寿命と起動ブラウザの探索順、起動失敗時のプロセス回収を確認する。"""

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import Mock, patch


LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "dev_launcher.py"
spec = importlib.util.spec_from_file_location("console_dev_launcher", LAUNCHER_PATH)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ConsoleLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        temporary = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.root = Path(temporary) / "開発 console's folder"
        for relative in ("backend/.venv/Scripts/python.exe", "frontend/node_modules/vite/bin/vite.js"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        (self.root / ".run").mkdir()
        self.processes = [Mock(pid=100 + index) for index in range(3)]
        for process in self.processes:
            process.poll.return_value = None
            process.wait.return_value = 0
            process.returncode = 0
        self.spawn = self.resources.enter_context(patch.object(launcher.subprocess, "Popen", side_effect=self.processes))
        self.resources.enter_context(patch.object(launcher.shutil, "which", return_value="node.exe"))
        self.resources.enter_context(patch.object(
            launcher, "find_browser", return_value=(Path("browser.exe"), "browser-profile-chrome")))
        self.ports = self.resources.enter_context(patch.object(launcher, "check_port_available"))
        self.ready = self.resources.enter_context(patch.object(launcher, "wait_until_ready"))
        self.find_window = self.resources.enter_context(patch.object(launcher, "find_browser_window", return_value=42))
        self.window = self.resources.enter_context(patch.object(launcher, "browser_window_is_open", side_effect=[True, False]))
        self.resources.enter_context(patch.object(launcher.time, "sleep"))
        self.notify = Mock()

    def assert_stopped(self, count=3):
        for index, process in enumerate(self.processes[:count]):
            if index == 0:
                process.stdin.close.assert_called_once()
                process.terminate.assert_not_called()
            else:
                process.terminate.assert_called_once()
            process.wait.assert_called()

    def test_closing_window_stops_services_even_if_browser_is_in_background(self):
        launcher.run_console(self.root, self.notify)
        self.notify.assert_called_once()
        self.assert_stopped()
        # 引用符や空白を含むパスも、シェルのコードとして解釈させない。
        commands = self.spawn.call_args_list
        self.assertEqual(commands[0].args[0][0], str(self.root / "backend/.venv/Scripts/python.exe"))
        self.assertEqual(commands[1].args[0][1], str(self.root / "frontend/node_modules/vite/bin/vite.js"))
        self.assertEqual(commands[2].args[0][0], "browser.exe")
        self.assertIn(f"--user-data-dir={self.root / '.run/browser-profile-chrome'}", commands[2].args[0])
        self.assertEqual(commands[0].kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(commands[0].kwargs["env"]["PERSONA_CONSOLE_MANAGED"], "1")
        self.assertNotIn("PERSONA_CONSOLE_MANAGED", commands[1].kwargs["env"])
        if os.name == "nt":
            for command in commands[:2]:
                self.assertEqual(command.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_run_console_creates_missing_run_dir_and_strips_managed_env_from_frontend(self):
        """main() を通さず run_console を呼んでも .run を作り、Vite には PERSONA_CONSOLE_MANAGED を渡さない（#510）。"""
        (self.root / ".run").rmdir()
        with patch.dict(launcher.os.environ, {"PERSONA_CONSOLE_MANAGED": "1"}):
            launcher.run_console(self.root, self.notify)
        self.assertTrue((self.root / ".run").is_dir())
        commands = self.spawn.call_args_list
        self.assertEqual(commands[0].kwargs["env"]["PERSONA_CONSOLE_MANAGED"], "1")
        self.assertNotIn("PERSONA_CONSOLE_MANAGED", commands[1].kwargs["env"])

    def test_window_stays_open_across_refreshes(self):
        checks = iter([True, True, True, False])

        def window_is_open(_process_id):
            for process in self.processes:
                process.terminate.assert_not_called()
            return next(checks)

        self.window.side_effect = window_is_open
        launcher.run_console(self.root, self.notify)
        self.assert_stopped()

    def test_forced_backend_shutdown_is_reported_as_launcher_failure(self):
        with patch.object(launcher, "stop_backend", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "正常終了できません"):
                launcher.run_console(self.root, self.notify)
        self.processes[1].terminate.assert_called_once()
        self.processes[2].terminate.assert_called_once()

    def test_second_service_launch_failure_cleans_up_first_service(self):
        self.spawn.side_effect = [self.processes[0], OSError("起動失敗")]
        with self.assertRaisesRegex(OSError, "起動失敗"):
            launcher.run_console(self.root, self.notify)
        self.assert_stopped(1)
        self.notify.assert_not_called()

    def test_readiness_failure_cleans_up_both_services_without_opening_browser(self):
        self.ready.side_effect = RuntimeError("起動確認失敗")
        with self.assertRaisesRegex(RuntimeError, "起動確認失敗"):
            launcher.run_console(self.root, self.notify)
        self.assertEqual(self.spawn.call_count, 2)
        self.assert_stopped(2)
        self.notify.assert_not_called()

    def test_browser_launch_failure_cleans_up_services(self):
        self.spawn.side_effect = [*self.processes[:2], OSError("画面起動失敗")]
        with self.assertRaisesRegex(OSError, "画面起動失敗"):
            launcher.run_console(self.root, self.notify)
        self.assert_stopped(2)

    def test_browser_without_window_times_out_and_cleans_up_everything(self):
        self.find_window.return_value = None
        with patch.object(launcher, "WINDOW_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "画面を開けません"):
                launcher.run_console(self.root, self.notify)
        self.assert_stopped()
        self.notify.assert_not_called()

    def test_service_crash_after_start_closes_browser_and_other_service(self):
        self.window.side_effect = [True, True]
        with patch.object(launcher, "check_services", side_effect=RuntimeError("バックエンドが終了")):
            with self.assertRaisesRegex(RuntimeError, "バックエンドが終了"):
                launcher.run_console(self.root, self.notify)
        self.notify.assert_called_once()
        self.assert_stopped()

    def test_frontend_exit_after_start_requests_graceful_backend_shutdown(self):
        # 監視処理自体は差し替えず、Vite が先に異常終了した状況を再現する。
        backend, frontend, browser = self.processes
        frontend.poll.return_value = 7
        frontend.returncode = 7
        with self.assertRaisesRegex(RuntimeError, "画面配信が終了しました"):
            launcher.run_console(self.root, self.notify)
        self.notify.assert_called_once()
        backend.stdin.close.assert_called_once()
        backend.wait.assert_called_once_with(timeout=launcher.BACKEND_SHUTDOWN_TIMEOUT)
        backend.terminate.assert_not_called()
        frontend.terminate.assert_not_called()
        browser.terminate.assert_called_once()

    def test_occupied_port_does_not_launch_or_stop_any_process(self):
        self.ports.side_effect = RuntimeError("ポートは使用中")
        with self.assertRaisesRegex(RuntimeError, "使用中"):
            launcher.run_console(self.root, self.notify)
        self.spawn.assert_not_called()
        for process in self.processes:
            process.terminate.assert_not_called()

    def test_missing_setup_does_not_launch_background_processes(self):
        with patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "初回セットアップ"):
                launcher.run_console(self.root, self.notify)
        self.spawn.assert_not_called()


class BrowserSearchTests(unittest.TestCase):
    CHROME = "Google/Chrome/Application/chrome.exe"
    EDGE = "Microsoft/Edge/Application/msedge.exe"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        # 実際の Program Files などは見ず、一時フォルダを3つの探索場所に見立てる。
        self.locations = {
            "PROGRAMFILES(X86)": root / "Program Files (x86)",
            "PROGRAMFILES": root / "Program Files",
            "LOCALAPPDATA": root / "AppData/Local",
        }

    def install(self, variable, suffix):
        path = self.locations[variable] / suffix
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return path

    def find(self, variables=("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")):
        environment = {variable: str(self.locations[variable]) for variable in variables}
        with patch.dict(launcher.os.environ, environment, clear=True):
            return launcher.find_browser()

    def test_chrome_is_preferred_over_edge_found_in_earlier_location(self):
        # Windows の標準的な配置。場所ごとに探すと (x86) の Edge が先に見つかっていた。
        self.install("PROGRAMFILES(X86)", self.EDGE)
        chrome = self.install("PROGRAMFILES", self.CHROME)
        self.assertEqual(self.find(), (chrome, "browser-profile-chrome"))

    def test_chrome_installed_per_user_is_preferred_over_edge_in_all_other_locations(self):
        self.install("PROGRAMFILES(X86)", self.EDGE)
        self.install("PROGRAMFILES", self.EDGE)
        chrome = self.install("LOCALAPPDATA", self.CHROME)
        self.assertEqual(self.find(), (chrome, "browser-profile-chrome"))

    def test_chrome_only(self):
        chrome = self.install("PROGRAMFILES", self.CHROME)
        self.assertEqual(self.find(), (chrome, "browser-profile-chrome"))

    def test_edge_is_used_with_existing_profile_when_chrome_is_missing(self):
        edge = self.install("PROGRAMFILES(X86)", self.EDGE)
        # Edge はこれまでのプロファイルを使い続け、Chrome とは保存先を分ける。
        self.assertEqual(self.find(), (edge, "browser-profile"))

    def test_locations_keep_previous_order_within_same_browser(self):
        first = self.install("PROGRAMFILES(X86)", self.CHROME)
        self.install("PROGRAMFILES", self.CHROME)
        self.install("LOCALAPPDATA", self.CHROME)
        self.assertEqual(self.find(), (first, "browser-profile-chrome"))

    def test_unset_location_and_directory_with_browser_name_are_skipped(self):
        # 環境変数が無い場所は探さず、実行ファイル名のフォルダは実行ファイルとみなさない。
        self.install("PROGRAMFILES(X86)", self.CHROME)
        (self.locations["PROGRAMFILES"] / self.CHROME).mkdir(parents=True)
        edge = self.install("LOCALAPPDATA", self.EDGE)
        self.assertEqual(self.find(("PROGRAMFILES", "LOCALAPPDATA")), (edge, "browser-profile"))

    def test_missing_both_browsers_names_chrome_first(self):
        with self.assertRaisesRegex(RuntimeError, r"^Google Chrome または Microsoft Edge が必要です。"):
            self.find()

    def test_missing_browser_does_not_launch_background_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("backend/.venv/Scripts/python.exe", "frontend/node_modules/vite/bin/vite.js"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                (root / relative).touch()
            with patch.object(launcher.shutil, "which", return_value="node.exe"), \
                    patch.object(launcher.subprocess, "Popen") as spawn, \
                    patch.object(launcher, "check_port_available") as ports, \
                    patch.dict(launcher.os.environ, {"PROGRAMFILES": str(self.locations["PROGRAMFILES"])}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "Google Chrome または Microsoft Edge"):
                    launcher.run_console(root, Mock())
            spawn.assert_not_called()
            ports.assert_not_called()


class LauncherBoundaryTests(unittest.TestCase):
    def test_managed_backend_waits_for_deploy_before_disconnect_and_exit(self):
        # 実uvicorn子プロセスでWindowsの終了経路を検証。SSHとデプロイだけを模擬する。
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script = directory / "managed_backend.py"
            script.write_text(textwrap.dedent('''\
                import asyncio
                from contextlib import asynccontextmanager
                from pathlib import Path
                import sys
                sys.path.insert(0, sys.argv[1])
                import app as backend
                from managed_server import run_server
                root = Path(sys.argv[2])
                def record(event):
                    with (root / 'order.txt').open('a', encoding='utf-8') as stream:
                        stream.write(event + '\\n')
                async def deploy():
                    while not (root / 'release').exists():
                        await asyncio.sleep(0.02)
                    record('rollback')
                original_wait = backend.deploys.wait
                async def wait():
                    record('wait')
                    (root / 'waiting').touch()
                    return await original_wait()
                async def disconnect():
                    record('disconnect')
                backend.deploys.wait = wait
                backend.session.disconnect = disconnect
                @asynccontextmanager
                async def fixture(app):
                    backend.deploys._task = asyncio.create_task(deploy())
                    async with backend.lifespan(app):
                        (root / 'started').touch()
                        yield
                    record('lifespan')
                backend.app.router.lifespan_context = fixture
                run_server(backend.app, host='127.0.0.1', port=0, managed=True)
                record('exit')
                '''), encoding="utf-8")
            with (directory / "backend.log").open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", str(script), str(LAUNCHER_PATH.parent / "backend"), str(directory)],
                    stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                results = []
                stopper = None
                try:
                    self.wait_for_marker(directory / "started", process)
                    stopper = threading.Thread(target=lambda: results.append(launcher.stop_backend(process, timeout=10)))
                    stopper.start()
                    self.wait_for_marker(directory / "waiting", process)
                    self.assertIsNone(process.poll())
                    self.assertEqual((directory / "order.txt").read_text(encoding="utf-8").splitlines(), ["wait"])
                    (directory / "release").touch()
                    stopper.join(timeout=10)
                    self.assertFalse(stopper.is_alive())
                    self.assertEqual(results, [True])
                    self.assertEqual(process.returncode, 0)
                    self.assertEqual((directory / "order.txt").read_text(encoding="utf-8").splitlines(),
                                     ["wait", "rollback", "disconnect", "lifespan", "exit"])
                finally:
                    (directory / "release").touch()
                    if stopper is not None:
                        stopper.join(timeout=12)
                    launcher.stop_process(process)
                    if process.stdin is not None:
                        process.stdin.close()

    @staticmethod
    def wait_for_marker(path, process):
        deadline = time.monotonic() + 10
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if not path.exists():
            raise AssertionError(f"検証用バックエンドの通知がありません: {path.name}")

    def test_graceful_shutdown_timeout_reports_forced_termination(self):
        process = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(60)"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            with self.assertLogs(level="ERROR") as logs:
                self.assertFalse(launcher.stop_backend(process, timeout=0.05))
            self.assertIsNotNone(process.poll())
            self.assertIn("強制終了", "".join(logs.output))
            self.assertIn("0.05", "".join(logs.output))
        finally:
            launcher.stop_process(process)
            process.stdin.close()

    def test_port_probe_preserves_existing_listener(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaisesRegex(RuntimeError, "使用中"):
                launcher.check_port_available(listener.getsockname()[1])
            self.assertGreater(listener.fileno(), -1)

    def test_stopping_exited_process_does_not_signal_another_process(self):
        process = Mock()
        process.poll.return_value = 0
        launcher.stop_process(process)
        process.terminate.assert_not_called()
        process.kill.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows の Job Object を確認する")
    def test_terminating_supervisor_also_terminates_descendants(self):
        # 実際のボットや SSH・ブラウザを使わず、監視役の強制終了を再現する。
        with tempfile.TemporaryDirectory() as temporary:
            status = Path(temporary) / "children.json"
            child_code = (
                "import json, pathlib, subprocess, sys, time; "
                "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                f"record = pathlib.Path({str(status)!r}); "
                "record.with_suffix('.tmp').write_text(json.dumps([__import__('os').getpid(), grandchild.pid])); "
                "record.with_suffix('.tmp').replace(record); "
                "time.sleep(60)"
            )
            code = (
                "import json, pathlib, subprocess, sys, time; "
                f"sys.path.insert(0, {str(LAUNCHER_PATH.parent)!r}); "
                "import dev_launcher; dev_launcher.attach_lifetime_job(); "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                "time.sleep(60)"
            )
            supervisor = subprocess.Popen([sys.executable, "-B", "-c", code],
                                          creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(lambda: launcher.stop_process(supervisor))
            deadline = time.monotonic() + 10
            while not status.exists() and supervisor.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(status.exists(), "検証用の子プロセスを起動できない")
            process_ids = json.loads(status.read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handles = [kernel.OpenProcess(0x100000, False, process_id) for process_id in process_ids]
            self.assertTrue(all(handles))
            try:
                supervisor.kill()
                supervisor.wait(timeout=5)
                for handle in handles:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0,
                                     "監視役が強制終了した後も子・孫プロセスが残っている")
            finally:
                for handle in handles:
                    if handle:
                        kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
