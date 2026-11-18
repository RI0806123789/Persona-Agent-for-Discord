"""記憶圧縮の結果へ記念日を統合する純粋関数（#469）。

DB・ファイル・Gemini に触れない。`memory_service` が再公開するため、従来の import 先も変わらない。
"""

import copy
from typing import Any

from persona_bot.config.constants import ANNIVERSARY_PROFILE_KEY
from persona_bot.utils.anniversaries import parse_anniversary


def anniversaries_by_label(profile: dict[str, Any]) -> dict[str, Any]:
    """生成待機中の更新を比較するため、有効な記念日をラベルで束ねる。"""
    entries = profile.get(ANNIVERSARY_PROFILE_KEY)
    result = {}
    if isinstance(entries, list):
        for entry in entries:
            anniversary = parse_anniversary(entry)
            if anniversary is not None:
                result[anniversary[0]] = entry
    return result


def merge_anniversary_profile(
    snapshot: dict[str, Any], compacted: dict[str, Any], *, previous_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """入力を変更せず既存の記念日を引き継ぎ、生成待機中の編集を優先する。"""
    result = copy.deepcopy(compacted)
    incoming = compacted.get(ANNIVERSARY_PROFILE_KEY)
    if previous_snapshot is not None and isinstance(incoming, list):
        # モデルは入力時の古い日付も出力するため、最新値との単純な統合では訂正が戻る。
        # 未変更ラベルへのモデルの訂正は採用し、同時に編集されたラベルだけを保護する。
        previous = anniversaries_by_label(previous_snapshot)
        current = anniversaries_by_label(snapshot)
        changed_labels = {
            label for label in previous.keys() | current.keys()
            if previous.get(label) != current.get(label)
        }
        if not isinstance(snapshot.get(ANNIVERSARY_PROFILE_KEY), list) and (
            snapshot.get(ANNIVERSARY_PROFILE_KEY) != previous_snapshot.get(ANNIVERSARY_PROFILE_KEY)
            or (ANNIVERSARY_PROFILE_KEY in snapshot) != (ANNIVERSARY_PROFILE_KEY in previous_snapshot)
        ):
            # キー全体の削除や手書き途中の値も、古い生成結果で元へ戻さない。
            incoming = None
        else:
            incoming = [
                entry for entry in incoming
                if (anniversary := parse_anniversary(entry)) is None or anniversary[0] not in changed_labels
            ]
    if not isinstance(incoming, list):
        if ANNIVERSARY_PROFILE_KEY in snapshot:
            result[ANNIVERSARY_PROFILE_KEY] = copy.deepcopy(snapshot[ANNIVERSARY_PROFILE_KEY])
        else:
            result.pop(ANNIVERSARY_PROFILE_KEY, None)
        return result

    # LLMだけでは削除できなくなるが、省略で失う記念日は戻せないため保持を優先する。
    # 既存の不正な要素も消さず、読取側の共通判定で読み飛ばす。新しい不正な要素は採用しない。
    existing = snapshot.get(ANNIVERSARY_PROFILE_KEY)
    entries: list[Any] = []
    positions: dict[str, int] = {}
    for source, preserve_invalid in ((existing, True), (incoming, False)):
        if not isinstance(source, list):
            continue
        for entry in source:
            anniversary = parse_anniversary(entry)
            if anniversary is None:
                if preserve_invalid:
                    entries.append(copy.deepcopy(entry))
                continue
            label, _, _ = anniversary
            if label in positions:
                entries[positions[label]] = copy.deepcopy(entry)
            else:
                positions[label] = len(entries)
                entries.append(copy.deepcopy(entry))
    result[ANNIVERSARY_PROFILE_KEY] = entries
    return result
