"""ブラックリスト管理サービス。

data/ng/blacklist.json を使って、不適切な発言を繰り返すユーザーを
三審制（3回違反でブロック）で管理する。
ユーザーの識別にはDiscordの固有ID（スノーフレークID）を使用する。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any

from persona_bot.config.constants import BLACKLIST_PATH, BLACKLIST_MAX_VIOLATIONS
from persona_bot.core.background import run_in_thread
from persona_bot.utils.normalization import normalize_bool_flag
from persona_bot.utils.storage import load_json_file, write_json_file
from persona_bot.utils.timeutil import utc_now_iso


class BlacklistService:
    """ユーザーの違反回数を記録し、閾値に達したらブロック状態にするサービス。"""

    def __init__(self, path: Path = BLACKLIST_PATH) -> None:
        self._path = path
        self._data: dict[str, Any] = {}
        self._io_lock = asyncio.Lock()
        self.reload()

    def reload(self) -> None:
        """data/ng/blacklist.json からデータを読み込む。"""
        self._data = load_json_file(self._path, {})

    def _save(self, data: dict[str, Any]) -> None:
        """現在のデータを data/ng/blacklist.json に書き込む。"""
        # 一時ファイルを同じディレクトリに作ってから置き換えるため、data/ng/ が無いと
        # 初回の違反記録で保存に失敗する（クリーンな環境では data/ng/ が無いことがある）。
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_file(self._path, data)

    @staticmethod
    def _record_of(data: dict[str, Any], key: str) -> dict[str, Any] | None:
        """1ユーザー分の記録を返す。手書きなどで dict でない値になっていれば None。

        blacklist.json は手で直すこともあるため、値の型を信用しない。is_blocked は
        on_message の最初に呼ばれるので、ここで例外を出すとそのユーザーの発言が
        すべて処理できなくなる。
        """
        record = data.get(key)
        return record if isinstance(record, dict) else None

    @staticmethod
    def _count_of(record: dict[str, Any]) -> int:
        """違反回数。数値として読めなければ 0 とみなす。"""
        value = record.get("count", 0)
        if isinstance(value, bool):
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _blocked_of(record: dict[str, Any]) -> bool:
        """ブロック状態。手書きの "false" や "0" を誤ってブロック扱いにしない。"""
        return normalize_bool_flag(record.get("blocked", False), accept_numbers=True) is True

    # ------------------------------------------------------------------
    # 参照系
    # ------------------------------------------------------------------

    def is_blocked(self, user_id: int) -> bool:
        """指定ユーザーがブロック中かどうかを返す。"""
        record = self._record_of(self._data, str(user_id))
        return record is not None and self._blocked_of(record)

    def get_violation_count(self, user_id: int) -> int:
        """指定ユーザーの現在の違反回数を返す。"""
        record = self._record_of(self._data, str(user_id))
        return 0 if record is None else self._count_of(record)

    def get_all_blocked_users(self) -> list[dict[str, Any]]:
        """ブロック中の全ユーザー情報をリストで返す。"""
        blocked: list[dict[str, Any]] = []
        for uid, record in self._data.items():
            # 解除パネルへ渡す ID は数値でなければならないため、読めない行は飛ばす
            if (not isinstance(record, dict) or not self._blocked_of(record)
                    or not isinstance(uid, str) or not uid.isascii() or not uid.isdigit()):
                continue
            try:
                user_id = int(uid)
            except ValueError:
                continue
            blocked.append({
                **record, "user_id": user_id,
                "count": max(BLACKLIST_MAX_VIOLATIONS, self._count_of(record)),
            })
        return blocked

    # ------------------------------------------------------------------
    # 更新系
    # ------------------------------------------------------------------

    async def record_violation(self, user_id: int, user_name: str) -> tuple[int, bool]:
        async with self._io_lock:
            return await run_in_thread(self._record_violation, user_id, user_name)

    def _record_violation(self, user_id: int, user_name: str) -> tuple[int, bool]:
        """違反を1回記録し、(新しい違反回数, ブロックされたか) を返す。

        3回目の違反でブロック状態に移行する。
        """
        key = str(user_id)
        data = deepcopy(self._data)
        # 記録が無い・壊れている場合は新しく数え始める（壊れた値は保存時に置き換わる）
        record = self._record_of(data, key) or {"count": 0, "blocked": False}

        # 既にブロック済みの場合はカウントを増やさずに現在の状態を返す
        if self._blocked_of(record):
            return max(BLACKLIST_MAX_VIOLATIONS, self._count_of(record)), False

        record["count"] = self._count_of(record) + 1
        record["user_name"] = user_name
        record["blocked"] = False

        just_blocked = False
        if record["count"] >= BLACKLIST_MAX_VIOLATIONS:
            record["blocked"] = True
            record["blocked_at"] = utc_now_iso()
            just_blocked = True

        data[key] = record
        self._save(data)
        self._data = data

        return record["count"], just_blocked

    async def unblock(self, user_id: int) -> bool:
        async with self._io_lock:
            return await run_in_thread(self._unblock, user_id)

    def _unblock(self, user_id: int) -> bool:
        """指定ユーザーのブロックを解除し、違反カウントをリセットする。

        Returns:
            True: ブロックが解除された。
            False: そのユーザーはブロックされていなかった（違反記録は保持する）。
        """
        key = str(user_id)
        if not self.is_blocked(user_id):
            return False

        data = deepcopy(self._data)
        del data[key]
        self._save(data)
        self._data = data
        return True
