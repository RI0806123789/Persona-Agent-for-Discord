"""`/join`・`/leave` のサーバー別操作世代・応答保留・直列化コーディネータ（#495）。

`voice_commands.py` から、Discord の VoiceClient や各サービスに依存しない直列化の制御だけを分けた。
`voice_commands` からも従来の名前（`VoiceCommandCoordinator`・`skip_superseded_voice_operation`）で再公開する。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import discord


@dataclass
class _GuildOperations:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tokens: list[object] = field(default_factory=list)
    # 応答待ち以外の有効なtokenは、ロック待ちまたは実行中。
    deferring: set[object] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending: int = 0

    @property
    def latest(self) -> object | None:
        return self.tokens[-1] if self.tokens else None

    def notify_changed(self) -> None:
        # 待機者のEventを起こしてから差し替え、確認と待機の間の通知も取りこぼさない。
        self.changed.set()
        self.changed = asyncio.Event()


async def skip_superseded_voice_operation(
    interaction: discord.Interaction, is_current: Callable[[], bool],
) -> bool:
    """後続の操作が受け付けられていれば、古い操作の接続変更を見送る。"""
    if is_current():
        return False
    await interaction.followup.send("後から受け付けた参加・退出操作を優先しました。")
    return True


def _resolve_skip_superseded() -> Callable[[discord.Interaction, Callable[[], bool]], Awaitable[bool]]:
    """テストが `voice_commands.skip_superseded_voice_operation` を差し替えた場合も反映する。"""
    from persona_bot.core import voice_commands

    return voice_commands.skip_superseded_voice_operation


class VoiceCommandCoordinator:
    """応答待ちから操作完了まで、サーバー単位でロックと操作世代を共有する。"""

    def __init__(self) -> None:
        self._guilds: dict[int | None, _GuildOperations] = {}

    async def run(
        self, interaction: discord.Interaction,
        change: Callable[[Callable[[], bool]], Awaitable[None]],
    ) -> None:
        guild_id = interaction.guild.id if interaction.guild is not None else None
        operations = self._guilds.setdefault(guild_id, _GuildOperations())
        token = object()
        # deferより前に同期的に記録する。応答待ちの古い参加も後続の退出から識別できる。
        operations.tokens.append(token)
        operations.deferring.add(token)
        operations.pending += 1
        operations.notify_changed()

        def is_current() -> bool:
            return operations.latest is token

        try:
            await interaction.response.defer()
            operations.deferring.discard(token)
            operations.notify_changed()
            notice_sent = False
            notice_error: Exception | None = None
            while True:
                changed = operations.changed
                async with operations.lock:
                    superseded = token not in operations.tokens
                    if not superseded and is_current():
                        # 実行を始めた操作より前の要求は、後続の脱落でも復活させない。
                        operations.tokens[:] = [token]
                        operations.notify_changed()
                        await change(is_current)
                        return
                    newer = (
                        operations.tokens[operations.tokens.index(token) + 1:]
                        if not superseded else []
                    )
                    can_notify = not any(item in operations.deferring for item in newer)

                if can_notify and not notice_sent:
                    try:
                        notice_sent = await _resolve_skip_superseded()(interaction, is_current)
                    except Exception as error:
                        # 通知の失敗でも、後続が実行を始めるまでは元の要求を残す。
                        notice_sent = True
                        notice_error = error
                if superseded:
                    if notice_error is not None:
                        raise notice_error
                    return
                # 応答待ちと通知中の脱落をロック外で待つ。後続の実行開始までは再判定する。
                await changed.wait()
        finally:
            # ロック前に脱落した最新要求を外すと、直前の有効な要求へ戻る。
            if token in operations.tokens:
                operations.tokens.remove(token)
            operations.deferring.discard(token)
            operations.pending -= 1
            operations.notify_changed()
            # 応答待ちも含めて最後の操作が終わるまで、ロックと世代を共有する。
            if operations.pending == 0:
                self._guilds.pop(guild_id, None)
