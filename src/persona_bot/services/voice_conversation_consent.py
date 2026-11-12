"""音声会話の同意の付与・撤回と、同意パネルの更新（`VoiceConversation` の一部。#415）。

`voice_conversation.py` が肥大化したため、責務ごとに切り出したミックスイン。状態は持たず、
`VoiceConversation` のフィールド（`self.consent`・`self.panel_message`・`self._live` など）と
開閉・一時停止（`_update_gate`・`_pause`）を前提にする。動作は分割前と同じ。

★ ログは分割前と同じロガー（`persona_bot.services.voice_conversation`）へ出す。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from persona_bot.config.constants import LIVE_CONSENT_PANEL_UPDATE_SECONDS
from persona_bot.core.background import spawn, wait_for_cleanup

logger = logging.getLogger("persona_bot.services.voice_conversation")

_NO_MENTIONS = discord.AllowedMentions.none()


class VoiceConsentMixin:
    """同意（音声処理・記憶・親密度）の付与と撤回、同意パネルの更新と終了表示。"""

    def grant_voice(self, user_id: int) -> None:
        self.consent.voice.add(user_id)
        self._after_consent_change()

    def withdraw(self, user_id: int) -> None:
        """音声処理への同意を撤回する。記憶・親密度への同意も一緒に外す。"""
        had_consent = user_id in self.consent.voice
        self.consent.voice.discard(user_id)
        self.consent.memory.discard(user_id)
        self.consent.affinity.discard(user_id)
        if had_consent:
            # 撤回した人の発言が残った文脈を使い続けない。撤回した人がボイスチャンネルに
            # いない（ゲートが開いたまま）場合もあるため、開閉を待たずに新しいセッションへ張り替える。
            self._live.forget_context()
            spawn(self._live.restart_fresh(), label="音声会話の文脈の破棄")
            # 張り替えが済むまでの間も、別の人の発話・ペルソナの返答を古い文脈のまま続けない
            # （再接続の完了通知を待たずに、進行中の発話と再生を止める）。
            self._pause()
        if self._speaker_state.active_user == user_id:
            self._end_activity(discard=True)
        if self._current is not None and self._current.user_id == user_id:
            self._discard_response(self._current, "同意の撤回")
        self._speaker_state.participants.pop(user_id, None)
        self._remove_reservation(user_id)
        self._after_consent_change()

    def toggle_memory(self, user_id: int) -> bool:
        return self._toggle(self.consent.memory, user_id)

    def toggle_affinity(self, user_id: int) -> bool:
        return self._toggle(self.consent.affinity, user_id)

    def _toggle(self, target: set[int], user_id: int) -> bool:
        if user_id in target:
            target.discard(user_id)
        else:
            target.add(user_id)
        self._schedule_panel_update()
        return user_id in target

    def _after_consent_change(self) -> None:
        self._update_gate()
        self._schedule_panel_update()

    def _schedule_panel_update(self) -> None:
        if self.panel_message is None or self._stopped:
            return
        task = self._panel_update_task
        if task is not None and not task.done():
            return
        self._panel_update_task = spawn(self._update_panel_later(), label="音声会話の同意パネルの更新")

    async def _update_panel_later(self) -> None:
        delay = self._panel_updated_at + LIVE_CONSENT_PANEL_UPDATE_SECONDS - self._now()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._stopped:
            return
        self._panel_updated_at = self._now()
        await self._edit_panel()

    async def _edit_panel(self, *, view: Any = ...) -> None:
        message = self.panel_message
        if message is None:
            return
        from persona_bot.views.voice_consent_view import build_consent_panel_text

        try:
            if view is ...:
                await message.edit(content=build_consent_panel_text(self), allowed_mentions=_NO_MENTIONS)
            else:
                await message.edit(content=build_consent_panel_text(self), view=view, allowed_mentions=_NO_MENTIONS)
        except discord.DiscordException as error:
            logger.warning("音声会話: 同意パネルを更新できませんでした。", exc_info=error)

    async def _finish_panel(self) -> None:
        if self.panel_view is not None:
            self.panel_view.stop()
        await wait_for_cleanup(self._edit_panel(view=None), label="音声会話の同意パネルの終了")
