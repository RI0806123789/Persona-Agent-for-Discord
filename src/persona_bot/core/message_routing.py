"""受付済みメッセージの配信・添付・リマインダー・委任を振り分ける。

サービスは `MessageHandler`（引数の self）から、外部操作（履歴取得・画像取得・読み上げ・背景タスク）は
`ops` から使う。定数と判定・組み立ての関数は直接 import する（#395）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from persona_bot.config.constants import (
    AGENT_ROUTER_HISTORY_LIMIT, DOCUMENT_QUEUE_FULL_NOTICE, MAX_DOCUMENT_SIZE_BYTES,
)
from persona_bot.core.app_state import GuildSettings
from persona_bot.core.message_operations import MESSAGE_LOGGER_NAME, MessageOperations
from persona_bot.services.agent_plan import AgentPlan, requests_subagent
from persona_bot.services.prompting import build_agent_declined_section
from persona_bot.utils.discord_helpers import (
    REPLY_ALLOWED_MENTIONS, attachment_is_image, close_image_attachments, describe_supported_attachments,
    is_supported_document_attachment,
)

if TYPE_CHECKING:
    from persona_bot.core.message_handler import MessageHandler

logger = logging.getLogger(MESSAGE_LOGGER_NAME)


async def _process_accepted_message(
    self: MessageHandler, message: discord.Message, question: str, guild_settings: GuildSettings, *,
    accepts_normally: bool, is_autonomous_comment: bool, image_objects: list | None,
    ops: MessageOperations,
) -> None:
    guild_id = message.guild.id if message.guild is not None else None
    is_dm = isinstance(message.channel, discord.DMChannel)
    # コメントの受付枠は画像のダウンロード・デコードとGeminiを呼ぶ前に確保する。
    # 見送る分も活動観測は上の mark_activity で済ませている。
    claimed_comment_session = None
    if is_autonomous_comment and self.autonomous.is_comment_channel(message.channel.id, guild_id):
        claimed_comment_session = self.autonomous.claim_comment_slot(message.channel.id, guild_id)
        if claimed_comment_session is None and self.autonomous.is_comment_channel(message.channel.id, guild_id):
            return

    normal_slot_acquired = False
    if claimed_comment_session is None:
        # 入口ではコメントでも、受付前に配信が終わった場合は通常の委任判定を使う。
        is_autonomous_comment = False
        if not accepts_normally:
            return
        normal_slot_acquired = await self._try_acquire_chat_slot(message, guild_id)
        if not normal_slot_acquired:
            return
    owns_images = False
    try:
        if is_autonomous_comment and not question and image_objects is None:
            image_objects = await ops.load_image_attachments(message.attachments)
            owns_images = True
            if not image_objects:
                # 画像確認中に配信が終わったら、通常の文書受付・画像失敗案内へ進める。
                if not accepts_normally or self.autonomous.is_comment_channel(message.channel.id, guild_id):
                    return
        # 安全判定・記憶カテゴリ・リマインダー依頼を1回のAPI呼び出しで取得する。
        # memory_categoryは通常チャットの記憶参照へ、reminder_requestは添付の振り分け後の登録へ渡す。
        memory_category = "none"
        reminder_request: dict[str, str] | None = None
        agent_plan: AgentPlan | None = None
        if question:
            if not is_autonomous_comment and requests_subagent(question):
                # サブエージェントの使用が明記された発言だけ、同じ1回の判定に委任の判定を足す（#170）。
                # 「さっきの件」のような依頼を子へ渡せる形に整理させるため、直近の会話も渡す。
                router_history = await ops.fetch_channel_history_text(
                    message.channel,
                    guild_settings.channel_reset_points.get(message.channel.id),
                    self.client.user,
                    AGENT_ROUTER_HISTORY_LIMIT,
                    is_blocked=self.blacklist.is_blocked,
                )
                is_safe, _reason, memory_category, reminder_request, agent_plan = (
                    await self.moderator.check_route_and_plan(question, router_history)
                )
            else:
                is_safe, _reason, memory_category, reminder_request = await self.moderator.check_and_route(question)
            if not is_safe:
                await self.notify_violation(message, "不適切な内容が検出されたため、メッセージを処理できません。")
                return

        self.state.touch()

        # モデレーション後の配信状態を取り直し、通常チャットの副作用より先に振り分ける。
        if self.autonomous.is_comment_channel(message.channel.id, guild_id):
            comment_session = guild_settings.autonomous_session
            response_settings = guild_settings.snapshot_response()
            response_text = await self.autonomous.handle_comment(
                message, question, guild_settings, claimed_session=claimed_comment_session,
                response_settings=response_settings, image_objects=image_objects,
            )
            if response_text and self.autonomous.can_speak_session(message.guild, guild_settings, comment_session):
                await ops.play_tts_response(
                    message, response_text, response_settings.prompt_file,
                    is_valid=lambda: self.autonomous.can_speak_session(message.guild, guild_settings, comment_session),
                )
            return
        if not accepts_normally:
            logger.info('自律行動: 配信の終了と入れ違いに届いた発言のため、応答しません。')
            return

        # 配信終了後に通常会話へ移る場合も、通常の受付制限を通す。
        if not normal_slot_acquired:
            normal_slot_acquired = await self._try_acquire_chat_slot(message, guild_id)
            if not normal_slot_acquired:
                return

        # 配信の再確認と通常枠の確保が済んだ発言だけ、話題提供の監視へ登録する。
        # 入力判定中に配信が終わった場合も対象にし、配信中のコメントとDMは登録しない。
        if not is_dm:
            self.auto_topic.watch_channel(message.channel.id, guild_id)

        document_attachments = [
            attachment
            for attachment in message.attachments
            if is_supported_document_attachment(attachment)
        ]
        unsupported_attachments = [
            attachment
            for attachment in message.attachments
            if not is_supported_document_attachment(attachment) and not attachment_is_image(attachment)
        ]

        if unsupported_attachments:
            await message.reply(
                f"未対応の添付ファイルが含まれています。{describe_supported_attachments()}に対応しています。",
                allowed_mentions=REPLY_ALLOWED_MENTIONS,
            )
            return

        if document_attachments:
            for attachment in document_attachments:
                if attachment.size > MAX_DOCUMENT_SIZE_BYTES:
                    await message.reply(
                        f"ファイルサイズが上限({MAX_DOCUMENT_SIZE_BYTES // (1024 * 1024)}MB)を超えています。",
                        allowed_mentions=REPLY_ALLOWED_MENTIONS,
                    )
                    return

            # 未処理件数の上限で受け付けなかった文書には、受付完了を伝えない（#202）。
            if await self.documents.enqueue_from_message(message, question, document_attachments) is None:
                await message.reply(DOCUMENT_QUEUE_FULL_NOTICE, allowed_mentions=REPLY_ALLOWED_MENTIONS)
                return
            await message.reply("受付が完了しました。順番に処理しています…", allowed_mentions=REPLY_ALLOWED_MENTIONS)
            return

        # --- 会話統計の記録（ボットへの発言のみを集計。失敗してもチャット本体は止めない） ---
        ops.spawn(
            self.stats.record(
                str(message.author.id),
                message.author.display_name,
                question,
                message.created_at,
            ),
            label="会話統計の記録",
        )

        # --- リマインダー依頼（モデレーションと同じ判定で抽出済み） ---
        # 添付ファイルの処理より後に置く: 文書を添えた依頼では文書処理を優先する。
        # 登録できたら確認だけを返し、通常のチャット応答は行わない
        # （同じ発言に対して確認とチャット返信の二重投稿になるのを避けるため）。
        # 日時が過去・解釈不能などで登録を見送った場合は None が返るので、
        # 依頼ではない発言を誤って拾ったものとして通常の会話へ流す。
        if reminder_request is not None:
            confirmation = await self.reminders.create_from_request(message, reminder_request)
            if confirmation:
                confirmation = self.ng_filter.mask_with_log(confirmation, label="リマインダー登録確認")
                await message.reply(confirmation, allowed_mentions=REPLY_ALLOWED_MENTIONS)
                return

        response_settings = guild_settings.snapshot_response()
        # テキストの返信は読み上げない（#268）。/join のボイスチャンネルでは Gemini Live が
        # 声で会話するため、テキストの返信まで読み上げると声が重なる。読み上げ（TTS）は
        # 自律行動の配信（上の配信コメントへの応答）だけで使う。
        if agent_plan is not None and agent_plan.delegate:
            await self._handle_agent_request(
                message, question, memory_category, agent_plan, response_settings,
                image_objects=image_objects,
            )
        else:
            # サブエージェントを求められたが不要と判断した場合は、人格役がその旨を一言添えて直接答える。
            await self.handle_chat_message(
                message, question, memory_category, response_settings=response_settings,
                image_objects=image_objects,
                delegation_section=build_agent_declined_section() if agent_plan is not None else "",
            )
    finally:
        if normal_slot_acquired:
            self._chat_admission.release(guild_id)
        if owns_images:
            close_image_attachments(image_objects)
