"""読み上げ用テキストの整形（#470。`voice.py` から分離）。

返信テキストから、声で聞いて意味をなさないもの（URL・コード・記号・メンションなど）を落とし、
長すぎる分は切る。通信・音声合成・再生には触れない純粋な関数だけを置く。
`voice` が従来の名前（`build_speech_text`）で再公開する。
"""

import re

from persona_bot.config.constants import TTS_MAX_CHARS, TTS_TRUNCATION_NOTICE
from persona_bot.services.affinity_service import strip_speech_tags


# 読み上げに不向きな要素を落とすためのパターン群。
# 音声で聞いて意味をなさないもの（URL・記号・コード）は読み上げる価値がないうえ、
# 音声エンジンは URL を1文字ずつ読み上げてしまい非常に長くなるため必ず除去する。
_CODE_BLOCK_PATTERN = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`[^`]*`")
# URL の直後に空白なしで続く日本語や全角句読点を巻き込まない。
_URL_PATTERN = re.compile(r"https?://[\x21-\x7e]+")
# Discord のカスタム絵文字 <:name:123> / アニメ絵文字 <a:name:123>
_CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:\w+:\d+>")
# メンション <@123> / <@!123> / <@&123>（ロール） / <#123>（チャンネル）
_MENTION_PATTERN = re.compile(r"<[@#][!&]?\d+>")
# Markdown の装飾記号と引用・見出し記号（読み上げでは雑音にしかならない）
_MARKDOWN_SYMBOL_PATTERN = re.compile(r"[*_~>#|`]")
_WHITESPACE_PATTERN = re.compile(r"\s+")


def build_speech_text(response_text: str) -> str:
    """返信テキストを読み上げ向けに整形する。

    テキストチャンネルには元の全文が既に投稿されているため、ここでは
    「声で聞いて分かる部分」だけを残す。長すぎる返信は途中で切り、
    ボイスチャンネルを何分も占有しないようにする。
    """
    text = strip_speech_tags(response_text)
    text = _CODE_BLOCK_PATTERN.sub(" ", text)
    text = _INLINE_CODE_PATTERN.sub(" ", text)
    text = _URL_PATTERN.sub(" ", text)
    text = _CUSTOM_EMOJI_PATTERN.sub(" ", text)
    text = _MENTION_PATTERN.sub(" ", text)
    text = _MARKDOWN_SYMBOL_PATTERN.sub(" ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip()

    if not any(character.isalnum() for character in text):
        return ""
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS].rstrip() + TTS_TRUNCATION_NOTICE
    return text
