"""サーバーごとの音声会話を管理するサービス（#470。`voice_conversation.py` から分離）。

同時実行の上限（開始の予約票）・開始（Live への接続と同意パネルの投稿）・終了・参加者の出入りの
振り分けを担う。1つのボイスチャンネルの会話そのもの（受信・VAD・Live への送信・再生）は
`voice_conversation.VoiceConversation` にある。

★ `voice_conversation` からは従来の名前（`VoiceConversationService`）で import できる（循環を避けるため
遅延で再公開している）。ログは分割前と同じロガー名 `persona_bot.services.voice_conversation` へ出す。
ペルソナの読込・送信権限の確認・同意パネルの投稿は、テストが差し替える名前をこのモジュールに持つ
（`load_persona_template` / `current_persona_catalog` / `can_bot_send_messages`）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import discord

from persona_bot.audio.voice_receive import LiveVoiceClient, VoiceReceiver
from persona_bot.config.constants import LIVE_MAX_CONCURRENT_SESSIONS
from persona_bot.config.paths import load_persona_template
from persona_bot.config.personas import current_persona_catalog
from persona_bot.core.background import wait_for_cleanup
from persona_bot.services.live_session import CLOSED_QUOTA, LiveOptions, LiveUnavailableError
from persona_bot.services.prompting import build_live_system_instruction
from persona_bot.services.voice_conversation import ConsentPanelError, VoiceConversation
from persona_bot.services.voice_guard import release_and_disconnect
from persona_bot.utils.discord_helpers import NO_MENTIONS, can_bot_send_messages

logger = logging.getLogger("persona_bot.services.voice_conversation")


class VoiceConversationService:
    """サーバーごとの音声会話を管理する（同時実行の上限・開始・終了・参加者の出入り）。"""

    def __init__(
        self,
        client: discord.Client,
        genai_client: Any,
        *,
        moderator: Any,
        ng_filter: Any,
        blacklist: Any,
        memory: Any,
        affinity: Any,
        usage: Any,
        voice_guard: Any = None,
        live_connect: Callable[..., Any] | None = None,
        receiver_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._client = client
        self.genai_client = genai_client
        self.moderator = moderator
        self.ng_filter = ng_filter
        self.blacklist = blacklist
        self.memory = memory
        self.affinity = affinity
        self.usage = usage
        self.voice_guard = voice_guard
        self.live_connect = live_connect
        # 受信の生成口: (voice_client, sink, echo_reference=参照信号または None) -> 受信（start/stop を持つ）。
        self.receiver_factory = receiver_factory or (
            lambda voice_client, sink, echo_reference=None: VoiceReceiver(
                voice_client, sink, echo_reference=echo_reference,
            )
        )
        self._sessions: dict[int, VoiceConversation] = {}
        self._reserved: dict[int, object] = {}

    def is_blocked(self, user_id: int) -> bool:
        return self.blacklist.is_blocked(user_id)

    def get(self, guild_id: int) -> VoiceConversation | None:
        return self._sessions.get(guild_id)

    def reserve(self, guild_id: int) -> object | None:
        """開始の予約票を返す。上限なら None（判定と確保の間に await を挟まない）。"""
        if guild_id in self._reserved:
            return self._reserved[guild_id]
        # 置換中も枠を保持し、既存会話と予約は同じサーバーなら二重に数えない。
        if (guild_id not in self._sessions
                and len(self._sessions.keys() | self._reserved.keys()) >= LIVE_MAX_CONCURRENT_SESSIONS):
            return None
        reservation = self._reserved[guild_id] = object()
        return reservation

    def release_reservation(self, guild_id: int, *, reservation: object | None = None) -> None:
        # 古い開始処理の finally で、停止後に取り直した予約を消さない。
        if reservation is None or self._reserved.get(guild_id) is reservation:
            self._reserved.pop(guild_id, None)

    async def start(
        self,
        guild: discord.Guild,
        voice_client: LiveVoiceClient,
        voice_channel: Any,
        response_settings: Any,
        *,
        fallback_channel: Any = None,
        reservation: object | None = None,
    ) -> VoiceConversation:
        """音声会話を始める。先行する接続・設定読込があれば reserve() の予約票を渡す。

        Raises:
            LiveUnavailableError: Live へ接続できなかった。
            ConsentPanelError: 同意パネルを出せなかった（同意を集められないので始めない）。
            asyncio.CancelledError: 待機中に予約が取り消された。
        """
        reservation = reservation if reservation is not None else self.reserve(guild.id)
        if reservation is None:
            raise LiveUnavailableError(quota=False)

        def check_reservation() -> None:
            if self._reserved.get(guild.id) is not reservation:
                # /leave・サーバー退出後は、待機から戻っても会話を復活させない。
                raise asyncio.CancelledError

        try:
            check_reservation()
            # 予約票が枠を保持するので、旧会話の所有権は停止を待つ前に外す。
            # 遅れて来た旧会話の end() が置換用の予約と接続を片付けないようにする。
            existing = self._sessions.pop(guild.id, None)
            if existing is not None:
                await wait_for_cleanup(existing.stop(), label="音声会話の置換前の終了")
                check_reservation()
            persona_prompt = await load_persona_template(response_settings.prompt_file)
            check_reservation()
            voice_name = str(current_persona_catalog().voice_profile(response_settings.prompt_file).get("live_voice") or "")
            options = LiveOptions(
                system_instruction=build_live_system_instruction(
                    persona_prompt, grounding=response_settings.grounding_enabled,
                ),
                voice_name=voice_name,
                grounding=response_settings.grounding_enabled,
            )
            notice_channel = (
                voice_channel if can_bot_send_messages(voice_channel, guild.me) else fallback_channel
            )
            conversation = VoiceConversation(
                self, guild, voice_client, voice_channel.id, notice_channel, options,
            )
            try:
                await conversation.start()
                check_reservation()
                # 同意パネルを出せなければ誰も同意できず、Live の接続と同時実行の枠だけが残る。
                # パネルの投稿までを開始の成功の条件にする（登録はその後）。
                await self._post_panel(conversation)
                check_reservation()
                if conversation.live_closed_reason is not None:
                    # パネルの投稿を待つ間に Live が終わった。未登録のため end() は何もしないので、
                    # 声に応答しない会話を登録せず、ここで開始を失敗にする。
                    raise LiveUnavailableError(quota=conversation.live_closed_reason == CLOSED_QUOTA)
                if conversation.end_requested:
                    # パネルの投稿を待つ間にボイス接続が切れた（切断・外部からの移動）。見回りが予約した
                    # end() も未登録のため何もしないので、切断済みの接続の会話を登録せず、ここで開始を失敗にする
                    # （登録すると end_requested のまま二度と終了が起動せず、Live の接続と枠が残る。#476）。
                    raise LiveUnavailableError(quota=False)
            except BaseException:
                await wait_for_cleanup(conversation.stop(), label="音声会話の開始失敗の後片付け")
                raise
            self._sessions[guild.id] = conversation
        finally:
            self.release_reservation(guild.id, reservation=reservation)
        logger.info("音声会話: 開始しました。")
        return conversation

    async def _post_panel(self, conversation: VoiceConversation) -> None:
        from persona_bot.views.voice_consent_view import VoiceConsentView, build_consent_panel_text

        channel = conversation.notice_channel
        if channel is None:
            logger.warning("音声会話: 同意パネルを出せるチャンネルがありません。")
            raise ConsentPanelError
        view = VoiceConsentView(conversation, is_blocked=self.is_blocked)
        try:
            conversation.panel_message = await channel.send(
                build_consent_panel_text(conversation), view=view, allowed_mentions=NO_MENTIONS,
            )
        except discord.DiscordException as error:
            view.stop()
            logger.error("音声会話: 同意パネルを送信できませんでした。", exc_info=error)
            raise ConsentPanelError from error
        conversation.panel_view = view

    async def stop(self, guild_id: int, *, reason: str) -> bool:
        """そのサーバーの音声会話を止める（切断は呼び出し側）。止めたら True。"""
        conversation = self._sessions.pop(guild_id, None)
        self.release_reservation(guild_id)
        if conversation is None:
            return False
        logger.info("音声会話: 終了します（%s）。", reason)
        await wait_for_cleanup(conversation.stop(), label="音声会話の終了")
        return True

    async def end(
        self, guild_id: int, *, reason: str, notice: str | None = None, disconnect: bool,
        conversation: VoiceConversation | None = None,
    ) -> None:
        """会話側の事情（Live の終了・接続の喪失）で止める。必要なら案内して退出する。"""
        current = self._sessions.get(guild_id)
        if conversation is not None and current is not conversation:
            # 既に /leave・/join で入れ替わった会話には触らない。
            return
        if current is None:
            return
        voice_client = current.voice_client
        await self.stop(guild_id, reason=reason)
        if guild_id in self._sessions or guild_id in self._reserved:
            # 終了を待つ間に /join が同じ接続で新しい会話を始めた（始めようとしている）。
            # 新しい会話の接続には触らず、退出の案内も出さない。
            return
        if notice:
            await current._notify(notice)
        if guild_id in self._sessions or guild_id in self._reserved:
            # 案内の送信中にも /join は始まる。保護解除・切断の直前に所有権を再確認する。
            return
        guild = current.guild
        if disconnect and guild.voice_client is voice_client and voice_client.is_connected():
            await release_and_disconnect(
                self.voice_guard, guild_id, voice_client, label="音声会話の終了時の切断",
                failure_message="音声会話: 終了時の切断に失敗しました。", log=logger,
            )

    async def stop_all(self) -> None:
        # まだ _sessions に登録されていない開始待ちも無効にする。
        self._reserved.clear()
        for guild_id in list(self._sessions):
            await self.stop(guild_id, reason="ボットの終了")

    def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """参加者の出入りを、そのサーバーの音声会話へ伝える（同期関数。届いた順に処理する）。"""
        guild = getattr(member, "guild", None)
        conversation = self._sessions.get(getattr(guild, "id", None))
        if conversation is None:
            return
        bot_user = self._client.user
        if bot_user is not None and member.id == bot_user.id:
            # 接続の終了・戻しは見回り（tick）と voice_guard が扱う。ただし別のチャンネルへ
            # 移された瞬間から、移動先の人の声を扱わないよう、ゲートだけはすぐ更新する。
            conversation.handle_self_update()
            return
        conversation.handle_member_update(member, before, after)
