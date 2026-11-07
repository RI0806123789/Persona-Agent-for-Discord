"""設定変更を挟んでも、生成・評価・読み上げで同じ応答設定を使うことを検証する。

実際の GeminiService を通した場合に、本文へ混入した内部形式が送信・記憶・読み上げへ流れないことも確かめる。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from google.genai import errors, types
from persona_bot.core.app_state import BotState
from persona_bot.core.database import Database
from persona_bot.core.background import spawn
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.persona_helpers import install_persona_catalog
from persona_bot.config.constants import GROUNDING_MODEL_NAME
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.feedback_service import FeedbackService
from persona_bot.utils.logging_setup import LOG_DATE_FORMAT, LOG_FORMAT, OperationalFormatter


LOG_FORMATTER = OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT)


# 検索の呼び出し・思考・最終回答が通常の text に連続した、架空の異常応答（Issue #167）。
LEAKED_TEXT = (
    'tool_code\nprint(google_search.search(queries=["架空の天気"]))\n\n'
    "thought\n\nI should answer the fictional question in character.\n\n"
    "架空の最終回答です。"
)


def sdk_text_response(text: str, uri: str | None = None) -> types.GenerateContentResponse:
    metadata = None
    if uri is not None:
        metadata = types.GroundingMetadata(grounding_chunks=[
            types.GroundingChunk(web=types.GroundingChunkWeb(title="架空の資料", uri=uri)),
        ])
    return types.GenerateContentResponse(candidates=[types.Candidate(
        finish_reason=types.FinishReason.STOP, grounding_metadata=metadata,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )])


class ReplySettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tasks = []
        self.stage = "generate"
        self.response = "架空の応答です。"
        self.state = BotState()
        install_persona_catalog(self)
        self.guild_id = 1
        self.generations = []
        self.settings = self.state.get_guild(self.guild_id)
        self.configure_old()
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=self.guild_id),
            channel=Mock(spec=discord.TextChannel, id=10),
            content="架空の質問です。", attachments=[], mentions=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc), reply=AsyncMock(),
        )

        @asynccontextmanager
        async def typing():
            await self.switch_at("typing")
            yield

        self.message.channel.typing.side_effect = typing

        async def images(attachments):
            await self.switch_at("images")
            return []

        async def generate(*args, **kwargs):
            self.generations.append((args, kwargs))
            await self.switch_at("generate")
            return self.response, None, [], args[1]

        async def moderate(*args, **kwargs):
            await self.switch_at("moderate")
            return True, ""

        async def send(*args, **kwargs):
            await self.switch_at("send")
            return SimpleNamespace(id=500)

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        self.feedback = SimpleNamespace(track_reply=Mock())
        self.voice = AsyncMock()
        self.loader = AsyncMock(side_effect=lambda path: f"ペルソナ: {Path(path).as_posix()}")
        self.namespace = {
            "state": self.state,
            "client": SimpleNamespace(user=SimpleNamespace(id=99)),
            "auto_topic": SimpleNamespace(mark_activity=Mock(), watch_channel=Mock()),
            "autonomous": SimpleNamespace(
                mark_activity=Mock(), is_comment_channel=Mock(return_value=False),
                claim_comment_slot=Mock(return_value=object()),
            ),
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": SimpleNamespace(
                contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
            ),
            "moderator": SimpleNamespace(
                check_and_route=AsyncMock(return_value=(True, "", "none", None)),
                check=AsyncMock(side_effect=moderate),
            ),
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(
                build_contexts=AsyncMock(return_value=("", "")),
                append_pending_log=AsyncMock(), flush_if_needed=AsyncMock(),
            ),
            "affinity": SimpleNamespace(
                build_dynamic_prompt=Mock(return_value="架空の親密度"), update_emotion=Mock(),
                save_background=AsyncMock(),
            ),
            "gemini": SimpleNamespace(generate_with_metadata=AsyncMock(side_effect=generate)),
            "usage": SimpleNamespace(log_snapshot=Mock()),
            "feedback": self.feedback, "play_tts_response": self.voice,
            "load_image_attachments": AsyncMock(side_effect=images),
            "fetch_channel_history_text": AsyncMock(return_value=""),
            "load_persona_template": self.loader,
            "send_long_reply": AsyncMock(side_effect=send), "spawn": start,
        }
        self.handler = make_message_handler(self.namespace, self)
        self.on_message = self.handler.on_message

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    def configure_old(self) -> None:
        self.settings.current_model_name = "old-model"
        self.settings.current_prompt_file = "../prompts/prompt_default.txt"
        self.settings.grounding_enabled = True
        self.settings.current_respond_mode = "all"

    async def switch_at(self, stage) -> None:
        if self.stage == stage:
            self.settings.current_model_name = "new-model"
            self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
            self.settings.grounding_enabled = False
        await asyncio.sleep(0)

    def assert_response_settings(self, *, model, prompt_file, grounding) -> None:
        args, kwargs = self.generations[-1]
        self.assertEqual(args[1], model)
        self.assertIn(f"ペルソナ: {prompt_file}", args[0])
        self.assertEqual(kwargs["grounding"], grounding)
        self.assertEqual(self.feedback.track_reply.call_args.kwargs, {
            "target_user_id": "11", "model_name": model, "prompt_file": prompt_file,
        })
        # テキストの返信は読み上げない（#268。声の会話は /join の Gemini Live が担う）。
        self.voice.assert_not_awaited()

    async def check_old_settings_are_used(self) -> None:
        await self.on_message(self.message)
        self.assert_response_settings(model="old-model", prompt_file="../prompts/prompt_default.txt", grounding=True)

    async def test_generation_change_uses_old_settings_then_next_reply_uses_new(self) -> None:
        await self.check_old_settings_are_used()
        await self.on_message(self.message)
        self.assert_response_settings(model="new-model", prompt_file="../prompts/prompt_maid.txt", grounding=False)

    async def test_images_are_closed_on_success_early_return_exception_and_cancellation(self) -> None:
        from PIL import Image
        from persona_bot.core import message_handler

        for case in ("success", "image_only", "prompt", "history", "generate", "cancel", "empty", "send"):
            with self.subTest(case=case):
                images = [Image.new("RGB", (2, 2)) for _ in range(2)]

                async def generate(*args, **kwargs):
                    for image in images:
                        image.getpixel((0, 0))
                    if case == "generate":
                        raise RuntimeError("架空の生成失敗")
                    if case == "cancel":
                        raise asyncio.CancelledError
                    return (None if case == "empty" else "架空の回答"), None, [], args[1]

                with patch.object(message_handler, "load_image_attachments", AsyncMock(return_value=images)), patch.object(
                    message_handler, "load_persona_template", side_effect=FileNotFoundError if case == "prompt" else None,
                    return_value="架空のペルソナ",
                ), patch.object(message_handler, "fetch_channel_history_text", AsyncMock(
                    side_effect=RuntimeError("架空の履歴失敗") if case == "history" else None, return_value="",
                )), patch.object(message_handler, "send_long_reply", AsyncMock(
                    side_effect=RuntimeError("架空の送信失敗") if case == "send" else None,
                )), patch.object(self.namespace["gemini"], "generate_with_metadata", AsyncMock(side_effect=generate)):
                    call = self.handler.handle_chat_message(self.message, "" if case == "image_only" else "架空の質問")
                    if case in ("history", "generate", "send", "cancel"):
                        with self.assertRaises(asyncio.CancelledError if case == "cancel" else RuntimeError):
                            await call
                    else:
                        await call
                for image in images:
                    with self.assertRaises(ValueError):
                        image.getpixel((0, 0))

    async def test_borrowed_images_are_not_reloaded_or_closed_by_chat(self) -> None:
        """借用画像は成功・失敗・中断でも再読込せず、所有者に解放を任せる。"""
        from PIL import Image
        from persona_bot.core import message_handler

        for case in ("成功", "生成失敗", "履歴失敗", "中断"):
            with self.subTest(終了=case):
                images = [Image.new("RGB", (2, 2))]
                self.addCleanup(images[0].close)

                async def generate(*args, **kwargs):
                    self.assertIs(args[2], images)
                    if case == "生成失敗":
                        raise RuntimeError("架空の生成失敗")
                    if case == "中断":
                        raise asyncio.CancelledError
                    return "架空の返信", None, [], args[1]

                with patch.object(message_handler, "load_image_attachments", AsyncMock()) as loader, patch.object(
                    message_handler, "fetch_channel_history_text", AsyncMock(
                        side_effect=RuntimeError("架空の履歴失敗") if case == "履歴失敗" else None,
                        return_value="",
                    ),
                ), patch.object(self.namespace["gemini"], "generate_with_metadata", AsyncMock(side_effect=generate)):
                    call = self.handler.handle_chat_message(self.message, "架空の質問", image_objects=images)
                    if case == "成功":
                        await call
                    else:
                        with self.assertRaises(asyncio.CancelledError if case == "中断" else RuntimeError):
                            await call
                loader.assert_not_awaited()
                images[0].getpixel((0, 0))
                images[0].close()

    async def test_ended_comment_stream_reuses_and_releases_preloaded_images(self) -> None:
        """配信の画像確認中に終了しても通常応答で二重に展開せず、外側で一度だけ閉じる。"""
        from PIL import Image
        from persona_bot.core import message_handler

        image = Image.new("RGB", (2, 2))
        self.addCleanup(image.close)
        images = [image]
        self.message.content = ""
        self.message.attachments = [SimpleNamespace(filename="fictional.png", content_type="image/png", size=10)]
        self.namespace["autonomous"].is_comment_channel.side_effect = [True, False, False]

        async def generate(*args, **kwargs):
            self.assertIs(args[2], images)
            image.getpixel((0, 0))
            return "架空の返信", None, [], args[1]

        with patch.object(message_handler, "load_image_attachments", AsyncMock(return_value=images)) as loader, patch.object(
            self.namespace["gemini"], "generate_with_metadata", AsyncMock(side_effect=generate),
        ), patch.object(image, "close", wraps=image.close) as close:
            await self.handler.on_message(self.message)
            loader.assert_awaited_once()
            close.assert_called_once()
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))

    async def test_settings_are_captured_before_typing_await(self) -> None:
        self.stage = "typing"
        await self.check_old_settings_are_used()

    async def test_attachment_wait_does_not_mix_old_and_new_settings(self) -> None:
        self.stage = "images"
        await self.check_old_settings_are_used()

    async def test_moderation_change_does_not_relabel_feedback(self) -> None:
        self.stage = "moderate"
        await self.check_old_settings_are_used()

    async def test_send_change_does_not_relabel_feedback(self) -> None:
        self.stage = "send"
        await self.check_old_settings_are_used()

    async def test_dm_reply_uses_same_snapshot(self) -> None:
        self.settings = self.state.get_guild(None)
        self.configure_old()
        self.message.guild = None
        self.message.channel = Mock(spec=discord.DMChannel, id=10)

        @asynccontextmanager
        async def typing():
            yield

        self.message.channel.typing.side_effect = typing
        await self.check_old_settings_are_used()

    async def test_generation_failure_is_not_tracked_or_spoken(self) -> None:
        self.response = None
        await self.on_message(self.message)
        self.feedback.track_reply.assert_not_called()
        self.voice.assert_not_awaited()
        self.message.reply.assert_awaited_once()

    async def test_affinity_tag_saves_only_the_reply_authors_changes(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        affinity = self.namespace["affinity"]
        affinity.update_emotion = Mock()
        affinity.save_background = AsyncMock()
        await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        affinity.update_emotion.assert_called_once_with("11", 1, 2)
        affinity.save_background.assert_awaited_once_with("11")

    async def test_affinity_is_unchanged_while_moderation_is_pending(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        affinity = self.namespace["affinity"]

        async def moderate(*args, **kwargs):
            affinity.update_emotion.assert_not_called()
            affinity.save_background.assert_not_awaited()
            return True, ""

        self.namespace["moderator"].check.side_effect = moderate
        await self.on_message(self.message)
        affinity.update_emotion.assert_called_once_with("11", 1, 2)

    async def test_affinity_changes_only_after_reply_send_completes(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        affinity = self.namespace["affinity"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return SimpleNamespace(id=500)

        self.namespace["send_long_reply"].side_effect = send
        task = asyncio.create_task(self.on_message(self.message))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            affinity.update_emotion.assert_not_called()
            affinity.save_background.assert_not_awaited()
            release.set()
            await asyncio.wait_for(task, 2)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*self.tasks)
        affinity.update_emotion.assert_called_once_with("11", 1, 2)
        affinity.save_background.assert_awaited_once_with("11")

    async def test_failed_or_canceled_reply_does_not_change_affinity(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        affinity = self.namespace["affinity"]
        for error in (discord.Forbidden(Mock(status=403), "架空の拒否"),
                      OSError("架空の通信失敗"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.namespace["send_long_reply"].side_effect = error
                with self.assertRaises(type(error)):
                    await self.handler.handle_chat_message(self.message, "架空の質問")
                affinity.update_emotion.assert_not_called()
                affinity.save_background.assert_not_awaited()
                self.feedback.track_reply.assert_not_called()
                self.namespace["memory"].append_pending_log.assert_not_awaited()

    async def test_affinity_failure_after_send_keeps_memory_and_feedback(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        self.namespace["affinity"].update_emotion.side_effect = RuntimeError("架空の更新失敗")
        with self.assertLogs("persona_bot.core.message_handler", level="WARNING"):
            await self.on_message(self.message)
        self.voice.assert_not_awaited()
        self.feedback.track_reply.assert_called_once()
        self.namespace["memory"].append_pending_log.assert_awaited_once()
        self.namespace["send_long_reply"].assert_awaited_once()

    async def test_missing_persona_is_logged_without_file_name_or_exception_details(self) -> None:
        self.loader.side_effect = FileNotFoundError("架空の内部ファイル詳細")
        with self.assertLogs("persona_bot.core.message_handler", level="WARNING") as logs:
            await self.on_message(self.message)
        reply = self.message.reply.await_args.args[0]
        self.assertNotIn(self.settings.current_prompt_file, reply)
        self.assertNotIn("架空の内部ファイル詳細", reply)
        self.assertIn("管理者", reply)
        log_text = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
        self.assertIn("プロンプトファイルが見つかりません", log_text)
        self.assertNotIn(self.settings.current_prompt_file, log_text)
        self.assertNotIn("架空の内部ファイル詳細", log_text)
        self.assertIs(logs.records[0].exc_info[1], self.loader.side_effect)
        self.namespace["gemini"].generate_with_metadata.assert_not_awaited()
        self.namespace["affinity"].update_emotion.assert_not_called()

    async def test_rejected_reply_does_not_update_affinity_memory_or_feedback(self) -> None:
        self.response = "架空の応答です。[V:1, A:2]"
        self.namespace["moderator"].check.side_effect = None
        self.namespace["moderator"].check.return_value = (False, "架空の不適切判定")
        await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        self.namespace["affinity"].update_emotion.assert_not_called()
        self.namespace["affinity"].save_background.assert_not_awaited()
        self.namespace["memory"].append_pending_log.assert_not_awaited()
        self.namespace["memory"].flush_if_needed.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()
        apology = "申し訳ありませんが、適切な応答を生成できませんでした。"
        self.namespace["send_long_reply"].assert_awaited_once_with(self.message, apology, suppress_embeds=False)
        self.voice.assert_not_awaited()

    async def test_tag_only_reply_does_not_change_affinity_or_memory(self) -> None:
        self.response = " [V:1, A:2] "
        await self.on_message(self.message)
        self.namespace["affinity"].update_emotion.assert_not_called()
        self.namespace["affinity"].save_background.assert_not_awaited()
        self.namespace["memory"].append_pending_log.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()

    async def test_grounding_previews_are_suppressed_only_for_accepted_reply(self) -> None:
        sources = [{"title": "架空の参考資料", "uri": "https://example.com/reference"}]
        generator = self.namespace["gemini"].generate_with_metadata
        generator.side_effect = None
        generator.return_value = (self.response, None, sources, "selected-model")
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                moderator = self.namespace["moderator"].check
                moderator.side_effect = None
                moderator.return_value = (accepted, "")
                sent = self.namespace["send_long_reply"]
                sent.reset_mock()
                await self.handler.handle_chat_message(self.message, "架空の質問")
                self.assertEqual(sent.await_args.kwargs["suppress_embeds"], accepted)
                self.assertEqual("https://example.com/reference" in sent.await_args.args[1], accepted)

    async def test_actual_generator_model_and_captured_persona_reach_feedback_database(self) -> None:
        from persona_bot.core import message_handler

        unsupported = errors.ClientError(400, {"error": {"message": "google_search is not supported"}})
        cases = [
            (True, [], "selected-model"),
            (True, [unsupported], GROUNDING_MODEL_NAME),
            (True, [unsupported, unsupported], "selected-model"),
            (False, [], "selected-model"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "bot.db")
            db.initialize()
            try:
                feedback = FeedbackService(SimpleNamespace(user=SimpleNamespace(id=99)), db)
                self.handler.feedback = feedback
                for index, (grounding, failures, expected) in enumerate(cases):
                    with self.subTest(grounding=grounding, failures=len(failures)):
                        self.configure_old()
                        sdk_request = self.use_real_gemini()
                        self.settings.current_model_name = "selected-model"
                        self.settings.grounding_enabled = grounding
                        responses = [*failures, sdk_text_response("架空の回答です。")]

                        async def generate(**kwargs):
                            # 生成待機中の変更を、実際の生成モデル・今回のペルソナへ混ぜない。
                            self.settings.current_model_name = "new-model"
                            self.settings.current_prompt_file = "../prompts/prompt_maid.txt"
                            result = responses.pop(0)
                            if isinstance(result, Exception):
                                raise result
                            return result

                        sdk_request.side_effect = generate
                        sent = SimpleNamespace(id=500 + index, guild=SimpleNamespace(id=1),
                                               channel=SimpleNamespace(id=10))
                        with patch.object(message_handler, "send_long_reply", AsyncMock(return_value=sent)), \
                                patch.object(feedback, "_claim_reaction_slot", return_value=False), \
                                patch("persona_bot.services.feedback_service.FEEDBACK_ENABLED", True):
                            await self.on_message(self.message)
                            await feedback.handle_reaction_add(SimpleNamespace(
                                message_id=sent.id, user_id=11, emoji="👍", member=SimpleNamespace(bot=False),
                            ))
                        rows = {row["model_name"]: row for row in db.feedback_summary(1)}
                        self.assertIn(expected, rows)
                        self.assertTrue(all(row["prompt_file"] == "../prompts/prompt_default.txt" for row in rows.values()))
                        self.assertEqual(feedback._tracked[sent.id].model_name, expected)
                self.assertEqual({name: row["good"] for name, row in rows.items()},
                                 {"selected-model": 3, GROUNDING_MODEL_NAME: 1})
            finally:
                db.close()

    # ---- 送信後の付随処理の失敗境界（#157） --------------------------------------

    def assert_sent_once(self) -> None:
        from persona_bot.core import message_handler
        message_handler.send_long_reply.assert_awaited_once()
        self.assertEqual(len(self.generations), 1)  # 再生成しない
        # テキストの返信は読み上げない（#268）。
        self.voice.assert_not_awaited()

    async def test_saved_reply_is_remembered_and_compacted(self) -> None:
        memory = self.namespace["memory"]
        await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        memory.append_pending_log.assert_awaited_once_with("11", "架空の質問です。", "架空の応答です。")
        memory.flush_if_needed.assert_awaited_once()
        self.assert_sent_once()

    async def test_memory_save_failure_after_send_keeps_the_sent_reply(self) -> None:
        memory = self.namespace["memory"]
        memory.append_pending_log.side_effect = OSError("架空の記憶保存失敗")
        with self.assertLogs("persona_bot.core.message_handler", level="ERROR") as logs:
            await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        self.assert_sent_once()
        self.feedback.track_reply.assert_called_once()
        # 保存できなかった会話を成功扱いにせず、記憶圧縮も起動しない
        memory.flush_if_needed.assert_not_awaited()
        messages = "\n".join(LOG_FORMATTER.format(record) for record in logs.records)
        self.assertIn("記憶への追記に失敗しました", messages)
        self.assertIn("OSError", messages)
        self.assertIn("user_id=<ID>", messages)
        self.assertNotIn("架空の記憶保存失敗", messages)
        self.assertTrue(any(record.exc_info and record.exc_info[1] is memory.append_pending_log.side_effect for record in logs.records))

    async def test_feedback_registration_failure_does_not_skip_memory(self) -> None:
        self.feedback.track_reply.side_effect = RuntimeError("架空の登録失敗")
        with self.assertLogs("persona_bot.core.message_handler", level="ERROR"):
            await self.on_message(self.message)
        await asyncio.gather(*self.tasks)
        self.namespace["memory"].append_pending_log.assert_awaited_once()
        self.assert_sent_once()

    async def test_send_failure_is_not_spoken_remembered_or_tracked(self) -> None:
        from persona_bot.core import message_handler
        with patch.object(message_handler, "send_long_reply", AsyncMock(side_effect=RuntimeError("架空の送信失敗"))):
            with self.assertRaises(RuntimeError):
                await self.on_message(self.message)
        self.voice.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()
        self.namespace["memory"].append_pending_log.assert_not_awaited()

    async def test_cancellation_during_memory_save_still_propagates(self) -> None:
        self.namespace["memory"].append_pending_log.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.on_message(self.message)
        self.voice.assert_not_awaited()
        self.namespace["memory"].flush_if_needed.assert_not_awaited()

    async def test_autonomous_comment_passes_snapshot_to_generation_and_voice(self) -> None:
        self.namespace["autonomous"].is_comment_channel.return_value = True
        self.namespace["autonomous"].can_speak_session = Mock(return_value=True)

        async def comment(*args, **kwargs):
            await self.switch_at("generate")
            return self.response

        self.namespace["autonomous"].handle_comment = AsyncMock(side_effect=comment)
        await self.on_message(self.message)
        self.namespace["gemini"].generate_with_metadata.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()
        settings = self.namespace["autonomous"].handle_comment.await_args.kwargs["response_settings"]
        self.assertEqual(settings.model_name, "old-model")
        self.assertTrue(settings.grounding_enabled)
        self.voice.assert_awaited_once_with(
            self.message, self.response, settings.prompt_file, is_valid=ANY,
        )
        self.assertEqual(settings.prompt_file, "../prompts/prompt_default.txt")

    def use_real_gemini(self, *responses) -> AsyncMock:
        """SDKの生成要求だけを差し替え、実際の GeminiService で検索の退避と本文の検証を通す。"""
        sdk_request = AsyncMock(side_effect=list(responses))
        self.handler.gemini = GeminiService(SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(
            generate_content=sdk_request,
        ))))
        self.stage = None
        self.settings.current_model_name = "gemini-3.5-flash-lite"
        return sdk_request

    def assert_only_final_answer_flows(self, answer: str) -> str:
        send = self.namespace["send_long_reply"]
        send.assert_awaited_once()
        sent_text = send.await_args.args[1]
        self.assertTrue(sent_text.startswith(answer))
        for marker in ("tool_code", "google_search", "thought", "I should answer", "架空の天気"):
            self.assertNotIn(marker, sent_text)
        # 本文のマスクには検証後の回答だけを渡し、参考リンクは別にマスクする。
        calls = self.namespace["ng_filter"].mask_with_log.call_args_list
        masked = [call.args[0] for call in calls if call.kwargs.get("label") == "Bot応答"]
        self.assertEqual(masked, [answer])
        for call in calls:
            self.assertNotIn("tool_code", call.args[0])
            self.assertNotIn("https://example.com/discarded", call.args[0])
        self.assertEqual(self.namespace["memory"].append_pending_log.await_args.args[2], answer)
        self.assertEqual(self.namespace["usage"].log_snapshot.call_args.args[4], answer)
        self.voice.assert_not_awaited()
        self.feedback.track_reply.assert_called_once()
        return sent_text

    async def test_internal_text_after_search_quota_is_regenerated_before_reply(self) -> None:
        sdk_request = self.use_real_gemini(
            errors.ClientError(429, {"error": {"message": "架空の利用枠超過"}}),
            sdk_text_response(LEAKED_TEXT, uri="https://example.com/discarded"),
            sdk_text_response("架空の最終回答です。", uri="https://example.com/source"),
        )
        await self.on_message(self.message)
        self.assertEqual([call.kwargs["model"] for call in sdk_request.call_args_list],
                         ["gemini-3.5-flash-lite", GROUNDING_MODEL_NAME, GROUNDING_MODEL_NAME])
        self.assertEqual([bool(call.kwargs["config"].tools) for call in sdk_request.call_args_list], [True, True, True])
        sent_text = self.assert_only_final_answer_flows("架空の最終回答です。")
        # 参考リンクは採用した応答のものだけを送信用の本文へ添える。
        self.assertIn("https://example.com/source", sent_text)
        self.assertNotIn("https://example.com/discarded", sent_text)

    async def test_internal_text_without_search_is_regenerated_before_reply(self) -> None:
        sdk_request = self.use_real_gemini(
            sdk_text_response(LEAKED_TEXT), sdk_text_response("架空の最終回答です。"),
        )
        self.settings.grounding_enabled = False
        await self.on_message(self.message)
        self.assertEqual([call.kwargs["model"] for call in sdk_request.call_args_list],
                         ["gemini-3.5-flash-lite", "gemini-3.5-flash-lite"])
        self.assertEqual([bool(call.kwargs["config"].tools) for call in sdk_request.call_args_list], [False, False])
        self.assert_only_final_answer_flows("架空の最終回答です。")

    async def test_repeated_internal_text_is_not_sent_remembered_or_spoken(self) -> None:
        sdk_request = self.use_real_gemini(
            sdk_text_response(LEAKED_TEXT),
            sdk_text_response('tool_code\nprint(google_search.search(queries=["架空の語"]))'),
            sdk_text_response("使われない架空の応答"),
        )
        self.settings.grounding_enabled = False
        await self.on_message(self.message)
        self.assertEqual(sdk_request.await_count, 2)
        # 定型の失敗案内だけを返し、内部形式は送信・記憶・ログ・読み上げのどこへも渡さない。
        self.message.reply.assert_awaited_once()
        self.assertNotIn("tool_code", self.message.reply.await_args.args[0])
        self.namespace["send_long_reply"].assert_not_awaited()
        self.namespace["ng_filter"].mask_with_log.assert_not_called()
        self.namespace["memory"].append_pending_log.assert_not_awaited()
        self.namespace["usage"].log_snapshot.assert_not_called()
        self.voice.assert_not_awaited()
        self.feedback.track_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
