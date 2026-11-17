"""メッセージの分割送信・通知の抑止・出典リンクの整形（`discord_helpers.py` から分けたモジュール。#468）。

`discord_helpers.py` は、従来の import 先として全部を再公開する窓口として残している。
新しいコードは、必要なものだけを読み込めるこのモジュールから直接 import してよい。
"""



from typing import Any, Sequence
from urllib.parse import quote

import discord

from persona_bot.config.constants import (
    DISCORD_MESSAGE_MAX_LENGTH,
)


def split_message(text: str, limit: int = DISCORD_MESSAGE_MAX_LENGTH) -> list[str]:
    """長いテキストを Discord の文字数制限内に分割する。

    改行の位置で分割し、それでも収まらない場合は強制的にカットする。
    空白だけの断片は除き、中身のある断片の空白や字下げは保持する。
    """
    if len(text) <= limit:
        return [text] if text.strip() else []

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # 制限内で最後の改行を探す。区切り文字は次のチャンクへ持ち越さず捨てるので、
        # text[limit]（制限ちょうどの次の文字）が区切りでも text[:limit] で切れる。
        # 探索範囲を limit + 1 にしないと、その区切りを見落として手前で不要に分割する。
        split_pos = text.rfind("\n", 0, limit + 1)
        if split_pos <= 0:
            # 改行が見つからない場合はスペースで分割を試みる
            split_pos = text.rfind(" ", 0, limit + 1)

        if split_pos <= 0:
            # 区切り文字が見つからない場合は強制カット（切った文字も残す）
            chunks.append(text[:limit])
            text = text[limit:]
            continue

        # split_pos の文字は区切り文字(改行またはスペース)なので次チャンクからは除外する
        chunks.append(text[:split_pos])
        text = text[split_pos + 1:]

    return [chunk for chunk in chunks if chunk.strip()]


async def _send_chunks(
    channel: Any,
    chunks: Sequence[str],
    *,
    suppress_embeds: bool,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """分割済みチャンクを順にチャンネルへ送信する（送信系ヘルパーの共通部分）。

    `allowed_mentions` は None のままなら Discord クライアントの既定に従う
    （＝これまでどおりの挙動）。`AllowedMentions.none()` を渡すと通知を一切
    飛ばさずに投稿できる。ボットが自分から投稿する文面（自律行動の独り言など）は
    誰かへの返信ではないため、通知を伴わせない用途で使う。
    """
    for chunk in chunks:
        if not chunk.strip():
            continue
        await channel.send(chunk, suppress_embeds=suppress_embeds, allowed_mentions=allowed_mentions)


# AI の返信で通知を許すのは、返信先（質問した本人）への通知だけ。
# 本文は AI が生成・引用したものなので、@everyone・@here・ロール・第三者の
# メンションが含まれていてもボットの権限で通知させない（モデレーションは
# 通知先の認可を保証しないため、送信側で止める）。
REPLY_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False, users=False, roles=False, replied_user=True,
)


# ボットが自分から投稿する文面（独り言・案内・同意パネルなど）では、通知を一切飛ばさない。
# 各モジュールで作り直さず、この1つを共有する（#453）。
NO_MENTIONS = discord.AllowedMentions.none()


async def send_long_reply(
    message: discord.Message,
    text: str,
    *,
    suppress_embeds: bool = False,
    allowed_mentions: discord.AllowedMentions = REPLY_ALLOWED_MENTIONS,
) -> discord.Message | None:
    """2000 文字を超える応答を分割して送信する。

    最初のチャンクは元メッセージへの reply、残りは通常の send で送信する。
    suppress_embeds が True の場合、リンクの埋め込みプレビューを抑制する。
    allowed_mentions は1通目と2通目以降の全チャンクへ同じものを渡す。既定の
    `REPLY_ALLOWED_MENTIONS` は返信先本人への通知だけを残す（通常会話・文書・
    配信コメントの返信がすべてここを通るため、呼び出し側で指定しなくてよい）。

    戻り値は**1通目**（元メッセージへの reply）。フィードバックのリアクション
    付与など、送信したメッセージを後から参照する用途に使う。2通目以降は
    元の質問への紐付けが無い channel.send なので返さない
    （評価対象は「1つの応答」であり、分割は表示上の都合にすぎないため）。
    戻り値を使わない呼び出しはそのままでよい。
    """
    # 分割側とは別に空白だけの断片を除き、最初に実際に送る1通を返信にする。
    chunks = [chunk for chunk in split_message(text) if chunk.strip()]
    if not chunks:
        return None
    sent = await message.reply(chunks[0], suppress_embeds=suppress_embeds, allowed_mentions=allowed_mentions)
    await _send_chunks(
        message.channel, chunks[1:], suppress_embeds=suppress_embeds, allowed_mentions=allowed_mentions,
    )
    return sent


async def send_long_message(
    channel: Any,
    text: str,
    *,
    suppress_embeds: bool = False,
    allowed_mentions: discord.AllowedMentions | None = None,
) -> None:
    """2000 文字を超えるテキストを分割してチャンネルへ送信する（reply しない版）。

    自発的な会話・記念日のお祝い・リマインダー通知・会話要約・自律行動の
    独り言のように「特定のメッセージへの返信ではない投稿」で使う。
    以前は `for chunk in split_message(...): await channel.send(chunk)` が
    4か所へ手書きコピーされており、send_long_reply が持つ suppress_embeds の
    考慮がそちらには無かった。リンク抑制などを足すときに触る場所を
    1か所に保つため集約している。
    """
    await _send_chunks(
        channel,
        split_message(text),
        suppress_embeds=suppress_embeds,
        allowed_mentions=allowed_mentions,
    )


def mask_grounding_sources(
    sources: list[dict[str, str]], ng_filter: "NgWordFilter | None",
) -> list[dict[str, str]]:
    """出典のタイトルとURLも本文と同じNGワードマスクを通す。元の取得結果は変えない。"""
    if ng_filter is None:
        return sources
    return [
        {**source, **{
            key: ng_filter.mask_with_log(source.get(key, ""), label="参考リンク")
            for key in ("title", "uri")
        }}
        for source in sources
    ]


def _sanitize_link_uri(uri: Any) -> str:
    """`<URL>` の囲みを壊す文字（空白・制御文字・山括弧）を URL エンコードする。"""
    text = "" if uri is None else str(uri).strip()
    return "".join(
        quote(char) if char.isspace() or char in "<>" or ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in text
    )


def format_grounding_sources(
    sources: list[dict[str, str]], ng_filter: "NgWordFilter | None" = None,
) -> str:
    """グラウンディングソースをコンパクトなテキストに整形する。

    グラウンディングの URI はリダイレクト用の無機質なアドレスのため、
    タイトルがあれば `[タイトル](URL)` 形式でリンク先が分かるように表示する。
    """
    if not sources:
        return ""
    lines: list[str] = []
    for source in mask_grounding_sources(sources, ng_filter):
        # URL に空白・改行・山括弧が入ると `<...>` の囲みが途中で終わるため、除くか符号化して1つのリンクに保つ
        uri = _sanitize_link_uri(source.get("uri", ""))
        # Markdown リンクの構文を壊さないよう、タイトルは1行にして角括弧は全角に置換する
        title = " ".join(str(source.get("title", "")).split()).replace("[", "［").replace("]", "］")
        if title:
            lines.append(f"・ [{title}](<{uri}>)")
        else:
            lines.append(f"・ <{uri}>")
    links = "\n".join(lines)
    return f"\n\n🔍 **参考リンク:**\n{links}"
