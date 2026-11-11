"""音声会話の割り込み（バージイン）を、入力の文字起こしで確かめる（#409）。

#409 の決定事項:

- フィラー（「あー」「えーと」）・相槌は割り込みにしない。文字列として意味のある発言だけを受け入れ、
  2秒以上にわたって複数の語や言葉を検知したものを割り込みとして確定する
  （2秒の継続は `voice_conversation` が数え、ここでは文字起こしの中身を判定する）。
- 誤って割り込んだときは確認する（Live への指示。`prompting.build_live_system_instruction`）。

加えて、文字起こしがボット自身の直近の発話とほとんど同じなら、スピーカーからマイクへの
回り込み（自分の声）とみなす。エコー除去で取り切れなかった回り込みで割り込んだり、
自分の声に返答し続ける音声ループを防ぐ最後の関門。

依存は標準ライブラリだけ。文字起こしの本文はメモリ上で判定するだけで、保存もログ出力もしない。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import deque

from persona_bot.config.constants import (
    LIVE_BARGE_IN_CJK_CHARS_PER_WORD,
    LIVE_BARGE_IN_FILLERS,
    LIVE_BARGE_IN_MIN_WORDS,
    LIVE_ECHO_TEXT_MIN_CHARS,
    LIVE_ECHO_TEXT_OVERLAP,
    LIVE_ECHO_TEXT_WINDOW_SECONDS,
    LIVE_TRANSCRIPT_MAX_CHARS,
)

# 判定の結果。
INTERRUPTION_SPEECH = "speech"   # 意味のある発言（割り込みとして確定してよい）
INTERRUPTION_FILLER = "filler"   # 言いよどみ・相槌だけ、または語が足りない
INTERRUPTION_ECHO = "echo"       # ボット自身の発話の回り込み

# 伸ばし棒・波線の重なりは1つの「ー」にまとめる（「えーーと」「あ〜」の表記ゆれを吸収する）。
_LONG_VOWEL = re.compile(r"[ー〜~]+")
# 語の区切り（空白・句読点・記号）。「ー」はかなの一部として残る（\w に含まれる）。
_SEPARATOR = re.compile(r"[\W_]+")
_KATAKANA_START, _KATAKANA_END = 0x30A1, 0x30F6
_KANA_OFFSET = 0x60
# 回り込みの照合で過去の発話を覚えておく件数の上限（時間でも区切る）。
_RECENT_MAX_ITEMS = 256


def normalize_speech_text(text: str) -> str:
    """表記ゆれを吸収する（NFKC・小文字化・カタカナをひらがなへ・伸ばし棒の重なりを1つに）。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = "".join(
        chr(ord(char) - _KANA_OFFSET) if _KATAKANA_START <= ord(char) <= _KATAKANA_END else char
        for char in text
    )
    return _LONG_VOWEL.sub("ー", text)


_FILLERS = frozenset(normalize_speech_text(word) for word in LIVE_BARGE_IN_FILLERS)
# 語の頭から取り除いてよいもの（伸ばし棒・促音を含む、はっきりした言いよどみだけ）。
# 「あの人」の「あの」のように、語の一部でもありうるものは取り除かない。
_PREFIX_FILLERS = tuple(sorted((word for word in _FILLERS if "ー" in word or "っ" in word), key=len, reverse=True))
# 語の全体がフィラーかを見るときの部品（伸ばし棒・促音だけの断片も含める）。
_FILLER_PIECES = tuple(sorted(_FILLERS | {"ー", "っ"}, key=len, reverse=True))


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF       # ひらがな・カタカナ（「ー」を含む）
        or 0x3400 <= code <= 0x4DBF    # 漢字（拡張A）
        or 0x4E00 <= code <= 0x9FFF    # 漢字
        or 0xF900 <= code <= 0xFAFF    # 互換漢字
        or char == "々"
    )


def _is_all_fillers(token: str) -> bool:
    """語の全体が、フィラー・相槌の繰り返しや組み合わせだけでできているか（「うんうん」「えーとあのー」）。"""
    reachable = [False] * (len(token) + 1)
    reachable[0] = True
    for index in range(len(token)):
        if not reachable[index]:
            continue
        for piece in _FILLER_PIECES:
            if token.startswith(piece, index):
                reachable[index + len(piece)] = True
    return reachable[-1]


def _strip_filler_prefix(token: str) -> str:
    """語の頭に付いた言いよどみを取り除く（「えーと明日」→「明日」）。"""
    stripped = True
    while stripped:
        stripped = False
        for piece in _PREFIX_FILLERS:
            if token.startswith(piece) and len(token) > len(piece):
                token = token[len(piece):]
                stripped = True
                break
    return token


def meaningful_tokens(text: str) -> list[str]:
    """フィラー・相槌を除いた語（区切りごと）の一覧。"""
    tokens = []
    for token in _SEPARATOR.split(normalize_speech_text(text[:LIVE_TRANSCRIPT_MAX_CHARS])):
        if not token or _is_all_fillers(token):
            continue
        token = _strip_filler_prefix(token)
        if token and not _is_all_fillers(token):
            tokens.append(token)
    return tokens


def estimate_word_count(text: str) -> int:
    """フィラー・相槌を除いた語数の見積もり。

    日本語は語の区切りが無いため、かな・漢字は LIVE_BARGE_IN_CJK_CHARS_PER_WORD 文字を1語と数え、
    英数字の並びは区切りごとに1語と数える。
    """
    count = 0
    for token in meaningful_tokens(text):
        cjk = sum(1 for char in token if _is_cjk(char))
        count += (1 if cjk < len(token) else 0) + math.ceil(cjk / LIVE_BARGE_IN_CJK_CHARS_PER_WORD)
    return count


def _compact(text: str) -> str:
    return "".join(_SEPARATOR.split(normalize_speech_text(text)))


def echo_overlap(text: str, reference_text: str) -> float:
    """text の文字の2つ組のうち、reference_text にも現れる割合（0〜1）。短すぎる text は 0。"""
    compact = _compact(text[:LIVE_TRANSCRIPT_MAX_CHARS])
    if len(compact) < LIVE_ECHO_TEXT_MIN_CHARS:
        return 0.0
    reference = _compact(reference_text)
    known = {reference[index:index + 2] for index in range(len(reference) - 1)}
    pairs = [compact[index:index + 2] for index in range(len(compact) - 1)]
    return sum(1 for pair in pairs if pair in known) / len(pairs)


def is_echo_text(text: str, recent_output: str) -> bool:
    """入力の文字起こしが、ボット自身の直近の発話の回り込みか。"""
    return bool(recent_output) and echo_overlap(text, recent_output) >= LIVE_ECHO_TEXT_OVERLAP


def classify_interruption(text: str, recent_output: str) -> str:
    """ペルソナの発話中に話し始めた人の文字起こしを判定する（INTERRUPTION_*）。"""
    if is_echo_text(text, recent_output):
        return INTERRUPTION_ECHO
    if estimate_word_count(text) >= LIVE_BARGE_IN_MIN_WORDS:
        return INTERRUPTION_SPEECH
    return INTERRUPTION_FILLER


class RecentSpeech:
    """ボット自身の直近の発話（出力の文字起こし）。回り込みの照合にだけ使う（メモリ上だけ）。"""

    def __init__(self, window_seconds: float = LIVE_ECHO_TEXT_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._items: deque[tuple[float, str]] = deque(maxlen=_RECENT_MAX_ITEMS)

    def add(self, now: float, text: str) -> None:
        if text:
            self._items.append((now, text))
        self._prune(now)

    def text(self, now: float) -> str:
        self._prune(now)
        return "".join(text for _, text in self._items)

    def clear(self) -> None:
        self._items.clear()

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self._window:
            self._items.popleft()
