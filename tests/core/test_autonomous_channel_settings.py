"""自律行動の配信先の指定（#266）を DB へ保存し、再起動・旧版の DB からも復元できることを確かめる。"""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.core import database
from persona_bot.core.app_state import BotState, GuildSettings
from persona_bot.core.database import Database, SCHEMA_VERSION

COLUMNS = ("autonomous_stage_channel_id", "autonomous_voice_channel_id")


class AutonomousChannelPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bot.db"
        self.db = Database(self.path)
        self.addCleanup(lambda: self.db.close())

    def reopen(self) -> None:
        self.db.close()
        self.db = Database(self.path)
        with patch("persona_bot.core.database.logger"):
            self.db.initialize()

    async def test_version_fourteen_rows_migrate_to_unset_and_keep_other_settings(self) -> None:
        with patch.object(database, "SCHEMA_VERSION", 14), patch("persona_bot.core.database.logger"), \
                patch.object(self.db, "_validate_guild_settings_schema"):
            self.db.initialize()
        with self.db._lock, self.db._conn:
            self.db._conn.execute(
                "INSERT INTO guild_settings (guild_id, model_name, autonomous_enabled) VALUES (5, '', 1)"
            )
        self.reopen()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            columns = [row[1] for row in connection.execute("PRAGMA table_info(guild_settings)")]
        for column in COLUMNS:
            self.assertEqual(columns.count(column), 1)

        state = BotState()
        state.restore_from_database(self.db)
        settings = state.get_guild(5)
        self.assertTrue(settings.autonomous_enabled)
        self.assertIsNone(settings.autonomous_stage_channel_id)
        self.assertIsNone(settings.autonomous_voice_channel_id)

    async def test_designation_survives_restart_and_rejoin_and_clearing_saves_zero(self) -> None:
        with patch("persona_bot.core.database.logger"):
            self.db.initialize()
        state = BotState()
        state.restore_from_database(self.db)
        settings = await state.get_guild_async(7)
        settings.autonomous_stage_channel_id = 100000000000002001
        settings.autonomous_voice_channel_id = 100000000000002002
        await settings.persist()

        self.reopen()
        restarted = BotState()
        restarted.restore_from_database(self.db)
        self.assertEqual(restarted.get_guild(7).autonomous_stage_channel_id, 100000000000002001)
        self.assertEqual(restarted.get_guild(7).autonomous_voice_channel_id, 100000000000002002)
        # 退出後の再参加（対象サーバーだけの読込）でも同じ値に戻る
        restarted.forget_guild(7)
        rejoined = await restarted.get_guild_async(7)
        self.assertEqual(rejoined.autonomous_voice_channel_id, 100000000000002002)

        # 他の設定を保存しても指定は消えない（行の総入れ替えで書き漏らすと既定値へ戻る）
        rejoined.grounding_enabled = False
        await rejoined.persist()
        self.assertEqual(self.db.guild_settings_load_all()[7]["autonomous_stage_channel_id"], 100000000000002001)

        rejoined.autonomous_stage_channel_id = None
        await rejoined.persist()
        values = self.db.guild_settings_load_all()[7]
        self.assertEqual(values["autonomous_stage_channel_id"], 0)
        self.assertEqual(values["autonomous_voice_channel_id"], 100000000000002002)

    def test_invalid_persisted_values_fall_back_to_unset(self) -> None:
        for value in (True, -5, "100", 1.5):
            with self.subTest(value=value):
                settings = GuildSettings()
                settings.autonomous_voice_channel_id = 99
                with patch("persona_bot.core.app_state.logger") as logged:
                    settings.apply_persisted({"autonomous_voice_channel_id": value})
                self.assertIsNone(settings.autonomous_voice_channel_id)
                args = logged.warning.call_args.args
                self.assertIn("autonomous_voice_channel_id", args[0] % args[1:] if len(args) > 1 else args[0])
        for value in (0, None):
            with self.subTest(value=value):
                settings = GuildSettings()
                with patch("persona_bot.core.app_state.logger") as logged:
                    settings.apply_persisted({"autonomous_stage_channel_id": value})
                self.assertIsNone(settings.autonomous_stage_channel_id)
                logged.warning.assert_not_called()
        # 列の無い古い値の組（キーが無い）では、現在の値を変えない
        settings = GuildSettings(autonomous_stage_channel_id=42)
        settings.apply_persisted({"model_name": ""})
        self.assertEqual(settings.autonomous_stage_channel_id, 42)


if __name__ == "__main__":
    unittest.main()
