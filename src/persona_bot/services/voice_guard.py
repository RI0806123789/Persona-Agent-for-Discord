"""ボイス接続先の保護（#171）。

管理者は Discord の操作（ドラッグなど。「メンバーを移動」権限）でボットを別のボイス
チャンネルへ移せる。`ADMINISTRATOR` はチャンネルの権限上書きを迂回するため、ボット側の
権限設定で移動そのものを事前に拒否する手段は無い。そこで、ボット自身のボイス状態の
変化（`on_voice_state_update`）から **予期しないチャンネル間の移動を検知した後に**、
次の保護動作を行う（一瞬の移動も起きない「完全な禁止」ではない）。

    ・`/join` で人が呼んだ接続 … 元のチャンネルへ戻る。戻れない場合（元のチャンネルが
      消えた・閲覧/接続/発言の権限が無い・移動やステージの発言者への復帰に失敗した）や、
      短い間に何度も移された場合は退出する（戻っては移されるの往復を無限に続けない）。
    ・自律行動の配信 …………… 配信を畳んで移動先から退出する（既存の「移されたら終了」を、
      毎分の見回りを待たずに行う。開いたステージ枠を channel_id で覚えているため追従しない）。

どちらの場合も、移動の瞬間に再生中だった読み上げは止める（移動先で古い音声を流さない）。
待ち行列の読み上げは voice.py が投入時の接続・チャンネルと照合して捨てる。

■ アプリ自身の移動と見分ける方法
アプリはチャンネル間の移動（`move_to`）を、このモジュールが元へ戻すとき以外に行わない。
`/join` の切り替えは「切断→接続」、`/leave`・配信の終了は「切断」、配信の開始は「接続」で、
discord.py の接続復旧も同じチャンネルへつなぎ直す。いずれもボット自身のボイス状態は
「なし→A」「A→なし」と変わるだけなので、**「A→B」の移動はすべて外部からの操作とみなせる**。
外部の利用者は、ボイスに居ないボットを通話へ引き込めないため、「なし→A」は必ずアプリ自身の接続。

★ アプリ側に `move_to` でチャンネルを移す処理を足すときは、移す前に `adopt()` で行き先を
登録すること。登録しないと手動の移動と誤認して、元のチャンネルへ戻してしまう。

接続の唯一の情報源は `guild.voice_client` のまま。ここでは「意図した接続先」と
「戻した時刻」だけをサーバーごとにメモリへ持つ（ボイス接続は再起動をまたげないため
永続化しない）。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from weakref import WeakValueDictionary

import discord

from persona_bot.config.constants import (
    VOICE_CONNECT_TIMEOUT_SECONDS,
    VOICE_MOVE_GUARD_ENABLED,
    VOICE_MOVE_GUARD_MAX_RETURNS,
    VOICE_MOVE_GUARD_SETTLE_RETRIES,
    VOICE_MOVE_GUARD_SETTLE_SECONDS,
    VOICE_MOVE_GUARD_WINDOW_SECONDS,
)
from persona_bot.core.background import spawn, wait_for_cleanup
from persona_bot.services.autonomous_stage import enable_stage_speaker
from persona_bot.utils.voice_permissions import missing_voice_permissions

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from persona_bot.services.autonomous_service import AutonomousService


@dataclass
class _HomeChannel:
    """意図した接続先と、そこへ戻した時刻（monotonic。連続移動の判定に使う）。"""

    channel_id: int
    returned_at: deque[float] = field(default_factory=deque)


def _channel_id(voice_state: Any) -> int | None:
    channel = getattr(voice_state, "channel", None)
    return getattr(channel, "id", None)


def _stop_playback(voice_client: Any) -> None:
    """再生中の読み上げを止める。移動先で古い音声を最後まで流さないため。"""
    if not isinstance(voice_client, discord.VoiceClient):
        return
    try:
        if voice_client.is_playing():
            voice_client.stop()
    except Exception as error:
        logger.error("ボイス接続の保護: 再生の停止に失敗しました。", exc_info=error)


async def release_and_disconnect(
    voice_guard: VoiceGuard | None, guild_id: int, voice_client: Any, *,
    label: str, failure_message: str, log: logging.Logger = logger,
    stop: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """保護の解除 →（あれば）`stop` → 強制切断 を、この順序でまとめて行う。

    ボイス接続を自分から切る処理は、すべてここを通すこと（解除や停止の漏れを片側だけ直さないため）。

    - 保護を先に解くのは、戻している最中の保護処理が、切る予定の接続を元のチャンネルへ
      動かしたり、退出後に入り直したりしないため。
    - `stop`（音声会話の停止など）は同意パネルの編集などで待たされるので、保護の解除の後、
      切断の前に行う（切断済みの接続を会話の受信・再生が触らないように）。
    - 切断の失敗は `failure_message` を `log` へ警告で残して False を返す（例外は外へ出さない。
      キャンセルは伝播する）。`stop` の例外は呼出元へそのまま伝える。
    """
    if voice_guard is not None:
        voice_guard.release(guild_id)
    if stop is not None:
        await stop()
    try:
        await wait_for_cleanup(voice_client.disconnect(force=True), label=label)
    except Exception:
        log.warning(failure_message, exc_info=True)
        return False
    return True


class VoiceGuard:
    """ボット自身の予期しないボイスチャンネル移動を検知し、接続先を守る。"""

    def __init__(self, client: discord.Client, autonomous: AutonomousService) -> None:
        self._client = client
        self._autonomous = autonomous
        self._homes: dict[int, _HomeChannel] = {}
        # 保護中の処理が持つ間だけ生きる弱参照にする（過去のサーバーのロックを溜めない）。
        self._locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    # ------------------------------------------------------------------
    # 意図した接続先の登録
    # ------------------------------------------------------------------

    def home_channel_id(self, guild_id: int) -> int | None:
        home = self._homes.get(guild_id)
        return None if home is None else home.channel_id

    def adopt(self, guild_id: int, channel_id: int) -> None:
        """アプリ自身が接続した先を、意図した接続先として登録する。

        同じチャンネルの登録し直しでは、戻した回数の記録を保つ（連続移動の歯止めを
        `/join` の打ち直しで外せないようにするため）。
        """
        home = self._homes.get(guild_id)
        if home is None or home.channel_id != channel_id:
            self._homes[guild_id] = _HomeChannel(channel_id)

    def release(self, guild_id: int) -> None:
        """保護を解く。退出・切断の後に、どこかへ自動で戻ることはない。"""
        self._homes.pop(guild_id, None)

    # ------------------------------------------------------------------
    # 検知（bot.py の on_voice_state_update から呼ぶ）
    # ------------------------------------------------------------------

    def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> None:
        """ボット自身のボイス状態の変化を見て、予期しない移動なら保護動作を始める。

        同期関数にしてあるのは、イベントが届いた順に登録を更新するため（ここで await を
        挟むと、`/join` の「切断→接続」の2件が入れ替わって処理されうる）。
        """
        if not VOICE_MOVE_GUARD_ENABLED:
            return
        bot_user = self._client.user
        if bot_user is None or getattr(member, "id", None) != bot_user.id:
            return
        guild = member.guild
        before_id = _channel_id(before)
        after_id = _channel_id(after)
        if before_id == after_id:
            # ミュート・ステージの発言者への切り替えなど、チャンネルが変わらない更新。
            return
        if after_id is None:
            # 退出・切断（/leave・配信の終了・外部からの切断）。保護を解き、再参加しない。
            self.release(guild.id)
            return
        if before_id is None:
            # 接続（/join・配信の開始・接続復旧）。ここを意図した接続先にする。
            self.adopt(guild.id, after_id)
            return

        home = self._homes.get(guild.id)
        if home is None:
            # 接続を見届けていない（イベントの取りこぼし）。移動前を意図した接続先とみなす。
            home = self._homes[guild.id] = _HomeChannel(before_id)
        if after_id == home.channel_id:
            # このモジュールが戻した移動か、誰かが元のチャンネルへ戻した。
            return

        logger.warning("ボイス接続の保護: 別のチャンネルへ移動されたことを検知しました。")
        _stop_playback(guild.voice_client)
        spawn(self._protect(guild.id), label="ボイス接続先の保護")

    # ------------------------------------------------------------------
    # 保護動作
    # ------------------------------------------------------------------

    async def _protect(self, guild_id: int) -> None:
        """移動先から元のチャンネルへ戻るか、戻れなければ退出する。

        サーバーごとのロックで直列化し、ロックを取ってから状態を取り直す。短い間に
        続けて移された場合も、2件目以降は「既に戻っている」と判断して何もしない。
        """
        lock = self._locks.get(guild_id)
        if lock is None:
            lock = self._locks[guild_id] = asyncio.Lock()
        async with lock:
            voice_client = await self._settled_voice_client(guild_id)
            guild = self._client.get_guild(guild_id)
            home = self._homes.get(guild_id)
            if voice_client is None or guild is None or home is None:
                return
            if getattr(voice_client.channel, "id", None) == home.channel_id:
                return

            # 配信の接続は配信側の後片付け（締めの一言・自分で開いた枠の終了・切断）に任せる。
            if self._autonomous.manages_connection(guild_id):
                await self._autonomous.handle_external_move(guild)
                return

            home_channel, blocker = self._return_target(guild, home)
            if blocker is not None:
                await self._leave(guild, voice_client, blocker, expected_home=home)
                return

            home.returned_at.append(monotonic())
            logger.info("ボイス接続の保護: 元のチャンネルへ戻ります。")
            try:
                # discord.py の move_to は、時間切れでも例外を出さずに戻ることがある。
                # 成否は戻った後の接続先で確かめる。
                await voice_client.move_to(home_channel, timeout=VOICE_CONNECT_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._leave(
                    guild, voice_client, f"元のチャンネルへ戻れなかった（{type(error).__name__}）",
                    expected_home=home,
                )
                return

            # 移動を待つ間に /leave・/join で接続が切れた・差し替わった、または意図した
            # 接続先が変わった場合は、そちらの操作を優先して何もしない。
            if (guild.voice_client is not voice_client or not voice_client.is_connected()
                    or self._homes.get(guild_id) is not home):
                return
            if getattr(voice_client.channel, "id", None) != home.channel_id:
                await self._leave(
                    guild, voice_client, "元のチャンネルへ戻れなかった", expected_home=home,
                )
                return
            logger.info("ボイス接続の保護: 元のチャンネルへ戻りました。")
            if isinstance(home_channel, discord.StageChannel):
                # ステージは入り直すと観客に戻るため、/join と同じく発言者へ昇格し直す。
                try:
                    await enable_stage_speaker(guild, home_channel)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.error("ボイス接続の保護: ステージの発言者へ戻れませんでした。", exc_info=error)
                    await self._leave(
                        guild, voice_client, "ステージの発言者へ戻れなかった", expected_home=home,
                    )

    async def _settled_voice_client(self, guild_id: int) -> discord.VoiceClient | None:
        """移動直後の張り直しが終わるのを待ち、接続中の VoiceClient を返す。

        移動直後は discord.py が音声の接続を張り直していることがあり、すぐに移動し直すと
        処理がぶつかる。待っても接続が戻らなければ、移動先に居座らないよう切断する。
        """
        voice_client = None
        home = None
        for _ in range(VOICE_MOVE_GUARD_SETTLE_RETRIES):
            await asyncio.sleep(VOICE_MOVE_GUARD_SETTLE_SECONDS)
            guild = self._client.get_guild(guild_id)
            voice_client = getattr(guild, "voice_client", None)
            home = self._homes.get(guild_id)
            if not isinstance(voice_client, discord.VoiceClient) or home is None:
                # 待つ間に切断された（/leave・外部からの切断・配信の終了）。
                return None
            if voice_client.is_connected():
                return voice_client
        guild = self._client.get_guild(guild_id)
        if (guild is not None and voice_client is not None and home is not None
                and guild.voice_client is voice_client and self._homes.get(guild_id) is home):
            # 未接続でも配信の締めと所有ステージ枠の回収を先に行う。
            # 開始待機中など配信側が処理しない場合は、残った同じ接続だけを切断する。
            if self._autonomous.manages_connection(guild_id):
                await self._autonomous.handle_external_move(guild)
            await self._leave(
                guild, voice_client, "移動後の接続が安定しなかった", expected_home=home,
            )
        return None

    def _return_target(
        self, guild: discord.Guild, home: _HomeChannel,
    ) -> tuple[Any, str | None]:
        """戻り先のチャンネルと、戻れない理由（戻れるなら None）を返す。"""
        now = monotonic()
        while home.returned_at and now - home.returned_at[0] > VOICE_MOVE_GUARD_WINDOW_SECONDS:
            home.returned_at.popleft()
        if len(home.returned_at) >= VOICE_MOVE_GUARD_MAX_RETURNS:
            return None, (
                f"{VOICE_MOVE_GUARD_WINDOW_SECONDS // 60}分以内に{VOICE_MOVE_GUARD_MAX_RETURNS}回戻しても"
                "移動され続けた"
            )
        channel = guild.get_channel(home.channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return None, "元のチャンネルが見つからない（削除された可能性がある）"
        bot_member = guild.me
        if bot_member is not None:
            # /join と同じ基準で、チャンネル単位の権限を確かめる。
            missing = missing_voice_permissions(
                channel, bot_member, stage_moderation=isinstance(channel, discord.StageChannel),
            )
            if missing:
                names = "・".join(f"「{name}」" for name in missing)
                return None, f"元のチャンネルで{names}の権限が無い"
        return channel, None

    async def _leave(
        self, guild: discord.Guild, voice_client: Any, reason: str, *, expected_home: _HomeChannel,
    ) -> None:
        """対象の接続と保護情報が現在も一致する場合だけ、保護を解いて退出する。"""
        if (guild.voice_client is not voice_client
                or self._homes.get(guild.id) is not expected_home):
            # 待つ間に /join・/leave で更新された接続と保護情報には触らない。
            return
        # 照合から解除・切断開始までは待機を挟まない。
        self.release(guild.id)
        logger.warning(
            "ボイス接続の保護: 保護の継続条件を満たさないため、移動先のチャンネルから退出します（%s）。",
            reason,
        )
        try:
            await voice_client.disconnect(force=True)
        except Exception as error:
            logger.error("ボイス接続の保護: 退出に失敗しました。", exc_info=error)
