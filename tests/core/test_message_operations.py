"""メッセージ処理ステージの外部操作の契約と、テスト用ビルダーの互換性を検証する（#395）。

各ステージ（受付・振り分け・応答）は MessageHandler のモジュール全体ではなく、型付きの
MessageOperations だけを受け取る。足りない操作は構築時に、ステージの未定義の名前や契約にない操作の
参照はこの静的な検査で、その経路を実行する前に見つける。
"""

import ast
import builtins
import inspect
from pathlib import Path
import symtable
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.audio import voice
from persona_bot.config import paths
from persona_bot.core import background, message_admission, message_handler, message_operations
from persona_bot.core import message_response, message_routing
from persona_bot.core.message_handler import MessageHandler
from persona_bot.core.message_operations import MESSAGE_LOGGER_NAME, MessageOperations, operation_names
from persona_bot.utils import discord_helpers
from tests.helpers.message_handler_helpers import make_message_handler

STAGES = (message_admission, message_routing, message_response)
REAL_OPERATIONS = {
    "fetch_channel_history_text": discord_helpers.fetch_channel_history_text,
    "send_long_reply": discord_helpers.send_long_reply,
    "play_tts_response": voice.play_tts_response,
    "load_image_attachments": discord_helpers.load_image_attachments,
    "load_persona_template": paths.load_persona_template,
    "spawn": background.spawn,
}


def services() -> dict[str, Mock]:
    return {name: Mock(name=name) for name in MessageHandler.__dataclass_fields__}


def parse(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8-sig"))


class OperationContractTests(unittest.TestCase):
    def test_handler_module_wires_each_operation_to_the_real_implementation(self) -> None:
        self.assertEqual(set(operation_names()), set(REAL_OPERATIONS))
        operations = MessageOperations.from_namespace(message_handler)
        for name, real in REAL_OPERATIONS.items():
            with self.subTest(name=name):
                self.assertIs(getattr(operations, name), real)

    def test_real_operations_accept_the_declared_call_shape(self) -> None:
        """契約の Protocol と実装の引数（名前・種類・既定値の有無）が食い違わない。"""
        for field_name, annotation in MessageOperations.__annotations__.items():
            protocol = getattr(message_operations, annotation)
            declared = list(inspect.signature(protocol.__call__).parameters.values())[1:]  # self を除く
            actual = inspect.signature(REAL_OPERATIONS[field_name]).parameters
            for parameter in declared:
                with self.subTest(operation=field_name, parameter=parameter.name):
                    self.assertIn(parameter.name, actual)
                    self.assertEqual(actual[parameter.name].kind, parameter.kind)
                    if parameter.default is not inspect.Parameter.empty:
                        self.assertIsNot(actual[parameter.name].default, inspect.Parameter.empty)
            # 契約にない必須の引数があると、ステージの呼出しが失敗する
            required = {
                name for name, parameter in actual.items()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            }
            self.assertLessEqual(required, {parameter.name for parameter in declared}, field_name)

    def test_missing_or_uncallable_operations_are_named(self) -> None:
        namespace = SimpleNamespace(**REAL_OPERATIONS)
        del namespace.send_long_reply
        namespace.spawn = "呼び出せない値"
        with self.assertRaisesRegex(TypeError, "send_long_reply, spawn"):
            MessageOperations.from_namespace(namespace)

    def test_handler_construction_detects_a_missing_operation(self) -> None:
        with patch.object(message_handler, "load_persona_template", None):
            with self.assertRaisesRegex(TypeError, "load_persona_template"):
                MessageHandler(**services())

    def test_operations_follow_module_patches_made_after_construction(self) -> None:
        handler = MessageHandler(**services())
        replacement = AsyncMock()
        with patch.object(message_handler, "send_long_reply", replacement):
            self.assertIs(handler._operations().send_long_reply, replacement)
        self.assertIs(handler._operations().send_long_reply, discord_helpers.send_long_reply)

    def test_stage_logs_keep_the_message_handler_logger(self) -> None:
        self.assertEqual(message_handler.logger.name, MESSAGE_LOGGER_NAME)
        for stage in (message_routing, message_response):
            with self.subTest(stage=stage.__name__):
                self.assertIs(stage.logger, message_handler.logger)


class StageBoundaryTests(unittest.TestCase):
    def test_stages_no_longer_receive_the_handler_module(self) -> None:
        for stage in STAGES:
            source = Path(stage.__file__).read_text(encoding="utf-8-sig")
            for forbidden in ("runtime", "ModuleType", "sys.modules"):
                with self.subTest(stage=stage.__name__, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_stages_call_operations_only_through_the_contract(self) -> None:
        """ステージは操作を直接 import しない（差し替え口を素通りしない）。"""
        for stage in STAGES:
            for name in operation_names():
                with self.subTest(stage=stage.__name__, operation=name):
                    self.assertFalse(hasattr(stage, name))

    def test_every_operation_reference_is_declared_and_every_operation_is_used(self) -> None:
        declared = set(operation_names())
        used: set[str] = set()
        for stage in STAGES:
            for node in ast.walk(parse(stage)):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ops":
                    with self.subTest(stage=stage.__name__, line=node.lineno):
                        self.assertIn(node.attr, declared)
                    used.add(node.attr)
        self.assertEqual(used, declared)

    def test_names_used_inside_functions_are_defined_in_each_module(self) -> None:
        """import を整理しても、その経路を実行する前に未定義の名前を見つける。"""
        for module in (*STAGES, message_handler):
            table = symtable.symtable(
                Path(module.__file__).read_text(encoding="utf-8-sig"), module.__file__, "exec",
            )
            pending = list(table.get_children())
            while pending:
                scope = pending.pop()
                pending.extend(scope.get_children())
                for symbol in scope.get_symbols():
                    if not (symbol.is_global() and symbol.is_referenced()):
                        continue
                    name = symbol.get_name()
                    with self.subTest(module=module.__name__, scope=scope.get_name(), name=name):
                        self.assertTrue(hasattr(module, name) or hasattr(builtins, name))


class BuilderCompatibilityTests(unittest.TestCase):
    def test_services_and_operations_are_replaced_and_restored(self) -> None:
        owner = unittest.TestCase()
        state = object()
        replacement = AsyncMock()
        handler = make_message_handler({"state": state, "send_long_reply": replacement}, owner)
        self.assertIs(handler.state, state)
        self.assertIsInstance(handler.memory, Mock)
        self.assertIs(message_handler.send_long_reply, replacement)
        self.assertIs(handler._operations().send_long_reply, replacement)
        owner.doCleanups()
        self.assertIs(message_handler.send_long_reply, discord_helpers.send_long_reply)

    def test_unknown_names_are_rejected_instead_of_silently_ignored(self) -> None:
        owner = unittest.TestCase()
        for name in ("HISTORY_LIMIT", "clean_message_content", "notify_violation"):
            with self.subTest(name=name), self.assertRaisesRegex(TypeError, name):
                make_message_handler({name: object()}, owner)
        owner.doCleanups()


if __name__ == "__main__":
    unittest.main()
