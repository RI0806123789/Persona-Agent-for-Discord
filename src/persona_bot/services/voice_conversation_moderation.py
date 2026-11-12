"""音声会話の入出力のモデレーション・違反記録・記憶と親密度への反映（`VoiceConversation` の一部。#415）。

`voice_conversation.py` が肥大化したため、責務ごとに切り出したミックスイン。状態は持たず、
`VoiceConversation` のフィールド（`self._service`・`self.consent`・`self._stopped` など）と
再生制御（`_discard_response`）を前提にする。動作は分割前と同じ。

★ ログは分割前と同じロガー（`persona_bot.services.voice_conversation`）へ出す。運用ログの
  文言を管理コンソール側（`privacy.ts` など）が参照するため、ロガー名は変えない。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from persona_bot.config.constants import (
    LIVE_INPUT_TRANSCRIPT_MAX_WAIT_SECONDS,
    LIVE_INPUT_TRANSCRIPT_SETTLE_SECONDS,
    LIVE_TICK_SECONDS,
)
from persona_bot.core.background import spawn
from persona_bot.services.content_moderator import VERDICT_SAFE, VERDICT_UNSAFE
from persona_bot.services.violation_notice import record_violation_notice
from persona_bot.services.voice_conversation_state import _Utterance

logger = logging.getLogger("persona_bot.services.voice_conversation")

_NO_MENTIONS = discord.AllowedMentions.none()
_NOTICE_INPUT_VIOLATION = "不適切な発言が検出されたため、応答を停止しました。"


class VoiceModerationMixin:
    """入力・出力の AI モデレーション、違反の記録と通知、記憶・親密度への反映。"""

    def _reject_input(self, utterance: _Utterance, reason: str) -> None:
        self._discard_response(utterance, reason)
        if utterance.violation_recorded:
            return
        # 同じ発言の違反は1回だけ数える（文字起こしは少しずつ届くため）。
        utterance.violation_recorded = True
        spawn(self._record_violation(utterance.user_id), label="音声会話の違反記録")

    async def _record_violation(self, user_id: int) -> None:
        member = self.guild.get_member(user_id)
        name = str(member) if member is not None else str(user_id)
        text, just_blocked = await record_violation_notice(
            self._service.blacklist, user_id, name, _NOTICE_INPUT_VIOLATION,
            blocked_text="以降の音声は処理されません。", log=logger,
        )
        if text is not None:
            await self._notify(text, member)
        if just_blocked:
            self._update_gate()

    async def _judge_input(self, utterance: _Utterance) -> None:
        """話し終わった発言の文字起こしが出そろうのを待って、AI モデレーションに掛ける。"""
        waited = 0.0
        while not self._stopped and not utterance.discarded and waited < LIVE_INPUT_TRANSCRIPT_MAX_WAIT_SECONDS:
            quiet = self._now() - utterance.input_updated_at
            if utterance.transcript_closed or (utterance.input_text and quiet >= LIVE_INPUT_TRANSCRIPT_SETTLE_SECONDS):
                break
            await asyncio.sleep(LIVE_TICK_SECONDS)
            waited += LIVE_TICK_SECONDS
        while not self._stopped and not utterance.discarded:
            text = utterance.input_text
            verdict = await self._service.moderator.judge_voice_input(text)
            utterance.input_verdict = verdict
            utterance.judged_input = text
            if verdict == VERDICT_UNSAFE:
                if not self._stopped:
                    self._reject_input(utterance, "入力の AI モデレーション")
                return
            if utterance.input_text == text:
                return
            # 判定を待つ間に文字起こしが増えた。増えた分も含めて判定し直す。

    async def _judge_output(self, utterance: _Utterance) -> None:
        """返答を AI モデレーションに掛け（感情の推定を相乗り）、条件を満たせば記憶・親密度へ反映する。"""
        verdict, emotion = await self._service.moderator.judge_voice_turn(
            utterance.input_text, utterance.output_text,
        )
        utterance.output_verdict = verdict
        utterance.emotion = emotion
        if verdict == VERDICT_UNSAFE:
            # 返答の生成は再生より速いため、判定の時点で残っている未再生の分を止められる。
            self._discard_response(utterance, "出力の AI モデレーション")
        # 遅れて届いた文字起こしで入力の判定がやり直される（タスクが差し替わる）ことがある。
        while utterance.input_task is not None and not utterance.input_task.done():
            await asyncio.gather(utterance.input_task, return_exceptions=True)
        if not self._stopped:
            await self._remember(utterance)

    async def _remember(self, utterance: _Utterance) -> None:
        if (
            utterance.discarded or utterance.output_ng or utterance.violation_recorded
            or utterance.transcript_incomplete or utterance.cut_off
            or utterance.input_verdict != VERDICT_SAFE or utterance.output_verdict != VERDICT_SAFE
            or utterance.judged_input != utterance.input_text
        ):
            # 判定不能・違反・割り込み（打ち切られた返答を含む）・文字起こしが欠けた・判定していない本文を
            # 含む発言は、記憶・親密度の更新に使わない。
            return
        user_id = utterance.user_id
        if user_id not in self.consent.voice or self._service.is_blocked(user_id):
            return
        key = str(user_id)
        if user_id in self.consent.memory:
            try:
                await self._service.memory.append_pending_log(key, utterance.input_text, utterance.output_text)
                spawn(self._service.memory.flush_if_needed(), label="音声会話の記憶圧縮")
            except Exception as error:
                logger.error("音声会話: 記憶への追記に失敗しました。", exc_info=error)
        if user_id in self.consent.affinity and utterance.emotion is not None:
            try:
                self._service.affinity.update_emotion(key, *utterance.emotion)
                spawn(self._service.affinity.save_background(key), label="音声会話の親密度の保存")
            except Exception as error:
                logger.error("音声会話: 親密度の更新に失敗しました。", exc_info=error)

    async def _notify(self, text: str, member: Any = None) -> None:
        channel = self.notice_channel
        if channel is None:
            return
        # 通知してよいのは対象の本人だけ（AllowedMentions の既定は @everyone・ロールも許可するため明示する）。
        mentions = (
            discord.AllowedMentions(everyone=False, roles=False, users=[member], replied_user=False)
            if member is not None else _NO_MENTIONS
        )
        content = f"{member.mention} {text}" if member is not None else text
        try:
            await channel.send(content, allowed_mentions=mentions)
        except discord.DiscordException as error:
            logger.error("音声会話: 案内を送信できませんでした。", exc_info=error)
