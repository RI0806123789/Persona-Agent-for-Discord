"""会話計数・起動案内・代替処理のログが本文や設定値を出さないことを確認する。"""

from contextlib import contextmanager
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config import settings_loader
from persona_bot.config.constants import LOG_DATE_FORMAT, LOG_FORMAT
from persona_bot.utils.logging_setup import OperationalFormatter
from persona_bot.utils.timeutil import resolve_timezone, timezone_label
from persona_bot.utils.usage_graph import UsageTracker


@contextmanager
def capture_log(name: str, level: int = logging.DEBUG):
    logger = logging.getLogger(name)
    previous = logger.handlers, logger.level, logger.propagate
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logger.handlers = [handler]
    logger.setLevel(level)
    logger.propagate = False
    try:
        yield output
    finally:
        logger.handlers, level, logger.propagate = previous
        logger.setLevel(level)
        handler.close()


class RuntimeLoggingPrivacyTests(unittest.TestCase):
    def test_conversation_count_and_tokens_remain_without_content_or_location(self) -> None:
        tracker = UsageTracker()
        private = ("架空の非公開場所", "架空の質問本文", "架空の添付名", "架空の内部プロンプト", "架空の応答本文")
        usage = {"promptTokenCount": 5, "candidatesTokenCount": 8, "totalTokenCount": 13}
        with capture_log("persona_bot.utils.usage_graph") as output:
            tracker.log_snapshot(*private, usage)
        text = output.getvalue()
        for value in private:
            self.assertNotIn(value, text)
        self.assertEqual(text.count("User (会話)："), 1)
        self.assertEqual(text.count("Total token count：13"), 1)
        self.assertEqual(list(tracker.input_tokens), [5])
        self.assertEqual(list(tracker.output_tokens), [8])
        self.assertEqual(list(tracker.total_tokens), [13])

    def test_internal_usage_is_not_counted_as_conversation_and_hides_context(self) -> None:
        tracker = UsageTracker()
        with capture_log("persona_bot.utils.usage_graph") as output:
            tracker.log_tokens("架空の非公開調査名", {"totalTokenCount": 7})
        self.assertNotIn("User (", output.getvalue())
        self.assertNotIn("架空の非公開調査名", output.getvalue())
        self.assertIn("Total token count：7", output.getvalue())
        self.assertEqual(list(tracker.total_tokens), [7])

    def test_missing_settings_keep_actionable_key_names_without_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fictional-private-config-") as directory:
            path = Path(directory) / "missing.env"
            with patch.object(settings_loader, "ENV_FILE_PATH", path), patch.dict(os.environ, {}, clear=True), \
                    capture_log(settings_loader.__name__, logging.WARNING) as output:
                with self.assertRaises(settings_loader.SettingsError):
                    settings_loader.load_settings()
            self.assertIn("キー未設定", output.getvalue())
            self.assertIn("API_KEY_GEMINI", output.getvalue())
            self.assertIn("TOKEN_DISCORD", output.getvalue())
            self.assertIn(".env.example", output.getvalue())
            self.assertIn("Copy-Item .env.example .env", output.getvalue())
            self.assertIn("python bot.py", output.getvalue())
            self.assertNotIn(str(path), output.getvalue())
            self.assertNotIn(directory, output.getvalue())

    def test_settings_guidance_remains_visible_at_default_warning_level(self) -> None:
        for status, expected in (
            (settings_loader._ENV_NO_DOTENV, "pip install -r requirements.txt"),
            (settings_loader._ENV_LOADED, "= の右側を自分のキーへ"),
        ):
            with self.subTest(status=status), capture_log(settings_loader.__name__, logging.WARNING) as output:
                settings_loader._report_missing(["TOKEN_DISCORD"], status)
            self.assertIn("TOKEN_DISCORD", output.getvalue())
            self.assertIn(expected, output.getvalue())
            self.assertIn("python bot.py", output.getvalue())

    def test_dotenv_installation_hint_remains_visible_without_logging_setup(self) -> None:
        with patch.object(settings_loader, "load_dotenv", None), \
                capture_log(settings_loader.__name__, logging.WARNING) as output:
            status = settings_loader._load_env_file(Path("fictional.env"))
        self.assertEqual(status, settings_loader._ENV_NO_DOTENV)
        self.assertIn("pip install -r requirements.txt", output.getvalue())

    def test_invalid_timezone_uses_utc_without_echoing_setting_or_exception_body(self) -> None:
        with capture_log("persona_bot.utils.timeutil") as output:
            timezone = resolve_timezone("架空の非公開タイムゾーン", purpose="架空の非公開用途")
        self.assertEqual(timezone_label(timezone), "UTC")
        self.assertIn("UTC で代用", output.getvalue())
        self.assertNotIn("架空の非公開タイムゾーン", output.getvalue())
        self.assertNotIn("架空の非公開用途", output.getvalue())


if __name__ == "__main__":
    unittest.main()
