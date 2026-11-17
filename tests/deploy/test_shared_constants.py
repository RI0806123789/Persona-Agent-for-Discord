"""deploy/ のスクリプトと src/ が、同じ値を別々に持っている定数の一致を確かめる（#460）。

どちらかだけを直すと、書き換え忘れの検出や旧データの移行が黙って効かなくなる。コメントの
「同じ値にすること」を、テストで強制する。
"""

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))
sys.path.insert(0, str(ROOT / "src"))

import migrate_config_to_env  # noqa: E402
import migrate_data_directory  # noqa: E402
from persona_bot.config import data_paths, settings_loader  # noqa: E402


class SharedConstantsTests(unittest.TestCase):
    def test_placeholder_values_match_between_bot_and_migration_script(self) -> None:
        self.assertEqual(
            set(settings_loader._PLACEHOLDER_VALUES), set(migrate_config_to_env.PLACEHOLDER_VALUES),
        )

    def test_env_example_values_are_all_treated_as_unset(self) -> None:
        # .env.example の右辺を変えたら、プレースホルダの定数も直す（コピーしたまま起動させないため）。
        text = (ROOT / ".env.example").read_text(encoding="utf-8-sig")
        values = {
            match.group(1): match.group(2).strip().strip("\"'")
            for match in re.finditer(r"^([A-Z_]+)=(.*)$", text, re.MULTILINE)
        }
        for key in settings_loader._REQUIRED_KEYS:
            with self.subTest(key=key):
                self.assertIn(values.get(key), settings_loader._PLACEHOLDER_VALUES)

    def test_legacy_directory_names_match_between_bot_and_migration_script(self) -> None:
        self.assertEqual(data_paths.LEGACY_DIRECTORY_NAMES, migrate_data_directory.LEGACY_DIRECTORY_NAMES)


if __name__ == "__main__":
    unittest.main()
