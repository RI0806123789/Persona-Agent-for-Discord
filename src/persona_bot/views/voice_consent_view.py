"""音声会話（Gemini Live。#268）の同意パネル。

`/join` で音声会話を始めると、ボイスチャンネルの付属チャット（送れなければ `/join` を実行した
チャンネル）にこのパネルを1通投稿する。ボイスチャンネルの人間の参加者全員が「同意して参加」を
押すまで、音声会話は一時停止する。記憶への保存・親密度の更新は任意で、初期はオフ。

同意は接続している間だけメモリに持ち（`voice_conversation.ConsentState`）、退出で消える。
パネルの本文には人数だけを出し、誰が同意していないかは出さない。

機能パネル（`PanelView`）ではなく、チャンネルに出す1通のメッセージに付ける部品なので
`OwnedView` を継承する（`agent_view.AgentCancelView` と同じ）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable

import discord
from discord import ui

from persona_bot.core.background import TaskOwner
from persona_bot.utils.discord_helpers import reject_blocked_interaction
from persona_bot.views.view_parts import OwnedView

if TYPE_CHECKING:
    from persona_bot.services.voice_conversation import VoiceConversation


logger = logging.getLogger(__name__)

_PANEL_INTRO = (
    "🎙️ **音声会話（Gemini Live）**\n"
    "このボイスチャンネルでは、ボットと声で会話できます。\n"
    "・話した声は Google の Gemini Live へ送られ、会話のために処理されます。"
    "ボットは録音・文字起こしの全文・会話の記録を保存しません"
    "（Google へ送信済みのデータを、すぐに削除できるとは限りません）。\n"
    "・ボイスチャンネルにいる全員が「🎙️ 同意して参加」を押すまで、音声会話は一時停止します。"
    "途中から参加した人も同じです。\n"
    "・「🧠 記憶への保存」「💗 親密度の更新」は任意です（初期はオフ）。"
    "オンにした人の会話のうち、安全と判定されたものだけを保存・反映します。"
    "ほかの参加者がいる場なので、あなたの記憶を会話の中で使うことはありません。\n"
    "・「✋ 同意しない・撤回」でいつでも止められます。撤回すると、それまでの会話の文脈も引き継ぎません。\n"
    "・利用には Google と Discord の規約（年齢の条件を含む）が適用されます。\n"
)


def build_consent_panel_text(conversation: VoiceConversation) -> str:
    """同意パネルの本文（説明と現在の状態）。人数だけを出し、名前は出さない。"""
    if conversation.stopped:
        status = "⏹️ 音声会話は終了しました。"
    elif conversation.is_open:
        status = "🟢 会話中です。話しかけてください。"
    elif conversation.waiting_count:
        status = f"⏸️ 一時停止中（同意待ち: {conversation.waiting_count}人）"
    else:
        status = "⏸️ 一時停止中（ボイスチャンネルに参加者がいません）"
    return f"{_PANEL_INTRO}\n**状態:** {status}"


def _describe_user(conversation: VoiceConversation, user_id: int) -> str:
    consent = conversation.consent
    def on_off(value: bool) -> str:
        return "オン" if value else "オフ"
    return (
        f"あなたの設定: 音声会話 {'同意済み' if user_id in consent.voice else '未同意'}"
        f" / 記憶への保存 {on_off(user_id in consent.memory)}"
        f" / 親密度の更新 {on_off(user_id in consent.affinity)}"
    )


class VoiceConsentView(OwnedView):
    """同意パネルのボタン。押した本人の同意だけを切り替え、結果は本人だけに返す。"""

    # SDK既定の repr 付きログへ渡さない（基底の on_error が使う）。
    _error_logger = logger
    _error_log_message = "音声会話: 同意パネルの操作中にエラーが発生しました。"

    def __init__(
        self, conversation: VoiceConversation, *, is_blocked: Callable[[int], bool],
        task_owner: TaskOwner | None = None,
    ) -> None:
        # 接続している間ずっと押せるようにする（終了時に呼び出し側が stop する）。
        # Discordが直接起動するコールバックにも、作成時の所有者を引き継ぐ（基底が行う）。
        super().__init__(timeout=None, task_owner=task_owner)
        self._conversation = conversation
        self._is_blocked = is_blocked

    async def _run(self, interaction: discord.Interaction, action: Callable[[int], str]) -> None:
        async def invoke() -> None:
            if await reject_blocked_interaction(interaction, self._is_blocked):
                return
            if self._conversation.stopped:
                await interaction.response.send_message("この音声会話は終了しています。", ephemeral=True)
                return
            notice = action(interaction.user.id)
            await interaction.response.send_message(
                f"{notice}\n{_describe_user(self._conversation, interaction.user.id)}", ephemeral=True,
            )

        try:
            await self.run_owned(invoke())
        except discord.HTTPException as error:
            # 応答に失敗しても、受け付けた同意の変更は維持する。
            logger.warning("音声会話: 同意パネルの操作に応答できませんでした。", exc_info=error)

    @ui.button(label="同意して参加", emoji="🎙️", style=discord.ButtonStyle.success, row=0)
    async def grant(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        def action(user_id: int) -> str:
            self._conversation.grant_voice(user_id)
            return "🎙️ 音声会話に同意しました。ボイスチャンネルで話しかけてください。"
        await self._run(interaction, action)

    @ui.button(label="同意しない・撤回", emoji="✋", style=discord.ButtonStyle.secondary, row=0)
    async def withdraw(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        def action(user_id: int) -> str:
            self._conversation.withdraw(user_id)
            return (
                "✋ 音声会話への同意を取り消しました。あなたがボイスチャンネルにいる間は、"
                "音声会話を一時停止します。"
            )
        await self._run(interaction, action)

    @ui.button(label="記憶への保存", emoji="🧠", style=discord.ButtonStyle.secondary, row=1)
    async def memory(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        def action(user_id: int) -> str:
            enabled = self._conversation.toggle_memory(user_id)
            return "🧠 記憶への保存をオンにしました。" if enabled else "🧠 記憶への保存をオフにしました。"
        await self._run(interaction, action)

    @ui.button(label="親密度の更新", emoji="💗", style=discord.ButtonStyle.secondary, row=1)
    async def affinity(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        def action(user_id: int) -> str:
            enabled = self._conversation.toggle_affinity(user_id)
            return "💗 親密度の更新をオンにしました。" if enabled else "💗 親密度の更新をオフにしました。"
        await self._run(interaction, action)
