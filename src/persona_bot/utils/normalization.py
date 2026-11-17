"""モデルの分類値と保存値を共通の許容値へ正規化する。"""

import re
from typing import Any

from persona_bot.config.constants import MEMORY_ROUTE_CATEGORIES, REMINDER_RECURRENCE_VALUES


def normalize_extracted_text(value: Any, max_chars: int) -> str:
    """AIの抽出文を1行・長さ上限内に収め、メンションとして解釈させない。"""
    if not isinstance(value, str):
        return ""
    # 改行で後続のプロンプトの節を作らせず、通知先の文字列も命令として持ち越さない。
    text = re.sub(r"\s+", " ", value).strip().replace("@", "＠")
    return text[:max_chars].rstrip()


def normalize_memory_category(value: Any) -> str:
    """記憶カテゴリの空白・大小文字を吸収し、未知の値はnoneにする。"""
    category = str(value or "none").strip().lower()
    return category if category in MEMORY_ROUTE_CATEGORIES else "none"


def normalize_recurrence(value: Any) -> str:
    """繰り返し種別を検証し、未知の値・欠落はnone（単発）にする。"""
    recurrence = str(value or "none").strip().lower()
    return recurrence if recurrence in REMINDER_RECURRENCE_VALUES else "none"


_TRUE_WORDS = frozenset({"true", "yes", "1"})
_FALSE_WORDS = frozenset({"false", "no", "0"})


def normalize_bool_flag(
    value: Any, *, true_words: frozenset[str] = frozenset(), false_words: frozenset[str] = frozenset(),
    accept_numbers: bool = False,
) -> bool | None:
    """モデルが返した真偽値の項目を True / False にする。判定できない値は None。

    JSON 上は bool が期待されるが、"true" / "no" / 0 のように返ることがある。
    "true"・"yes"・"1" と "false"・"no"・"0"（前後の空白・大小文字は無視）は常に認め、
    項目ごとの同義語（"safe" / "unsafe" など）は true_words / false_words で足す。
    accept_numbers が真なら、数値は 0 以外を True とする（bool は数値として扱わない）。
    不明な値をどちらへ倒すかは呼び出し側が決める（安全側・判定なし など）。
    """
    if isinstance(value, bool):
        return value
    if accept_numbers and isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_WORDS or normalized in true_words:
            return True
        if normalized in _FALSE_WORDS or normalized in false_words:
            return False
    return None


def fold_kana(text: str) -> str:
    """大小文字を畳み、カタカナを対応するひらがなへ統一する（NGワード判定・割り込み判定で共有）。

    全半角の統一（NFKC）は呼び出し側が先に行う。ヷ〜ヺには合成済みのひらがなが無いので、
    基底文字と結合濁点に分ける。
    """
    folded: list[str] = []
    for character in text.casefold():
        codepoint = ord(character)
        if 0x30A1 <= codepoint <= 0x30F6 or codepoint in {0x30FD, 0x30FE}:
            folded.append(chr(codepoint - 0x60))
        elif 0x30F7 <= codepoint <= 0x30FA:
            folded.append("わゐゑを"[codepoint - 0x30F7] + "\u3099")
        else:
            folded.append(character)
    return "".join(folded)
