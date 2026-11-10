"""prompts/ のファイルからペルソナの選択肢を作り、利用者の設定で表示名・声・既定を決める（#189）。

実際の prompts/ は読まず、一時フォルダに架空のペルソナを置いて確かめる
（利用者のキャラ別ファイルを検証に使わないため）。
"""

import codecs
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

import discord

from persona_bot.config import constants, personas
from persona_bot.config.constants import (
    BUILTIN_PERSONA_PROMPT,
    DISCORD_SELECT_MAX_OPTIONS,
    PERSONA_SETTINGS_TEMPLATE_FILE_NAME,
    PROMPT_TEMPLATE_FILE_NAME,
    VOICE_PROFILE_FALLBACK,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.core.app_state import GuildSettings
from persona_bot.views import functions_view
from persona_bot.views.settings_view import PersonaSelect, SettingsView
from tests.helpers.persona_helpers import install_empty_persona_catalog, install_persona_catalog
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


class PersonaFolderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.prompts = Path(directory.name) / "prompts"
        self.prompts.mkdir()
        self.settings_path = self.prompts / "personas.json"

    def write(self, name: str, text: str = "架空のペルソナです。") -> Path:
        path = self.prompts / name
        path.write_text(text, encoding="utf-8")
        return path

    def write_settings(self, settings) -> None:
        self.settings_path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")

    def load(self) -> personas.PersonaCatalog:
        return personas.load_persona_catalog(self.prompts, self.settings_path)


class CatalogDiscoveryTests(PersonaFolderTestCase):
    def test_text_files_become_choices_and_template_is_excluded(self) -> None:
        self.write(PROMPT_TEMPLATE_FILE_NAME)
        for name in ("b_架空.txt", "a_架空.TXT", ".隠し.txt", "メモ.md"):
            self.write(name)
        (self.prompts / "フォルダ.txt").mkdir()
        catalog = self.load()
        # ファイル名の順に並び、拡張子なしのファイル名を表示名にする
        self.assertEqual([p.value for p in catalog.personas], ["../prompts/a_架空.TXT", "../prompts/b_架空.txt"])
        self.assertEqual([p.name for p in catalog.personas], ["a_架空", "b_架空"])
        self.assertEqual(catalog.default_value, "../prompts/a_架空.TXT")
        self.assertIs(catalog.voice_profile("../prompts/a_架空.TXT")["voice"], VOICE_PROFILE_FALLBACK["voice"])

    def test_template_only_or_missing_folder_gives_builtin_default(self) -> None:
        self.write(PROMPT_TEMPLATE_FILE_NAME)
        for prompts_dir in (self.prompts, self.prompts / "存在しない"):
            with self.subTest(prompts_dir=prompts_dir.name):
                catalog = personas.load_persona_catalog(prompts_dir, self.settings_path)
                self.assertEqual(catalog.personas, ())
                self.assertEqual(catalog.default_value, personas.FOLLOW_DEFAULT_PERSONA)
                self.assertEqual(catalog.display_name(catalog.default_value), personas.BUILTIN_PERSONA_DISPLAY_NAME)

    def test_name_too_long_for_discord_is_skipped_with_warning(self) -> None:
        # 保存値（"../prompts/" + ファイル名）が Discord の上限ちょうどなら出し、1文字でも超えたら外す。
        # 上限を超える名前は ASCII で作る（Linux のファイル名は 255 バイトまでで、日本語 100 文字は 300 バイトになり作れない）
        stem_limit = constants.DISCORD_SELECT_VALUE_MAX_LENGTH - len(constants.PERSONA_VALUE_PREFIX + ".txt")
        fits = "a" * stem_limit + ".txt"
        too_long = "b" * (stem_limit + 1) + ".txt"
        # 日本語は 3 バイトでも 1 文字と数える（Discord の上限は文字数）
        japanese = "長" * 40 + ".txt"
        for name in (fits, too_long, japanese):
            self.write(name)
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            catalog = self.load()
        self.assertEqual(len(personas.persona_value(fits)), constants.DISCORD_SELECT_VALUE_MAX_LENGTH)
        self.assertEqual([p.value for p in catalog.personas], [personas.persona_value(fits), personas.persona_value(japanese)])
        warnings = "\n".join(LOG_FORMATTER.format(record) for record in captured.records)
        self.assertIn("長すぎる", warnings)
        self.assertIn("<設定値>", warnings)
        self.assertNotIn(too_long, warnings)
        self.assertNotIn(fits, warnings)


class PersonaSettingsTests(PersonaFolderTestCase):
    def test_settings_set_order_names_voices_and_default(self) -> None:
        for name in ("a.txt", "b.txt", "c.txt"):
            self.write(name)
        self.write_settings({
            "default": "b.txt",
            "personas": {
                "c.txt": {"name": "  架空の\nキャラC ", "voice": {"voice": constants.EDGE_TTS_VOICE_MALE, "pitch": "+20Hz"}},
                "b.txt": {"name": "架空のキャラB"},
            },
        })
        catalog = self.load()
        # 設定に書いた順が先、残りはファイル名の順
        self.assertEqual([p.name for p in catalog.personas], ["架空の キャラC", "架空のキャラB", "a"])
        self.assertEqual(catalog.default_value, "../prompts/b.txt")
        voice = catalog.voice_profile("../prompts/c.txt")
        self.assertEqual(voice["voice"], constants.EDGE_TTS_VOICE_MALE)
        self.assertEqual(voice["pitch"], "+20Hz")
        # 書かなかった項目は既定値で補う
        self.assertEqual((voice["rate"], voice["eq"]), (VOICE_PROFILE_FALLBACK["rate"], VOICE_PROFILE_FALLBACK["eq"]))

    def test_invalid_voice_values_fall_back_with_warnings(self) -> None:
        self.write("a.txt")
        self.write_settings({"personas": {"a.txt": {"voice": {"pitch": "+2st", "rate": 5, "eq": "warm", "pich": "+1Hz"}}}})
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            voice = self.load().voice_profile("../prompts/a.txt")
        self.assertEqual(voice["pitch"], VOICE_PROFILE_FALLBACK["pitch"])
        self.assertEqual(voice["rate"], VOICE_PROFILE_FALLBACK["rate"])
        self.assertEqual(voice["eq"], "warm")
        logs = "\n".join(LOG_FORMATTER.format(record) for record in captured.records)
        for word in ("pitch", "rate", "未対応の項目"):
            self.assertIn(word, logs)
        self.assertNotIn("pich", logs)

    def test_live_voice_name_is_checked_before_reaching_the_connection(self) -> None:
        """音声会話（#268）のボイス名は接続の設定にそのまま載るため、英字の名前だけを通す。"""
        self.write("a.txt")
        self.write("b.txt")
        self.write_settings({"personas": {
            "a.txt": {"voice": {"live_voice": "Kore"}},
            "b.txt": {"voice": {"live_voice": "Kore\n{\"tools\": []}"}},
        }})
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            catalog = self.load()
        self.assertEqual(catalog.voice_profile("../prompts/a.txt")["live_voice"], "Kore")
        self.assertEqual(catalog.voice_profile("../prompts/b.txt")["live_voice"], VOICE_PROFILE_FALLBACK["live_voice"])
        logs = "\n".join(LOG_FORMATTER.format(record) for record in captured.records)
        self.assertIn("live_voice", logs)
        self.assertNotIn("tools", logs)  # 書かれた値そのものはログに出さない

    def test_missing_files_and_default_are_warned(self) -> None:
        self.write("a.txt")
        self.write_settings({"default": "転送忘れ.txt", "personas": {"転送忘れ.txt": {"name": "架空"}}})
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            catalog = self.load()
        self.assertEqual(catalog.default_value, "../prompts/a.txt")
        self.assertEqual([p.name for p in catalog.personas], ["a"])
        log_text = "\n".join(LOG_FORMATTER.format(record) for record in captured.records)
        self.assertIn("<設定値>", log_text)
        self.assertIn("default", log_text)
        self.assertNotIn("転送忘れ.txt", log_text)

    def test_broken_or_bom_settings_are_handled_without_rewriting(self) -> None:
        self.write("a.txt")
        broken = '{"personas": {"a.txt": {"name": "架空の秘密の設定"'
        self.settings_path.write_text(broken, encoding="utf-8")
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            catalog = self.load()
        self.assertEqual([p.name for p in catalog.personas], ["a"])
        # ファイルは書き換えず、内容をログに出さない
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), broken)
        self.assertNotIn("架空の秘密の設定", "\n".join(LOG_FORMATTER.format(record) for record in captured.records))

        # メモ帳の「UTF-8 (BOM 付き)」保存でも読める
        self.settings_path.write_bytes(codecs.BOM_UTF8 + json.dumps(
            {"personas": {"a.txt": {"name": "架空のA"}}}, ensure_ascii=False).encode("utf-8"))
        self.assertEqual([p.name for p in self.load().personas], ["架空のA"])

    def test_non_object_settings_are_ignored(self) -> None:
        self.write("a.txt")
        for settings in ([], {"personas": []}, {"personas": {"a.txt": "架空"}}):
            with self.subTest(settings=settings), patch("persona_bot.config.personas.logger"):
                self.write_settings(settings)
                self.assertEqual([p.name for p in self.load().personas], ["a"])

    def test_non_object_sections_warn_with_what_happens_and_without_the_value(self) -> None:
        """各項目が dict でないときの警告（#414 で共通ヘルパーへまとめた文面を保つ）。"""
        self.write("a.txt")
        cases = {
            "personas": ({"personas": ["秘密の値"]}, "personas.json の personas はオブジェクト", "表示名と声の設定を使わずに続行"),
            "persona": ({"personas": {"a.txt": "秘密の値"}}, "「<設定値>」はオブジェクト", "既定の表示名と声を使います"),
            "voice": ({"personas": {"a.txt": {"voice": "秘密の値"}}}, "「<設定値>」の voice はオブジェクト", "既定の声を使います"),
            "agent": ({"personas": {"a.txt": {"agent": "秘密の値"}}}, "「<設定値>」の agent はオブジェクト", "既定の文面を使います"),
        }
        for name, (settings, subject, consequence) in cases.items():
            with self.subTest(section=name):
                self.write_settings(settings)
                with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
                    catalog = self.load()
                text = "\n".join(LOG_FORMATTER.format(record) for record in captured.records)
                self.assertIn(subject, text)
                self.assertIn(consequence, text)
                self.assertNotIn("秘密の値", text)
                self.assertEqual(catalog.voice_profile("../prompts/a.txt")["voice"], VOICE_PROFILE_FALLBACK["voice"])
                self.assertEqual(dict(catalog.agent_lines("../prompts/a.txt")), constants.AGENT_DEFAULT_LINES)

    def test_unsupported_keys_are_warned_once_and_skipped_without_printing_them(self) -> None:
        self.write("a.txt")
        self.write_settings({"personas": {"a.txt": {
            "voice": {"pitch": "+10Hz", "秘密の項目1": "x", "秘密の項目2": "y"},
            "agent": {"done": "架空の完了", "秘密の項目3": "z", "avatar_url": "https://example.invalid/a.png"},
        }}})
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            catalog = self.load()
        messages = [LOG_FORMATTER.format(record) for record in captured.records]
        # 未対応の項目が複数あっても、項目ごとに同じ警告を繰り返さず、使える項目を 1 回だけ案内する
        self.assertEqual(sum("voice に未対応の項目" in message for message in messages), 1)
        self.assertEqual(sum("agent に未対応の項目" in message for message in messages), 1)
        self.assertNotIn("秘密の項目", "\n".join(messages))
        # 使える項目は反映される（avatar_url は文面ではなく、別に点検されて残る）
        self.assertEqual(catalog.voice_profile("../prompts/a.txt")["pitch"], "+10Hz")
        self.assertEqual(catalog.agent_lines("../prompts/a.txt")["done"], "架空の完了")
        self.assertEqual(catalog.agent_avatar_url("../prompts/a.txt"), "https://example.invalid/a.png")


class SettingsTemplateTests(PersonaFolderTestCase):
    """公開している personas.json の見本（prompts/00_prompt_format.json）が、コピーしてそのまま使えること。"""

    def setUp(self) -> None:
        super().setUp()
        # 読むのは Git 管理対象の見本だけ（利用者のキャラ別ファイルと personas.json は読まない）
        self.template_path = PROJECT_ROOT / "prompts" / PERSONA_SETTINGS_TEMPLATE_FILE_NAME
        self.template = json.loads(self.template_path.read_text(encoding="utf-8"))

    def test_copied_template_loads_without_warnings(self) -> None:
        entries = self.template["personas"]
        for name in entries:
            self.write(name)
        shutil.copyfile(self.template_path, self.settings_path)
        with self.assertNoLogs("persona_bot.config.personas", level="WARNING"):
            catalog = self.load()
        # 説明欄（_説明）を残したままでも警告が出ず、書いた内容がそのまま反映される
        self.assertEqual([p.value for p in catalog.personas], [personas.persona_value(name) for name in entries])
        self.assertEqual([p.name for p in catalog.personas], [entry["name"] for entry in entries.values()])
        self.assertEqual(catalog.default_value, personas.persona_value(self.template["default"]))
        for name, entry in entries.items():
            with self.subTest(persona=name):
                self.assertEqual(dict(catalog.voice_profile(personas.persona_value(name))), entry["voice"])
                self.assertIn(entry["voice"]["voice"], (constants.EDGE_TTS_VOICE_FEMALE, constants.EDGE_TTS_VOICE_MALE))
                self.assertIn(entry["voice"]["eq"], constants.VOICE_EQ_PROFILES)
                # サブエージェントの進捗の定型文（#170）も、書いた項目だけが反映される
                lines = catalog.agent_lines(personas.persona_value(name))
                self.assertEqual(set(lines), set(constants.AGENT_LINE_KEYS))
                for key, line in entry.get("agent", {}).items():
                    self.assertEqual(lines[key], line)

    def test_template_itself_is_not_read_as_settings(self) -> None:
        for name in self.template["personas"]:
            self.write(name)
        shutil.copyfile(self.template_path, self.prompts / PERSONA_SETTINGS_TEMPLATE_FILE_NAME)
        catalog = self.load()
        # personas.json を作るまでは見本の表示名・既定を使わず、ファイル名の順・名前のままにする
        names = sorted(self.template["personas"])
        self.assertEqual([p.value for p in catalog.personas], [personas.persona_value(name) for name in names])
        self.assertEqual([p.name for p in catalog.personas], [Path(name).stem for name in names])


class AgentLinesTests(PersonaFolderTestCase):
    """サブエージェントの進捗の定型文（personas.json の "agent"。#170）。"""

    def test_written_lines_override_defaults_and_others_stay(self) -> None:
        self.write("a.txt")
        self.write_settings({"personas": {"a.txt": {"agent": {"progress": " {step}中だよ（{current}/{total}） "}}}})
        lines = self.load().agent_lines("../prompts/a.txt")
        self.assertEqual(lines["progress"], "{step}中だよ（{current}/{total}）")
        self.assertEqual(lines["start"], constants.AGENT_DEFAULT_LINES["start"])

    def test_unknown_or_unregistered_persona_uses_defaults(self) -> None:
        catalog = self.load()
        for value in (None, "", "../prompts/無い.txt"):
            with self.subTest(value=value):
                self.assertEqual(dict(catalog.agent_lines(value)), constants.AGENT_DEFAULT_LINES)

    def test_invalid_lines_are_warned_and_replaced_by_defaults(self) -> None:
        self.write("a.txt")
        self.write_settings({"personas": {"a.txt": {"agent": {
            "start": "{rol}に任せたよ", "progress": "閉じ忘れ{", "done": 3, "failed": "  ", "未知": "架空",
            "busy": "{{かっこ}}も書ける",
        }}}})
        with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
            lines = self.load().agent_lines("../prompts/a.txt")
        for key in ("start", "progress", "done", "failed"):
            with self.subTest(key=key):
                self.assertEqual(lines[key], constants.AGENT_DEFAULT_LINES[key])
        self.assertEqual(lines["busy"], "{{かっこ}}も書ける")
        self.assertEqual(len(captured.records), 5)
        # 警告に設定の中身（利用者の書いた文面）を出さない
        self.assertNotIn("閉じ忘れ", "\n".join(LOG_FORMATTER.format(record) for record in captured.records))

    def test_non_object_agent_is_ignored(self) -> None:
        self.write("a.txt")
        self.write_settings({"personas": {"a.txt": {"agent": "架空"}}})
        with patch("persona_bot.config.personas.logger"):
            self.assertEqual(dict(self.load().agent_lines("../prompts/a.txt")), constants.AGENT_DEFAULT_LINES)
            self.assertIsNone(self.load().agent_avatar_url("../prompts/a.txt"))

    def test_format_specs_and_conversions_fall_back_without_formatting(self) -> None:
        self.write("a.txt")
        for text in ("{current:100d}", "{current:999999999d}", "{role!r}", "{step!s}",
                     "{role!a}", "{current:{total}d}", "{role.__class__}", "{role[0]}"):
            with self.subTest(text=text):
                self.write_settings({"personas": {"a.txt": {"agent": {"progress": text}}}})
                with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
                    lines = self.load().agent_lines("../prompts/a.txt")
                self.assertEqual(lines["progress"], constants.AGENT_DEFAULT_LINES["progress"])
                self.assertEqual(len(captured.records), 1)
                self.assertNotIn(text, LOG_FORMATTER.format(captured.records[-1]))

    def test_plain_fields_and_escaped_braces_remain_valid(self) -> None:
        self.write("a.txt")
        text = "{{調査}} {role}: {step}（{current}/{total}）"
        self.write_settings({"personas": {"a.txt": {"agent": {"progress": text}}}})
        with self.assertNoLogs("persona_bot.config.personas", level="WARNING"):
            lines = self.load().agent_lines("../prompts/a.txt")
        self.assertEqual(lines["progress"].format(role="担当", step="検索", current=1, total=2),
                         "{調査} 担当: 検索（1/2）")

    def test_avatar_url_is_read_apart_from_lines(self) -> None:
        """作業スレッドで子が使うアイコン（#267）。文面としては扱わない。"""
        self.write("a.txt")
        url = "https://example.invalid/agent.png"
        self.write_settings({"personas": {"a.txt": {"agent": {"avatar_url": f" {url} ", "name": "見習い{role}"}}}})
        with self.assertNoLogs("persona_bot.config.personas", level="WARNING"):
            catalog = self.load()
        self.assertEqual(catalog.agent_avatar_url("../prompts/a.txt"), url)
        lines = catalog.agent_lines("../prompts/a.txt")
        self.assertNotIn("avatar_url", lines)
        self.assertEqual(lines["name"], "見習い{role}")
        # 書いていない・一覧に無いペルソナは既定のアイコン
        for value in (None, "", "../prompts/無い.txt"):
            with self.subTest(value=value):
                self.assertIsNone(catalog.agent_avatar_url(value))

    def test_invalid_avatar_url_is_warned_without_its_value(self) -> None:
        self.write("a.txt")
        for value in ("http://example.invalid/秘密.png", "https://example.invalid/秘密 .png", 3, "",
                      "https://example.invalid/秘密" + "a" * constants.AGENT_AVATAR_URL_MAX_CHARS):
            with self.subTest(value=value):
                self.write_settings({"personas": {"a.txt": {"agent": {"avatar_url": value}}}})
                with self.assertLogs("persona_bot.config.personas", level="WARNING") as captured:
                    catalog = self.load()
                self.assertIsNone(catalog.agent_avatar_url("../prompts/a.txt"))
                self.assertEqual(len(captured.records), 1)
                self.assertNotIn("秘密", "\n".join(LOG_FORMATTER.format(record) for record in captured.records))


class SelectablePersonaTests(PersonaFolderTestCase):
    def test_only_existing_files_in_prompts_can_be_confirmed(self) -> None:
        self.write("架空.txt")
        with patch.object(personas, "resolve_prompt_path", side_effect=lambda value: self.prompts / Path(value).name):
            self.assertTrue(personas.is_selectable_persona(personas.FOLLOW_DEFAULT_PERSONA))
            self.assertTrue(personas.is_selectable_persona("../prompts/架空.txt"))
            self.assertFalse(personas.is_selectable_persona("../prompts/無い.txt"))
            for value in ("../.env", "../prompts/../.env", f"../prompts/{PROMPT_TEMPLATE_FILE_NAME}", "架空.txt"):
                with self.subTest(value=value):
                    self.assertFalse(personas.is_selectable_persona(value))


class PersonaLoadingTests(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_prompt_is_used_without_reading_files(self) -> None:
        with patch("persona_bot.config.paths.load_prompt_template") as loader:
            self.assertEqual(await load_persona_template(personas.FOLLOW_DEFAULT_PERSONA), BUILTIN_PERSONA_PROMPT)
        loader.assert_not_called()

    async def test_refresh_reads_in_worker_and_keeps_previous_on_error(self) -> None:
        previous = install_persona_catalog(self, "../prompts/前回.txt")
        threads = []

        def failing():
            threads.append(threading.get_ident())
            raise PermissionError("架空の権限エラー")

        with patch.object(personas, "load_persona_catalog", side_effect=failing), patch("persona_bot.config.personas.logger"):
            self.assertIs(await personas.refresh_persona_catalog(), previous)
        self.assertIs(personas.current_persona_catalog(), previous)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_refresh_installs_the_new_catalog_quietly_while_reload_announces(self) -> None:
        """パネルを開くたびの再読込は件数を記録しない。起動時の再読込は記録する（#414）。"""
        install_persona_catalog(self, "../prompts/前回.txt")
        fresh = personas.PersonaCatalog((personas.Persona("../prompts/新.txt", "新", VOICE_PROFILE_FALLBACK),))
        with patch.object(personas, "load_persona_catalog", return_value=fresh), \
                patch("persona_bot.config.personas.logger") as logger:
            self.assertIs(await personas.refresh_persona_catalog(), fresh)
            logger.info.assert_not_called()
            self.assertIs(personas.current_persona_catalog(), fresh)
            self.assertIs(personas.reload_persona_catalog(), fresh)
            logger.info.assert_called_once()
        self.assertIs(personas.current_persona_catalog(), fresh)


class SettingsPanelTests(unittest.IsolatedAsyncioTestCase):
    def make_view(self, current: str = personas.FOLLOW_DEFAULT_PERSONA) -> SettingsView:
        view = SettingsView(GuildSettings(current_prompt_file=current), 0, "なし")
        self.addCleanup(view.stop)
        return view

    async def test_no_persona_hides_select_and_explains_how_to_create(self) -> None:
        install_empty_persona_catalog(self)
        view = self.make_view()
        self.assertFalse(any(isinstance(item, PersonaSelect) for item in view.children))
        preview = view.build_preview()
        self.assertIn(personas.BUILTIN_PERSONA_DISPLAY_NAME, preview)
        self.assertIn(PROMPT_TEMPLATE_FILE_NAME, preview)
        self.assertIn(PERSONA_SETTINGS_TEMPLATE_FILE_NAME, preview)

    async def test_select_is_capped_at_discord_limit(self) -> None:
        values = [f"../prompts/架空{index:02}.txt" for index in range(DISCORD_SELECT_MAX_OPTIONS + 5)]
        install_persona_catalog(self, *values)
        view = self.make_view(values[0])
        select = next(item for item in view.children if isinstance(item, PersonaSelect))
        self.assertEqual(len(select.options), DISCORD_SELECT_MAX_OPTIONS)
        self.assertTrue(select.options[0].default)
        self.assertIn(f"{DISCORD_SELECT_MAX_OPTIONS}件", view.build_preview())

    async def test_status_shows_default_and_missing_file(self) -> None:
        install_persona_catalog(self, "../prompts/架空の既定.txt", default="../prompts/架空の既定.txt")
        self.assertIn("既定（架空の既定）", self.make_view().build_preview())
        preview = self.make_view("../prompts/消えたキャラ.txt").build_preview()
        self.assertIn("消えたキャラ", preview)
        self.assertIn("見つからない", preview)

    async def test_unchanged_missing_persona_does_not_block_other_settings(self) -> None:
        install_persona_catalog(self, "../prompts/架空の既定.txt")
        view = self.make_view("../prompts/消えたキャラ.txt")
        view.guild_settings.persist = AsyncMock()
        view.temp_model = "fictional-model"
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock(), is_done=Mock(return_value=True)),
            edit_original_response=AsyncMock(), extras={},
        )
        button = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "確定")
        with patch.object(personas, "resolve_prompt_path") as resolve:
            await button.callback(interaction)
        resolve.assert_not_called()
        # 見つからないペルソナは保存値のまま残し、モデルだけを変える
        self.assertEqual(view.guild_settings.current_prompt_file, "../prompts/消えたキャラ.txt")
        self.assertEqual(view.guild_settings.current_model_name, "fictional-model")
        view.guild_settings.persist.assert_awaited_once()

    async def test_opening_settings_reloads_folder_after_deferring(self) -> None:
        install_empty_persona_catalog(self)
        with tempfile.TemporaryDirectory() as directory:
            prompts = Path(directory)
            (prompts / "新しいキャラ.txt").write_text("架空のペルソナです。", encoding="utf-8")
            settings = GuildSettings()
            hub = functions_view.FunctionsView(
                settings, Mock(), Mock(),
                SimpleNamespace(is_blocked=Mock(return_value=False), get_all_blocked_users=Mock(return_value=[])),
                Mock(), Mock(), Mock(), Mock(),
            )
            self.addCleanup(hub.stop)
            order = []

            async def defer(**kwargs):
                order.append("defer")
                response.is_done.return_value = True

            async def edit(**kwargs):
                order.append(("edit", kwargs["view"]))

            response = SimpleNamespace(is_done=Mock(return_value=False), defer=AsyncMock(side_effect=defer))
            interaction = SimpleNamespace(
                # 管理者でない利用者（Member でないため 🎙️ 配信先 は出ない）
                user=SimpleNamespace(id=11),
                response=response, edit_original_response=AsyncMock(side_effect=edit),
                original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())), extras={},
            )
            original_load = personas.load_persona_catalog

            def load():
                order.append("load")
                return original_load(prompts, prompts / "personas.json")

            button = next(item for item in hub.children if isinstance(item, functions_view.OpenSettingsButton))
            with patch.object(personas, "load_persona_catalog", side_effect=load):
                await button.callback(interaction)
        self.assertEqual(order[:2], ["defer", "load"])
        panel = order[2][1]
        self.addCleanup(panel.stop)
        select = next(item for item in panel.children if isinstance(item, PersonaSelect))
        self.assertEqual([option.label for option in select.options], ["新しいキャラ"])


@unittest.skipUnless(shutil.which("git") and (PROJECT_ROOT / ".git").exists(), "Git の作業コピーでのみ確認する")
class PublishedFilesTests(unittest.TestCase):
    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, encoding="utf-8")

    def test_only_templates_are_tracked_and_new_personas_are_ignored(self) -> None:
        templates = sorted(f"prompts/{name}" for name in (PROMPT_TEMPLATE_FILE_NAME, PERSONA_SETTINGS_TEMPLATE_FILE_NAME))
        tracked = sorted(self.git("ls-files", "prompts").stdout.split())
        self.assertEqual(tracked, templates)
        for path in ("prompts/架空のキャラ.txt", "prompts/prompt_fictional.txt", "prompts/personas.json"):
            with self.subTest(path=path):
                self.assertEqual(self.git("check-ignore", "-q", "--no-index", path).returncode, 0)
        for path in templates:
            with self.subTest(template=path):
                self.assertNotEqual(self.git("check-ignore", "-q", "--no-index", path).returncode, 0)


if __name__ == "__main__":
    unittest.main()
