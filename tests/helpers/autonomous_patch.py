"""自律行動のテスト用: 発話・コメント応答を切り出したモジュール（#415）に合わせたパッチ。

`autonomous_service.py` の発話・コメント応答は `autonomous_speech.py` へ移った。どちらのモジュールが
その名前を使うかを呼び出し側で意識しなくて済むよう、名前を持つ全モジュールへ同じ代役を当てる。
"""

from unittest.mock import patch

from persona_bot.services import autonomous_service, autonomous_speech

AUTONOMOUS_MODULES = (autonomous_service, autonomous_speech)


def patch_autonomous(testcase, name, replacement):
    """`name` を持つ自律行動のモジュールすべてへ replacement を当て、テスト終了時に戻す。"""
    started = False
    for module in AUTONOMOUS_MODULES:
        if hasattr(module, name):
            patcher = patch.object(module, name, replacement)
            patcher.start()
            testcase.addCleanup(patcher.stop)
            started = True
    if not started:
        raise AttributeError(f"自律行動のモジュールに {name} がありません。")
    return replacement
