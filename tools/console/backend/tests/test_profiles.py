"""接続先の設定（profiles.py）と、見本のファイル（profiles.example.json・examples/）の回帰テスト。"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

import app  # noqa: E402
import profiles  # noqa: E402
import remote_commands as rc  # noqa: E402

EXAMPLES_DIR = BACKEND_DIR.parent / "examples"


def _raw_profile(**overrides) -> dict:
    raw = {
        "id": "prod",
        "label": "本番サーバー",
        "environment": "production",
        "host": "bot-server",
        "port": None,
        "username": None,
        "use_ssh_config": True,
        "service": "bot",
        "bot_dir": "/home/someone/Persona-Chat-in-Discord",
    }
    raw.update(overrides)
    return raw


class ExampleProfilesTests(unittest.TestCase):
    def test_falls_back_to_example_when_profiles_json_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(profiles, "PROFILES_PATH", Path(directory) / "profiles.json"):
                loaded, source = profiles.load_profiles()
        self.assertEqual(source, "profiles.example.json")
        # 見本には、そのままつながる接続先を置かない（開発用の模擬サーバーは画面から外した）
        self.assertEqual(list(loaded), ["prod"])
        # 本番は見本の値のままなので、書き換えるまで接続させない
        self.assertIn("host", loaded["prod"].unset_fields)
        self.assertIn("bot_dir", loaded["prod"].unset_fields)
        # デプロイの既定（main を取り込み、ボットの .venv で依存関係を入れる）
        self.assertEqual((loaded["prod"].deploy_branch, loaded["prod"].python), ("main", ".venv/bin/python"))

    def test_unset_fields_are_sent_to_the_screen(self) -> None:
        profile = profiles._parse_profile(_raw_profile(host="YOUR_SSH_HOST"))
        self.assertEqual(profile.public_dict()["unset_fields"], ["host"])

    def test_message_tells_how_to_fix(self) -> None:
        profile = profiles._parse_profile(_raw_profile(host="YOUR_SSH_HOST"))
        message = app._unset_profile_message(profile, "profiles.example.json")
        self.assertIn("profiles.json にコピー", message)
        self.assertIn("host", message)
        message = app._unset_profile_message(profile, "profiles.json")
        self.assertIn("backend/profiles.json の YOUR_", message)


class DeploySettingsTests(unittest.TestCase):
    def test_rejects_unsafe_deploy_settings(self) -> None:
        with self.assertRaises(profiles.ProfileError):
            profiles._parse_profile(_raw_profile(deploy_branch="main; reboot"))
        with self.assertRaises(profiles.ProfileError):
            profiles._parse_profile(_raw_profile(python="../../usr/bin/python"))

    def test_deploy_branch_is_sent_to_the_screen(self) -> None:
        profile = profiles._parse_profile(_raw_profile(deploy_branch="release/1.0"))
        self.assertEqual(profile.public_dict()["deploy_branch"], "release/1.0")


class ProfileValidationTests(unittest.TestCase):
    def test_utf8_profiles_with_and_without_bom_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.dumps({"profiles": [_raw_profile(label="架空の接続先")]}, ensure_ascii=False)
            for encoding in ("utf-8", "utf-8-sig"):
                with self.subTest(encoding=encoding), mock.patch.object(profiles, "PROFILES_PATH", path):
                    path.write_text(payload, encoding=encoding)
                    loaded, source = profiles.load_profiles()
                    self.assertEqual(loaded["prod"].label, "架空の接続先")
                    self.assertEqual(source, "profiles.json")

    def test_invalid_profile_encoding_becomes_safe_actionable_profile_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            payload = json.dumps({"profiles": [_raw_profile(note="非公開の架空設定値")]}, ensure_ascii=False)
            for data in (payload.encode("cp932"), b'{"profiles": [], "note": "\xff"}'):
                with self.subTest(data_length=len(data)), mock.patch.object(profiles, "PROFILES_PATH", path):
                    path.write_bytes(data)
                    with self.assertRaises(profiles.ProfileError) as raised:
                        profiles.load_profiles()
                    self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)
                    message = str(raised.exception)
                    self.assertIn("profiles.json", message)
                    self.assertIn("UTF-8", message)
                    self.assertIn("保存し直し", message)
                    self.assertNotIn(str(path.parent), message)
                    self.assertNotIn("非公開の架空設定値", message)
                    self.assertNotIn(str(raised.exception.__cause__), message)
                    self.assertEqual(path.read_bytes(), data)

    def test_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps({"profiles": [_raw_profile(), _raw_profile()]}), encoding="utf-8")
            with mock.patch.object(profiles, "PROFILES_PATH", path):
                with self.assertRaisesRegex(profiles.ProfileError, "重複"):
                    profiles.load_profiles()

    def test_rejects_invalid_names_paths_and_types(self):
        for override in ({"service": "bot\n"}, {"service": "--help"}, {"service": "bot; id"},
                         {"bot_dir": "relative/bot"}, {"bot_dir": "/srv/bot\x00"},
                         {"port": True}, {"journal_sudo": "false"}, {"use_ssh_config": "false"}):
            with self.subTest(override=override):
                with self.assertRaises(profiles.ProfileError):
                    profiles._parse_profile(_raw_profile(**override))


class UnsetFieldsTests(unittest.TestCase):
    def test_real_values_are_configured(self) -> None:
        self.assertEqual(profiles._parse_profile(_raw_profile()).unset_fields, [])

    def test_detects_placeholder_in_any_connection_field(self) -> None:
        profile = profiles._parse_profile(
            _raw_profile(username="YOUR_SSH_USER", key_path="~/.ssh/YOUR_PRIVATE_KEY", known_hosts="YOUR_KNOWN_HOSTS")
        )
        self.assertEqual(profile.unset_fields, ["username", "key_path", "known_hosts"])

    def test_lowercase_words_are_not_placeholders(self) -> None:
        # 「your」を含むだけの普通の名前は、見本の値として扱わない
        self.assertEqual(profiles._parse_profile(_raw_profile(host="your-server")).unset_fields, [])


class ExampleFilesTests(unittest.TestCase):
    """見本どうしが食い違うと、手順どおりに書き換えてもつながらなくなる。"""

    def test_ssh_config_host_matches_profiles_example(self) -> None:
        example = json.loads((BACKEND_DIR / "profiles.example.json").read_text(encoding="utf-8"))
        prod = next(raw for raw in example["profiles"] if raw["id"] == "prod")
        ssh_config = (EXAMPLES_DIR / "ssh_config.example").read_text(encoding="utf-8")
        self.assertRegex(ssh_config, rf"(?m)^Host {re.escape(prod['host'])}$")

    def test_sudoers_example_allows_only_the_service_actions(self) -> None:
        lines = [
            line
            for line in (EXAMPLES_DIR / "sudoers.example").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(len(lines), 1)
        rule = lines[0]
        self.assertNotIn("NOPASSWD: ALL", rule)
        for action in rc.SERVICE_ACTIONS:
            self.assertIn(f"/usr/bin/systemctl {action} bot", rule)
        # 画面に出す案内（sudo にパスワードが要るとき）と同じ1行であること
        self.assertIn(rule, app._sudo_password_message("YOUR_SSH_USER", "bot"))

    def test_server_examples_use_lf(self) -> None:
        # sudoers は CRLF だと visudo が構文エラーにする（.gitattributes で LF に固定している）
        for path in EXAMPLES_DIR.glob("*.example"):
            self.assertNotIn(b"\r\n", path.read_bytes(), path.name)


if __name__ == "__main__":
    unittest.main()
