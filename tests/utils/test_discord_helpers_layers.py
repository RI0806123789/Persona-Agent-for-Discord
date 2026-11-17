"""discord_helpers.py を責務別に分けた構成（#468）の契約を確かめる。

- 窓口（discord_helpers）が、従来の import 先として全部を再公開していること。
- 軽いモジュール（送信・チャンネル・権限）が、画像（PIL）・プロンプト・背景処理まで連鎖して読み込まないこと。
"""

from pathlib import Path
import subprocess
import sys
import unittest

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))

from persona_bot.utils import (  # noqa: E402
    discord_attachments, discord_channels, discord_helpers, discord_history, discord_permissions, discord_send,
)

HEAVY_MODULES = (
    "PIL", "persona_bot.core.background", "persona_bot.services.prompting",
    "persona_bot.utils.discord_attachments", "persona_bot.utils.discord_history",
)


def loaded_modules_after_import(module: str) -> set[str]:
    """新しい Python で module だけを import し、読み込まれた上の重いモジュールを返す。"""
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        f"import {module}\n"
        f"print(','.join(name for name in {HEAVY_MODULES!r} if name in sys.modules))\n"
    )
    # -X utf8: 日本語の出力を含む環境でも、親の文字コード設定に左右されない（英語版 Windows の CI 対策）。
    result = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return {name for name in result.stdout.strip().split(",") if name}


class FacadeTests(unittest.TestCase):
    def test_facade_reexports_the_same_objects_as_the_split_modules(self) -> None:
        for module in (discord_attachments, discord_channels, discord_history, discord_permissions, discord_send):
            for name, value in vars(module).items():
                if name.startswith("__") or name not in vars(discord_helpers):
                    continue
                with self.subTest(module=module.__name__, name=name):
                    # 窓口に同じ名前があるなら、同じオブジェクト（別の定義を持たない）。
                    if callable(value) or name.isupper():
                        self.assertIs(getattr(discord_helpers, name), value)

    def test_names_used_by_the_rest_of_the_bot_are_still_importable(self) -> None:
        for name in (
            "REPLY_ALLOWED_MENTIONS", "NO_MENTIONS", "send_long_reply", "send_long_message", "split_message",
            "load_image_attachments", "close_image_attachments", "fetch_channel_history_text", "build_history_text",
            "is_administrator", "is_current_administrator", "is_bot_owner", "can_bot_send_messages",
            "channel_display_name", "post_welcome_message", "pick_announcement_channel", "format_grounding_sources",
            "is_supported_message_channel", "is_connected_voice_chat", "clean_message_content",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(discord_helpers, name))


class LightImportTests(unittest.TestCase):
    def test_light_modules_do_not_pull_in_pil_or_upper_layers(self) -> None:
        for module in ("discord_send", "discord_channels", "discord_permissions"):
            with self.subTest(module=module):
                self.assertEqual(loaded_modules_after_import(f"persona_bot.utils.{module}"), set())

    def test_bot_client_does_not_pull_in_the_image_or_history_modules(self) -> None:
        # PIL 自体は Gemini の SDK が読み込むので、ここでは見ない。窓口経由で全部を読み込まないことだけを確かめる。
        loaded = loaded_modules_after_import("persona_bot.core.bot_client")
        self.assertNotIn("persona_bot.utils.discord_attachments", loaded)
        self.assertNotIn("persona_bot.utils.discord_history", loaded)
        self.assertNotIn("persona_bot.services.prompting", loaded)


if __name__ == "__main__":
    unittest.main()
