"""コメント受付の流量制限を、入力モデレーションより前に適用する経路を検証する。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core.app_state import AutonomousSession, BotState
from persona_bot.core import message_handler
from persona_bot.services import autonomous_service, autonomous_speech
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.core.background import spawn
from persona_bot.config.constants import (
    AUTONOMOUS_COMMENT_COOLDOWN_SECONDS, CHAT_MAX_CONCURRENT_PER_GUILD, MAX_IMAGE_SIZE_BYTES,
)
from persona_bot.utils import discord_helpers
from tests.helpers.autonomous_patch import patch_autonomous
from tests.helpers.message_handler_helpers import make_message_handler


class AutonomousCommentAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.state = BotState()
        self.settings = self.state.get_guild(1)
        self.settings.current_respond_mode = "mention"
        self.session = self.new_session()
        self.settings.autonomous_session = self.session
        self.client = SimpleNamespace(user=SimpleNamespace(id=99))
        self.message = SimpleNamespace(
            content="架空のコメントです。", attachments=[], mentions=[],
            author=SimpleNamespace(id=11, bot=False, display_name="架空の視聴者"),
            guild=SimpleNamespace(id=1, voice_client=None), channel=Mock(spec=discord.VoiceChannel, id=10),
            created_at=self.now, reply=AsyncMock(),
        )

        @asynccontextmanager
        async def typing():
            yield

        self.message.channel.typing.side_effect = typing
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check=AsyncMock(return_value=(True, "")),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock(return_value=("配信への架空の応答", None, [])))
        self.ng_filter = SimpleNamespace(
            contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
        )
        self.service = AutonomousService(
            self.client, self.state, self.gemini, self.moderator, self.ng_filter,
            SimpleNamespace(log_snapshot=Mock()),
        )
        self.service._load_persona = AsyncMock(return_value="架空のペルソナ")
        self.sent = AsyncMock()
        for name, replacement in (
            ("load_image_attachments", AsyncMock(return_value=[])),
            ("fetch_channel_history_text", AsyncMock(return_value="")),
            ("send_long_reply", self.sent),
            ("datetime", Mock(wraps=datetime)),
        ):
            patch_autonomous(self, name, replacement)  # 発話は autonomous_speech、時刻は両方が使う
        autonomous_service.datetime.now.side_effect = lambda *args: self.now
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        self.normal_reply = AsyncMock(return_value="通常の架空の応答")
        self.notify = AsyncMock()
        self.topic = SimpleNamespace(mark_activity=Mock(), watch_channel=Mock())
        namespace = {
            "client": self.client, "state": self.state,
            "auto_topic": self.topic, "autonomous": self.service,
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": self.ng_filter, "moderator": self.moderator,
            "stats": SimpleNamespace(record=AsyncMock()), "spawn": start,
            "play_tts_response": AsyncMock(),
        }
        self.handler = make_message_handler(namespace, self)
        self.handler.handle_chat_message = self.normal_reply
        self.handler.notify_violation = self.notify
        self.on_message = self.handler.on_message

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    def new_session(self):
        return AutonomousSession(
            channel_id=10, is_stage=False, started_at=self.now,
            last_human_at=self.now - timedelta(minutes=1),
            last_spoke_at=self.now - timedelta(minutes=1),
        )

    async def test_five_comments_make_only_one_input_and_output_check(self) -> None:
        for _ in range(5):
            await self.on_message(self.message)
            self.now += timedelta(seconds=1)
        self.assertEqual(self.moderator.check_and_route.await_count, 1)
        self.assertEqual(self.moderator.check.await_count, 1)
        self.assertEqual(self.gemini.generate.await_count, 1)
        self.sent.assert_awaited_once()
        self.assertEqual(self.topic.mark_activity.call_count, 5)
        self.assertEqual(len(self.settings.autonomous_recent_activity), 5)
        self.assertEqual(self.session.last_human_at, self.now - timedelta(seconds=1))

    async def test_comment_reply_restricts_mentions_without_bot_mention(self) -> None:
        # メンション無しで受け付ける配信コメントでも、応答の通知は返信先本人だけに限る。
        self.gemini.generate.return_value = ("@everyone <@&22> <@33> 架空の応答", None, [])
        with patch.object(autonomous_speech, "send_long_reply", discord_helpers.send_long_reply):
            await self.on_message(self.message)
        self.message.reply.assert_awaited_once()
        mentions = self.message.reply.await_args.kwargs["allowed_mentions"]
        self.assertEqual(mentions.to_dict(), {"parse": [], "replied_user": True})

    async def test_cooldown_keeps_local_ng_checks_without_more_ai_calls(self) -> None:
        await self.on_message(self.message)
        claimed_at = self.session.last_comment_reply_at
        self.ng_filter.contains_ng_word.reset_mock()
        self.ng_filter.contains_ng_word.return_value = True
        self.ng_filter.find_ng_words = Mock(return_value=["架空のNG対象"])

        # 受付枠が埋まっていても、ローカルNG判定と違反通知は省略しない。
        for _ in range(3):
            await self.on_message(self.message)

        self.assertEqual(self.ng_filter.contains_ng_word.call_count, 3)
        self.assertEqual(self.ng_filter.find_ng_words.call_count, 3)
        self.assertEqual(self.notify.await_count, 3)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()
        self.sent.assert_awaited_once()
        self.assertEqual(self.session.last_comment_reply_at, claimed_at)

    async def test_comment_images_are_closed_on_every_exit(self) -> None:
        from PIL import Image

        for case in ("success", "image_only", "persona", "history", "generate", "cancel", "empty", "session", "send"):
            with self.subTest(case=case):
                self.settings.autonomous_session = self.session
                self.session.last_comment_reply_at = None
                images = [Image.new("RGB", (2, 2)) for _ in range(2)]

                async def generate(*args, **kwargs):
                    for image in images:
                        image.getpixel((0, 0))
                    if case == "generate":
                        raise RuntimeError("架空の生成失敗")
                    if case == "cancel":
                        raise asyncio.CancelledError
                    if case == "session":
                        self.settings.autonomous_session = None
                    return "" if case == "empty" else "架空の回答"

                with patch.object(autonomous_speech, "load_image_attachments", AsyncMock(return_value=images)), patch.object(
                    self.service, "_load_persona", return_value=None if case == "persona" else "架空のペルソナ",
                ), patch.object(autonomous_speech, "fetch_channel_history_text", AsyncMock(
                    side_effect=RuntimeError("架空の履歴失敗") if case == "history" else None, return_value="",
                )), patch.object(autonomous_speech, "send_long_reply", AsyncMock(
                    side_effect=RuntimeError("架空の送信失敗") if case == "send" else None,
                )), patch.object(self.service, "_generate", AsyncMock(side_effect=generate)):
                    call = self.service.handle_comment(self.message, "" if case == "image_only" else "架空のコメント", self.settings)
                    if case in ("history", "generate", "send", "cancel"):
                        with self.assertRaises(asyncio.CancelledError if case == "cancel" else RuntimeError):
                            await call
                    else:
                        await call
                for image in images:
                    with self.assertRaises(ValueError):
                        image.getpixel((0, 0))

    async def test_empty_comment_skips_persona_history_and_generation(self) -> None:
        self.assertIsNone(await self.service.handle_comment(self.message, " \n", self.settings))
        self.service._load_persona.assert_not_called()
        autonomous_speech.fetch_channel_history_text.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.assertIsNone(self.session.last_comment_reply_at)

    async def test_stream_comment_does_not_register_reminders_stats_or_documents(self) -> None:
        self.handler.reminders.create_from_request = AsyncMock(return_value="架空の登録確認")
        self.handler.documents.enqueue_from_message = AsyncMock()
        self.moderator.check_and_route.return_value = (
            True, "", "projects", {"remind_at": "2026-01-02", "message": "架空の予定"},
        )
        self.message.attachments = [SimpleNamespace(filename="fictional.pdf", content_type="application/pdf", size=10)]
        await self.on_message(self.message)
        self.handler.reminders.create_from_request.assert_not_awaited()
        self.handler.documents.enqueue_from_message.assert_not_awaited()
        self.handler.stats.record.assert_not_awaited()
        self.handler.memory.build_contexts.assert_not_called()
        self.message.reply.assert_not_awaited()
        self.normal_reply.assert_not_awaited()
        self.sent.assert_awaited_once()

    async def test_stream_comment_with_text_ignores_unsupported_attachments(self) -> None:
        self.message.attachments = [SimpleNamespace(filename="fictional.zip", content_type="application/zip", size=10)]
        await self.on_message(self.message)
        self.message.reply.assert_not_awaited()
        self.handler.stats.record.assert_not_awaited()
        self.sent.assert_awaited_once()

    async def test_unusable_attachment_only_comments_are_limited_before_decoding(self) -> None:
        self.message.content = ""
        attachments = [
            SimpleNamespace(filename="fictional.zip", content_type="application/zip", size=10),
            SimpleNamespace(filename="fictional.pdf", content_type="application/pdf", size=10),
            SimpleNamespace(filename="fictional.mp4", content_type="video/mp4", size=10),
            SimpleNamespace(filename="fictional.png", content_type="image/png", size=MAX_IMAGE_SIZE_BYTES + 1),
            SimpleNamespace(filename="broken.png", content_type="image/png", size=10,
                            read=AsyncMock(return_value=b"fictional invalid image")),
        ]
        for attachment in attachments:
            with self.subTest(filename=attachment.filename):
                self.session.last_comment_reply_at = None
                self.message.attachments = [attachment]
                await self.on_message(self.message)
                self.assertEqual(self.session.last_comment_reply_at, self.now)
                self.assertEqual(self.session.last_spoke_at, self.now)
        self.message.attachments = []
        await self.on_message(self.message)  # スタンプのみ等、本文・添付の両方が無い場合。
        self.moderator.check_and_route.assert_not_awaited()
        self.message.reply.assert_not_awaited()
        self.now += timedelta(seconds=AUTONOMOUS_COMMENT_COOLDOWN_SECONDS)
        self.message.content = "直後の架空のコメント"
        await self.on_message(self.message)
        self.sent.assert_awaited_once()

    async def test_preloaded_image_only_comments_are_reused_and_closed(self) -> None:
        from PIL import Image

        self.message.content = ""
        self.message.attachments = [SimpleNamespace(filename="fictional.png")]
        for case in ("success", "cancel", "typing_failure"):
            with self.subTest(case=case):
                self.session.last_comment_reply_at = None
                image = Image.new("RGB", (2, 2))
                self.sent.reset_mock()
                self.gemini.generate.reset_mock()
                if case == "typing_failure":
                    self.message.channel.typing.side_effect = RuntimeError("架空の入力中表示失敗")
                self.service._load_persona.side_effect = asyncio.CancelledError if case == "cancel" else None
                with patch.object(message_handler, "load_image_attachments", AsyncMock(return_value=[image])) as loader:
                    if case in ("cancel", "typing_failure"):
                        with self.assertRaises(asyncio.CancelledError if case == "cancel" else RuntimeError):
                            await self.on_message(self.message)
                    else:
                        await self.on_message(self.message)
                    loader.assert_awaited_once()
                with self.assertRaises(ValueError):
                    image.getpixel((0, 0))
                if case == "success":
                    self.sent.assert_awaited_once()
                    self.assertEqual(self.gemini.generate.await_args.kwargs["images"], [image])

    async def test_image_only_cooldown_rejects_before_download_or_decode(self) -> None:
        self.session.last_comment_reply_at = self.now
        self.message.content = ""
        attachment = SimpleNamespace(filename="fictional.png", content_type="image/png", size=10,
                                     read=AsyncMock(return_value=b"fictional image"))
        self.message.attachments = [attachment]
        with patch.object(message_handler, "load_image_attachments", AsyncMock()) as loader:
            await asyncio.gather(*(self.on_message(self.message) for _ in range(5)))
        loader.assert_not_awaited()
        attachment.read.assert_not_awaited()
        self.moderator.check_and_route.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.normal_reply.assert_not_awaited()
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_concurrent_image_only_comments_decode_once_after_admission(self) -> None:
        from PIL import Image

        self.message.content = ""
        self.message.attachments = [SimpleNamespace(filename="fictional.png", content_type="image/png", size=10)]
        image = Image.new("RGB", (2, 2))
        self.addCleanup(image.close)

        async def load(*args):
            self.assertEqual(self.session.last_comment_reply_at, self.now)
            await asyncio.sleep(0)
            return [image]

        with patch.object(message_handler, "load_image_attachments", AsyncMock(side_effect=load)) as loader:
            await asyncio.gather(*(self.on_message(self.message) for _ in range(5)))
        loader.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))

    async def test_ended_stream_before_image_admission_uses_normal_slot(self) -> None:
        self.message.content = "<@99>"
        self.message.mentions = [self.client.user]
        self.message.attachments = [SimpleNamespace(filename="fictional.png", content_type="image/png", size=10)]

        async def end_before_admission(*args):
            self.settings.autonomous_session = None
            return False

        self.handler._reject_ng_input = AsyncMock(side_effect=end_before_admission)
        with patch.object(self.service, "claim_comment_slot", wraps=self.service.claim_comment_slot) as claim:
            await self.on_message(self.message)
        claim.assert_not_called()
        self.assertIsNone(self.session.last_comment_reply_at)
        self.normal_reply.assert_awaited_once()
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_concurrent_comments_share_one_admission_slot(self) -> None:
        async def check(*args):
            await asyncio.sleep(0)
            return True, "", "none", None

        self.moderator.check_and_route.side_effect = check
        await asyncio.gather(*(self.on_message(self.message) for _ in range(5)))
        self.assertEqual(self.moderator.check_and_route.await_count, 1)
        self.assertEqual(self.gemini.generate.await_count, 1)
        self.sent.assert_awaited_once()

    async def test_next_comment_after_cooldown_is_accepted(self) -> None:
        await self.on_message(self.message)
        self.now += timedelta(seconds=AUTONOMOUS_COMMENT_COOLDOWN_SECONDS + 1)
        await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(self.gemini.generate.await_count, 2)

    async def test_first_comment_after_monologue_is_accepted(self) -> None:
        self.session.last_spoke_at = self.now
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.sent.assert_awaited_once()

    async def test_admitted_unsafe_input_is_still_rejected(self) -> None:
        self.moderator.check_and_route.return_value = (False, "架空の判定", "none", None)
        await self.on_message(self.message)
        self.notify.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()

    async def test_repeated_unsafe_inputs_keep_the_admission_slot(self) -> None:
        self.moderator.check_and_route.return_value = (False, "架空の判定", "none", None)
        for _ in range(5):
            await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.notify.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.moderator.check.assert_not_awaited()
        self.sent.assert_not_awaited()

        # AIで拒否された入力もAPI枠を消費し、直後の入力で判定をやり直さない。
        self.moderator.check_and_route.return_value = (True, "", "none", None)
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.now += timedelta(seconds=AUTONOMOUS_COMMENT_COOLDOWN_SECONDS)
        await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.gemini.generate.assert_awaited_once()

    async def test_local_ng_rejections_do_not_consume_the_admission_slot(self) -> None:
        self.ng_filter.contains_ng_word.return_value = True
        self.ng_filter.find_ng_words = Mock(return_value=["架空のNG対象"])
        for _ in range(5):
            await self.on_message(self.message)
        self.assertEqual(self.ng_filter.contains_ng_word.call_count, 5)
        self.assertEqual(self.notify.await_count, 5)
        self.moderator.check_and_route.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.assertIsNone(self.session.last_comment_reply_at)

        # ローカル判定ではAPIを使わないため、直後の安全な入力は受け付ける。
        self.ng_filter.contains_ng_word.return_value = False
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.gemini.generate.assert_awaited_once()

    async def test_cooldown_accepts_at_the_exact_admission_boundary(self) -> None:
        await self.on_message(self.message)
        accepted_at = self.session.last_comment_reply_at
        boundary = accepted_at + timedelta(seconds=AUTONOMOUS_COMMENT_COOLDOWN_SECONDS)
        self.now = boundary - timedelta(microseconds=1)
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.assertEqual(self.session.last_comment_reply_at, accepted_at)

        self.now = boundary
        await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(self.gemini.generate.await_count, 2)
        self.assertEqual(self.session.last_comment_reply_at, boundary)

    async def test_admitted_unsafe_output_is_still_screened(self) -> None:
        self.moderator.check.return_value = (False, "架空の判定")
        await self.on_message(self.message)
        self.moderator.check.assert_awaited_once()
        self.sent.assert_not_awaited()

    async def end_session_during_input(self, *args):
        self.settings.autonomous_session = None
        return True, "", "none", None

    async def test_session_end_falls_back_to_normal_mention(self) -> None:
        self.message.mentions = [self.client.user]
        self.moderator.check_and_route.side_effect = self.end_session_during_input
        await self.on_message(self.message)
        self.normal_reply.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.topic.watch_channel.assert_called_once_with(10, 1)

    async def test_session_end_without_normal_acceptance_is_not_answered(self) -> None:
        self.moderator.check_and_route.side_effect = self.end_session_during_input
        await self.on_message(self.message)
        self.normal_reply.assert_not_awaited()
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()

    async def test_session_end_during_image_validation_preserves_normal_mention(self) -> None:
        from PIL import Image

        self.message.content = "<@99>"
        self.message.mentions = [self.client.user]
        self.message.attachments = [SimpleNamespace(filename="fictional.png", content_type="image/png", size=10)]
        image = Image.new("RGB", (2, 2))

        async def load(*args):
            self.settings.autonomous_session = None
            return [image]

        with patch.object(message_handler, "load_image_attachments", AsyncMock(side_effect=load)):
            await self.on_message(self.message)
        self.normal_reply.assert_awaited_once()
        self.gemini.generate.assert_not_awaited()
        self.assertEqual(self.session.last_comment_reply_at, self.now)
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))
        self.topic.watch_channel.assert_called_once_with(10, 1)

    async def test_ending_before_admission_uses_normal_agent_routing_and_channel_watch(self) -> None:
        self.message.content = "<@99> サブエージェントで架空の調べ物をしてください"
        self.message.mentions = [self.client.user]

        async def end_before_admission(*args):
            self.settings.autonomous_session = None
            return False

        self.handler._reject_ng_input = AsyncMock(side_effect=end_before_admission)
        self.handler._handle_agent_request = AsyncMock(return_value=None)
        for delegate in (False, True):
            with self.subTest(delegate=delegate):
                self.settings.autonomous_session = self.session
                self.topic.watch_channel.reset_mock()
                self.normal_reply.reset_mock()
                self.handler._handle_agent_request.reset_mock()
                plan = AgentPlan(delegate=delegate, task="架空の調べ物")
                self.moderator.check_route_and_plan = AsyncMock(return_value=(True, "", "none", None, plan))
                with patch.object(message_handler, "fetch_channel_history_text", AsyncMock(return_value="架空の履歴")) as history:
                    await self.on_message(self.message)
                self.moderator.check_route_and_plan.assert_awaited_once_with(
                    "サブエージェントで架空の調べ物をしてください", "架空の履歴",
                )
                history.assert_awaited_once()
                self.moderator.check_and_route.assert_not_awaited()
                self.topic.watch_channel.assert_called_once_with(10, 1)
                self.assertIsNone(self.session.last_comment_reply_at)
                self.assertEqual(self.handler._chat_admission._running, 0)
                if delegate:
                    self.handler._handle_agent_request.assert_awaited_once()
                    self.assertIs(self.handler._handle_agent_request.await_args.args[3], plan)
                    self.normal_reply.assert_not_awaited()
                else:
                    self.handler._handle_agent_request.assert_not_awaited()
                    self.normal_reply.assert_awaited_once()
                    self.assertTrue(self.normal_reply.await_args.kwargs["delegation_section"])

    async def test_ending_before_admission_does_not_accept_an_unmentioned_comment(self) -> None:
        async def end_before_admission(*args):
            self.settings.autonomous_session = None
            return False

        self.handler._reject_ng_input = AsyncMock(side_effect=end_before_admission)
        await self.on_message(self.message)
        self.moderator.check_and_route.assert_not_awaited()
        self.topic.watch_channel.assert_not_called()
        self.normal_reply.assert_not_awaited()
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_empty_image_validation_after_stream_end_uses_normal_attachment_routing(self) -> None:
        self.message.content = "<@99>"
        self.message.mentions = [self.client.user]
        enqueue = self.handler.documents.enqueue_from_message = AsyncMock(return_value="fictional-task")

        async def load(*args):
            self.settings.autonomous_session = None
            return []

        for index, (filename, content_type, notice) in enumerate((
            ("fictional.pdf", "application/pdf", "受付が完了"),
            ("fictional.txt", "text/plain", "受付が完了"),
            ("broken.png", "image/png", "画像を読み込めませんでした"),
            ("fictional.zip", "application/zip", "未対応の添付ファイル"),
        )):
            with self.subTest(filename=filename):
                self.settings.autonomous_session = self.session
                self.session.last_comment_reply_at = None
                self.message.author.id = 11 + index
                self.message.attachments = [SimpleNamespace(filename=filename, content_type=content_type, size=10)]
                self.message.reply.reset_mock()
                self.topic.watch_channel.reset_mock()
                enqueue.reset_mock()
                # 画像の失敗案内まで、実際の通常会話ハンドラーを通す。
                self.handler.handle_chat_message = message_handler.MessageHandler.handle_chat_message.__get__(self.handler)
                with patch.object(message_handler, "load_image_attachments", AsyncMock(side_effect=load)) as loader:
                    await self.on_message(self.message)
                loader.assert_awaited_once()
                self.message.reply.assert_awaited_once()
                self.assertIn(notice, self.message.reply.await_args.args[0])
                if filename.endswith((".pdf", ".txt")):
                    enqueue.assert_awaited_once_with(self.message, "", self.message.attachments)
                else:
                    enqueue.assert_not_awaited()
                self.topic.watch_channel.assert_called_once_with(10, 1)
                self.moderator.check_and_route.assert_not_awaited()
                self.gemini.generate.assert_not_awaited()
                self.assertEqual(self.session.last_comment_reply_at, self.now)
                self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_empty_image_validation_keeps_live_stream_comments_out_of_document_queue(self) -> None:
        self.message.content = "<@99>"
        self.message.mentions = [self.client.user]
        self.message.attachments = [SimpleNamespace(filename="fictional.pdf", content_type="application/pdf", size=10)]
        self.handler.documents.enqueue_from_message = AsyncMock()
        with patch.object(message_handler, "load_image_attachments", AsyncMock(return_value=[])):
            await self.on_message(self.message)
        self.handler.documents.enqueue_from_message.assert_not_awaited()
        self.message.reply.assert_not_awaited()
        self.topic.watch_channel.assert_not_called()
        self.assertEqual(self.session.last_comment_reply_at, self.now)
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_empty_image_validation_drops_unmentioned_comments_after_stream_end(self) -> None:
        self.message.content = ""
        self.message.attachments = [SimpleNamespace(filename="fictional.pdf", content_type="application/pdf", size=10)]
        self.handler.documents.enqueue_from_message = AsyncMock()

        async def load(*args):
            self.settings.autonomous_session = None
            return []

        with patch.object(message_handler, "load_image_attachments", AsyncMock(side_effect=load)):
            await self.on_message(self.message)
        self.handler.documents.enqueue_from_message.assert_not_awaited()
        self.message.reply.assert_not_awaited()
        self.topic.watch_channel.assert_not_called()
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_empty_image_fallback_respects_full_normal_admission(self) -> None:
        self.message.content = "<@99>"
        self.message.mentions = [self.client.user]
        self.message.attachments = [SimpleNamespace(filename="fictional.pdf", content_type="application/pdf", size=10)]
        self.handler.documents.enqueue_from_message = AsyncMock()
        for index in range(CHAT_MAX_CONCURRENT_PER_GUILD):
            self.assertIsNone(self.handler._chat_admission.try_acquire(100 + index, 1))

        async def load(*args):
            self.settings.autonomous_session = None
            return []

        try:
            with patch.object(message_handler, "load_image_attachments", AsyncMock(side_effect=load)):
                await self.on_message(self.message)
            self.message.reply.assert_awaited_once()
            self.handler.documents.enqueue_from_message.assert_not_awaited()
            self.moderator.check_and_route.assert_not_awaited()
        finally:
            for _ in range(CHAT_MAX_CONCURRENT_PER_GUILD):
                self.handler._chat_admission.release(1)
        self.assertEqual(self.handler._chat_admission._running, 0)

    async def test_replaced_session_does_not_reuse_old_admission(self) -> None:
        replacement = self.new_session()

        async def replace(*args):
            self.settings.autonomous_session = replacement
            return True, "", "none", None

        self.moderator.check_and_route.side_effect = replace
        await self.on_message(self.message)
        self.gemini.generate.assert_not_awaited()
        self.sent.assert_not_awaited()
        self.assertIsNone(replacement.last_comment_reply_at)

    async def test_direct_comment_handler_still_applies_cooldown(self) -> None:
        await self.service.handle_comment(self.message, self.message.content, self.settings)
        await self.service.handle_comment(self.message, self.message.content, self.settings)
        self.gemini.generate.assert_awaited_once()
        self.sent.assert_awaited_once()

    async def test_normal_mention_outside_stream_is_not_rate_limited(self) -> None:
        self.message.channel.id = 20
        self.message.mentions = [self.client.user]
        for _ in range(2):
            await self.on_message(self.message)
        self.assertEqual(self.moderator.check_and_route.await_count, 2)
        self.assertEqual(self.normal_reply.await_count, 2)
        self.assertIsNone(self.session.last_comment_reply_at)


if __name__ == "__main__":
    unittest.main()
