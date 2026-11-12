"""自律行動の発話（第一声・独り言）とコメントへの応答（`AutonomousService` の一部。#415）。

`autonomous_service.py` が開始・停止の非同期状態機械と発話の生成を同居させて肥大化したため、
生成・検閲・投稿・読み上げを切り出したミックスイン。状態は持たず、`AutonomousService` の
フィールド（`self._gemini`・`self._moderator`・`self._ng_filter`・`self._usage` など）と、
セッション確認（`claim_comment_slot` など）・停止（`stop_session`）を前提にする。動作は分割前と同じ。

設計上の約束（独り言・第一声には AI モデレーションを掛けず、コメントへの応答には掛けること。
長期記憶を渡さないこと）は `autonomous_service.py` の冒頭を参照。

★ ログは分割前と同じロガー（`persona_bot.services.autonomous_service`）へ出す。運用ログの文言を
  管理コンソール側（`privacy.ts` など）が参照するため、ロガー名は変えない。
"""

import logging
from datetime import datetime, timezone
from typing import Any

import discord

from persona_bot.audio.voice import speak_text
from persona_bot.config.constants import (
    AUTONOMOUS_HISTORY_LIMIT,
    AUTONOMOUS_IMAGE_ONLY_COMMENT,
    AUTONOMOUS_OPENING_NOTICE,
    AUTONOMOUS_TIMEOUT_SECONDS,
)
from persona_bot.config.paths import load_persona_template
from persona_bot.core.app_state import AutonomousSession, GuildSettings, ResponseSettings
from persona_bot.core.background import wait_for_cleanup
from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.services.content_moderator import screen_bot_response_with_status
from persona_bot.services.prompting import (
    build_autonomous_monologue_prompt,
    build_autonomous_opening_prompt,
    build_autonomous_reply_prompt,
)
from persona_bot.utils.discord_helpers import (
    channel_display_name,
    close_image_attachments,
    fetch_channel_history_text,
    load_image_attachments,
    send_long_message,
    send_long_reply,
)

logger = logging.getLogger("persona_bot.services.autonomous_service")

# ボットが自分から投稿する文面では通知を一切飛ばさない。独り言は誰かへの返信では
# ないため、メンションらしき文字列が混じっても通知爆撃にしないための保険。
# （コメントへの応答だけは reply でコメント主に通知する。）
_NO_MENTIONS = discord.AllowedMentions.none()


class AutonomousSpeechMixin:
    """第一声・独り言・コメント応答の生成、検閲、投稿、読み上げ。"""

    async def handle_comment(
        self,
        message: discord.Message,
        question: str,
        guild_settings: GuildSettings,
        *,
        claimed_session: AutonomousSession | None = None,
        response_settings: ResponseSettings | None = None,
        image_objects: list | None = None,
    ) -> str | None:
        """配信中のチャンネルに届いたコメントへ、配信者のように応答する。

        送信した本文を返す（`bot.py` がそれを読み上げに回す）。生成に失敗した
        場合は None を返し、呼び出し側は何もしない。

        通常のチャット（`handle_chat_message`）と違い、長期記憶・親密度・
        👍👎 の評価対象登録は行わない。リスナーは「傍観者」であって1対1の
        会話相手ではない、という idea.md の位置づけに合わせている
        （記憶を混ぜない理由はモジュール冒頭の説明を参照）。
        """
        session = guild_settings.autonomous_session
        if session is None:
            return None
        if claimed_session is not None:
            # 入力判定を待っている間に、別の配信へ差し替わった予約は使わない。
            if session is not claimed_session:
                return None

        if response_settings is None:
            response_settings = guild_settings.snapshot_response()
        channel = message.channel
        async with channel.typing():
            # 本文が無く画像だけのコメントは、そのまま渡すと「空のコメントへの返事」を
            # 生成してしまう。通常のチャット経路と同じように、代わりの文を置いたうえで
            # 画像そのものもモデルへ渡す（配信中だけ画像を見なくなるのを避ける）。
            images = image_objects if image_objects is not None else await load_image_attachments(message.attachments)
            try:
                comment_text = question.strip()
                if not comment_text and images:
                    comment_text = AUTONOMOUS_IMAGE_ONLY_COMMENT
                if not comment_text:
                    return None
                if not self._is_current_session(guild_settings, session):
                    return None
                if claimed_session is None and not self._claim_comment_slot(session):
                    # 直接呼ぶ場合も、有効な入力を確認してから枠を取る。
                    return None

                persona_prompt = await self._load_persona(response_settings)
                if persona_prompt is None:
                    return None

                history_text = await fetch_channel_history_text(
                    channel, guild_settings.channel_reset_points.get(channel.id),
                    self._client.user, AUTONOMOUS_HISTORY_LIMIT,
                    is_blocked=self._is_blocked,
                )
                full_prompt = build_autonomous_reply_prompt(
                    persona_prompt,
                    message.author.display_name,
                    comment_text,
                    history_text,
                )
                # コメントは人からの問いかけなので、通常のチャットと同じく
                # そのサーバーの Google 検索設定に従う（独り言と違って調べ物がありうる）。
                response_text = await self._generate(
                    full_prompt,
                    response_settings,
                    label="コメント応答",
                    detail=f"{message.author.display_name} さんのコメント（画像{len(images)}枚）",
                    grounding=response_settings.grounding_enabled,
                    images=images or None,
                )
                if not response_text:
                    return None

                response_text = strip_affinity_tag(response_text)
                if not response_text.strip():
                    logger.warning("自律行動: コメント応答が感情タグのみだったため見送ります。")
                    return None

                # 「ユーザーからのインプットに対しては今まで通りフィルターを使う」（idea.md）
                # ため、自発発言と違いここでは AI モデレーションまで通す。
                response_text, accepted = await screen_bot_response_with_status(
                    response_text, self._ng_filter, self._moderator, log_label="自律行動のコメント応答"
                )
                if not accepted:
                    return None

                # 生成を待っている間に配信が終わっている（/leave など）ことがある。
                # 終わった配信の「配信者としての応答」を後から投稿しないよう、
                # 独り言・第一声と同じく送信の直前で確かめる。
                if not self._is_current_session(guild_settings, session):
                    logger.info("自律行動: 応答の生成中に配信が終了したため、この応答は見送ります。")
                    return None

                await send_long_reply(message, response_text)
                # 応答した直後に独り言が重なると会話がぶつかるため、発話時刻を更新しておく。
                session.last_spoke_at = datetime.now(timezone.utc)
                return response_text
            finally:
                # 事前に検証された画像は呼出元が所有し、未使用時もそちらで回収する。
                if image_objects is None:
                    close_image_attachments(images)

    async def _stop_failed_speech(
        self, guild: discord.Guild, guild_settings: GuildSettings, session: AutonomousSession,
    ) -> None:
        """送信待機中に交代した新しい配信を停止せず、失敗した配信だけを回収する。"""
        if self._is_current_session(guild_settings, session):
            await wait_for_cleanup(
                self.stop_session(guild.id, reason="配信テキストの送信失敗"),
                label="自律行動の送信失敗後の終了",
            )

    async def _speak_opening(
        self,
        guild: discord.Guild,
        guild_settings: GuildSettings,
        session: AutonomousSession,
        channel: Any,
    ) -> None:
        """配信開始の第一声を投稿・発話する。

        生成に失敗しても「会話相手募集中…」の看板（`AUTONOMOUS_OPENING_NOTICE`）
        だけは必ず出す — 人を誘うことがこの機能の目的なので、AI の調子で
        案内そのものが消えては困るため。
        """
        await self._speak_turn(guild, guild_settings, session, channel=channel, opening=True)

    async def _speak_monologue(
        self,
        guild: discord.Guild,
        guild_settings: GuildSettings,
        session: AutonomousSession,
    ) -> None:
        """独り言を1つ生成して投稿・発話する。"""
        await self._speak_turn(guild, guild_settings, session, channel=None, opening=False)

    async def _speak_turn(
        self, guild: discord.Guild, guild_settings: GuildSettings, session: AutonomousSession,
        *, channel: Any | None, opening: bool,
    ) -> None:
        """第一声と独り言の生成・送信・読み上げを同じ手順で行う。"""
        response_settings = guild_settings.snapshot_response()
        label = "第一声" if opening else "独り言"
        try:
            if not opening:
                channel = self._client.get_channel(session.channel_id)
            if channel is None:
                return
            persona_prompt = await self._load_persona(response_settings)
            if persona_prompt is None and not opening:
                return
            speech = ""
            if persona_prompt is not None:
                history_text = await fetch_channel_history_text(
                    channel, guild_settings.channel_reset_points.get(channel.id),
                    self._client.user, AUTONOMOUS_HISTORY_LIMIT, is_blocked=self._is_blocked,
                )
                full_prompt = (
                    build_autonomous_opening_prompt(persona_prompt, getattr(channel, "name", ""), history_text)
                    if opening else build_autonomous_monologue_prompt(persona_prompt, history_text, session.turn_count)
                )
                generated = await self._generate(
                    full_prompt, response_settings, label=label, detail=channel_display_name(channel),
                )
                speech = self._screen_speech(generated, f"自律行動の{label}")
            if not opening and not speech:
                return

            # 生成を待っている間に配信が終わっている（/leave など）ことがある。
            # 終わったチャンネルへ投稿しないよう、送信の直前で確かめる。
            if not self._is_current_session(guild_settings, session):
                return

            try:
                body = f"{AUTONOMOUS_OPENING_NOTICE}\n{speech}" if opening and speech else (
                    AUTONOMOUS_OPENING_NOTICE if opening else speech
                )
                await send_long_message(channel, body, allowed_mentions=_NO_MENTIONS)
            except Exception as error:
                logger.error("自律行動: %sの送信に失敗しました。", label, exc_info=error)
                await self._stop_failed_speech(guild, guild_settings, session)
                return
            if not speech or not self.can_speak_session(guild, guild_settings, session):
                return
            await speak_text(
                guild, speech, response_settings.prompt_file,
                is_valid=lambda: self.can_speak_session(guild, guild_settings, session),
            )
            if not opening:
                session.turn_count += 1
        finally:
            session.busy = False
            session.last_spoke_at = datetime.now(timezone.utc)

    @staticmethod
    def can_speak_session(
        guild: discord.Guild | None, guild_settings: GuildSettings,
        session: AutonomousSession | None,
    ) -> bool:
        """元の配信が続いており、接続先もその配信チャンネルのままか確かめる。"""
        if guild is None or session is None or guild_settings.autonomous_session is not session:
            return False
        voice_client = guild.voice_client
        return bool(
            voice_client is not None and voice_client.is_connected()
            and getattr(voice_client.channel, "id", None) == session.channel_id
        )

    @staticmethod
    def _is_current_session(guild_settings: GuildSettings, session: AutonomousSession) -> bool:
        """その配信がまだ生きているか（別の配信に差し替わっていないか）を確かめる。"""
        return guild_settings.autonomous_session is session

    def _screen_speech(self, text: str | None, label: str) -> str:
        """ボットの自発発言に掛ける後処理（感情タグの除去 → NG ワードのマスク）。

        **AI モデレーションは意図的に掛けない**（idea.md の方針。理由は
        モジュール冒頭の説明を参照）。NG ワードのマスクはローカルの正規表現
        マッチだけで済み、API 呼び出しも遅延も増えないため残している。
        """
        if not text:
            return ""
        cleaned = strip_affinity_tag(text)
        if not cleaned.strip():
            return ""
        return self._ng_filter.mask_with_log(cleaned, label=label)

    async def _generate(
        self,
        full_prompt: str,
        response_settings: ResponseSettings,
        *,
        label: str,
        detail: str,
        grounding: bool = False,
        images: list[Any] | None = None,
    ) -> str | None:
        """Gemini を呼び、生成結果を返す（失敗時は None）。利用ログもここで残す。

        `images` はコメントに添付された画像（自発発言では常に None）。
        """
        response_text, usage_info, _sources = await self._gemini.generate(
            full_prompt,
            response_settings.model_name,
            images=images,
            timeout=AUTONOMOUS_TIMEOUT_SECONDS,
            grounding=grounding,
        )
        if not response_text:
            logger.error("自律行動: 発言の生成に失敗しました。")
            return None
        self._usage.log_snapshot(f"自律行動/{label}", detail, "自動実行", full_prompt, response_text, usage_info)
        return response_text

    async def _load_persona(self, response_settings: ResponseSettings) -> str | None:
        """そのサーバーで選ばれているペルソナのプロンプトを読む（無ければ None）。"""
        try:
            return await load_persona_template(response_settings.prompt_file)
        except FileNotFoundError:
            logger.warning("自律行動: プロンプトファイルが見つかりません。", exc_info=True)
            return None
