"""NGワードの読み込み・判定・マスク処理を提供するサービス。

data/ng/NG_WORD.txt と data/ng/NG_WORD_private.txt から1行ずつNGワードを読み込み、
ユーザーの入力やBotの応答に含まれていないかチェックする。
"""

import logging
import re
import unicodedata
from pathlib import Path

from persona_bot.config.constants import NG_WORD_PATH, NG_WORD_PRIVATE_PATH
from persona_bot.utils.normalization import fold_kana

logger = logging.getLogger(__name__)


def _is_ignored_character(character: str) -> bool:
    """ゼロ幅・非表示の制御文字を除く。タブ・改行などの空白は語の境界に残す。"""
    codepoint = ord(character)
    return (
        (unicodedata.category(character) in {"Cc", "Cf"} and not character.isspace())
        or codepoint == 0x034F  # 結合書記素接合子も照合の境界にしない。
        or 0xFE00 <= codepoint <= 0xFE0F
        or 0xE0100 <= codepoint <= 0xE01EF
        or 0x180B <= codepoint <= 0x180D
        or codepoint == 0x180F
    )


def _normalize_for_matching(text: str) -> tuple[str, list[tuple[int, int]]]:
    """照合文と、各文字が由来する元本文の区間を返す。

    NFKCは文字ごとに行うと半角カナ＋濁点や分解済み文字を合成できない。
    合成に関わる単位をstdlibの正規化でまとめ、単位の全出力へ元の区間を付ける。
    展開された文字の一部だけが一致した場合も、元の文字を丸ごと伏せられる。
    """
    normalized_parts: list[str] = []
    source_spans: list[tuple[int, int]] = []
    pending: list[str] = []
    start = end = 0

    def append_pending(normalized: str) -> None:
        folded = fold_kana(normalized)
        normalized_parts.append(folded)
        source_spans.extend([(start, end)] * len(folded))

    for index, character in enumerate(text):
        if _is_ignored_character(character):
            continue
        if not pending:
            pending = [character]
            start, end = index, index + 1
            continue
        # 非スターターは後から並び替え・合成されるため、まとめて一度だけ正規化する。
        if unicodedata.combining(unicodedata.normalize("NFKD", character)[0]):
            pending.append(character)
            end = index + 1
            continue
        previous = unicodedata.normalize("NFKC", "".join(pending))
        following = unicodedata.normalize("NFKC", character)
        together = previous + following
        if unicodedata.normalize("NFKC", together) != together:
            # Hangulや、結合クラスが0の文字同士の合成もstdlibに任せる。
            pending.append(character)
            end = index + 1
            continue
        append_pending(previous)
        pending = [character]
        start, end = index, index + 1
    if pending:
        append_pending(unicodedata.normalize("NFKC", "".join(pending)))
    return "".join(normalized_parts), source_spans


def _source_ranges(text: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """正規化後の一致を、ゼロ幅文字も含む元本文の連続した区間へ戻す。"""
    normalized, spans = _normalize_for_matching(text)
    return [(spans[match.start()][0], spans[match.end() - 1][1])
            for match in pattern.finditer(normalized)]


def _mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    """展開で重複する区間を含め、元の文字数・周囲の文面を変えずに伏せる。"""
    characters = list(text)
    for start, end in ranges:
        characters[start:end] = ["●"] * (end - start)
    return "".join(characters)


def _load_words_from_file(file_path: Path) -> list[str]:
    """ファイルからNGワードを1行ずつ読み込む。空行・コメント行(#)はスキップする。

    メモ帳などで「UTF-8 (BOM 付き)」保存されたファイルも読めるよう utf-8-sig で読む。
    BOM (U+FEFF) は strip() でも消えないため、残すと1行目のコメントがNGワードとして
    登録されたり、1行目のNGワードが検知できなくなったりする。
    """
    try:
        lines = file_path.read_text(encoding="utf-8-sig").splitlines()
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
    except FileNotFoundError:
        return []
    except Exception as error:
        logger.error("NGワードの読み込みに失敗しました。", exc_info=error)
        # 読込失敗を空の成功結果へ変えず、再読込元に従来の判定を維持させる。
        raise


def load_ng_words() -> list[str]:
    """NG_WORD.txt と NG_WORD_private.txt の両方からNGワードを読み込み、統合して返す。"""
    public_words = _load_words_from_file(NG_WORD_PATH)
    private_words = _load_words_from_file(NG_WORD_PRIVATE_PATH)
    # 重複を除去しつつ順序を維持
    seen: set[str] = set()
    merged: list[str] = []
    for word in public_words + private_words:
        normalized, _spans = _normalize_for_matching(word)
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(word)
    return merged


def _build_pattern(ng_words: list[str]) -> re.Pattern[str] | None:
    """登録語も本文と同じ正規化を通し、大文字小文字を無視したパターンを構築する。

    `|` は左の選択肢から順にマッチするため、長い語を先に並べて最長一致を
    優先する（短い語が先だと「ワード」と「ワード表現」の両方がある場合に
    「ワード表現」が部分マスクされてしまう）。
    """
    normalized_words = dict.fromkeys(_normalize_for_matching(word)[0] for word in ng_words)
    normalized_words.pop("", None)
    if not normalized_words:
        return None
    escaped = [re.escape(word) for word in sorted(normalized_words, key=len, reverse=True)]
    return re.compile("|".join(escaped), re.IGNORECASE)


class NgWordFilter:
    """NGワードのフィルタリングを行うクラス。"""

    def __init__(self) -> None:
        self._snapshot: tuple[list[str], re.Pattern[str] | None] = ([], None)
        self.reload()

    def reload(self) -> None:
        """NGワードリストをファイルから再読み込みする。"""
        words = load_ng_words()
        pattern = _build_pattern(words)
        # ワーカーで準備してから一度に公開し、再読込中も従来の判定を維持する。
        self._snapshot = (words, pattern)
        if words:
            logger.info("NGワード: %d件 読み込み済み", len(words))

    @property
    def word_count(self) -> int:
        return len(self._snapshot[0])

    @property
    def words(self) -> list[str]:
        """NGワードのリストを返す（ContentModerator 連携用）。"""
        return list(self._snapshot[0])

    def contains_ng_word(self, text: str) -> bool:
        """テキストにNGワードが含まれているかどうかを判定する。"""
        pattern = self._snapshot[1]
        if pattern is None:
            return False
        normalized, _spans = _normalize_for_matching(text)
        return bool(pattern.search(normalized))

    def find_ng_words(self, text: str) -> list[str]:
        """テキスト中に含まれるNGワードの一覧を、重複を除いて最初に現れた順で返す。

        set で重複を除くと順序が実行ごとに変わり、ログの比較やテストが不安定になるため
        dict.fromkeys で出現順を保つ。
        """
        pattern = self._snapshot[1]
        if pattern is None:
            return []
        return list(dict.fromkeys(text[start:end] for start, end in _source_ranges(text, pattern)))

    def mask_ng_words(self, text: str) -> str:
        """テキスト中のNGワードを伏字(●)に置換して返す。"""
        pattern = self._snapshot[1]
        if pattern is None:
            return text
        return _mask_ranges(text, _source_ranges(text, pattern))

    def mask_with_log(self, text: str, *, label: str) -> str:
        """NGワードが含まれていれば件数をログに出してマスクし、無ければ元のテキストを返す。

        マスク仕様を変えるときに1か所で済ませるために集約している。以前、感情タグの
        正規表現が3ファイルに重複して差異が生まれ、タグがユーザーへ露出する不具合に
        つながった前例があるため、同じ再発を避ける。

        Args:
            text: 判定対象のテキスト（ユーザー入力・Bot応答・リマインダー本文など）。
            label: 呼び出し側の文脈名。既存の呼び出しとの互換性のため受け取るが、
                運用ログには文脈や検出語を含めず件数だけを残す。

        Returns:
            NGワードを含む場合はマスク済みのテキスト。含まない場合は加工しない元のテキスト。
        """
        # ログとマスクを同じスナップショット・同じ一致区間から作る。
        pattern = self._snapshot[1]
        if pattern is None:
            return text
        ranges = _source_ranges(text, pattern)
        if not ranges:
            return text
        detected = list(dict.fromkeys(text[start:end] for start, end in ranges))
        logger.warning("NGワード検出: %d件をマスクしました。", len(detected))
        return _mask_ranges(text, ranges)
