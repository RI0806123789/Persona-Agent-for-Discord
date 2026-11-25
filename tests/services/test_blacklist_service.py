"""ブラックリストの初回保存と、手書きなどで崩れた記録への耐性を確かめる（#68）。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services.blacklist_service import BlacklistService


class BlacklistFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        quiet = patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def saved(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    async def test_first_violation_creates_the_missing_directory(self) -> None:
        path = self.root / "data" / "ng" / "blacklist.json"  # data/ng/ がまだ無いクリーンな環境
        service = BlacklistService(path)
        self.assertEqual(await service.record_violation(11, "架空の利用者"), (1, False))
        self.assertEqual(self.saved(path)["11"]["count"], 1)

    async def test_unblock_only_succeeds_for_a_blocked_record(self) -> None:
        path = self.root / "blacklist.json"
        service = BlacklistService(path)
        await service.record_violation(11, "架空の警告中")
        for _ in range(3):
            await service.record_violation(22, "架空のブロック中")
        original = path.read_bytes()
        for user_id in (11, 33):
            self.assertFalse(await service.unblock(user_id))
            self.assertEqual(path.read_bytes(), original)
        with patch.object(service, "_save", side_effect=OSError("架空の保存失敗")):
            with self.assertRaises(OSError):
                await service.unblock(22)
        self.assertTrue(service.is_blocked(22))
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(await service.unblock(22))
        self.assertFalse(await service.unblock(22))
        self.assertFalse(service.is_blocked(22))
        self.assertEqual(service.get_violation_count(22), 0)
        self.assertEqual(self.saved(path)["11"]["count"], 1)

    async def test_blocked_records_with_incomplete_counts_never_return_warning_counts(self) -> None:
        path = self.root / "blacklist.json"
        path.write_text(json.dumps({"11": {"blocked": True}}), encoding="utf-8")
        service = BlacklistService(path)
        for count in (None, 0, 1, True, float("inf"), "不正な数値"):
            with self.subTest(count=count):
                service._data["11"]["count"] = count
                self.assertEqual(await service.record_violation(11, "架空の利用者"), (3, False))

    async def test_invalid_counts_ids_and_embedded_user_id_do_not_break_unblock_list(self) -> None:
        service = BlacklistService(self.root / "blacklist.json")
        service._data = {
            "11": {"blocked": True, "user_id": 99, "count": True},
            "１２": {"blocked": True}, "²": {"blocked": True},
            "13": {"blocked": True, "count": float("inf")},
        }
        blocked = service.get_all_blocked_users()
        self.assertEqual([record["user_id"] for record in blocked], [11, 13])
        self.assertEqual([record["count"] for record in blocked], [3, 3])
        self.assertEqual(service.get_violation_count(11), 0)
        self.assertEqual(service.get_violation_count(13), 0)

    async def test_malformed_records_do_not_break_checks_or_updates(self) -> None:
        path = self.root / "blacklist.json"
        path.write_text(json.dumps({
            "11": "架空の壊れた値",
            "12": {"count": "架空の非数値", "blocked": False},
            "架空の非数値ID": {"count": 3, "blocked": True},
            "13": {"count": 3, "blocked": True, "user_name": "架空のブロック中"},
        }, ensure_ascii=False), encoding="utf-8")
        service = BlacklistService(path)
        self.assertFalse(service.is_blocked(11))
        self.assertEqual(service.get_violation_count(11), 0)
        self.assertEqual(service.get_violation_count(12), 0)
        self.assertTrue(service.is_blocked(13))
        self.assertEqual([user["user_id"] for user in service.get_all_blocked_users()], [13])
        # 壊れた記録は新しく数え直し、保存時に正しい形へ置き換わる
        self.assertEqual(await service.record_violation(11, "架空の利用者"), (1, False))
        self.assertEqual(await service.record_violation(12, "架空の利用者"), (1, False))
        self.assertEqual(await service.record_violation(13, "架空の利用者"), (3, False))
        saved = self.saved(path)
        self.assertEqual((saved["11"]["count"], saved["12"]["count"]), (1, 1))

    async def test_string_false_or_zero_blocked_flag_is_not_treated_as_blocked(self) -> None:
        """手書きの "false" や "0" をブロック中と誤認せず、違反カウントを進められる（#509）。"""
        path = self.root / "blacklist.json"
        path.write_text(json.dumps({
            "11": {"count": 2, "blocked": "false"},
            "12": {"count": 1, "blocked": "0"},
            "13": {"count": 3, "blocked": "true"},
        }), encoding="utf-8")
        service = BlacklistService(path)
        self.assertFalse(service.is_blocked(11))
        self.assertFalse(service.is_blocked(12))
        self.assertTrue(service.is_blocked(13))
        self.assertEqual([user["user_id"] for user in service.get_all_blocked_users()], [13])
        self.assertEqual(await service.record_violation(11, "架空の利用者"), (3, True))
        self.assertTrue(service.is_blocked(11))


if __name__ == "__main__":
    unittest.main()
