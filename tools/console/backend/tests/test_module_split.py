"""バックエンドの責務ごとの分割（#482）が、従来の import 先・ロガー名・依存の向きを変えていないことを確かめる。

`app`・`deploy`・`ssh_session` は、テストが `from deploy import DeployRunner` や `patch.object(app, ...)` で
参照する窓口なので、切り出した名前もそこから取れる。切り出し先は元のモジュールを import しない
（循環参照になり、再公開が壊れる）。
"""

import ast
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def imported_modules(module_name: str) -> set[str]:
    """モジュールのソースが import している名前（トップレベルのモジュール名）を集める。"""
    tree = ast.parse((BACKEND / f"{module_name}.py").read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


class DeploySplitTests(unittest.TestCase):
    def test_runner_lives_in_deploy_runner_and_is_reexported_by_deploy(self):
        import deploy
        import deploy_runner
        self.assertIs(deploy.DeployRunner, deploy_runner.DeployRunner)
        self.assertEqual(deploy.DeployRunner.__module__, "deploy_runner")
        self.assertEqual(deploy.DeployManager.__module__, "deploy")
        for name in (
            "FETCH_TIMEOUT", "PIP_TIMEOUT", "RESTART_TIMEOUT", "RESTART_SETTLE_TIMEOUT", "HEALTH_TIMEOUT",
            "POLL_INTERVAL", "HEALTH_LOG_TAIL", "OUTPUT_MAX_CHARS",
            "UNEXPECTED_ERROR_MESSAGE", "UNEXPECTED_ERROR_STEP_MESSAGE",
        ):
            self.assertIs(getattr(deploy, name), getattr(deploy_runner, name), name)

    def test_deploy_keeps_the_record_names_and_asyncio_for_existing_patches(self):
        import deploy
        import deploy_models
        for name in ("DEPLOY_STEPS", "ROLLBACK_STEPS", "Check", "DeployRecord", "Step", "StepFailed", "new_record"):
            self.assertIs(getattr(deploy, name), getattr(deploy_models, name), name)
        # test_deploy.py は `patch("deploy.asyncio.sleep")` で asyncio モジュールを差し替える。
        self.assertTrue(hasattr(deploy, "asyncio"))

    def test_runner_does_not_import_deploy_and_keeps_the_original_logger(self):
        import deploy_runner
        self.assertNotIn("deploy", imported_modules("deploy_runner"))
        self.assertEqual(deploy_runner.logger.name, "deploy")

    def test_manager_does_not_carry_the_runner_internals(self):
        import deploy
        source = (BACKEND / "deploy.py").read_text(encoding="utf-8-sig")
        self.assertNotIn("class DeployRunner", source)
        self.assertTrue(hasattr(deploy.DeployManager, "start"))
        self.assertFalse(hasattr(deploy.DeployManager, "_rollback"))


class SshErrorsSplitTests(unittest.TestCase):
    def test_error_types_and_constants_are_reexported_by_ssh_session(self):
        import ssh_errors
        import ssh_session
        for name in (
            "SshError", "NotConnectedError", "CommandStateUnknownError", "CONNECTION_LOST_SAFE_MESSAGE",
            "NOT_CONNECTED_MESSAGE", "COMMAND_STATE_UNKNOWN_MESSAGE", "STATE_UNKNOWN_SUFFIX",
        ):
            self.assertIs(getattr(ssh_session, name), getattr(ssh_errors, name), name)
        self.assertTrue(issubclass(ssh_errors.NotConnectedError, ssh_errors.SshError))
        self.assertTrue(issubclass(ssh_errors.CommandStateUnknownError, ssh_errors.SshError))
        # 接続の流れに結びつくものは ssh_session に残す（テストが ssh_session の属性を差し替える）。
        for name in ("SshSession", "CommandResult", "CommandRecord", "CONNECT_TIMEOUT_SECONDS"):
            self.assertTrue(hasattr(ssh_session, name), name)
        self.assertFalse(hasattr(ssh_errors, "SshSession"))

    def test_ssh_errors_is_pure_and_does_not_import_the_session_or_profiles(self):
        imported = imported_modules("ssh_errors")
        for forbidden in ("ssh_session", "profiles", "deploy", "app"):
            self.assertNotIn(forbidden, imported)

    def test_config_errors_pair_a_diagnostic_message_with_a_safe_one(self):
        from ssh_errors import missing_default_known_hosts_error, missing_key_error, missing_known_hosts_error
        key = missing_key_error(Path("C:/Users/fictional-owner/.ssh/fictional_key"))
        self.assertIn("fictional-owner", str(key))  # 通常の診断文はパスを含む
        self.assertNotIn("fictional", key.safe_message)  # 伏せ字用には所有者名・ファイル名を出さない
        self.assertIn("<KEY_FILE>", key.safe_message)
        known = missing_known_hosts_error(Path("/home/fictional-owner/.ssh/known_hosts"))
        self.assertIn("fictional-owner", str(known))
        self.assertNotIn("fictional", known.safe_message)
        self.assertIn("<LOCAL_HOME>/.ssh/known_hosts", known.safe_message)
        default = missing_default_known_hosts_error(Path("/home/fictional-owner/.ssh/known_hosts"), "fictional.invalid")
        self.assertIn("fictional.invalid", str(default))
        self.assertNotIn("fictional", default.safe_message)
        self.assertIn("<SSH_HOST>", default.safe_message)

    def test_command_errors_keep_the_safe_message_when_rebuilt(self):
        from ssh_errors import (
            STATE_UNKNOWN_SUFFIX, CommandStateUnknownError, communication_error, completion_unknown_from_transport,
            completion_unknown_from_unexpected, start_error, with_unknown_state_suffix,
        )
        failure = communication_error("架空の操作", OSError("fictional-host の失敗"))
        self.assertIn("OSError", str(failure))
        self.assertEqual(failure.safe_message, "架空の操作: 通信に失敗しました。")
        started = start_error("架空の操作", OSError("fictional-host"))
        self.assertIn("fictional-host", str(started))
        self.assertNotIn("fictional-host", started.safe_message)
        unknown = completion_unknown_from_transport("架空の操作", OSError("fictional-host"))
        self.assertNotIn("fictional-host", unknown.safe_message)
        enriched = with_unknown_state_suffix(unknown)
        self.assertIsInstance(enriched, CommandStateUnknownError)
        self.assertTrue(str(enriched).endswith(STATE_UNKNOWN_SUFFIX))
        self.assertTrue(enriched.safe_message.endswith(STATE_UNKNOWN_SUFFIX))
        self.assertNotIn("fictional-host", enriched.safe_message)
        # 安全な文面が無い例外は、無いまま引き継ぐ（作り直しで生の文面を安全な文面にしない）。
        bare = with_unknown_state_suffix(completion_unknown_from_unexpected("架空の操作", RuntimeError("x")))
        self.assertIsNone(bare.safe_message)

    def test_connect_errors_are_described_by_type_with_the_given_timeout(self):
        import asyncio
        import asyncssh
        from ssh_errors import SshError, connection_lost_message, describe_connect_error
        host, timeout = "fictional.invalid", 10
        described = {
            "hostkey": describe_connect_error(asyncssh.HostKeyNotVerifiable("x"), host, timeout),
            "denied": describe_connect_error(asyncssh.PermissionDenied("x"), host, timeout),
            "timeout": describe_connect_error(asyncio.TimeoutError(), host, timeout),
            "refused": describe_connect_error(ConnectionRefusedError(), host, timeout),
            "os": describe_connect_error(OSError(), host, timeout),
            "ssh": describe_connect_error(asyncssh.Error(1, "x"), host, timeout),
            "other": describe_connect_error(RuntimeError(), host, timeout),
        }
        self.assertEqual(len(set(described.values())), len(described))  # 種類ごとに別の案内になる
        self.assertIn(f"ssh {host}", described["hostkey"])
        self.assertTrue(described["timeout"].startswith("10 秒以内"))
        self.assertEqual(describe_connect_error(SshError("架空の設定エラー"), host, timeout), "架空の設定エラー")
        self.assertIn("fictional-reason", connection_lost_message(OSError("fictional-reason")))
        self.assertEqual(connection_lost_message(None), "SSH 接続が切れました。もう一度接続してください。")


if __name__ == "__main__":
    unittest.main()
