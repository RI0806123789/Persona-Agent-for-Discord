"""`GuildSettings` / `BotState` が保存・復元に使う、データベースの入口の型（#480）。

実体は `Database`（`db_guild.py` の `GuildStateMixin`）。`app_state.py` はここに書いたメソッドだけを
呼ぶので、依存する範囲が型で分かり、`Any` のままでは見えなかった契約の食い違い（メソッド名・引数の変更）を
静的な型の確認や読み手が気づけるようにする。テストの代役（`Mock` や `SimpleNamespace`）も、
同じメソッドを持てばそのまま渡せる（実行時には型を検査しない）。

★ `app_state.py` が DB のメソッドを新しく呼ぶようになったら、ここにも足すこと。
  `tests/core/test_guild_state_store.py` が、`Database` の同名メソッドの引数名と食い違っていないことを確かめる。
"""

from __future__ import annotations

from typing import Any, Protocol


class GuildStateStore(Protocol):
    """サーバーごとの設定と、記念日の投稿記録の保存先。"""

    def guild_state_load_all(self) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, str]]]:
        """起動時に、全サーバー（と DM）の設定と記念日の投稿記録を読み出す。"""
        ...

    def guild_state_load(self, guild_id: int) -> tuple[dict[str, Any], dict[str, str]]:
        """再参加時などに、1サーバー分の設定と記念日の投稿記録を読み出す。"""
        ...

    def guild_settings_save(self, guild_id: int, **values: Any) -> None:
        """1サーバー分の設定を丸ごと保存する（`GUILD_SETTINGS_FIELDS` の全項目を渡す）。"""
        ...

    def anniversary_post_record(self, guild_id: int, user_id: str, label: str, posted_date: str) -> None:
        """記念日のお祝いを投稿したことを記録する。"""
        ...
