"""サーバー退出時の実行中セッションとメモリ上の設定を回収する。"""

import logging
from typing import Any

from persona_bot.core.app_state import BotState
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.voice_guard import VoiceGuard


logger = logging.getLogger(__name__)


def prepare_guild_join(guild_id: int, state: BotState, voice_guard: VoiceGuard) -> None:
    """退出待機中の再参加を区切り、新しい設定・案内・接続保護を受け付ける。"""
    if state.guild_removals.pop(guild_id, None) is not None:
        state.forget_guild(guild_id)
        state.welcomed_guild_ids.discard(guild_id)
        voice_guard.release(guild_id)


async def remove_guild_state(
    guild_id: int, state: BotState, autonomous: AutonomousService, voice_guard: VoiceGuard,
    voice_conversation: Any = None,
) -> None:
    """停止処理が設定を参照し終わってからキャッシュを外す。永続化データは保持する。"""
    removal = object()
    state.guild_removals[guild_id] = removal
    settings = state.guild_settings.get(guild_id)
    if settings is not None:
        settings._active = False
    # ★ 読込中の古い結果は無効にして、辞書からも外す。ローカル変数が強参照を持つ間は
    # 弱参照の辞書にも残り続け、無効なまま停止の待機中に居座ってしまう（待機中に
    # 来た新しい読込要求が、無効な読込を掴んで失敗する）。
    loading = state._guild_loads.pop(guild_id, None)
    if loading is not None:
        loading.valid = False

    def is_current() -> bool:
        # 開始時に未ロード（None）なら、待機中に停止処理自身が get_guild で作った設定も
        # 回収対象にする（残すと退出済みサーバーの設定が孤児になる）。再参加で作り直された
        # 設定は、prepare_guild_join が guild_removals の所有印を外すので守られる。
        return (state.guild_removals.get(guild_id) is removal
                and (settings is None or state.guild_settings.get(guild_id) is settings))

    try:
        await autonomous.stop_session(guild_id, reason="サーバーからの退出")
    except Exception as error:
        logger.error(f'サーバー退出時の配信停止に失敗しました: {type(error).__name__}', exc_info=error)
    finally:
        try:
            # 音声会話（#268）の受信・Live の接続・同意パネルも、そのサーバー分を片付ける。
            if voice_conversation is not None and is_current():
                await voice_conversation.stop(guild_id, reason="サーバーからの退出")
        except Exception as error:
            logger.error(f'サーバー退出時の音声会話の停止に失敗しました: {type(error).__name__}', exc_info=error)
        finally:
            if is_current():
                voice_guard.release(guild_id)
                state.forget_guild(guild_id)
                state.welcomed_guild_ids.discard(guild_id)
            if state.guild_removals.get(guild_id) is removal:
                state.guild_removals.pop(guild_id)
