"""実際のメッセージ処理へテスト用サービスと外部境界のモックを渡す。"""

from dataclasses import fields
from unittest.mock import Mock, patch

from persona_bot.core import message_handler
from persona_bot.core.message_handler import MessageHandler
from persona_bot.core.message_operations import operation_names


def make_message_handler(dependencies, test_case):
    """サービス（MessageHandler のフィールド）と外部操作（MessageOperations）を差し替えて組み立てる。

    渡さなかったサービスは `Mock` で埋める。外部操作は `message_handler` の同名の属性を
    テストの間だけ差し替える（ステージは呼出しのたびにそこから操作を組み立てる。#395）。
    どちらでもない名前は、差し替えたつもりで何も起きないのを防ぐため拒否する
    （定数・判定の関数は差し替え口ではない。ハンドラのメソッドは組み立て後に直接代入する）。
    """
    service_names = {field.name for field in fields(MessageHandler)}
    operations = set(operation_names())
    unknown = sorted(set(dependencies) - service_names - operations)
    if unknown:
        raise TypeError(f"MessageHandler のサービスでも外部操作でもない名前です: {', '.join(unknown)}")
    handler = MessageHandler(**{
        name: dependencies.get(name, Mock(name=name)) for name in service_names
    })
    for name in operations & set(dependencies):
        patcher = patch.object(message_handler, name, dependencies[name])
        patcher.start()
        test_case.addCleanup(patcher.stop)
    return handler
