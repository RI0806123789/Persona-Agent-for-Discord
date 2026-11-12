"""サーバー側で動かすスクリプト（insights.py・DB 集計・テーブルの中身）を、手元の一時フォルダに作った架空のボットで検証する。

本物のボット・Discord・ネットワークには一切触れない（Discord API の接続先は使われていないポートへ差し替える）。
"""

import importlib.util
import io
import re
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import remote_commands as rc  # noqa: E402
from remote_scripts import db_table, insights  # noqa: E402
from fake_ssh_server import FAKE_OPTED_OUT_USER, FAKE_USERS, make_fake_database  # noqa: E402

FAKE_TOKEN = "fake-token-for-test-only"
# 架空の ID（実在のサーバーではない）
GUILD_ID = 100000000000000001

CONSTANTS_PY = '''
DEFAULT_PROMPT_FILE = "../prompts/prompt_default.txt"
DEFAULT_MODEL_NAME = "gemini-3.1-flash-lite"
DEFAULT_RESPOND_MODE = "mention"
DEFAULT_AUTO_TOPIC_INTERVAL_HOURS = 6
'''
CHOICES_PY = '''
from discord import app_commands
PERSONA_CHOICES = [
    app_commands.Choice(name="ツンデレ", value="../prompts/prompt_tsun.txt"),
    app_commands.Choice(name="標準", value=DEFAULT_PROMPT_FILE),
]
MODEL_CHOICES = [app_commands.Choice(name="Gemini 3.1 Flash Lite", value=DEFAULT_MODEL_NAME)]
RESPOND_MODE_CHOICES = [
    app_commands.Choice(name="すべての発言に反応", value="all"),
    app_commands.Choice(name="メンションのみに反応", value=DEFAULT_RESPOND_MODE),
]
AUTO_TOPIC_INTERVAL_CHOICES = [
    app_commands.Choice(name="1時間", value=1),
    app_commands.Choice(name="3時間", value=3),
    app_commands.Choice(name="6時間", value=6),
]
'''


def make_fake_bot(root: Path, *, layout: str, with_autonomous_column: bool,
                  with_channel_columns: bool = False) -> None:
    """ボットのフォルダ構成だけを真似た一時フォルダを作る。"""
    source = root / ("index" if layout == "index" else "src/persona_bot/config")
    source.mkdir(parents=True)
    (source / "constants.py").write_text(CONSTANTS_PY, encoding="utf-8")
    (source / "choices.py").write_text(CHOICES_PY, encoding="utf-8")
    if with_autonomous_column:
        services = root / "src/persona_bot/services"
        services.mkdir(parents=True)
        (services / "autonomous_service.py").write_text("", encoding="utf-8")
    (root / "README.md").write_text("# bot\n\n## 📋 更新履歴\n\n### ver0.1.6 — 2026/08/14\n", encoding="utf-8")

    (root / "data" / "memory").mkdir(parents=True)
    con = sqlite3.connect(root / "data" / "memory" / "bot.db")
    columns = "guild_id INTEGER PRIMARY KEY, prompt_file TEXT, model_name TEXT, respond_mode TEXT, " \
              "grounding_enabled INTEGER, auto_topic_enabled INTEGER, auto_topic_interval_hours INTEGER"
    if with_autonomous_column:
        columns += ", autonomous_enabled INTEGER"
    if with_channel_columns:
        columns += ", autonomous_stage_channel_id INTEGER, autonomous_voice_channel_id INTEGER"
    con.execute(f"CREATE TABLE guild_settings ({columns})")
    con.execute(
        "INSERT INTO guild_settings (guild_id, prompt_file, model_name, respond_mode, grounding_enabled, "
        "auto_topic_enabled, auto_topic_interval_hours) VALUES (0, '../prompts/prompt_tsun.txt', '', 'all', 0, 1, 3)"
    )
    con.commit()
    con.close()


# ボット本体のペルソナ一覧（インサイトの推定がボットの規則と一致するかを確かめる）
BOT_CONFIG = Path(__file__).resolve().parents[4] / "src" / "persona_bot" / "config"
BOT_CATALOG_SCRIPT = '''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from persona_bot.config.personas import load_persona_catalog, persona_file_name
prompts = Path(sys.argv[2])
catalog = load_persona_catalog(prompts, prompts / "personas.json")
views = []
for saved in json.loads(sys.argv[3]):
    effective = catalog.resolve(saved)
    # 形の正しい保存値はファイルが無くても保持され、形の誤った値は復元時に破棄される
    source = "saved" if catalog.contains(saved) else "missing" if persona_file_name(saved) else "default"
    missing = Path(persona_file_name(saved)).stem if source == "missing" else None
    views.append([catalog.display_name(effective), source, missing, effective == ""])
print(json.dumps(views))
'''


def bot_persona_views(prompts: Path, saved_values: list[str]) -> list[list]:
    """ボット本体の personas.py で、保存値ごとの実効ペルソナを求める（別プロセスで import する）。

    ボットは日本語の警告を標準出力へ書く。-I は PYTHONIOENCODING などの環境変数を無視するため、
    英語版 Windows（cp1252）でも書けるよう、コマンドラインの -X utf8 で入出力を UTF-8 にする。
    """
    completed = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", BOT_CATALOG_SCRIPT, str(BOT_CONFIG.parents[1]), str(prompts),
         json.dumps(saved_values)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout.strip().splitlines()[-1])


def run_script(script: str, arguments: list[str], *, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-"] + arguments,
        input=script, capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **(env or {})},
    )


class InsightsScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        # 使われていないポート（つながらずにすぐ失敗する）。本物の Discord へは出ない
        self.offline_env = {"PBC_DISCORD_API_BASE": "http://127.0.0.1:9", "TOKEN_DISCORD": ""}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_guild_settings_table_does_not_report_read_error(self):
        db_path = self.root / "data" / "memory" / "bot.db"
        db_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(db_path)):
            pass
        self.insights(journal=[])
        self.assertNotIn("guild_settings を読めませんでした", self.stdout)

    def insights(self, env: dict | None = None, *, responses=None, journal=None, journal_code=0,
                 journal_grep_code=None, journal_stderr="private journal detail", sudo=False,
                 journal_error=None, settings_cases=None, journal_recent=None) -> dict:
        # 通常のimportと収集メソッドを使い、外部との境界だけを差し替える。
        self.journal_calls = []

        def fake_urlopen(request, **kwargs):
            if responses is None:
                raise urllib.error.URLError("架空のオフライン")
            parsed = urllib.parse.urlsplit(request.full_url)
            path = parsed.path.removeprefix("/api/v10")
            target = path + ("?" + parsed.query if parsed.query else "")
            value = responses.get(target, responses.get(path, 404))
            if isinstance(value, int):
                raise urllib.error.HTTPError(request.full_url, value, "test", {}, None)
            return io.BytesIO(json.dumps(value).encode())

        def fake_run(command, **kwargs):
            if "journalctl" in command:
                self.journal_calls.append(command)
                self.assertEqual("sudo" in command, sudo)
                if journal_error == "os":
                    raise OSError("架空の実行失敗")
                if journal_error == "timeout":
                    raise subprocess.TimeoutExpired(command, 1)
                if journal is None:
                    raise OSError("架空のjournal未設定")
                values = (journal if "-g" in command or journal_recent is None else journal_recent) or []
                if "-g" in command:
                    pattern = command[command.index("-g") + 1]
                    values = [row for row in values if isinstance(row, dict)
                              and re.search(pattern, str(row.get("MESSAGE", "")))][-1:]
                output = "\n".join(json.dumps(row) for row in values)
                code = journal_grep_code if "-g" in command else None
                return subprocess.CompletedProcess(command, journal_code if code is None else code,
                                                   output, journal_stderr)
            if command[0] == "systemctl":
                return subprocess.CompletedProcess(command, 0, "", "")
            raise AssertionError("許可していない外部コマンド")

        with patch.dict(os.environ, {**self.offline_env, **(env or {})}), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen), \
                patch("subprocess.run", side_effect=fake_run):
            collector = insights.InsightsCollector(self.root, "bot", sudo)
            data = collector.collect()
            if settings_cases is not None:
                data["settings_cases"] = [collector.describe_settings(row) for row in settings_cases]
        self.stdout = json.dumps(data)
        return data

    def test_settings_validate_choices_and_types_in_both_layouts(self):
        for layout in ("index", "src"):
            with self.subTest(layout=layout):
                self.root = Path(self.temp.name) / layout
                make_fake_bot(self.root, layout=layout, with_autonomous_column=layout == "src")
                invalid = (None, "", "廃止された設定", False, True, 0, 99, "3", 3.0, [], {})
                cases = [{"model_name": value, "respond_mode": value, "auto_topic_interval_hours": value}
                         for value in invalid]
                cases += [{"model_name": "gemini-3.1-flash-lite", "respond_mode": "all",
                           "auto_topic_interval_hours": value} for value in (1, 3, 6)]
                before = (self.root / "data/memory/bot.db").read_bytes()
                views = self.insights(journal=[], settings_cases=cases)["settings_cases"]
                for view in views[:len(invalid)]:
                    self.assertEqual((view["model"], view["respond_mode"], view["auto_topic_interval_hours"]),
                                     ("Gemini 3.1 Flash Lite", "メンションのみに反応", 6))
                for interval, view in zip((1, 3, 6), views[len(invalid):]):
                    self.assertEqual((view["model"], view["respond_mode"], view["auto_topic_interval_hours"]),
                                     ("Gemini 3.1 Flash Lite", "すべての発言に反応", interval))
                self.assertEqual((self.root / "data/memory/bot.db").read_bytes(), before)

    def test_initial_journal_failure_skips_fallback_and_records_one_error(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        for options in ({"journal_code": 1}, {"journal_code": 2, "journal_stderr": ""},
                        {"journal_error": "os"}, {"journal_error": "timeout"}):
            with self.subTest(options=options):
                data = self.insights(journal=[], **options)
                self.assertEqual(len(self.journal_calls), 1)
                self.assertEqual(len(data["errors"]), 1)
                self.assertIsNone(data["activity"]["last_conversation_at"])

    def test_successful_empty_journal_still_searches_older_conversation(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        for prefix in ("", "[2026-10-29T07:32:26+0900] [INFO] [persona_bot.utils.usage_graph] "):
            with self.subTest(prefix=prefix):
                data = self.insights(journal=[{"MESSAGE": prefix + "User (架空の利用者): 架空の会話",
                                              "__REALTIME_TIMESTAMP": "42000000"}],
                                     journal_recent=[], journal_stderr="")
                self.assertEqual(len(self.journal_calls), 2)
                self.assertIn("-g", self.journal_calls[1])
                self.assertEqual(data["activity"]["conversations"], 0)
                self.assertEqual(data["activity"]["last_conversation_at"], 42)
                self.assertNotIn("架空の会話", self.stdout)

    def test_repeated_voice_errors_are_deduplicated_but_distinct_errors_remain(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"username": "架空のボット"},
            "/users/@me/guilds": [{"id": str(index), "name": "架空のサーバー"} for index in (11, 12, 13)],
            "/guilds/11/voice-states/@me": 403,
            "/guilds/12/voice-states/@me": 403,
            "/guilds/13/voice-states/@me": 500,
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        self.assertEqual(len(data["discord"]["guilds"]), 3)
        self.assertEqual(len(data["errors"]), 2)
        self.assertIn("403", data["errors"][0])
        self.assertIn("500", data["errors"][1])
        self.assertTrue(all(guild["voice_error"] for guild in data["discord"]["guilds"]))

    def test_old_layout_reads_display_names_and_marks_autonomous_unsupported(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        data = self.insights()
        self.assertEqual(data["source_dir"], "index")
        self.assertEqual(data["version"], "ver0.1.6 — 2026/08/14")
        self.assertFalse(data["autonomous_supported"])
        # DM（guild_id=0）の行。モデルは空なので既定値の表示名、自律行動は列が無いので None
        self.assertEqual(
            data["dm_settings"],
            {"persona": "ツンデレ", "persona_source": "saved", "persona_missing": None, "persona_builtin": False,
             "model": "Gemini 3.1 Flash Lite", "respond_mode": "すべての発言に反応",
             "grounding": False, "auto_topic_enabled": True, "auto_topic_interval_hours": 3,
             "autonomous_enabled": None, "saved": True},
        )
        # 旧版は固定の選択肢で表示し、prompts/ のファイル構成からは推定しない
        self.assertFalse(data["persona_estimated"])

    def test_new_layout_and_autonomous_column(self) -> None:
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        data = self.insights()
        self.assertEqual(data["source_dir"], "src")
        self.assertTrue(data["autonomous_supported"])
        self.assertIs(data["dm_settings"]["autonomous_enabled"], False)

    def test_v15_channel_settings_preserve_ids_and_default_without_writing_db(self) -> None:
        make_fake_bot(self.root, layout="src", with_autonomous_column=True, with_channel_columns=True)
        stage_id, voice_id = 2 ** 60 + 1, 2 ** 60 + 3  # 架空値。JS の安全な整数範囲を超える。
        db_path = self.root / "data/memory/bot.db"
        # 接続のwithはトランザクションだけを終えるため、Windowsの削除前に明示的に閉じる。
        with closing(sqlite3.connect(db_path)) as con, con:
            con.execute(
                "INSERT INTO guild_settings (guild_id, autonomous_stage_channel_id, autonomous_voice_channel_id) "
                "VALUES (?, ?, ?)", (11, stage_id, voice_id),
            )
        before = db_path.read_bytes()
        data = self.insights(journal=[], settings_cases=[{}])
        settings = data["discord"]["guilds"][0]["settings"]
        self.assertEqual(settings["autonomous_stage_channel_id"], str(stage_id))
        self.assertEqual(settings["autonomous_voice_channel_id"], str(voice_id))
        for defaults in (data["dm_settings"], data["settings_cases"][0]):
            self.assertIsNone(defaults["autonomous_stage_channel_id"])
            self.assertIsNone(defaults["autonomous_voice_channel_id"])
        self.assertEqual(db_path.read_bytes(), before)

    def test_v15_channel_settings_reject_invalid_saved_values(self) -> None:
        make_fake_bot(self.root, layout="src", with_autonomous_column=True, with_channel_columns=True)
        invalid = (None, 0, -1, True, False, "12", 1.2, [], {})
        cases = [{"autonomous_stage_channel_id": value, "autonomous_voice_channel_id": value}
                 for value in invalid]
        for view in self.insights(journal=[], settings_cases=cases)["settings_cases"]:
            self.assertIsNone(view["autonomous_stage_channel_id"])
            self.assertIsNone(view["autonomous_voice_channel_id"])

    def test_without_token_reports_error(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        discord = self.insights()["discord"]
        self.assertFalse(discord["ok"])
        self.assertIn("TOKEN_DISCORD", discord["error"])

    def test_token_from_env_file_is_never_printed(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        (self.root / ".env").write_text(f'API_KEY_GEMINI=x\nTOKEN_DISCORD="{FAKE_TOKEN}"\n', encoding="utf-8")
        discord = self.insights()["discord"]
        self.assertFalse(discord["ok"])
        self.assertIn("接続できませんでした", discord["error"])  # .env から読めたので API へ行こうとした
        self.assertNotIn(FAKE_TOKEN, self.stdout)

    def test_output_has_no_guild_ids(self) -> None:
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        con = sqlite3.connect(self.root / "data" / "memory" / "bot.db")
        con.execute("INSERT INTO guild_settings (guild_id, prompt_file) VALUES (?, '../prompts/prompt_tsun.txt')", (GUILD_ID,))
        con.commit()
        con.close()
        self.insights()
        self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_discord_success_has_names_defaults_and_no_ids_or_token(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"id": str(GUILD_ID + 2), "username": "架空のボット", "discriminator": "0"},
            "/users/@me/guilds": [{"id": str(GUILD_ID), "name": "架空のサーバー",
                                   "approximate_member_count": 12, "approximate_presence_count": 3}],
            f"/guilds/{GUILD_ID}/voice-states/@me": {"channel_id": str(GUILD_ID + 1)},
            f"/channels/{GUILD_ID + 1}": {"id": str(GUILD_ID + 1), "name": "架空のボイス"},
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        guild = data["discord"]["guilds"][0]
        self.assertEqual((guild["members"], guild["online"], guild["voice_channel"]), (12, 3, "架空のボイス"))
        self.assertEqual(guild["settings"]["persona"], "標準")
        self.assertIs(guild["settings"]["autonomous_enabled"], False)
        self.assertFalse(guild["settings"]["saved"])
        for secret in (FAKE_TOKEN, str(GUILD_ID), str(GUILD_ID + 1), str(GUILD_ID + 2)):
            self.assertNotIn(secret, self.stdout)

    def test_paginated_discord_script_keeps_settings_after_first_200_guilds(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        con = sqlite3.connect(self.root / "data/memory/bot.db")
        try:
            con.execute("INSERT INTO guild_settings (guild_id, respond_mode) VALUES (?, 'all')", (GUILD_ID + 200,))
            con.commit()
        finally:
            con.close()
        guilds = [{"id": str(GUILD_ID + index), "name": f"架空サーバー {index}"} for index in range(201)]
        responses = {
            "/users/@me": {"username": "架空ボット"},
            "/users/@me/guilds?with_counts=true&limit=200": guilds[:200],
            f"/users/@me/guilds?with_counts=true&limit=200&after={GUILD_ID + 199}": guilds[200:],
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        self.assertTrue(data["discord"]["ok"])
        self.assertFalse(data["discord"]["partial"])
        self.assertEqual(len(data["discord"]["guilds"]), 201)
        last = data["discord"]["guilds"][-1]
        self.assertEqual(last["name"], "架空サーバー 200")
        self.assertTrue(last["settings"]["saved"])
        self.assertEqual(last["settings"]["respond_mode"], "すべての発言に反応")
        for secret in (FAKE_TOKEN, str(GUILD_ID), str(GUILD_ID + 200)):
            self.assertNotIn(secret, self.stdout)

    def test_discord_errors_keep_db_and_log_results(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        con = sqlite3.connect(self.root / "data/memory/bot.db")
        con.execute("INSERT INTO guild_settings (guild_id, respond_mode) VALUES (?, 'all')", (GUILD_ID,))
        con.commit()
        con.close()
        for code in (401, 429):
            with self.subTest(code=code):
                data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses={"/users/@me": code},
                                     journal=[{"MESSAGE": "Total token count：42"}])
                self.assertIn(str(code), data["discord"]["error"])
                self.assertEqual(data["discord"]["guilds"][0]["settings"]["respond_mode"], "すべての発言に反応")
                self.assertEqual(data["activity"]["gemini_tokens"], 42)
                self.assertNotIn(FAKE_TOKEN, self.stdout)
                self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_journal_counts_events_without_returning_conversation_or_ids(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        messages = ["Total token count：42", "User (架空の利用者)：架空の会話本文",
                    "has connected to Gateway", "successfully RESUMED", "session has been invalidated",
                    "heartbeat blocked", "Traceback (most recent call last):",
                    f"自律行動: ボイス「架空のボイス」で配信を開始しました（guild={GUILD_ID}）。",
                    f"自律行動: 配信を終了しました（guild={GUILD_ID} / 理由: 架空の終了理由）。"]
        entries = [{"MESSAGE": message, "__REALTIME_TIMESTAMP": "1000000"} for message in messages]
        entries += [None, [], {"MESSAGE": [-1, 300]}, {"MESSAGE": [255], "__REALTIME_TIMESTAMP": None}]
        data = self.insights(journal=entries, sudo=True)
        activity = data["activity"]
        for key in ("gemini_calls", "conversations", "gateway_connects", "gateway_resumes", "gateway_invalidated",
                    "heartbeat_blocked", "exceptions"):
            self.assertEqual(activity[key], 1, key)
        self.assertEqual(activity["last_conversation_at"], 1)
        self.assertEqual([event["text"] for event in activity["autonomous_events"]],
                         ["配信を開始しました", "配信を終了しました"])
        for secret in (str(GUILD_ID), "架空の会話本文", "架空の終了理由", "架空の利用者"):
            self.assertNotIn(secret, self.stdout)

    def test_journal_failure_is_reported_without_stderr(self):
        make_fake_bot(self.root, layout="index", with_autonomous_column=False)
        data = self.insights(journal=[], journal_code=1)
        self.assertTrue(any("読み取り" in error or "journal_sudo" in error for error in data["errors"]))
        self.assertNotIn("private journal detail", self.stdout)

    def test_logging_and_print_activity_are_counted_without_returning_bodies(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        prefix = "[2026-10-29T07:32:26+0900] [INFO] [persona_bot.utils.usage_graph] "
        messages = ["User (架空の旧利用者)：架空の旧会話", "Total token count：7",
                    prefix + "User (架空の新利用者)：架空の新会話", prefix + "Total token count：42",
                    prefix + "自律行動: 配信を終了しました（理由: 架空の終了理由）。",
                    "[invalid timestamp] [INFO] [persona_bot.utils.usage_graph] Total token count：999"]
        entries = [{"MESSAGE": message, "__REALTIME_TIMESTAMP": str((index + 1) * 1_000_000)}
                   for index, message in enumerate(messages)]
        activity = self.insights(journal=entries)["activity"]
        self.assertEqual((activity["conversations"], activity["last_conversation_at"]), (2, 3))
        self.assertEqual((activity["gemini_calls"], activity["gemini_tokens"]), (2, 49))
        self.assertEqual(activity["autonomous_events"], [{"at": 5, "text": "配信を終了しました"}])
        for secret in ("架空の旧利用者", "架空の新利用者", "架空の旧会話", "架空の新会話", "架空の終了理由"):
            self.assertNotIn(secret, self.stdout)

    def test_current_formatter_exception_lines_are_counted(self):
        # OperationalFormatter は「例外: <型名>」だけを出す。旧形式と合わせて数える（#433）
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        messages = ["例外: ValueError", "例外情報を読み取れませんでした。", "Traceback (most recent call last):",
                    "例外: 空白 を 含む", "通常の運用ログ 例外: ValueError"]
        entries = [{"MESSAGE": message, "__REALTIME_TIMESTAMP": "1000000"} for message in messages]
        self.assertEqual(self.insights(journal=entries)["activity"]["exceptions"], 3)

    def test_malformed_voice_responses_do_not_drop_the_guild_list(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"username": "架空のボット"},
            "/users/@me/guilds": [{"id": "11", "name": "架空A"}, {"id": "12", "name": "架空B"},
                                  {"id": "13", "name": "架空C"}, {"id": "14", "name": "架空D"}],
            "/guilds/11/voice-states/@me": ["配列の応答"],
            "/guilds/12/voice-states/@me": {"channel_id": "../users/@me"},
            "/guilds/13/voice-states/@me": {"channel_id": "15"},
            "/channels/15": ["配列の応答"],
            "/guilds/14/voice-states/@me": {"channel_id": None},
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])["discord"]
        self.assertTrue(data["ok"])
        first, second, third, fourth = data["guilds"]
        self.assertIsNone(first["voice_channel"])
        self.assertIsNone(first["voice_error"])  # dict でなければ「接続していない」扱い
        self.assertIsNone(second["voice_channel"])
        self.assertIsNotNone(second["voice_error"])  # Snowflake でない値はパスに連結しない
        self.assertIsNotNone(third["voice_channel"])  # 名前だけ取れなくても接続先は分かる
        self.assertIsNone(fourth["voice_error"])

    def test_no_matching_conversation_is_empty_without_permission_warning(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        data = self.insights(journal=[], journal_grep_code=1, journal_stderr="")
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["activity"]["conversations"], 0)
        self.assertIsNone(data["activity"]["last_conversation_at"])

    def test_empty_grep_failure_with_stderr_is_still_reported(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        data = self.insights(journal=[], journal_grep_code=1)
        self.assertTrue(any("journal_sudo" in error for error in data["errors"]))
        self.assertNotIn("private journal detail", self.stdout)

    def test_other_journal_failures_are_not_treated_as_no_matches(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        for code, grep_code in ((1, 1), (0, 2)):
            with self.subTest(code=code, grep_code=grep_code):
                data = self.insights(journal=[], journal_code=code,
                                     journal_grep_code=grep_code, journal_stderr="")
                self.assertTrue(any("journal_sudo" in error for error in data["errors"]))

    def test_voice_rate_limit_keeps_guilds_and_marks_voice_unknown(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {"/users/@me": {"username": "架空のボット"},
                     "/users/@me/guilds": [{"id": str(GUILD_ID), "name": "架空のサーバー"}],
                     f"/guilds/{GUILD_ID}/voice-states/@me": 429}
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        self.assertTrue(data["discord"]["ok"])
        self.assertIn("429", data["discord"]["guilds"][0]["voice_error"])
        self.assertNotIn(str(GUILD_ID), self.stdout)

    def test_env_comments_are_supported_and_database_is_unchanged(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        (self.root / ".env").write_text(f'export TOKEN_DISCORD="{FAKE_TOKEN}" # 架空の設定\n', encoding="utf-8")
        before = (self.root / "data/memory/bot.db").read_bytes()
        data = self.insights(responses={"/users/@me": {"username": "架空のボット"}, "/users/@me/guilds": []}, journal=[])
        self.assertTrue(data["discord"]["ok"])
        self.assertNotIn(FAKE_TOKEN, self.stdout)
        self.assertEqual((self.root / "data/memory/bot.db").read_bytes(), before)

    def test_annotated_sources_are_read_without_importing(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        source = self.root / "src/persona_bot/config"
        (source / "constants.py").write_text(CONSTANTS_PY.replace("DEFAULT_MODEL_NAME =", "DEFAULT_MODEL_NAME: str =")
                                            + '\nraise RuntimeError("実行禁止")', encoding="utf-8")
        (source / "choices.py").write_text(CHOICES_PY.replace("PERSONA_CHOICES =", "PERSONA_CHOICES: list =")
                                          + '\nraise RuntimeError("実行禁止")', encoding="utf-8")
        data = self.insights(journal=[])
        self.assertEqual(data["dm_settings"]["persona"], "ツンデレ")
        self.assertEqual(data["dm_settings"]["model"], "Gemini 3.1 Flash Lite")

    def make_current_bot(self, files=(), settings=None):
        """現行版（personas.py がある版）の構成。files は prompts/ に置く架空のファイル名。"""
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        source = self.root / "src/persona_bot/config"
        # 新しい版には DEFAULT_PROMPT_FILE と PERSONA_CHOICES が無く、personas.py がある
        constants = [line for line in CONSTANTS_PY.splitlines() if not line.startswith("DEFAULT_PROMPT_FILE")]
        (source / "constants.py").write_text("\n".join(constants), encoding="utf-8")
        (source / "choices.py").write_text(
            'MODEL_CHOICES = [app_commands.Choice(name="Gemini 3.1 Flash Lite", value=DEFAULT_MODEL_NAME)]\n',
            encoding="utf-8",
        )
        (source / "personas.py").write_text((BOT_CONFIG / "personas.py").read_text(encoding="utf-8"), encoding="utf-8")
        prompts = self.root / "prompts"
        prompts.mkdir()
        for name in files:
            (prompts / name).write_text("架空のペルソナ", encoding="utf-8")
        if settings is not None:
            text = settings if isinstance(settings, str) else json.dumps(settings, ensure_ascii=False)
            (prompts / "personas.json").write_text(text, encoding="utf-8")
        return prompts

    def personas_for(self, saved_values):
        """保存値ごとのペルソナ表示。Discord API を使わず「保存済みサーバー」として ID 順に並べる。"""
        con = sqlite3.connect(self.root / "data/memory/bot.db")
        con.execute("DELETE FROM guild_settings")
        con.executemany("INSERT INTO guild_settings (guild_id, prompt_file) VALUES (?, ?)",
                        list(enumerate(saved_values, 1)))
        con.commit()
        con.close()
        data = self.insights(journal=[])
        settings = [guild["settings"] for guild in data["discord"]["guilds"]]
        self.assertEqual(len(settings), len(saved_values))
        return data, settings

    def persona_view(self, settings):
        return tuple(settings[key] for key in ("persona", "persona_source", "persona_missing", "persona_builtin"))

    def test_personas_json_names_and_default_override_old_choices(self):
        # ver0.1.8 以降はペルソナの表示名と既定をサーバー上の prompts/personas.json に書く
        prompts = self.make_current_bot(["prompt_tsun.txt", "架空の既定.txt"], {
            "default": "架空の既定.txt",
            "personas": {"prompt_tsun.txt": {"name": "架空の キャラ"}, "架空の既定.txt": {"name": "架空の既定キャラ"}},
        })
        responses = {
            "/users/@me": {"username": "架空のボット"},
            "/users/@me/guilds": [{"id": str(GUILD_ID), "name": "架空のサーバー"}],
            f"/guilds/{GUILD_ID}/voice-states/@me": 404,
        }
        data = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])
        self.assertTrue(data["persona_estimated"])
        self.assertEqual(self.persona_view(data["dm_settings"]), ("架空の キャラ", "saved", None, False))
        self.assertEqual(self.persona_view(data["discord"]["guilds"][0]["settings"]),
                         ("架空の既定キャラ", "default", None, False))

        (prompts / "personas.json").write_text("{壊れた", encoding="utf-8")
        data = self.insights(journal=[])
        self.assertEqual(data["dm_settings"]["persona"], "prompt_tsun")
        self.assertTrue(any("personas.json" in error for error in data["errors"]))

    def test_default_without_settings_file_is_first_file_by_name(self):
        # personas.json が無くても、ボットは有効なファイルのうちファイル名順で先頭を既定にする（#223）
        self.make_current_bot(["prompt_b.txt", "prompt_a.txt", ".隠し.txt", "メモ.md", "00_prompt_format.txt"])
        data, settings = self.personas_for(["", "../prompts/prompt_b.txt"])
        self.assertEqual(self.persona_view(settings[0]), ("prompt_a", "default", None, False))
        self.assertEqual(self.persona_view(settings[1]), ("prompt_b", "saved", None, False))
        self.assertEqual(self.persona_view(data["dm_settings"]), ("prompt_a", "default", None, False))
        self.assertTrue(data["persona_estimated"])

    def test_omitted_or_missing_default_falls_back_to_first_listed_persona(self):
        entries = {"prompt_c.txt": {"name": "架空のC"}, "prompt_a.txt": {"name": "架空のA"}}
        for index, default in enumerate((None, "架空の存在しない既定.txt", 123)):
            with self.subTest(default=default):
                self.root = Path(self.temp.name) / f"case-{index}"
                settings = {"personas": entries} if default is None else {"default": default, "personas": entries}
                self.make_current_bot(["prompt_a.txt", "prompt_b.txt", "prompt_c.txt"], settings)
                _, views = self.personas_for([""])
                # JSON に書いた順が先なので prompt_c が先頭になる。存在しない既定の名前は表示しない
                self.assertEqual(self.persona_view(views[0]), ("架空のC", "default", None, False))

    def test_missing_saved_file_shows_saved_name_separately_from_effective_default(self):
        self.make_current_bot(["prompt_a.txt"], {"default": "prompt_a.txt", "personas": {"prompt_a.txt": {"name": "架空のA"}}})
        _, views = self.personas_for(["../prompts/prompt_消えた.txt", "../prompts/../.env", "架空の旧形式"])
        # ボットは保存値を消さずに既定で応答する。保存値は別の項目で返す
        self.assertEqual(self.persona_view(views[0]), ("架空のA", "missing", "prompt_消えた", False))
        # ペルソナの形でない値は、ボットが復元時に破棄して既定へ戻す
        self.assertEqual(self.persona_view(views[1]), ("架空のA", "default", None, False))
        self.assertEqual(self.persona_view(views[2]), ("架空のA", "default", None, False))

    def test_templates_only_uses_builtin_persona(self):
        self.make_current_bot(["00_prompt_format.txt", "00_prompt_format.json"])
        data, views = self.personas_for(["", "../prompts/prompt_消えた.txt", "../prompts/00_prompt_format.txt"])
        self.assertEqual(self.persona_view(views[0]), ("標準（組み込み）", "default", None, True))
        self.assertEqual(self.persona_view(views[1]), ("標準（組み込み）", "missing", "prompt_消えた", True))
        self.assertEqual(self.persona_view(views[2]), ("標準（組み込み）", "default", None, True))
        self.assertEqual(data["errors"], [])

    def test_estimate_matches_bot_catalog_rules(self):
        # 同じファイル構成に対して、ボット本体の load_persona_catalog と結果が一致する
        long_name = "x" * (100 - len("../prompts/") - len(".txt") + 1) + ".txt"
        just_fits = "y" * (100 - len("../prompts/") - len(".txt")) + ".txt"
        files = ["prompt_b.txt", "prompt_a.txt", "架空.txt", "UPPER.TXT", ".hidden.txt", "note.md",
                 "00_prompt_format.txt", "00_prompt_format.json", long_name, just_fits]
        layouts = {
            "設定ファイルなし": None,
            "既定の省略と表示名": {"personas": {"架空.txt": {"name": " 架空の  名前 "}, "prompt_b.txt": {"name": ""},
                                          "ghost.txt": {"name": "存在しない"}, "UPPER.TXT": "誤った形"}},
            "存在しない既定": {"default": "ghost.txt", "personas": {"prompt_b.txt": {}}},
            "有効な既定": {"default": "prompt_b.txt"},
            "テンプレートを既定": {"default": "00_prompt_format.txt"},
            "壊れたJSON": "{壊れた",
            "最上位が配列": [],
            "personasが配列": {"personas": ["prompt_b.txt"], "default": 1},
        }
        saved_values = ["", "../prompts/prompt_a.txt", "../prompts/prompt_b.txt", "../prompts/ghost.txt",
                        "../prompts/UPPER.TXT", "../prompts/" + long_name, "../prompts/" + just_fits,
                        "../prompts/../.env", "../prompts/00_prompt_format.txt", "../prompts/.hidden.txt", "架空の旧形式"]
        for index, (layout, settings) in enumerate(layouts.items()):
            with self.subTest(layout=layout):
                self.root = Path(self.temp.name) / f"case-{index}"
                prompts = self.make_current_bot(files, settings)
                (prompts / "dir.txt").mkdir()
                # ボットと同じ定数（接頭辞・テンプレート名・値の上限）を読む
                (self.root / "src/persona_bot/config/constants.py").write_text(
                    (BOT_CONFIG / "constants.py").read_text(encoding="utf-8"), encoding="utf-8")
                _, views = self.personas_for(saved_values)
                expected = bot_persona_views(prompts, saved_values)
                self.assertEqual([list(self.persona_view(view)) for view in views], expected)

    def test_voice_permission_error_is_local_but_rate_limit_stops_later_requests(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        responses = {
            "/users/@me": {"username": "架空のボット"},
            "/users/@me/guilds": [{"id": "11", "name": None}, {"id": "12", "name": "架空の次サーバー"}],
            "/guilds/11/voice-states/@me": 403,
            "/guilds/12/voice-states/@me": {"channel_id": "13"},
            "/channels/13": {"name": "架空の通話"},
        }
        first, second = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])["discord"]["guilds"]
        self.assertIsInstance(first["name"], str)
        self.assertIn("403", first["voice_error"])
        self.assertIsNone(second["voice_error"])
        self.assertEqual(second["voice_channel"], "架空の通話")
        responses["/guilds/11/voice-states/@me"] = 429
        first, second = self.insights({"TOKEN_DISCORD": FAKE_TOKEN}, responses=responses, journal=[])["discord"]["guilds"]
        self.assertIn("429", second["voice_error"])
        self.assertIsNone(second["voice_channel"])

    def test_env_empty_or_placeholder_lines_do_not_erase_valid_token(self):
        make_fake_bot(self.root, layout="src", with_autonomous_column=True)
        (self.root / ".env").write_text(f'TOKEN_DISCORD={FAKE_TOKEN}\nTOKEN_DISCORD=\nTOKEN_DISCORD=YOUR_DISCORD_BOT_TOKEN\n', encoding="utf-8-sig")
        responses = {"/users/@me": {"username": "架空"}, "/users/@me/guilds": []}
        self.assertTrue(self.insights(responses=responses, journal=[])["discord"]["ok"])
        (self.root / ".env").write_text('TOKEN_DISCORD=YOUR_DISCORD_BOT_TOKEN\n', encoding="utf-8")
        self.assertIn("TOKEN_DISCORD がありません", self.insights(responses=responses, journal=[])["discord"]["error"])


class DbSummaryScriptTests(unittest.TestCase):
    def test_daily_feedback_uses_composite_index_and_keeps_mixed_timestamp_boundaries(self):
        namespace = {"__name__": "test_db_summary"}
        exec(compile(rc.DB_SUMMARY_SCRIPT, "db_summary.py", "exec"), namespace)
        plans = []

        class TracedConnection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql.startswith("SELECT date(created_at"):
                    plans.extend(row[3] for row in super().execute("EXPLAIN QUERY PLAN " + sql))
                return super().execute(sql, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bot.db"
            con = sqlite3.connect(path)
            try:
                con.execute("CREATE TABLE feedback(guild_id INTEGER, created_at TEXT, rating INTEGER)")
                con.execute("CREATE INDEX idx_feedback_guild_created ON feedback(guild_id, created_at)")
                stamps = con.execute(
                    "SELECT datetime('now', '+9 hours', 'start of day', '-9 hours'), "
                    "datetime('now', '+9 hours', 'start of day', '+1 day', '-9 hours')"
                ).fetchone()
                for stamp, rating in ((stamps[0], 1), (stamps[1], -1)):
                    for value in (stamp, stamp.replace(" ", "T") + "+00:00"):
                        con.execute("INSERT INTO feedback VALUES (1, ?, ?)", (value, rating))
                con.commit()
            finally:
                con.close()
            connect = sqlite3.connect
            with patch.object(sqlite3, "connect", side_effect=lambda *args, **kwargs: connect(
                *args, factory=TracedConnection, **kwargs,
            )):
                data = namespace["collect"](str(path), str(root / "missing.json"))
            self.assertEqual(sum(day["good"] for day in data["feedback_by_day"]), 2)
            self.assertEqual(sum(day["bad"] for day in data["feedback_by_day"]), 0)
        self.assertTrue(any("SEARCH feedback USING INDEX idx_feedback_guild_created" in plan
                            and "created_at>?" in plan and "created_at<?" in plan for plan in plans), plans)

    def summarize(self, root):
        completed = run_script(rc.DB_SUMMARY_SCRIPT, [str(root / "bot.db"), str(root / "blacklist.json")])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_missing_tables_keep_other_counts_and_never_read_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            con.execute('CREATE TABLE "fictional\""table" (content TEXT)')
            con.execute('INSERT INTO "fictional\""table" VALUES (?)', ("架空の秘密の会話",))
            con.execute("CREATE TABLE reminders (status TEXT)")
            con.execute("INSERT INTO reminders VALUES ('pending')")
            con.commit()
            con.close()
            before = (root / "bot.db").read_bytes()
            (root / "blacklist.json").write_text(json.dumps({"11": {"blocked": True}, "12": {"blocked": False}}), encoding="utf-8")
            data = self.summarize(root)
            self.assertEqual(data["tables"]['fictional"table'], 1)
            self.assertEqual(data["reminders_by_status"], {"pending": 1})
            self.assertIsNone(data["queue_by_status"])
            self.assertIsNone(data["feedback"])
            self.assertEqual(data["blacklist"], {"users": 2, "blocked": 1})
            self.assertTrue(any("文書キュー" in error for error in data["errors"]))
            self.assertNotIn("架空の秘密の会話", json.dumps(data, ensure_ascii=False))
            self.assertEqual((root / "bot.db").read_bytes(), before)

    def test_missing_database_and_broken_blacklist_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "blacklist.json").write_text("[]", encoding="utf-8")
            data = self.summarize(root)
            self.assertFalse(data["exists"])
            self.assertIsNone(data["blacklist"])
            self.assertTrue(any("blacklist.json" in error for error in data["errors"]))
            self.assertFalse((root / "bot.db").exists())

    def test_corrupt_database_keeps_blacklist_counts_without_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bot.db").write_bytes(b"fictional-invalid-database")
            (root / "blacklist.json").write_text('{"11":{"blocked":true}}', encoding="utf-8")
            data = self.summarize(root)
            self.assertEqual(data["blacklist"], {"users": 1, "blocked": 1})
            self.assertTrue(any("bot.db を読み取れません" in error for error in data["errors"]))
            self.assertFalse(any("file is not a database" in error for error in data["errors"]))

    def test_daily_feedback_excludes_future_and_old_days_at_jst_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            con.execute("CREATE TABLE feedback (rating INTEGER, created_at TEXT)")
            now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
            today = now_jst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)
            rows = [(1, today), (-1, today - timedelta(seconds=1)),
                    (1, today - timedelta(days=6)), (-1, today - timedelta(days=6, seconds=1)),
                    (1, today + timedelta(days=1)), (1, today - timedelta(days=20))]
            con.executemany("INSERT INTO feedback VALUES (?, ?)", [(rating, at.isoformat()) for rating, at in rows])
            con.commit()
            con.close()
            data = self.summarize(root)
            self.assertEqual(sum(day["good"] + day["bad"] for day in data["feedback_by_day"]), 3)
            today_key = now_jst.strftime("%Y-%m-%d")
            self.assertEqual(next(day for day in data["feedback_by_day"] if day["day"] == today_key)["good"], 1)

    def test_boundary_accepts_old_space_and_iso_utc_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            con = sqlite3.connect(root / "bot.db")
            try:
                con.execute("CREATE TABLE feedback(created_at TEXT, rating INTEGER)")
                start = con.execute("SELECT datetime('now', '+9 hours', 'start of day', '-6 days', '-9 hours')").fetchone()[0]
                for stamp in (start, start.replace(" ", "T") + "+00:00"):
                    con.execute("INSERT INTO feedback VALUES (?, 1)", (stamp,))
                con.commit()
            finally:
                con.close()
            data = self.summarize(root)
            self.assertEqual(sum(day["good"] for day in data["feedback_by_day"]), 2)

    def test_counts_and_feedback_by_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "bot.db"
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE reminders (status TEXT)")
            con.execute("CREATE TABLE queue_tasks (status TEXT)")
            con.execute("CREATE TABLE feedback (rating INTEGER, created_at TEXT)")
            con.executemany("INSERT INTO reminders VALUES (?)", [("pending",), ("done",), ("done",)])
            now = datetime.now(timezone.utc).replace(microsecond=0)
            rows = [(1, now), (1, now), (-1, now), (1, now - timedelta(days=1)), (-1, now - timedelta(days=20))]
            con.executemany("INSERT INTO feedback VALUES (?, ?)", [(rating, at.isoformat()) for rating, at in rows])
            con.execute("PRAGMA user_version = 8")
            con.commit()
            con.close()

            completed = run_script(rc.DB_SUMMARY_SCRIPT, [str(db_path), str(Path(temp) / "missing.json")])
            self.assertEqual(completed.returncode, 0, completed.stderr)
            data = json.loads(completed.stdout.strip().splitlines()[-1])

        self.assertEqual(data["schema_version"], 8)
        self.assertEqual(data["reminders_by_status"], {"pending": 1, "done": 2})
        self.assertEqual(data["feedback"], {"good": 3, "bad": 2})
        # 20日前の評価は直近7日に入らない。日付は日本時間で区切る
        by_day = {item["day"]: (item["good"], item["bad"]) for item in data["feedback_by_day"]}
        today_jst = (now + timedelta(hours=9)).strftime("%Y-%m-%d")
        self.assertEqual(by_day[today_jst], (2, 1))
        self.assertEqual(sum(good + bad for good, bad in by_day.values()), 4)
        self.assertIsNone(data["blacklist"])


class DbTableScriptTests(unittest.TestCase):
    """テーブルの中身を取り出すスクリプト（remote_scripts/db_table.py）。架空の bot.db で確かめる。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path, _ = make_fake_database(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def read(self, table, offset=0, limit=50, db_path=None):
        completed = run_script(rc.DB_TABLE_SCRIPT, ["--db-table", str(db_path or self.db_path), table, str(offset), str(limit)])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # 出力は ASCII だけ（サーバーのロケールが UTF-8 でなくても書き出せるように）
        self.assertTrue(completed.stdout.isascii())
        return json.loads(completed.stdout.strip().splitlines()[-1])

    def test_tables_match_the_backend_whitelist(self) -> None:
        self.assertEqual(set(db_table.TABLES), set(rc.DB_TABLES))

    def test_every_table_can_be_read(self) -> None:
        for table in rc.DB_TABLES:
            with self.subTest(table=table):
                data = self.read(table)
                self.assertEqual(data["errors"], [])
                self.assertEqual(data["table"], table)
                self.assertGreater(data["total"], 0)
                self.assertEqual(len(data["rows"]), min(50, data["total"]))
                self.assertEqual(set(data["rows"][0]["cells"]), set(data["columns"]))

    def test_ids_are_strings_and_names_are_attached(self) -> None:
        data = self.read("feedback")
        con = sqlite3.connect(self.db_path)
        newest = con.execute("SELECT message_id, user_id FROM feedback ORDER BY created_at DESC LIMIT 1").fetchone()
        con.close()
        first = data["rows"][0]["cells"]
        # 19 桁の ID も、下の桁が丸まらないよう文字列で届く
        self.assertEqual(first["message_id"], str(newest[0]))
        self.assertIsInstance(first["guild_id"], str)
        self.assertIsInstance(first["rating"], int)
        self.assertEqual(data["users"][newest[1]], dict(FAKE_USERS)[newest[1]])
        created = [row["cells"]["created_at"] for row in data["rows"]]
        self.assertEqual(created, sorted(created, reverse=True))

    def test_pages_do_not_overlap_and_limit_is_capped(self) -> None:
        pages = [self.read("feedback", offset=offset) for offset in (0, 50, 100)]
        self.assertEqual([len(page["rows"]) for page in pages], [50, 50, 30])
        keys = [(row["cells"]["message_id"], row["cells"]["user_id"]) for page in pages for row in page["rows"]]
        self.assertEqual(len(set(keys)), 130)
        self.assertEqual(self.read("feedback", limit=100000)["limit"], 200)
        self.assertEqual(self.read("feedback", offset=-5)["offset"], 0)

    def test_json_columns_are_parsed(self) -> None:
        rows = {row["cells"]["user_id"]: row["cells"] for row in self.read("user_stats")["rows"]}
        stats = rows[FAKE_USERS[0][0]]
        self.assertEqual(len(stats["hourly"]), 24)
        self.assertIsInstance(stats["recent_samples"], list)
        self.assertEqual(set(stats["analysis"]["big_five"]), {"openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"})
        values = {row["cells"]["key"]: row["cells"]["value"] for row in self.read("profile")["rows"] if row["cells"]["user_id"] == FAKE_USERS[0][0]}
        self.assertEqual(values["好きなもの"], ["猫", "カレー", "RPG"])
        self.assertEqual(values["anniversaries"][0]["label"], "誕生日")

    def test_deep_json_keeps_other_rows_visible_and_database_unchanged(self) -> None:
        # パーサー・表示用変換・出力の再帰上限をまたぐ複数の深さ。
        deep = "[" * 1500 + "0" + "]" * 1500
        medium = "[" * 600 + "0" + "]" * 600
        near_limit = "[" * 995 + "0" + "]" * 995
        con = sqlite3.connect(self.db_path)
        try:
            con.executemany("INSERT INTO profile (user_id, key, value) VALUES (?, ?, ?)", [
                ("fictional", "deep", deep), ("fictional", "medium", medium),
                ("fictional", "near_limit", near_limit),
                ("fictional", "normal", '["架空"]'),
            ])
            con.commit()
        finally:
            con.close()
        before = self.db_path.read_bytes()
        rows = self.read("profile")["rows"]
        values = {row["cells"]["key"]: row["cells"]["value"] for row in rows
                  if row["cells"]["user_id"] == "fictional"}
        self.assertEqual(values["deep"], deep)
        self.assertEqual(values["normal"], ["架空"])
        # Python の版ごとに再帰上限へ達する深さが異なる。
        for key, raw, depth in (("medium", medium, 600), ("near_limit", near_limit, 995)):
            item = values[key]
            if isinstance(item, str):
                self.assertEqual(item, raw)
            else:
                for _ in range(depth):
                    self.assertIsInstance(item, list)
                    self.assertEqual(len(item), 1)
                    item = item[0]
                self.assertEqual(item, 0)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_opted_out_user_samples_and_analysis_are_not_returned(self) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "UPDATE user_stats SET recent_samples = ?, analysis = ? WHERE user_id = ?",
            (json.dumps(["架空の停止中の発言"], ensure_ascii=False), json.dumps({"personality": "架空の停止中の解析"}, ensure_ascii=False), FAKE_OPTED_OUT_USER),
        )
        con.commit()
        con.close()
        data = self.read("user_stats")
        row = next(row for row in data["rows"] if row["cells"]["user_id"] == FAKE_OPTED_OUT_USER)
        self.assertIsNone(row["cells"]["recent_samples"])
        self.assertIsNone(row["cells"]["analysis"])
        self.assertEqual(row["hidden"], ["recent_samples", "analysis"])
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("架空の停止中の発言", text)
        self.assertNotIn("架空の停止中の解析", text)
        # 止めていない人の発言サンプルは届く
        others = [row for row in data["rows"] if row["cells"]["user_id"] != FAKE_OPTED_OUT_USER]
        self.assertTrue(all(row["cells"]["recent_samples"] and "hidden" not in row for row in others))

    def test_database_is_opened_read_only(self) -> None:
        before = self.db_path.read_bytes()
        for table in rc.DB_TABLES:
            self.read(table)
        self.assertEqual(self.db_path.read_bytes(), before)
        self.assertFalse(Path(str(self.db_path) + "-wal").exists())

    def test_unknown_table_and_missing_database_are_reported_without_paths(self) -> None:
        data = self.read("sqlite_master")
        self.assertEqual(data["rows"], [])
        self.assertTrue(data["errors"])
        missing = Path(self.temp.name) / "fictional-missing" / "bot.db"
        data = self.read("feedback", db_path=missing)
        self.assertEqual(data["errors"], ["bot.db が見つかりません。"])
        self.assertNotIn("fictional-missing", json.dumps(data))


    def test_names_are_looked_up_only_for_the_shown_users(self) -> None:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE reminders (reminder_id INTEGER PRIMARY KEY, user_id TEXT, user_name TEXT)")
        con.execute("CREATE TABLE user_stats (user_id TEXT PRIMARY KEY, user_name TEXT)")
        # 表示しない利用者の行を多数置いても、問い合わせは表示中の ID だけに絞られる
        con.executemany("INSERT INTO reminders (user_id, user_name) VALUES (?, ?)",
                        [(str(1000 + index), "架空の他人") for index in range(300)])
        con.executemany("INSERT INTO reminders (user_id, user_name) VALUES (?, ?)", [
            ("11", "架空の旧名"), ("11", "架空の新名"), ("12", "架空のリマインダー名"), ("13", ""),
        ])
        con.executemany("INSERT INTO user_stats VALUES (?, ?)", [("12", "架空の統計名"), ("14", "架空の統計だけ")])
        statements = []
        con.set_trace_callback(statements.append)

        names = db_table.user_names(con, {"11", "12", "13", "14", "99"})

        # 新しいリマインダーの名前で上書きし、会話統計の名前を最優先にする。空の名前は使わない
        self.assertEqual(names, {"11": "架空の新名", "12": "架空の統計名", "14": "架空の統計だけ"})
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(selects), 2)
        self.assertTrue(all("IN (" in sql for sql in selects))
        self.assertFalse(any("1000" in sql for sql in selects))
        self.assertEqual(db_table.user_names(con, set()), {})

    def test_many_shown_users_are_split_into_bounded_queries(self) -> None:
        chunk = db_table.NAME_LOOKUP_CHUNK
        self.assertLess(chunk, 999)  # 古い SQLite の変数の上限
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE user_stats (user_id TEXT PRIMARY KEY, user_name TEXT)")
        ids = [str(10_000 + index) for index in range(chunk * 2 + 1)]
        con.executemany("INSERT INTO user_stats VALUES (?, ?)", [(user_id, "架空" + user_id) for user_id in ids])
        statements = []
        con.set_trace_callback(statements.append)
        names = db_table.user_names(con, set(ids))
        self.assertEqual(len(names), len(ids))
        self.assertEqual(sum(sql.lstrip().upper().startswith("SELECT") and "user_stats" in sql for sql in statements), 3)

    def test_old_schema_and_broken_values_do_not_stop_the_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "bot.db"
            con = sqlite3.connect(db_path)
            # 並び順に使う created_at が無い古い形・JSON として読めない値・NaN が入った値
            con.execute("CREATE TABLE feedback (message_id INTEGER, user_id TEXT, rating INTEGER)")
            con.executemany("INSERT INTO feedback VALUES (?, ?, ?)", [(1, "11", 1), (2, "12", -1)])
            con.execute("CREATE TABLE user_stats (user_id TEXT, hourly TEXT, analysis TEXT)")
            con.execute("INSERT INTO user_stats VALUES ('11', '[NaN, 2, Infinity]', 'not-json')")
            con.commit()
            con.close()
            feedback = self.read("feedback", db_path=db_path)
            self.assertEqual([row["cells"]["message_id"] for row in feedback["rows"]], ["2", "1"])
            stats = self.read("user_stats", db_path=db_path)["rows"][0]["cells"]
            self.assertEqual(stats["hourly"], [None, 2, None])
            self.assertEqual(stats["analysis"], "not-json")
            missing = self.read("reminders", db_path=db_path)
            self.assertEqual(missing["rows"], [])
            self.assertTrue(any("reminders" in error for error in missing["errors"]))


    def test_invalid_paging_values_fall_back_to_defaults_without_raising(self) -> None:
        """None・bool・数値でない型を渡しても、開始位置と件数を一緒に既定値へ戻す（#413）。"""
        for offset, limit in ((None, 10), (5, None), (True, 10), (5, False), ("abc", 10), (5, "xyz"),
                              ([], 10), (5, {}), (float("inf"), 10), (5, float("nan"))):
            with self.subTest(offset=offset, limit=limit):
                result = db_table.collect(str(self.db_path), "feedback", offset=offset, limit=limit)
                self.assertEqual((result["offset"], result["limit"]), (0, 50))
        # 正しい値と、これまでどおり変換できる値（数値の文字列）はそのまま使う。
        result = db_table.collect(str(self.db_path), "feedback", offset="2", limit=3)
        self.assertEqual((result["offset"], result["limit"]), (2, 3))
        result = db_table.collect(str(self.db_path), "feedback", offset=-4, limit=10**9)
        self.assertEqual((result["offset"], result["limit"]), (0, db_table.MAX_LIMIT))


class DbSummaryEntryPointTests(unittest.TestCase):
    """db_summary.py を直接 import して使うときの入口（#413）。"""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location(
            "db_summary_under_test", Path(__file__).resolve().parents[1] / "remote_scripts" / "db_summary.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_path_objects_are_accepted_and_the_result_is_json_serializable(self) -> None:
        db_path, _ = make_fake_database(self.root)
        for exists in (True, False):
            with self.subTest(exists=exists):
                target = Path(db_path) if exists else self.root / "missing.db"
                result = self.module.collect(target, self.root / "blacklist.json")
                self.assertEqual(result["exists"], exists)
                self.assertIsInstance(result["path"], str)
                json.dumps(result)  # Path が残っていると TypeError になる
        self.assertIn("tables", self.module.collect(Path(db_path), self.root / "blacklist.json"))

    def test_main_runs_with_arguments_and_explains_missing_ones(self) -> None:
        db_path, _ = make_fake_database(self.root)
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            self.module.main([str(db_path), str(self.root / "blacklist.json")])
        self.assertTrue(json.loads(out.getvalue())["exists"])
        for argv in ([], [str(db_path)]):
            with self.subTest(argv=argv), patch("sys.stdout", new_callable=io.StringIO) as out:
                self.module.main(argv)  # 生の TypeError にせず、案内を返す
                result = json.loads(out.getvalue())
                self.assertFalse(result["exists"])
                self.assertTrue(result["errors"])

    def test_script_runs_from_stdin_like_the_backend(self) -> None:
        db_path, _ = make_fake_database(self.root)
        completed = run_script(rc.DB_SUMMARY_SCRIPT, [str(db_path), str(self.root / "blacklist.json")])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout.strip().splitlines()[-1])["exists"])


if __name__ == "__main__":
    unittest.main()
