"""チャンネル履歴の取得・整形と、会話要約の元データの組み立て（`discord_helpers.py` から分けたモジュール。#468）。

`discord_helpers.py` は、従来の import 先として全部を再公開する窓口として残している。
新しいコードは、必要なものだけを読み込めるこのモジュールから直接 import してよい。
"""



import logging
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Callable, Sequence

import discord

from persona_bot.config.constants import (
    SUMMARY_TIMEZONE,
)
from persona_bot.services.prompting import SanitizedHistory, sanitize_prompt_data
from persona_bot.utils.timeutil import resolve_timezone
from persona_bot.utils.discord_attachments import attachment_is_image

# 運用ログの出元は分割前と同じ `persona_bot.utils.discord_helpers`。
logger = logging.getLogger("persona_bot.utils.discord_helpers")


def clean_message_content(message: discord.Message, bot_user: Any) -> str:
    content = message.content.strip()
    if bot_user is None:
        return content
    return re.sub(rf"<@!?{bot_user.id}>", "", content).strip()


def _include_history_message(
    message: discord.Message, bot_user: Any, is_blocked: Callable[[int], bool] | None,
) -> bool:
    """自ボットの回答は残し、他ボットと現在ブロック中の利用者を履歴から除く。"""
    author = message.author
    if bot_user is not None and author == bot_user:
        return True
    if getattr(author, "bot", False):
        return False
    if is_blocked is not None:
        user_id = getattr(author, "id", None)
        # 判定対象を識別できない発言は、ブロックしていないと決めつけて使わない。
        if user_id is None or is_blocked(user_id):
            return False
    return True


def build_history_text(
    messages: Sequence[discord.Message], bot_user: Any, *,
    is_blocked: Callable[[int], bool] | None = None,
) -> str:
    """発言本文を無害化してから、コード側の正規ロール行に組み立てる。"""
    lines: list[str] = []
    for history_message in messages:
        if not _include_history_message(history_message, bot_user, is_blocked):
            continue
        role = ("BOTの回答" if bot_user is not None and history_message.author == bot_user
                else "ユーザーの質問")
        clean_content = sanitize_prompt_data(clean_message_content(history_message, bot_user))
        # 画像と文書などを区別して書く（すべて「画像」と書くと、PDF を添えた発言まで
        # モデルが画像付きと誤解して応答してしまう）。
        if any(attachment_is_image(attachment) for attachment in history_message.attachments):
            clean_content += " [画像添付あり]"
        # 画像以外の添付が1つでもあるときだけ付ける。添付が空なら any() は偽になり、何も付かない
        # （`not all(...)` と等価だが、空リストの扱いを言語仕様に暗黙に頼らず条件式で読めるようにする）。
        if any(not attachment_is_image(attachment) for attachment in history_message.attachments):
            clean_content += " [添付ファイルあり]"
        if not clean_content.strip():
            # 本文も添付も無い発言（スタンプだけ・メンションだけなど）は、役割だけの空行にしない。
            continue
        lines.append(f"{role}: {clean_content}")
    return SanitizedHistory("\n".join(lines))


async def fetch_channel_history_text(
    channel: Any,
    reset_message_id: int | None,
    bot_user: Any,
    limit: int,
    *,
    exclude_message_id: int | None = None,
    is_blocked: Callable[[int], bool] | None = None,
) -> str:
    """チャンネルの直近履歴を、プロンプトへ差し込める1つのテキストに整形する。

    exclude_message_id を渡すと、その発言を履歴から除く（文書ワーカーが、受付後に
    編集されうる元の投稿の本文をプロンプトへ入れないために使う。#203）。
    is_blocked は呼出元から渡し、現在ブロック中の発言者を取得後の整形時に除く。

    履歴の取得に失敗した場合は例外を外へ出さず、空文字を返して会話を続行する
    （フェイルオープン）。この関数は on_message の主経路から呼ばれるため、
    例外を外へ漏らすと handle_chat_message → on_message と素通りして
    discord.py の既定 on_error がログに吐くだけになり、ユーザーには入力中
    インジケータが消えるだけで何も返らない。履歴が無くても会話は成立するので
    フェイルオープンにする。
    """
    # 常に新しい順で直近 limit 件を取得する。リセットポイント指定時に古い順で
    # 取得すると、リセット以降のメッセージが limit を超えた時点で
    # 最新の発言が履歴に入らなくなるため。
    after = discord.Object(id=reset_message_id) if reset_message_id else None
    history_messages: list[discord.Message] = []
    try:
        async for history_message in channel.history(limit=limit, after=after, oldest_first=False):
            if exclude_message_id is not None and history_message.id == exclude_message_id:
                continue
            history_messages.append(history_message)
        return build_history_text(list(reversed(history_messages)), bot_user, is_blocked=is_blocked)
    # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること
    # （順序を逆にすると権限エラーが一時障害として扱われ、原因がログから消える）。
    except discord.Forbidden:
        logger.error('履歴の取得権限がありません（チャンネルID: <ID>）。「メッセージ履歴を読む」権限を確認してください。履歴なしで応答します。', exc_info=True)
        return ""
    except discord.HTTPException as error:
        logger.error("履歴の取得に失敗しました（チャンネルID: <ID>）。履歴なしで応答します。", exc_info=error)
        return ""
    except Exception as error:
        # 除外判定・本文整形が失敗しても、不明な履歴を使わず会話だけを続ける。
        logger.error("履歴の取得・整形に失敗しました。履歴なしで応答します。", exc_info=error)
        return ""


@dataclass(frozen=True)
class SummarySource:
    """要約へ実際に採用した履歴と、そのメッセージ件数。"""

    text: SanitizedHistory
    message_count: int


def build_summary_source(
    messages: Sequence[discord.Message],
    bot_user: Any,
    tz: tzinfo | None = None,
    *,
    is_blocked: Callable[[int], bool] | None = None,
) -> SummarySource:
    """議事録要約用に「発言者名つき」で会話ログを整形する。

    通常の履歴整形（build_history_text）は全員を「ユーザー/BOT」の2種類に潰すが、
    議事録では「誰が発言したか」が重要なため、実際の表示名を明示する。
    他のBOTの発言は除外し、自ボットの発言は会話の流れとして含める。

    Args:
        tz: 時刻表記に使うタイムゾーン。None なら SUMMARY_TIMEZONE を解決して使う。
            実行環境のローカル時刻（astimezone() の既定）に頼ると、UTC 運用の VPS で
            議事録の時刻だけが9時間ずれる（統計・記念日・リマインダーは
            Asia/Tokyo 固定のため、そこだけ基準が食い違う）。
    """
    # 解決はメッセージごとではなく1回だけ行う（1件ごとに ZoneInfo を引くと無駄）。
    # ZoneInfo を直接呼ばず resolve_timezone を通すこと（tzdata の無い環境では
    # 例外になるため、そこを UTC フォールバックで受けるのがこの関数の役割）。
    display_tz = tz if tz is not None else resolve_timezone(SUMMARY_TIMEZONE, purpose="会話要約の時刻表示")
    lines: list[str] = []
    for history_message in messages:
        # 他のBOTの発言は議事録に含めない（人間の会話に集中する）。
        # 自ボット自身の発言は文脈が繋がるように含める。
        if not _include_history_message(history_message, bot_user, is_blocked):
            continue
        content = sanitize_prompt_data(clean_message_content(history_message, bot_user))
        if history_message.attachments:
            content += " [添付ファイルあり]"
        # スタンプや埋め込みのみで本文が空の発言はノイズになるためスキップ
        if not content.strip():
            continue
        speaker = ("（このBOT）" if history_message.author == bot_user
                   else " ".join(sanitize_prompt_data(history_message.author.display_name).splitlines()))
        # created_at は UTC。表示用のタイムゾーン（既定は SUMMARY_TIMEZONE）へ明示的に変換する。
        timestamp = history_message.created_at.astimezone(display_tz).strftime("%m/%d %H:%M")
        lines.append(f"[{timestamp}] {speaker}: {content}")
    # 本文の改行数とは無関係に、採用したメッセージ1件につき要素を1つ追加する。
    return SummarySource(SanitizedHistory("\n".join(lines)), len(lines))


def build_summary_source_text(
    messages: Sequence[discord.Message],
    bot_user: Any,
    tz: tzinfo | None = None,
    *,
    is_blocked: Callable[[int], bool] | None = None,
) -> str:
    """本文だけを必要とする呼び出し元へ、従来どおり整形済み履歴を返す。"""
    return build_summary_source(messages, bot_user, tz, is_blocked=is_blocked).text


async def fetch_messages_since(
    channel: Any,
    after_time: datetime,
    max_messages: int,
) -> list[discord.Message]:
    """指定時刻以降のメッセージを古い順で取得する。

    上限超過を判定できるよう、あえて max_messages + 1 件まで取得する。
    戻り値の件数が max_messages を超えていたら「多すぎ」と判断できる。
    """
    collected: list[discord.Message] = []
    async for history_message in channel.history(limit=max_messages + 1, after=after_time, oldest_first=True):
        collected.append(history_message)
    return collected
