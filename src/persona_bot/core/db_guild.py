"""サーバーごとの設定（guild_settings）と記念日のお祝い投稿記録（anniversary_posts）の読み書き（`Database` のミックスイン。#468）。

`database.py` が肥大化したため、テーブル群ごとに切り出した。接続（`self._conn`）とロック（`self._lock`）は
`Database` が持ち、ここでは SQL と行の変換だけを担う。外から見たメソッド名・引数・挙動は分割前と同じ。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from persona_bot.core.guild_settings_fields import GUILD_SETTINGS_FIELDS
from persona_bot.core.db_base import DbMixinBase


class GuildDbMixin(DbMixinBase):
    """サーバーごとの設定（guild_settings）と記念日のお祝い投稿記録（anniversary_posts）。"""

    def _load_all_guild_settings_unlocked(self) -> dict[int, dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM guild_settings").fetchall()
        return self._settings_by_guild(rows)

    def _load_all_anniversary_posts_unlocked(self) -> dict[int, dict[str, str]]:
        rows = self._conn.execute("SELECT * FROM anniversary_posts").fetchall()
        return self._posts_by_guild(rows)

    def guild_settings_load_all(self) -> dict[int, dict[str, Any]]:
        """保存済みのギルド設定を {guild_id: {列名: 値}} 形式で全件返す（起動時の復元用）。

        DM の設定は guild_id=0 の行として保存されている（DM にはギルドIDが無いため）。
        """
        with self._lock:
            return self._load_all_guild_settings_unlocked()

    @staticmethod
    def _guild_settings_values(row: sqlite3.Row) -> dict[str, Any]:
        """全件復元と再参加時の復元で、保存列の変換を揃える。"""
        return {
            name: bool(row[name]) if kind == "bool" else row[name]
            for name, _, kind, _ in GUILD_SETTINGS_FIELDS
        }

    @classmethod
    def _settings_by_guild(cls, rows: list[sqlite3.Row]) -> dict[int, dict[str, Any]]:
        """guild_settings の全行を {guild_id: {列名: 値}} に変換する（全件読込の共通処理）。"""
        return {row["guild_id"]: cls._guild_settings_values(row) for row in rows}

    @staticmethod
    def _post_key(row: sqlite3.Row) -> str:
        """anniversary_posts の1行を GuildSettings.anniversary_posted_dates のキー（"ユーザーID:呼び名"）にする。"""
        return f"{row['user_id']}:{row['label']}"

    @classmethod
    def _posts_by_guild(cls, rows: list[sqlite3.Row]) -> dict[int, dict[str, str]]:
        """anniversary_posts の全行を {guild_id: {"ユーザーID:呼び名": 投稿日}} に変換する（全件読込の共通処理）。"""
        posted: dict[int, dict[str, str]] = {}
        for row in rows:
            posted.setdefault(row["guild_id"], {})[cls._post_key(row)] = row["posted_date"]
        return posted

    def guild_state_load_all(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, str]]]:
        """起動時の設定と投稿記録を同じロック内で読み出す。"""
        with self._lock:
            return self._load_all_guild_settings_unlocked(), self._load_all_anniversary_posts_unlocked()

    def guild_state_load(self, guild_id: int) -> tuple[dict[str, Any], dict[str, str]]:
        """1サーバー分の設定と記念日の投稿記録を、同じロック内で読み出す。

        再参加時に全件を走査せず、そのサーバーの主キーで取得する。
        どちらかの読込に失敗した場合は例外を伝え、未復元の設定を使わせない。
        """
        guild_key = int(guild_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_key,)
            ).fetchone()
            posts = self._conn.execute(
                "SELECT user_id, label, posted_date FROM anniversary_posts WHERE guild_id = ?",
                (guild_key,),
            ).fetchall()
        values = self._guild_settings_values(row) if row is not None else {}
        posted = {self._post_key(post): post["posted_date"] for post in posts}
        return values, posted

    def guild_settings_save(self, guild_id: int, **values: Any) -> None:
        """共通定義とスキーマが一致するときだけ設定を丸ごと保存する。"""
        names = [name for name, _, _, _ in GUILD_SETTINGS_FIELDS]
        if set(values) != set(names):
            raise ValueError("guild_settings の保存値が定義と一致しません。")
        converted = []
        for name, _, kind, _ in GUILD_SETTINGS_FIELDS:
            value = values[name]
            if kind == "text":
                value = str(value)
            elif kind == "bool":
                value = int(bool(value))
            elif kind == "channel":
                value = int(value or 0)
            else:
                value = int(value)
            converted.append(value)
        columns = ", ".join(["guild_id", *names])
        placeholders = ", ".join("?" for _ in range(len(names) + 1))
        with self._lock, self._conn:
            # 外部接続のスキーマ変更が検査と置換の間へ割り込まないようにする。
            self._conn.execute("BEGIN IMMEDIATE")
            self._validate_guild_settings_schema()
            self._conn.execute(
                f"INSERT OR REPLACE INTO guild_settings ({columns}) VALUES ({placeholders})",
                (int(guild_id), *converted),
            )

    def _validate_guild_settings_schema(self, *, force: bool = False) -> None:
        """初期化時とスキーマ変更時だけ列を照合する。呼出元でロックを保持する。"""
        schema_version = int(self._conn.execute("PRAGMA schema_version").fetchone()[0])
        if not force and schema_version == self._guild_settings_schema_version:
            return
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(guild_settings)")}
        expected = {"guild_id", *(name for name, _, _, _ in GUILD_SETTINGS_FIELDS)}
        if columns != expected:
            raise ValueError("guild_settings の保存列と DB の列が一致しません。")
        self._guild_settings_schema_version = schema_version

    def anniversary_posts_load_all(self) -> dict[int, dict[str, str]]:
        """記念日の投稿記録を {guild_id: {"ユーザーID:呼び名": "YYYY-MM-DD"}} 形式で返す。

        内側の辞書は GuildSettings.anniversary_posted_dates と同じ形にしてあるため、
        起動時にそのまま代入して復元できる。
        """
        with self._lock:
            return self._load_all_anniversary_posts_unlocked()

    def anniversary_post_record(self, guild_id: int, user_id: str, label: str, posted_date: str) -> None:
        """お祝いを投稿したことを記録する（同じサーバー・同じ記念日の行は上書き）。

        主キーが (guild_id, user_id, label) なので、毎年お祝いしても行は増えない
        （その記念日の最終投稿日だけが残る）。
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO anniversary_posts (guild_id, user_id, label, posted_date)"
                " VALUES (?, ?, ?, ?)",
                (int(guild_id), str(user_id), str(label), str(posted_date)),
            )
