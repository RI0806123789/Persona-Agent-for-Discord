"""サブエージェントの作業スレッドの、インメモリの状態（#267・#408・#480）。

- `AgentWebhooks`: 子の投稿に使う Webhook をチャンネルごとに1つ用意して使い回す。
- `AgentWorkThreads` / `WorkThreadEntry`: ボットが作った作業スレッドと、閉じたかどうかの記録。

Discord へ投稿する処理・進捗の表示（`AgentThreadProgress` など）は `agent_thread.py` にある。
そこから従来どおりこれらの名前を import できる（`from persona_bot.views.agent_thread import AgentWebhooks`）。
どちらも再起動で消えてよい一時状態で、永続化しない。

★ ログは分割前と同じロガー名 `persona_bot.views.agent_thread` へ出す（運用ログの出元を変えない）。
★ このモジュールは `agent_thread.py` を import しない（循環参照になる）。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import logging
import asyncio
from typing import Mapping

import discord

from persona_bot.config.constants import AGENT_WEBHOOK_NAME, AGENT_WORK_THREADS_MAX


logger = logging.getLogger("persona_bot.views.agent_thread")


class AgentWebhooks:
    """子の投稿に使う Webhook を、チャンネルごとに1つ用意して使い回す（インメモリ）。

    ボット自身が作った同名の Webhook があればそれを使い、無ければ作る。再起動しても
    同じものを見つけて使うので、Webhook は増え続けない。
    """

    def __init__(self) -> None:
        self._cache: dict[int, discord.Webhook] = {}
        # 作成は初回だけなので、全チャンネルで1つのロックにする（同時の依頼で2つ作らないため）。
        self._lock = asyncio.Lock()

    async def get(self, channel: discord.TextChannel, bot_user_id: int) -> discord.Webhook | None:
        """使える Webhook を返す。用意できなければ None（呼び出し側は返信の進捗表示へ切り替える）。"""
        webhook = self._cache.get(channel.id)
        if webhook is not None:
            return webhook
        async with self._lock:
            webhook = self._cache.get(channel.id)
            if webhook is not None:
                return webhook
            try:
                webhook = next(
                    (hook for hook in await channel.webhooks() if _is_own_webhook(hook, bot_user_id)), None,
                )
                if webhook is None:
                    webhook = await channel.create_webhook(
                        name=AGENT_WEBHOOK_NAME, reason="サブエージェントの作業スレッドへの投稿に使う",
                    )
                    logger.info("サブエージェント: 作業スレッド用の Webhook を作りました。")
            except discord.DiscordException as error:
                logger.warning("サブエージェント: Webhook を用意できませんでした。", exc_info=error)
                return None
            self._cache[channel.id] = webhook
            return webhook

    def forget(self, channel_id: int, webhook: discord.Webhook) -> None:
        """消された Webhook を次回から使わない（別の依頼が入れ替えた後なら何もしない）。"""
        if self._cache.get(channel_id) is webhook:
            del self._cache[channel_id]


def _is_own_webhook(webhook: discord.Webhook, bot_user_id: int) -> bool:
    return (
        webhook.type is discord.WebhookType.incoming
        and bool(webhook.token)
        and webhook.user is not None
        and webhook.user.id == bot_user_id
        and webhook.name == AGENT_WEBHOOK_NAME
    )


@dataclass
class WorkThreadEntry:
    """作業スレッド1つ分の記録。lines と role は、そのスレッドで依頼を受けたときの定型文とロール。"""

    lines: Mapping[str, str]
    role: str
    # ボットが閉じた後、まだ書き込みで再開されていないか
    archived: bool = False


class AgentWorkThreads:
    """作業スレッドの記録（インメモリ。#408）。

    - 作業スレッドかどうか: 再開したスレッドの中で受けた追加の依頼も、完了したら閉じ直すため。
    - ボットが閉じたか: 閉じたスレッドへ書き込みがあると Discord が再開するので、そのとき一度だけ
      追加の依頼を確かめるため（自動アーカイブや人の操作で閉じたものは対象にしない）。

    再起動で消える（それ以前のスレッドは再開しても確かめない）。`AGENT_WORK_THREADS_MAX` を超えたら
    古いものから忘れる。
    """

    def __init__(self, max_entries: int = AGENT_WORK_THREADS_MAX) -> None:
        self._entries: OrderedDict[int, WorkThreadEntry] = OrderedDict()
        self._max_entries = max_entries

    def register(self, thread_id: int, lines: Mapping[str, str], role: str) -> None:
        """依頼のために作ったスレッドを覚える。"""
        self._entries[thread_id] = WorkThreadEntry(lines, role)
        self._entries.move_to_end(thread_id)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def is_work_thread(self, thread_id: int) -> bool:
        return thread_id in self._entries

    def mark_archived(self, thread_id: int) -> None:
        """ボットが閉じたことを記録する（記録に無いスレッドなら何もしない）。"""
        entry = self._entries.get(thread_id)
        if entry is not None:
            entry.archived = True
            self._entries.move_to_end(thread_id)

    def take_reopened(self, thread_id: int) -> WorkThreadEntry | None:
        """ボットが閉じたスレッドへの最初の書き込みなら記録を返し、以後は返さない。

        判定と記録の更新の間に await を挟まない（同時に届いた2件へ二重に確かめないため）。
        """
        entry = self._entries.get(thread_id)
        if entry is None or not entry.archived:
            return None
        entry.archived = False
        return entry
