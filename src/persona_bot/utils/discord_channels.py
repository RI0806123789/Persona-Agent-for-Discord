"""チャンネルの種別・権限・表示名と、サーバー参加時の案内投稿（`discord_helpers.py` から分けたモジュール。#468）。

`discord_helpers.py` は、従来の import 先として全部を再公開する窓口として残している。
新しいコードは、必要なものだけを読み込めるこのモジュールから直接 import してよい。
"""



import logging
from typing import Any, TypeGuard

import discord

from persona_bot.config.constants import (
    WELCOME_MESSAGE_ENABLED,
)

# 運用ログの出元は分割前と同じ `persona_bot.utils.discord_helpers`。
logger = logging.getLogger("persona_bot.utils.discord_helpers")


# ボットが会話・履歴取得に対応しているチャンネル種別
SupportedMessageChannel = (
    discord.TextChannel | discord.Thread | discord.VoiceChannel | discord.StageChannel | discord.DMChannel
)


def is_supported_message_channel(channel: Any) -> TypeGuard[SupportedMessageChannel]:
    return isinstance(channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel, discord.StageChannel, discord.DMChannel))


def is_connected_voice_chat(guild: Any, channel_id: int) -> bool:
    """ボットがいま接続しているボイス/ステージチャンネルの付属チャットかどうかを返す。

    接続の情報源は常に `guild.voice_client` とし、呼ばれるたびにその場で確かめる。
    ボットが別のチャンネルへ移された・切断された場合も、判定が自然に追従する
    （接続先を別に覚えておくと、実態と食い違った範囲を見続けてしまう）。
    DM（guild が None）や、ボイス接続を持たないサーバーでは False。
    """
    voice_client = getattr(guild, "voice_client", None)
    if not isinstance(voice_client, discord.VoiceClient) or not voice_client.is_connected():
        return False
    return getattr(voice_client.channel, "id", None) == channel_id


def pick_announcement_channel(guild: discord.Guild) -> Any:
    """サーバー全体へのお知らせを投稿できるテキストチャンネルを1つ選ぶ。

    優先順位:
      1. システムチャンネル（サーバー側で「お知らせ用」に指定されている場所）
      2. `guild.text_channels` を順に見て、最初に送信できるチャンネル
      3. どこにも送れなければ None（呼び出し側は投稿を諦める）

    走査に `guild.text_channels` を使うのは、`guild.channels` だとカテゴリや
    ボイスチャンネルまで混ざるため（position 順に並んだテキストチャンネル
    だけが欲しい）。

    権限は必ず `channel.permissions_for(guild.me)` で**チャンネル単位に**
    確認する。サーバー全体のロールで許可されていても、チャンネル個別の設定で
    拒否されていることがあるため（`/join` の接続権限チェックと同じ作法）。
    「送ってみて失敗したら次」という例外頼みにすると、投稿できないチャンネルの
    数だけ無駄に API を叩くうえ、権限エラーとネットワーク障害を区別できない。
    """
    bot_member = guild.me
    if bot_member is None:
        # キャッシュ未取得などで自分自身の Member が引けないときは権限を判定できない。
        return None

    system_channel = guild.system_channel
    if system_channel is not None and can_bot_send_messages(system_channel, bot_member):
        return system_channel

    for channel in guild.text_channels:
        if can_bot_send_messages(channel, bot_member):
            return channel
    return None


async def post_welcome_message(
    guild: discord.Guild,
    body: str,
    posted_guild_ids: set[int],
) -> bool:
    """サーバー参加時の案内を、そのサーバーへ1回だけ投稿する。

    投稿できたら True、投稿しなかった（無効化されている・投稿済み・投稿先が
    無い・送信に失敗した）場合は False を返す。**例外は外へ出さない** —
    招待そのものは成功させたいので、案内の失敗でイベント処理を落とさない。

    Args:
        guild: 招待されたサーバー。
        body: 投稿する本文（`help_view.build_welcome_text` の戻り値）。
        posted_guild_ids: 投稿済みサーバーIDの集合（`BotState.welcomed_guild_ids`）。
            この関数が投稿の直前に書き込む。

    メンションは `AllowedMentions.none()` で完全に無効化する。本文には
    `@everyone` を書かない方針だが、ボット名を差し込む都合で万一 `@` を
    含む文字列が入っても通知爆撃にならないよう、送信側でも止めておく。
    """
    if not WELCOME_MESSAGE_ENABLED:
        return False
    if guild.id in posted_guild_ids:
        # 同一プロセス内でイベントが重複して届いた場合の保険（連投防止）。
        return False

    channel = pick_announcement_channel(guild)
    if channel is None:
        logger.info('ウェルカムメッセージを投稿できるチャンネルが見つかりませんでした（guild=<ID>）。')
        return False

    # 送信を待つ前に「投稿済み」を立てる。ここから送信までの間に await を
    # 挟まないため、イベントが重複しても2件目は上の判定で確実に弾かれる
    # （送信後に立てると、送信の待ち時間に入ってきた2件目が二重投稿になる）。
    posted_guild_ids.add(guild.id)

    try:
        await channel.send(body, allowed_mentions=discord.AllowedMentions.none())
    # Forbidden は HTTPException の派生クラスなので、必ず先に捕まえること。
    except discord.Forbidden:
        logger.error('ウェルカムメッセージの送信を拒否されました（guild=<ID> / channel=<ID>）。', exc_info=True)
        return False
    except discord.HTTPException as error:
        logger.error(f'ウェルカムメッセージの送信に失敗しました（guild=<ID>）: {type(error).__name__}', exc_info=error)
        return False

    logger.info('ウェルカムメッセージを投稿しました（guild=<ID> / channel=<ID>）。')
    return True


def can_bot_send_messages(channel: Any, member: Any) -> bool:
    """閲覧と、そのチャンネル種別に対応した送信権限を確認する。"""
    if member is None:
        return False
    permissions = channel.permissions_for(member)
    send = permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages
    return bool(permissions.view_channel and send)


def channel_display_name(channel: Any) -> str:
    """名前のないチャンネルも識別可能な表示名にする。"""
    return getattr(channel, "name", None) or f"channel {getattr(channel, 'id', None) or '不明'}"
