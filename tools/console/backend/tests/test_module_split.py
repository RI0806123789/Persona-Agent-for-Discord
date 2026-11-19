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


if __name__ == "__main__":
    unittest.main()
