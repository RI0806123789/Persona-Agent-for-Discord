"""通常会話の受付数と同時実行数を、待機処理を作らず制限する。"""

from collections import OrderedDict, deque
import time

from persona_bot.config.constants import (
    CHAT_MAX_CONCURRENT_GLOBAL, CHAT_MAX_CONCURRENT_PER_GUILD, CHAT_MAX_REQUESTS_PER_USER,
    CHAT_RATE_WINDOW_SECONDS, CHAT_RATE_MAX_USERS, CHAT_RATE_LIMIT_NOTICE, CHAT_BUSY_NOTICE,
)


class ChatAdmission:
    """単一イベントループ上で、判定と確保・返却の間に await を挟まない。"""

    def __init__(self) -> None:
        self._accepted: OrderedDict[int, deque[float]] = OrderedDict()
        self._notified: OrderedDict[int, float] = OrderedDict()
        self._running_by_guild: dict[int | None, int] = {}
        self._running = 0

    def try_acquire(self, user_id: int, guild_id: int | None) -> str | None:
        """確保できたら None、見送るなら追加APIを使わない案内文を返す。"""
        now = time.monotonic()
        cutoff = now - CHAT_RATE_WINDOW_SECONDS
        # 最後の受付が古い順。期間内の記録は追い出さず、上限の迂回を防ぐ。
        while self._accepted:
            oldest = next(iter(self._accepted.values()))
            if oldest[-1] > cutoff:
                break
            self._accepted.popitem(last=False)
        accepted = self._accepted.get(user_id)
        if accepted is not None:
            while accepted and accepted[0] <= cutoff:
                accepted.popleft()
            if len(accepted) >= CHAT_MAX_REQUESTS_PER_USER:
                return CHAT_RATE_LIMIT_NOTICE
        elif len(self._accepted) >= CHAT_RATE_MAX_USERS:
            return CHAT_BUSY_NOTICE
        guild_running = self._running_by_guild.get(guild_id, 0)
        if self._running >= CHAT_MAX_CONCURRENT_GLOBAL or guild_running >= CHAT_MAX_CONCURRENT_PER_GUILD:
            return CHAT_BUSY_NOTICE

        if accepted is None:
            accepted = deque()
            self._accepted[user_id] = accepted
        accepted.append(now)
        self._accepted.move_to_end(user_id)
        self._running_by_guild[guild_id] = guild_running + 1
        self._running += 1
        return None

    def try_claim_notice(self, user_id: int) -> bool:
        """送信前に同期的に記録し、理由・サーバー・DMをまたいだ案内の連投を防ぐ。"""
        now = time.monotonic()
        cutoff = now - CHAT_RATE_WINDOW_SECONDS
        while self._notified:
            if next(iter(self._notified.values())) > cutoff:
                break
            self._notified.popitem(last=False)
        if user_id in self._notified or len(self._notified) >= CHAT_RATE_MAX_USERS:
            return False
        # 送信失敗でも期間内は再送しない。見送りでは有効期限を延ばさない。
        self._notified[user_id] = now
        return True

    def release(self, guild_id: int | None) -> None:
        """処理中の枠だけを返し、期間内の受付数は失敗・中断でも戻さない。"""
        running = self._running_by_guild.get(guild_id, 0)
        if running <= 0:
            return
        remaining = running - 1
        if remaining:
            self._running_by_guild[guild_id] = remaining
        else:
            del self._running_by_guild[guild_id]
        self._running = max(0, self._running - 1)
