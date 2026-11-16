"""実際の NG 判定・ブラックリスト・メッセージ処理を、架空の語と一時ファイルでつなぐ（#172・#173）。

Discord・Gemini はモックに置き換える。NG 語は架空の1語だけを読み込み、実際の
data/ng/NG_WORD.txt・data/ng/NG_WORD_private.txt・data/ng/blacklist.json には触れない。
"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from persona_bot.core.app_state import BotState
from persona_bot.services import ng_word_service
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.services.ng_word_service import NgWordFilter
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.discord_mocks import make_spec_channel

FICTIONAL_NG_WORD = "ふわもこ禁止語"
BOT_USER_ID = 99
GUILD_ID = 1
VOICE_CHANNEL_ID = 20


def close_coroutine(coro, **kwargs):
    # 会話統計の記録など、裏で走らせる処理はこのテストの対象外なので実行せずに閉じる
    coro.close()


def make_channel(spec: type, channel_id: int) -> Mock:
    return make_spec_channel(spec, channel_id)


def make_voice_client(channel_id: int, *, connected: bool = True) -> Mock:
    voice_client = Mock(spec=discord.VoiceClient)
    voice_client.channel = SimpleNamespace(id=channel_id)
    voice_client.is_connected.return_value = connected
    return voice_client


class NgPipeline:
    """1つのテストで使う、架空のサーバー・利用者・保存先の組。"""

    def __init__(self, test_case) -> None:
        directory = tempfile.TemporaryDirectory()
        test_case.addCleanup(directory.cleanup)
        self.blacklist_path = Path(directory.name) / "data" / "ng" / "blacklist.json"
        self.blacklist = BlacklistService(self.blacklist_path)
        with patch.object(ng_word_service, "load_ng_words", return_value=[FICTIONAL_NG_WORD]):
            self.ng_filter = NgWordFilter()

        self.bot_user = SimpleNamespace(id=BOT_USER_ID)
        self.state = BotState()
        self.guild_settings = self.state.get_guild(GUILD_ID)
        self.guild_settings.current_respond_mode = "mention"
        self.guild = SimpleNamespace(id=GUILD_ID, voice_client=make_voice_client(VOICE_CHANNEL_ID))
        self.auto_topic = SimpleNamespace(mark_activity=Mock(), watch_channel=Mock())
        self.autonomous = SimpleNamespace(
            mark_activity=Mock(), is_comment_channel=Mock(return_value=False),
            claim_comment_slot=Mock(return_value=None), handle_comment=AsyncMock(),
            can_speak_session=Mock(return_value=False),
        )
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check=AsyncMock(return_value=(True, "")),
        )
        self.gemini = SimpleNamespace(generate_with_metadata=AsyncMock(
            return_value=("架空の応答です。", None, [], "fictional-model"),
        ))
        self.send_long_reply = AsyncMock(return_value=SimpleNamespace(id=500))
        self.handler = make_message_handler({
            "client": SimpleNamespace(user=self.bot_user), "state": self.state,
            "auto_topic": self.auto_topic, "autonomous": self.autonomous,
            "blacklist": self.blacklist, "ng_filter": self.ng_filter,
            "moderator": self.moderator, "gemini": self.gemini,
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(
                build_contexts=AsyncMock(return_value=("", "")), append_pending_log=AsyncMock(),
                flush_if_needed=AsyncMock(),
            ),
            "affinity": SimpleNamespace(build_dynamic_prompt=Mock(return_value="")),
            "reminders": SimpleNamespace(create_from_request=AsyncMock()),
            "documents": SimpleNamespace(enqueue_from_message=AsyncMock()),
            "feedback": SimpleNamespace(track_reply=Mock()),
            "usage": SimpleNamespace(log_snapshot=Mock()),
            "spawn": close_coroutine, "play_tts_response": AsyncMock(),
            "load_image_attachments": AsyncMock(return_value=[]),
            "fetch_channel_history_text": AsyncMock(return_value=""),
            "load_persona_template": AsyncMock(return_value="架空のペルソナ"),
            "send_long_reply": self.send_long_reply,
        }, test_case)

    def message(self, content: str, *, channel=None, mention: bool = False, user_id: int = 11):
        channel = channel if channel is not None else make_channel(discord.VoiceChannel, VOICE_CHANNEL_ID)
        if mention:
            content = f"<@{BOT_USER_ID}> {content}"
        return SimpleNamespace(
            author=SimpleNamespace(id=user_id, bot=False, display_name="架空の利用者"),
            guild=self.guild, channel=channel, content=content, attachments=[],
            mentions=[self.bot_user] if mention else [],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reply=AsyncMock(),
        )

    def reloaded_blacklist(self) -> BlacklistService:
        """保存したファイルから作り直したサービス（再起動後の状態）。"""
        return BlacklistService(self.blacklist_path)
