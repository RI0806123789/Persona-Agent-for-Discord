"""通常応答の生成・出力検査・評価と記憶への後処理を担当する。

サービスは `MessageHandler`（引数の self）から、外部操作（履歴取得・画像取得・ペルソナの読込・送信・
背景タスク）は `ops` から使う。定数と検査・組み立ての関数は直接 import する（#395）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

from persona_bot.config.constants import GEMINI_RESPONSE_TIMEOUT_SECONDS, HISTORY_LIMIT, IMAGE_ONLY_QUESTION
from persona_bot.core.app_state import ResponseSettings
from persona_bot.core.message_operations import MESSAGE_LOGGER_NAME, MessageOperations
from persona_bot.services.affinity_service import extract_affinity_changes
from persona_bot.services.content_moderator import screen_bot_response_with_status
from persona_bot.services.prompting import build_full_prompt, build_memory_sections
from persona_bot.utils.discord_helpers import (
    REPLY_ALLOWED_MENTIONS, close_image_attachments, describe_supported_attachments,
    format_grounding_sources, mask_grounding_sources,
)

if TYPE_CHECKING:
    from persona_bot.core.message_handler import MessageHandler

logger = logging.getLogger(MESSAGE_LOGGER_NAME)


async def _screen_response(self: MessageHandler, text, sources, *, log_label: str, screen_sources: bool = False):
    """応答を検査し、送信用の出典を付ける。報告は出典も一緒に検査する。"""
    if screen_sources:
        text = self.ng_filter.mask_with_log(text, label=log_label)
        sources = mask_grounding_sources(sources, self.ng_filter)
        formatted = format_grounding_sources(sources)
        _, accepted = await screen_bot_response_with_status(
            text + formatted, None, self.moderator, log_label=log_label,
        )
        return text, sources, text + formatted, accepted
    text, accepted = await screen_bot_response_with_status(
        text, self.ng_filter, self.moderator, log_label=log_label,
    )
    reply = text + format_grounding_sources(sources, self.ng_filter) if accepted and sources else text
    return text, sources, reply, accepted


async def handle_chat_message(
    self: MessageHandler,
    message: discord.Message,
    question: str,
    memory_category: str = "none",
    *,
    response_settings: ResponseSettings | None = None,
    delegation_section: str = "",
    delegated_sources: list[dict[str, str]] | None = None,
    image_objects: list | None = None,
    ops: MessageOperations,
) -> str | None:
    """人格役として返信し、送った本文を返す。

    Args:
        delegation_section: サブエージェントの報告・委任しなかった旨の差し込み（#170）。
        delegated_sources: サブエージェントが検索で参照した出典。渡したときは人格役は検索せず
            （報告の言い直しなので）、この出典を返信に添える。
        image_objects: 読込済み画像の借用。配信終了後の通常経路でも再読込せず、呼出元が閉じる。
    """
    guild_id = message.guild.id if message.guild is not None else None
    guild_settings = await self.state.get_guild_async(guild_id)
    # typing() の開始も待機を伴うため、その前に今回の値を確定する。
    if response_settings is None:
        response_settings = guild_settings.snapshot_response()
    async with message.channel.typing():
        user_id = str(message.author.id)
        owns_images = image_objects is None
        if owns_images:
            image_objects = await ops.load_image_attachments(message.attachments)
        try:
            if not question:
                if not image_objects:
                    # 画像だけの発言で、どの画像も読み込めなかった。質問も画像も無いまま
                    # Gemini を呼ぶと、見えていない画像への反応を作り話で返してしまうため断る。
                    await message.reply(
                        "画像を読み込めませんでした。"
                        f"{describe_supported_attachments(documents=False)}の画像か確認して、もう一度送ってください。",
                        allowed_mentions=REPLY_ALLOWED_MENTIONS,
                    )
                    return None
                question = IMAGE_ONLY_QUESTION

            history_text = await ops.fetch_channel_history_text(
                message.channel,
                guild_settings.channel_reset_points.get(message.channel.id),
                self.client.user,
                HISTORY_LIMIT,
                is_blocked=self.blacklist.is_blocked,
            )
            # 記憶カテゴリは on_message のモデレーション統合判定で既に決まっている
            # （追加の Gemini 呼び出しはしない）。ユーザー別の記憶スペースを読み込む。
            memory_context, document_context = await self.memory.build_contexts(memory_category, user_id)

            try:
                prompt = await ops.load_persona_template(response_settings.prompt_file)
            except FileNotFoundError:
                logger.warning("プロンプトファイルが見つかりません。", exc_info=True)
                await message.reply(
                    "会話設定のファイルが見つかりません。管理者に設定の確認を依頼してください。",
                    allowed_mentions=REPLY_ALLOWED_MENTIONS,
                )
                return None

            full_prompt = build_full_prompt(
                prompt,
                build_memory_sections(memory_category, memory_context, document_context),
                history_text,
                question,
                affinity_prompt=self.affinity.build_dynamic_prompt(str(message.author.id)),
                delegation_section=delegation_section,
            )
            delegated = delegated_sources is not None
            response_text, usage_info, grounding_sources, generated_model = await self.gemini.generate_with_metadata(
                full_prompt,
                response_settings.model_name,
                image_objects,
                timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                grounding=response_settings.grounding_enabled and not delegated,
            )

            if response_text is None:
                await message.reply(
                    "エラーが発生しました。（Gemini APIの呼び出しに失敗、または非対応の画像形式です）",
                    allowed_mentions=REPLY_ALLOWED_MENTIONS,
                )
                return None
            if delegated:
                grounding_sources = delegated_sources

            self.usage.log_snapshot(
                "サブエージェント経由" if delegated else "直接会話",
                question, f"画像添付: {len(image_objects)}枚", full_prompt, response_text, usage_info,
            )

            # タグの抽出だけ先に行い、親密度の更新はモデレーション通過・送信成功後に行う。
            response_text, emotion_changes = extract_affinity_changes(response_text)

            # タグを除去した結果、本文が空になった場合は空メッセージ送信を防ぐ
            # （モデルが感情タグだけを返したケース）。
            if not response_text.strip():
                logger.info('応答本文が空です（感情タグのみ、または空応答）。')
                await message.reply(
                    "申し訳ありません、うまく応答を生成できませんでした。もう一度お試しください。",
                    allowed_mentions=REPLY_ALLOWED_MENTIONS,
                )
                return None

            # 参考リンクは送信用テキストにのみ追記する
            # （長期記憶には URL の列挙はノイズになるため含めない）。
            response_text, _, reply_text, accepted = await self._screen_response(
                response_text, grounding_sources, log_label="Bot応答",
            )

            # 送信の失敗は呼出元へ伝える。
            sent = await ops.send_long_reply(message, reply_text, suppress_embeds=bool(accepted and grounding_sources))
            # ★ ここから先は送信済みの返信に付随する処理。どれが失敗しても、送った本文は
            # 呼出元へ返す（#157）。再生成・再送はしない（二重返信になるため）。
            if accepted and emotion_changes is not None:
                try:
                    v_change, a_change = emotion_changes
                    self.affinity.update_emotion(user_id, v_change, a_change)
                    ops.spawn(self.affinity.save_background(user_id), label="親密度の保存")
                except Exception:
                    logger.warning("親密度の更新に失敗しました（返信は送信済み）。", exc_info=True)
            if accepted:
                self._track_reply_for_feedback(sent, user_id, response_settings, generated_model)
            if accepted and await self._remember_reply(user_id, question, response_text):
                ops.spawn(self.memory.flush_if_needed(), label="記憶圧縮（チャット便乗）")
            return response_text
        finally:
            if owns_images:
                close_image_attachments(image_objects)


def _track_reply_for_feedback(
    self: MessageHandler, sent: discord.Message, user_id: str, response_settings: ResponseSettings,
    generated_model: str,
) -> None:
    """送った返信を 👍👎 の評価対象として登録する（リアクション付与は裏で走る）。

    実際の生成モデルと今回のペルソナを渡し、待機中の設定変更を混ぜない。登録の失敗は
    評価を取りこぼすだけなので、ログに残して記憶へ進む。
    """
    try:
        self.feedback.track_reply(
            sent,
            target_user_id=user_id,
            model_name=generated_model,
            prompt_file=response_settings.prompt_file,
        )
    except Exception as error:
        logger.error(f'評価対象の登録に失敗しました（返信は送信済み）: {type(error).__name__}', exc_info=error)


async def _remember_reply(self: MessageHandler, user_id: str, question: str, response_text: str) -> bool:
    """会話を記憶圧縮前の一時ログへ追記し、成功したかを返す。

    失敗しても返信は送信済みなので、例外を外へ出さずにログへ残す（成功扱いにはしない。
    記憶圧縮も起動しない）。キャンセルは Exception に含まれないため従来どおり伝わる。
    """
    try:
        await self.memory.append_pending_log(user_id, question, response_text)
    except Exception as error:
        logger.error(f'会話記憶: 返信は送信済みですが、記憶への追記に失敗しました（user_id=<ID>）: {type(error).__name__}', exc_info=error)
        return False
    return True
