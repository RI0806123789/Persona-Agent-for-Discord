"""Discord のセレクトメニューに並べる項目の絞り込み（#434）。

セレクトの選択肢は 25 件までなので、先頭の `limit` 件だけを並べる。ただし、いま選択中の項目が
範囲外にあるときは、選択が消えて見えなくならないよう、並べる最後の枠をその項目に差し替える。
統計のユーザー選択・配信先チャンネルの選択が同じ規則を使う。
"""

from __future__ import annotations

from typing import Callable, Sequence, TypeVar

from persona_bot.config.constants import DISCORD_SELECT_MAX_OPTIONS


T = TypeVar("T")


def limit_keeping_selected(
    items: Sequence[T],
    is_selected: Callable[[T], bool],
    limit: int | None = None,
) -> list[T]:
    """先頭 `limit` 件を返す。選択中の項目が範囲外にあれば、最後の1件をその項目に差し替える。

    `limit` を省略すると Discord のセレクトの上限（`DISCORD_SELECT_MAX_OPTIONS`）を使う。
    すでに `limit` 件以内に収まっている一覧へ重ねて使っても結果は変わらない（冪等）。
    """
    if limit is None:
        limit = DISCORD_SELECT_MAX_OPTIONS
    visible = list(items[:max(limit, 0)])
    if not visible or any(is_selected(item) for item in visible):
        return visible
    selected = next((item for item in items[len(visible):] if is_selected(item)), None)
    if selected is not None:
        visible[-1] = selected
    return visible
