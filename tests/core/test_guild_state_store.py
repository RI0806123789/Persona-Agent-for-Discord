"""`GuildStateStore`（#480）が、`Database` の実装と `app_state.py` の利用の両方と食い違わないことを確かめる。

`_db` を `Any` から Protocol にしても、型の確認ツールは CI に無い。代わりにこのテストで、
メソッド名・引数名の食い違いと、Protocol に書いていない DB メソッドの利用を検出する。
"""

import ast
import inspect
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core import app_state
from persona_bot.core.database import Database
from persona_bot.core.guild_state_store import GuildStateStore


def protocol_methods() -> list[str]:
    return [
        name for name, member in vars(GuildStateStore).items()
        if not name.startswith("_") and callable(member)
    ]


def parameter_names(function) -> list[tuple[str, inspect._ParameterKind]]:
    parameters = list(inspect.signature(function).parameters.values())[1:]  # self を除く
    return [(parameter.name, parameter.kind) for parameter in parameters]


class GuildStateStoreTests(unittest.TestCase):
    def test_protocol_lists_the_methods(self):
        self.assertEqual(
            sorted(protocol_methods()),
            ["anniversary_post_record", "guild_settings_save", "guild_state_load", "guild_state_load_all"],
        )

    def test_database_implements_every_method_with_the_same_parameters(self):
        for name in protocol_methods():
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(Database, name, None)), f"Database に {name} がありません。")
                self.assertEqual(
                    parameter_names(getattr(Database, name)), parameter_names(getattr(GuildStateStore, name)),
                )

    def test_app_state_calls_only_methods_declared_by_the_protocol(self):
        """`self._db.xxx` / `db.xxx` で呼ぶ DB のメソッドは、すべて Protocol に書いてあること。"""
        tree = ast.parse(Path(app_state.__file__).read_text(encoding="utf-8-sig"))
        used: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            if (isinstance(owner, ast.Attribute) and owner.attr == "_db") or (
                isinstance(owner, ast.Name) and owner.id == "db"
            ):
                used.add(node.attr)
        self.assertTrue(used, "app_state.py が DB を呼ぶ箇所を検出できませんでした（このテストを直す）。")
        self.assertLessEqual(used, set(protocol_methods()))
        # 宣言したのに使っていないメソッドは残さない。
        self.assertEqual(used, set(protocol_methods()))

    def test_state_fields_no_longer_use_any(self):
        hints = {
            "GuildSettings": app_state.GuildSettings.__dataclass_fields__["_db"].type,
            "BotState": app_state.BotState.__dataclass_fields__["_db"].type,
        }
        for owner, hint in hints.items():
            with self.subTest(owner=owner):
                self.assertIn("GuildStateStore", str(hint))
                self.assertNotEqual(str(hint), "Any")


if __name__ == "__main__":
    unittest.main()
