"""管理者・ボット所有者・ブロック中の利用者の判定（`discord_helpers.py` から分けたモジュール。#468）。

`discord_helpers.py` は、従来の import 先として全部を再公開する窓口として残している。
新しいコードは、必要なものだけを読み込めるこのモジュールから直接 import してよい。
"""



import logging
from typing import Any, Callable

import discord

# 運用ログの出元は分割前と同じ `persona_bot.utils.discord_helpers`。
logger = logging.getLogger("persona_bot.utils.discord_helpers")


def is_administrator(user: Any) -> bool:
    """そのユーザーがサーバーの管理者権限を持つかを判定する。

    この判定は「他人の会話統計の閲覧可否」などの
    セキュリティ境界であり、条件を1か所に固定するためここへ集約している
    （各所へコピーすると、片方だけ条件が緩んだときに気づけない）。

    DM では discord.Member ではなく discord.User が渡るため False を返す
    （＝DM では管理者機能を使えない）。DM にはサーバーが無く、
    guild_permissions で権限を確認できないため。
    """
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def _is_application_owner(user_id: int, application: Any) -> bool:
    """個人所有なら所有者、チーム所有ならチーム所有者本人だけを許可する。"""
    team = getattr(application, "team", None)
    owner_id = (getattr(team, "owner_id", None) if team is not None
                else getattr(getattr(application, "owner", None), "id", None))
    return type(owner_id) is int and type(user_id) is int and owner_id > 0 and owner_id == user_id


def is_bot_owner(interaction: discord.Interaction) -> bool:
    """ログイン時のアプリ情報で、所有者向けボタンの表示可否を決める。"""
    client = getattr(interaction, "client", None)
    return _is_application_owner(interaction.user.id, getattr(client, "application", None))


async def is_current_bot_owner(interaction: discord.Interaction) -> bool:
    """全サーバー共通の違反情報を扱う直前に所有者を取り直し、確認失敗は拒否する。"""
    try:
        application = await interaction.client.application_info()
    except Exception as error:
        logger.error(f'ボット所有者の再確認に失敗したため拒否します: {type(error).__name__}', exc_info=error)
        return False
    return _is_application_owner(interaction.user.id, application)


async def is_current_administrator(interaction: discord.Interaction) -> bool:
    """長い処理の後に、その利用者が今も管理者かを最新の Member で判定する。

    `interaction.user` はボタンを押した時点の写しで、描画や集計を待つ間にロールが
    外されても更新されない。他人の統計やフィードバック集計のような管理者専用の結果を
    送る直前は、この関数で Member を取り直す（#208）。members インテントに頼らないよう、
    キャッシュではなく `guild.fetch_member` で取得する。

    DM・Member でない・脱退済み・取得失敗は、すべて管理者でないものとして扱う
    （確認できないときは拒否する）。キャンセルは握りつぶさず伝える。
    """
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    try:
        member = await user.guild.fetch_member(user.id)
    except Exception as error:
        logger.error(f'管理者権限の再確認に失敗したため拒否します: {type(error).__name__}', exc_info=error)
        return False
    return is_administrator(member)


BLOCKED_USER_NOTICE = "利用が制限されているため、この操作は実行できません。"


async def reject_blocked_interaction(
    interaction: discord.Interaction, is_blocked: Callable[[int], bool],
) -> bool:
    """ブロック中なら本人だけに拒否を通知し、処理を中止するためTrueを返す。"""
    if not is_blocked(interaction.user.id):
        return False
    await interaction.response.send_message(
        BLOCKED_USER_NOTICE, ephemeral=True,
    )
    return True
