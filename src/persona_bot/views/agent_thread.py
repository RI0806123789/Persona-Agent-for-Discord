"""サブエージェントの作業スレッド（#267）。

委任するとき、依頼メッセージに作業用のスレッドを作り、親子のやりとりをそこに残す。

1. 親（ボット自身の名義）がスレッドで子へ委託内容（依頼内容・期待する出力・手順）を伝える。
   このメッセージに中止ボタンを付ける（押せるのは依頼した本人だけ）。
2. 子が Webhook で名前とアイコンを変えて、作業の開始・手順ごとの進捗・最終報告を投稿する。
   同じボット・同じトークンのままで、追加のボット登録は要らない。進捗は書き換えずに1通ずつ残す。
3. 親が終了の定型文をスレッドに残す。完了なら人格役が元のチャンネルへ返信し（`MessageHandler`）、
   失敗・中止なら元のチャンネルにも定型文を返す（人格役の返信が来ないため）。

子の実体はこれまでどおり独立した文脈で動く Gemini の呼び出しで、スレッドは委託と報告の
可視化にすぎない（子に会話履歴や長期記憶を読ませない）。スレッドを作れない場面
（DM・スレッドの中・ボイスのチャット・権限不足・作成の失敗など）では、`agent_view.AgentProgress`
（依頼への返信1通の書き換え）へ切り替える（フェイルオープン）。

- 投稿はすべて `AllowedMentions.none()`。子の発言（定型文と報告）は呼び出し側が NG ワードを伏せてから渡す。
- Webhook はチャンネルごとに1つだけ作って使い回す（`AgentWebhooks`）。依頼のたびに作ると
  チャンネルあたりの上限に達するため。
- スレッドへの投稿の失敗はログに残すだけで、子の作業も人格役の返信も止めない。
"""

from __future__ import annotations

import logging
import asyncio
import re
from typing import Mapping

import discord

from persona_bot.config.constants import (
    AGENT_CHILD_FALLBACK_NAME,
    AGENT_CHILD_NAME_MAX_CHARS,
    AGENT_THREAD_AUTO_ARCHIVE_MINUTES,
    AGENT_THREAD_ENABLED,
    AGENT_THREAD_NAME,
    AGENT_THREAD_NAME_MAX_CHARS,
    AGENT_WEBHOOK_NAME,
    DISCORD_MESSAGE_MAX_LENGTH,
)
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport, render_agent_line
from persona_bot.utils.discord_helpers import format_grounding_sources, split_message
from persona_bot.views.agent_view import AgentProgress, AgentProgressBase


logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()
# スレッドを作り、親子がやりとりするのに要るボットの権限（チャンネル単位で確かめる）。
_THREAD_PERMISSIONS = ("view_channel", "create_public_threads", "send_messages_in_threads", "manage_webhooks")
# Discord の名前の制約。Webhook 名は "discord" / "clyde" を含められず、利用者名と同じく
# @ # : ``` も使えない。記号は全角に置き換え、禁止語は取り除く（役割の名前は LLM が決めるため、
# 「Discord ボットの調査担当」のような名前で投稿が 400 にならないようにする）。
_FORBIDDEN_NAME_WORDS = re.compile(r"discord|clyde", re.IGNORECASE)
_NAME_SYMBOLS = str.maketrans({"@": "＠", "#": "＃", ":": "：", "`": "｀"})
_RESERVED_NAMES = frozenset({"everyone", "here"})


def webhook_username(text: str) -> str:
    """子の表示名を Discord の Webhook 名の制約に合わせる。使えなければ既定の名前にする。"""
    name = " ".join(_FORBIDDEN_NAME_WORDS.sub("", text.translate(_NAME_SYMBOLS)).split())
    name = name[:AGENT_CHILD_NAME_MAX_CHARS].rstrip()
    # 取り除いた結果つながって禁止語ができた場合（"disdiscordcord" など）も既定の名前にする。
    if not name or _FORBIDDEN_NAME_WORDS.search(name) or name.lower() in _RESERVED_NAMES:
        return AGENT_CHILD_FALLBACK_NAME
    return name


def thread_name(role: str) -> str:
    """作業スレッドの名前。Discord の上限で切り、空なら既定の名前にする。"""
    name = " ".join(AGENT_THREAD_NAME.format(role=role).split())[:AGENT_THREAD_NAME_MAX_CHARS].rstrip()
    return name or AGENT_CHILD_FALLBACK_NAME


def build_handoff_text(lines: Mapping[str, str], plan: AgentPlan) -> str:
    """親が子へ作業を頼むメッセージ（定型文 + ルーターが整理した委託内容）。"""
    parts = [
        render_agent_line(lines, "handoff", role=plan.role, total=len(plan.steps)),
        "",
        "**依頼内容**",
        plan.task,
    ]
    if plan.expected_output:
        parts += ["", "**期待する出力**", plan.expected_output]
    parts += ["", "**手順**", *(f"{number}. {step}" for number, step in enumerate(plan.steps, start=1))]
    # 中止ボタンを付けるため1通に収める（各項目は agent_plan の上限で切ってあるので、通常は収まる）。
    return "\n".join(parts)[:DISCORD_MESSAGE_MAX_LENGTH]


def thread_unavailable_reason(message: discord.Message) -> str | None:
    """作業スレッドを使えない理由。使えるなら None（API は呼ばない）。"""
    if not AGENT_THREAD_ENABLED:
        return "AGENT_THREAD_ENABLED が False"
    guild = message.guild
    if guild is None or not isinstance(message.channel, discord.TextChannel):
        # DM・スレッドの中・ボイスやステージのチャットからはスレッドを作れない
        return "スレッドを作れないチャンネル"
    me = getattr(guild, "me", None)
    if me is None:
        return "ボット自身のメンバー情報を取得できない"
    permissions = message.channel.permissions_for(me)
    missing = [name for name in _THREAD_PERMISSIONS if not getattr(permissions, name, False)]
    if missing:
        return f"権限不足: {', '.join(missing)}"
    return None


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


class AgentThreadProgress(AgentProgressBase):
    """作業スレッドでの進捗表示。open（作成・委託）→ update（手順ごと）→ finish の順に使う。"""

    shows_report = True

    def __init__(
        self,
        message: discord.Message,
        lines: Mapping[str, str],
        plan: AgentPlan,
        *,
        thread: discord.Thread,
        webhook: discord.Webhook,
        webhooks: AgentWebhooks,
        avatar_url: str | None,
        bot_user_id: int | None = None,
    ) -> None:
        super().__init__(message.author.id, lines, plan)
        self._source = message
        self._thread = thread
        self._webhook: discord.Webhook | None = webhook
        self._webhooks = webhooks
        # ★ Webhook の再取得は長時間の作業の後に起こる。その時点で guild.me がキャッシュから
        # 外れていると AttributeError になるため、開始時に分かっているボットの ID を保持して使う。
        if bot_user_id is None:
            bot_user_id = getattr(getattr(message.guild, "me", None), "id", None)
        self._bot_user_id = bot_user_id
        self._child_name = webhook_username(self._line("name"))
        self._avatar_url = avatar_url
        self._handoff: discord.Message | None = None

    @classmethod
    async def open(
        cls,
        message: discord.Message,
        lines: Mapping[str, str],
        plan: AgentPlan,
        *,
        webhooks: AgentWebhooks,
        avatar_url: str | None,
    ) -> AgentThreadProgress | None:
        """スレッドを作り、親が委託内容を伝えて子が引き受けるまで進める。

        使えない場面・途中の失敗では None を返す（呼び出し側は返信の進捗表示へ切り替える）。
        Webhook を先に用意するのは、スレッドだけ作って投稿できない状態を避けるため。
        """
        reason = thread_unavailable_reason(message)
        if reason is not None:
            logger.warning("サブエージェント: 作業スレッドを使えないため、返信の進捗表示にします。")
            return None
        bot_user_id = message.guild.me.id
        webhook = await webhooks.get(message.channel, bot_user_id)
        if webhook is None:
            logger.warning("サブエージェント: Webhook を使えないため、返信の進捗表示にします。")
            return None
        try:
            thread = await message.create_thread(
                name=thread_name(plan.role),
                auto_archive_duration=AGENT_THREAD_AUTO_ARCHIVE_MINUTES,
                reason="サブエージェントの作業スレッド",
            )
        except discord.DiscordException as error:
            logger.warning("サブエージェント: 作業スレッドを作れないため、返信の進捗表示にします。", exc_info=error)
            return None
        progress = cls(
            message, lines, plan, thread=thread, webhook=webhook, webhooks=webhooks, avatar_url=avatar_url,
            bot_user_id=bot_user_id,
        )
        try:
            if not await progress._post_handoff():
                return None
            await progress._child_send(progress._line("child_start"))
        except BaseException:
            # 途中で止められたら、押しても誰も待っていない中止ボタンを残さない
            await progress.close()
            raise
        return progress

    async def _post_handoff(self) -> bool:
        """親の名義で委託内容を伝え、中止ボタンを付ける。送れなければ False。"""
        try:
            self._handoff = await self._thread.send(
                build_handoff_text(self._lines, self._plan), view=self._view, allowed_mentions=_NO_MENTIONS,
            )
        except discord.DiscordException as error:
            # 作ったスレッドは残る（消すには別の権限が要り、利用者の目にも触れた後のため）。
            logger.warning("サブエージェント: 作業スレッドへ委託内容を送れないため、返信の進捗表示にします。", exc_info=error)
            self._view.stop()
            return False
        return True

    async def update(self, index: int, step: str) -> None:
        """子が手順の開始を投稿する（書き換えずに1通ずつ残す）。"""
        await self._child_send(self._line("child_progress", index=index, step=step))

    async def finish(self, key: str, report: AgentReport | None = None) -> None:
        """中止ボタンを外し、子の報告（完了時）と親の終了の定型文をスレッドに残す。"""
        self._view.stop()
        await self._remove_cancel_button()
        if key == "done" and report is not None:
            await self._child_send(
                f"{self._line('child_report')}\n\n{report.text}{format_grounding_sources(report.sources)}",
                suppress_embeds=True,
            )
        status = self._line(key)
        await self._parent_send(status)
        if key != "done":
            # 人格役の返信が来ないので、依頼したチャンネルでも結果を伝える
            try:
                await self._source.reply(status, allowed_mentions=_NO_MENTIONS)
            except discord.DiscordException as error:
                logger.warning("サブエージェント: 終了の案内を送れませんでした。", exc_info=error)

    async def _remove_cancel_button(self) -> None:
        if self._handoff is None:
            return
        try:
            await self._handoff.edit(view=None)
        except discord.DiscordException as error:
            logger.warning("サブエージェント: 中止ボタンを外せませんでした。", exc_info=error)

    async def _parent_send(self, content: str) -> None:
        try:
            await self._thread.send(content, allowed_mentions=_NO_MENTIONS)
        except discord.DiscordException as error:
            logger.error("サブエージェント: 作業スレッドへ投稿できませんでした。", exc_info=error)

    async def _child_send(self, content: str, *, suppress_embeds: bool = False) -> None:
        """子の名前とアイコンで投稿する。長い報告は分割する。失敗しても作業は止めない。"""
        retried = False
        for chunk in split_message(content):
            while self._webhook is not None:
                try:
                    await self._webhook.send(
                        chunk,
                        username=self._child_name,
                        avatar_url=self._avatar_url,
                        thread=self._thread,
                        allowed_mentions=_NO_MENTIONS,
                        suppress_embeds=suppress_embeds,
                        wait=True,
                    )
                    break
                except discord.NotFound as error:
                    self._webhooks.forget(self._source.channel.id, self._webhook)
                    self._webhook = None
                    if retried:
                        logger.warning("サブエージェント: 再取得した Webhook でも投稿できませんでした。", exc_info=error)
                        return
                    logger.warning("サブエージェント: Webhook が見つからないため、再取得して投稿をやり直します。", exc_info=error)
                    retried = True
                    if self._bot_user_id is None:
                        logger.warning("サブエージェント: ボット自身の ID が分からないため、Webhook を再取得できません。")
                        return
                    self._webhook = await self._webhooks.get(self._source.channel, self._bot_user_id)
                except discord.DiscordException as error:
                    logger.warning("サブエージェント: 子の投稿を送れませんでした。", exc_info=error)
                    return
            else:
                return


async def start_agent_progress(
    message: discord.Message,
    lines: Mapping[str, str],
    plan: AgentPlan,
    *,
    webhooks: AgentWebhooks,
    avatar_url: str | None,
) -> AgentProgressBase:
    """作業スレッドを優先し、使えなければ返信1通の進捗表示で始める。"""
    progress = await AgentThreadProgress.open(message, lines, plan, webhooks=webhooks, avatar_url=avatar_url)
    if progress is not None:
        return progress
    fallback = AgentProgress(message, lines, plan)
    try:
        await fallback.start()
    except BaseException:
        await fallback.close()
        raise
    return fallback
