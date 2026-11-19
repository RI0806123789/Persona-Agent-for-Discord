"""constants.py の領域別モジュールへの分割（#480）が、利用側と管理コンソールの契約を壊さないことを確かめる。

- 領域モジュール（constants_*.py）の名前は、constants から同じ値のまま取れる。
- どの名前も1か所でだけ定義する（後から読み込まれた方が黙って勝つのを防ぐ）。
- 領域モジュールは何も import しない（constants.py との循環参照を作らない）。
- 管理コンソールが AST で読む定数は、再公開ではなく constants.py に直接書く。
"""

import ast
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.config import constants

CONFIG_DIR = SRC / "persona_bot" / "config"
DOMAIN_FILES = sorted(CONFIG_DIR.glob("constants_*.py"))

# tools/console/backend/remote_scripts/insights.py が constants.py の AST から読む名前。
# 変えるなら insights.py・tools/console/backend/tests/test_target_consistency.py と一緒に直す。
AST_READ_BY_CONSOLE = (
    "PERSONA_VALUE_PREFIX",
    "PROMPT_TEMPLATE_FILE_NAME",
    "DISCORD_SELECT_VALUE_MAX_LENGTH",
    "DEFAULT_MODEL_NAME",
    "DEFAULT_RESPOND_MODE",
    "DEFAULT_AUTO_TOPIC_INTERVAL_HOURS",
)


def top_level_names(path: Path) -> list[str]:
    """モジュール直下の代入で定義された名前を、出てくる順に返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


class ConstantsLayoutTests(unittest.TestCase):
    def test_domain_modules_exist(self):
        self.assertTrue(DOMAIN_FILES, "領域モジュール（constants_*.py）が見つかりません。")

    def test_domain_names_are_reexported_with_identical_values(self):
        for path in DOMAIN_FILES:
            module = importlib.import_module(f"persona_bot.config.{path.stem}")
            for name in top_level_names(path):
                with self.subTest(module=path.stem, name=name):
                    self.assertTrue(hasattr(constants, name), f"constants から {name} を取れません。")
                    # 同じオブジェクトであること（コピーや別の値に差し替わっていない）。
                    self.assertIs(getattr(constants, name), getattr(module, name))

    def test_every_name_is_defined_in_exactly_one_file(self):
        owners: dict[str, list[str]] = {}
        for path in [CONFIG_DIR / "constants.py", *DOMAIN_FILES]:
            for name in top_level_names(path):
                owners.setdefault(name, []).append(path.name)
        duplicated = {name: files for name, files in owners.items() if len(files) > 1}
        self.assertEqual(duplicated, {})

    def test_domain_modules_import_nothing(self):
        for path in DOMAIN_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
            with self.subTest(module=path.stem):
                self.assertEqual(imports, [], "領域モジュールは何も import しない（循環参照を避ける）。")

    def test_constants_reexports_every_domain_module(self):
        source = (CONFIG_DIR / "constants.py").read_text(encoding="utf-8-sig")
        for path in DOMAIN_FILES:
            with self.subTest(module=path.stem):
                self.assertIn(f"from persona_bot.config.{path.stem} import *", source)

    def test_names_read_by_the_console_stay_direct_literal_assignments(self):
        tree = ast.parse((CONFIG_DIR / "constants.py").read_text(encoding="utf-8-sig"))
        literals = {
            node.targets[0].id
            for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)
        }
        for name in AST_READ_BY_CONSOLE:
            with self.subTest(name=name):
                self.assertIn(name, literals)

    def test_patch_multiple_on_constants_reaches_domain_owned_names(self):
        # test_tuning_constants と同じ方法。領域モジュールの名前も、constants への差し替えが
        # 利用側の `from persona_bot.config.constants import ...` に届く。
        with patch.multiple("persona_bot.config.constants", STATS_TOPIC_COUNT=3, AGENT_MAX_STEPS=1, TTS_QUEUE_MAX=9):
            namespace: dict = {}
            exec(
                "from persona_bot.config.constants import STATS_TOPIC_COUNT, AGENT_MAX_STEPS, TTS_QUEUE_MAX",
                namespace,
            )
        self.assertEqual((namespace["STATS_TOPIC_COUNT"], namespace["AGENT_MAX_STEPS"], namespace["TTS_QUEUE_MAX"]),
                         (3, 1, 9))

    def test_value_derived_from_another_domain_stays_consistent(self):
        # STATS_MAX_USERS_IN_SELECT は UI の上限（constants.py）から決まるため constants.py に置く。
        self.assertEqual(constants.STATS_MAX_USERS_IN_SELECT, constants.DISCORD_SELECT_MAX_OPTIONS)


if __name__ == "__main__":
    unittest.main()
