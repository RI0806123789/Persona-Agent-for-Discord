"""起動処理から分離したメッセージ処理。サービスは bot.py から注入する。

受付・振り分け・応答の各ステージへは、サービス（このクラスのフィールド）と、型付きの外部操作
（`message_operations.MessageOperations`）だけを渡す。外部操作はこのモジュールの同名の属性から
呼出しのたびに組み立てるので、テストはここを差し替えればよい（#395）。
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sys
from typing import TYPE_CHECKING

import discord

from persona_bot.core.app_state import BotState, GuildSettings, ResponseSettings
from persona_bot.core.chat_admission import ChatAdmission
from persona_bot.core import message_admission, message_routing, message_response
from persona_bot.core.agent_request import confirm_reopened_work_thread, handle_agent_request
from persona_bot.core.message_operations import MessageOperations
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.violation_notice import record_violation_notice
from persona_bot.utils.discord_helpers import REPLY_ALLOWED_MENTIONS
from persona_bot.views.agent_thread import AgentWebhooks, AgentWorkThreads, WorkThreadEntry, start_agent_progress

# ★ 外部操作の差し替え口（MessageOperations のフィールドと同名）。ここで使っていなくても消さないこと。
# 各ステージはこれらを直接 import せず、MessageOperations 経由で呼ぶ。
from persona_bot.audio.voice import play_tts_response  # noqa: F401
from persona_bot.config.paths import load_persona_template  # noqa: F401
from persona_bot.core.background import spawn  # noqa: F401
from persona_bot.utils.discord_helpers import (  # noqa: F401
    fetch_channel_history_text, load_image_attachments, send_long_reply,
)

if TYPE_CHECKING:
    from persona_bot.services.affinity_service import AffinityService
    from persona_bot.services.agent_service import AgentService
    from persona_bot.services.auto_topic_service import AutoTopicService
    from persona_bot.services.autonomous_service import AutonomousService
    from persona_bot.services.blacklist_service import BlacklistService
    from persona_bot.services.content_moderator import ContentModerator
    from persona_bot.services.document_service import DocumentService
    from persona_bot.services.feedback_service import FeedbackService
    from persona_bot.services.gemini_service import GeminiService
    from persona_bot.services.memory_service import MemoryService
    from persona_bot.services.ng_word_service import NgWordFilter
    from persona_bot.services.reminder_service import ReminderService
    from persona_bot.services.stats_service import StatsService
    from persona_bot.utils.usage_graph import UsageTracker


logger = logging.getLogger(__name__)


@dataclass
class MessageHandler:
    """受信時の関門と応答処理を、明示的に渡されたサービスで実行する。"""

    client: discord.Client
    state: BotState
    auto_topic: AutoTopicService
    autonomous: AutonomousService
    blacklist: BlacklistService
    ng_filter: NgWordFilter
    moderator: ContentModerator
    stats: StatsService
    reminders: ReminderService
    documents: DocumentService
    memory: MemoryService
    affinity: AffinityService
    gemini: GeminiService
    usage: UsageTracker
    feedback: FeedbackService
    agent: AgentService

    def __post_init__(self) -> None:
        # 外部操作の不足は、その経路を通るまで待たずに構築時に知らせる（#395）
        self._operations()
        self._chat_admission = ChatAdmission()
        # サブエージェントの作業スレッドへ子が投稿する Webhook（チャンネルごとに使い回す。#267）
        self._agent_webhooks = AgentWebhooks()
        # 作業スレッドの記録（完了後に閉じたスレッドの再開を見分ける。#408）
        self._agent_work_threads = AgentWorkThreads()

    @staticmethod
    def _operations() -> MessageOperations:
        """このモジュールの差し替え口から、ステージへ渡す外部操作を組み立てる。

        ★ 構築時に1回だけ作って持ち回さないこと。テストは MessageHandler を作った後に
        モジュールの属性を差し替える（make_message_handler・patch.object）ため、呼出しのたびに読み直す。
        """
        return MessageOperations.from_namespace(sys.modules[__name__])

    async def _screen_response(self, text, sources, *, log_label: str, screen_sources: bool = False):
        return await message_response._screen_response(
            self, text, sources, log_label=log_label, screen_sources=screen_sources,
        )

    async def notify_violation(self, message: discord.Message, notice: str) -> None:
        """違反を1回記録し、ブロック状況に応じた警告をユーザーに返信する。

        記録の成否はログにも残す。「NGワード検出」のログは記録より前に出るため、
        それだけでは違反が数えられたのか分からない（#173）。
        """
        text, _ = await record_violation_notice(
            self.blacklist, message.author.id, str(message.author), notice,
            blocked_text="以降のメッセージは無視されます。", log=logger,
        )
        if text is not None:
            await self._send_violation_notice(message, text)

    @staticmethod
    async def _send_violation_notice(message: discord.Message, text: str) -> None:
        """違反の保存結果は保持し、通知の送信失敗をイベントへ漏らさない。"""
        try:
            await message.reply(text, allowed_mentions=REPLY_ALLOWED_MENTIONS)
        except discord.DiscordException as error:
            logger.error(f'違反通知: 送信に失敗しました: {type(error).__name__}', exc_info=error)

    async def _reject_ng_input(self, message: discord.Message, question: str, notice: str) -> bool:
        """入力にNGワードがあれば違反として記録し、True を返す（Gemini は呼ばない）。"""
        if not question or not self.ng_filter.contains_ng_word(question):
            return False
        detected = self.ng_filter.find_ng_words(question)
        # 検出した語には非公開の設定や会話が含まれるため、件数だけを記録する。
        logger.info('NGワード検出 (ユーザー入力): %d 件', len(detected))
        await self.notify_violation(message, notice)
        return True

    async def _try_acquire_chat_slot(self, message: discord.Message, guild_id: int | None) -> bool:
        """受付判定と案内枠の記録を、最初の await より前に済ませる。"""
        notice = self._chat_admission.try_acquire(message.author.id, guild_id)
        if notice is None:
            return True
        if self._chat_admission.try_claim_notice(message.author.id):
            try:
                await message.reply(notice, allowed_mentions=discord.AllowedMentions.none())
            except Exception as error:
                logger.error(f'通常会話: 見送り案内の送信に失敗しました: {type(error).__name__}', exc_info=error)
        return False

    async def on_message(self, message: discord.Message) -> None:
        await message_admission.on_message(self, message)

    async def _process_accepted_message(
        self, message: discord.Message, question: str, guild_settings: GuildSettings, *,
        accepts_normally: bool, is_autonomous_comment: bool, image_objects: list | None,
    ) -> None:
        await message_routing._process_accepted_message(
            self, message, question, guild_settings, accepts_normally=accepts_normally,
            is_autonomous_comment=is_autonomous_comment, image_objects=image_objects,
            ops=self._operations(),
        )

    async def _handle_agent_request(
        self, message: discord.Message, question: str, memory_category: str, plan: AgentPlan,
        response_settings: ResponseSettings, *, image_objects: list | None = None,
    ) -> str | None:
        return await handle_agent_request(
            self, message, question, memory_category, plan, response_settings,
            image_objects=image_objects, start_progress=start_agent_progress,
        )

    async def _confirm_reopened_work_thread(
        self, message: discord.Message, question: str, entry: WorkThreadEntry, *, accepts_normally: bool,
    ) -> None:
        await confirm_reopened_work_thread(self, message, question, entry, accepts_normally=accepts_normally)

    async def handle_chat_message(
        self, message: discord.Message, question: str, memory_category: str = "none", *,
        response_settings: ResponseSettings | None = None, delegation_section: str = "",
        delegated_sources: list[dict[str, str]] | None = None, image_objects: list | None = None,
    ) -> str | None:
        """人格役として返信する。読込済み画像は借用し、呼出元が回収する。"""
        return await message_response.handle_chat_message(
            self, message, question, memory_category, response_settings=response_settings,
            delegation_section=delegation_section, delegated_sources=delegated_sources,
            image_objects=image_objects, ops=self._operations(),
        )

    def _track_reply_for_feedback(
        self, sent: discord.Message, user_id: str, response_settings: ResponseSettings, generated_model: str,
    ) -> None:
        message_response._track_reply_for_feedback(self, sent, user_id, response_settings, generated_model)

    async def _remember_reply(self, user_id: str, question: str, response_text: str) -> bool:
        return await message_response._remember_reply(self, user_id, question, response_text)
