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


# ローマ字→ひらがな変換表（ヘボン式・訓令式・一般的なキーボード入力に対応。最長一致で走査する）。
_ROMAJI_TABLE: dict[str, str] = {
    "kya": "きゃ", "kyi": "きぃ", "kyu": "きゅ", "kye": "きぇ", "kyo": "きょ",
    "sha": "しゃ", "shi": "し", "shu": "しゅ", "she": "しぇ", "sho": "しょ",
    "sya": "しゃ", "syi": "しぃ", "syu": "しゅ", "sye": "しぇ", "syo": "しょ",
    "cha": "ちゃ", "chi": "ち", "chu": "ちゅ", "che": "ちぇ", "cho": "ちょ",
    "tya": "ちゃ", "tyi": "ちぃ", "tyu": "ちゅ", "tye": "ちぇ", "tyo": "ちょ",
    "cya": "ちゃ", "cyi": "ちぃ", "cyu": "ちゅ", "cye": "ちぇ", "cyo": "ちょ",
    "tsu": "つ", "tsa": "つぁ", "tsi": "つぃ", "tse": "つぇ", "tso": "つぉ",
    "nya": "にゃ", "nyi": "にぃ", "nyu": "にゅ", "nye": "にぇ", "nyo": "にょ",
    "hya": "ひゃ", "hyi": "ひぃ", "hyu": "ひゅ", "hye": "ひぇ", "hyo": "ひょ",
    "fya": "ふゃ", "fyu": "ふゅ", "fyo": "ふょ",
    "mya": "みゃ", "myi": "みぃ", "myu": "みゅ", "mye": "みぇ", "myo": "みょ",
    "rya": "りゃ", "ryi": "りぃ", "ryu": "りゅ", "rye": "りぇ", "ryo": "りょ",
    "gya": "ぎゃ", "gyi": "ぎぃ", "gyu": "ぎゅ", "gye": "ぎぇ", "gyo": "ぎょ",
    "jya": "じゃ", "jyi": "じぃ", "jyu": "じゅ", "jye": "じぇ", "jyo": "じょ",
    "zya": "じゃ", "zyi": "じぃ", "zyu": "じゅ", "zye": "じぇ", "zyo": "じょ",
    "dya": "ぢゃ", "dyi": "ぢぃ", "dyu": "ぢゅ", "dye": "ぢぇ", "dyo": "ぢょ",
    "bya": "びゃ", "byi": "びぃ", "byu": "びゅ", "bye": "びぇ", "byo": "びょ",
    "pya": "ぴゃ", "pyi": "ぴぃ", "pyu": "ぴゅ", "pye": "ぴぇ", "pyo": "ぴょ",
    "xtu": "っ", "ltu": "っ", "xtsu": "っ", "ltsu": "っ",
    "xya": "ゃ", "lya": "ゃ", "xyu": "ゅ", "lyu": "ゅ", "xyo": "ょ", "lyo": "ょ",
    "xwa": "ゎ", "lwa": "ゎ",
    "ka": "か", "ki": "き", "ku": "く", "ke": "け", "ko": "こ",
    "sa": "さ", "si": "し", "su": "す", "se": "せ", "so": "そ",
    "ta": "た", "ti": "ち", "tu": "つ", "te": "て", "to": "と",
    "na": "な", "ni": "に", "nu": "ぬ", "ne": "ね", "no": "の",
    "ha": "は", "hi": "ひ", "hu": "ふ", "fu": "ふ", "he": "へ", "ho": "ほ",
    "ma": "ま", "mi": "み", "mu": "む", "me": "め", "mo": "も",
    "ya": "や", "yi": "い", "yu": "ゆ", "ye": "いぇ", "yo": "よ",
    "ra": "ら", "ri": "り", "ru": "る", "re": "れ", "ro": "ろ",
    "wa": "わ", "wi": "うぃ", "wu": "う", "we": "うぇ", "wo": "を",
    "ga": "が", "gi": "ぎ", "gu": "ぐ", "ge": "げ", "go": "ご",
    "za": "ざ", "zi": "じ", "ji": "じ", "zu": "ず", "ze": "ぜ", "zo": "ぞ",
    "da": "だ", "di": "ぢ", "du": "づ", "de": "で", "do": "ど",
    "ba": "ば", "bi": "び", "bu": "ぶ", "be": "べ", "bo": "ぼ",
    "pa": "ぱ", "pi": "ぴ", "pu": "ぷ", "pe": "ぺ", "po": "ぽ",
    "ja": "じゃ", "ju": "じゅ", "je": "じぇ", "jo": "じょ",
    "fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ",
    "va": "ゔぁ", "vi": "ゔぃ", "vu": "ゔ", "ve": "ゔぇ", "vo": "ゔぉ",
    "la": "ぁ", "li": "ぃ", "lu": "ぅ", "le": "ぇ", "lo": "ぉ",
    "xa": "ぁ", "xi": "ぃ", "xu": "ぅ", "xe": "ぇ", "xo": "ぉ",
    "nn": "ん",
    "a": "あ", "i": "い", "u": "う", "e": "え", "o": "お",
}
_VOWELS = frozenset("aiueo")
_CONSONANTS = frozenset("bcdfghjklmpqrstvwxyz")


def romaji_to_hiragana(text: str) -> str:
    """小文字化した文字列中のローマ字部分をひらがなへ変換する（NGワード変形候補の検出用）。

    かなや数字などローマ字以外の文字はそのまま残すため、かなとローマ字を混ぜた崩し表記も扱える。
    """
    lowered = text.lower()
    out: list[str] = []
    index = 0
    length = len(lowered)
    while index < length:
        if lowered[index : index + 4] in _ROMAJI_TABLE:
            out.append(_ROMAJI_TABLE[lowered[index : index + 4]])
            index += 4
            continue
        if lowered[index : index + 3] == "tch":
            out.append("っ")
            index += 1
            continue
        if lowered[index : index + 3] in _ROMAJI_TABLE:
            out.append(_ROMAJI_TABLE[lowered[index : index + 3]])
            index += 3
            continue
        pair = lowered[index : index + 2]
        if pair in _ROMAJI_TABLE:
            out.append(_ROMAJI_TABLE[pair])
            index += 2
            continue
        char = lowered[index]
        nxt = lowered[index + 1] if index + 1 < length else ""
        # ヘボン式の唇音前の m（sampo, shimbun, nekomamma 等）
        if char == "m" and nxt in {"b", "p", "m"}:
            out.append("ん")
            index += 1
            continue
        # 促音（同じ子音の連続: kka, tta, ppa 等。n の連続は上の nn で処理済み）
        if char in _CONSONANTS and char != "n" and nxt == char:
            out.append("っ")
            index += 1
            continue
        # 撥音（母音・y 以外の前の n、または語末の n）
        if char == "n" and nxt not in _VOWELS and nxt != "y":
            out.append("ん")
            index += 1
            continue
        if char in _ROMAJI_TABLE:
            out.append(_ROMAJI_TABLE[char])
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)
