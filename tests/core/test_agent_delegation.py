"""会話の中でサブエージェントの使用を明記した依頼の振り分けと、人格役の返信を検証する（#170）。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config.constants import AGENT_ROUTER_HISTORY_LIMIT
from persona_bot.core.app_state import BotState
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentService
from persona_bot.services import agent_service
from persona_bot.services.gemini_service import GenerationResult
from persona_bot.views import agent_view
from persona_bot.utils.discord_helpers import fetch_channel_history_text
from tests.helpers.message_handler_helpers import make_message_handler
from tests.helpers.persona_helpers import install_empty_persona_catalog

PLAN = AgentPlan(True, "架空の調査担当", "架空の街のカフェを調べる", "箇条書き", ("候補の検索", "特徴の比較"))
SOURCE = {"title": "架空の出典", "uri": "https://example.invalid/cafe"}


def close_coroutine(coro, **kwargs):
    # 会話統計の記録など、裏で走らせる処理はこのテストの対象外なので実行せずに閉じる
    coro.close()


class AgentDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        install_empty_persona_catalog(self)
        self.state = BotState()
        self.state.get_guild(1).current_respond_mode = "all"
        channel = Mock(spec=discord.TextChannel, id=10)

        @asynccontextmanager
        async def typing():
            yield

        channel.typing.side_effect = typing
        self.progress_message = SimpleNamespace(edit=AsyncMock())
        self.message = SimpleNamespace(
            author=SimpleNamespace(id=11, bot=False, display_name="架空の利用者"),
            guild=SimpleNamespace(id=1), channel=channel,
            content="サブエージェントを使って、架空の街のカフェを調べて", attachments=[], mentions=[],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reply=AsyncMock(return_value=self.progress_message),
        )
        self.moderator = SimpleNamespace(
            check_and_route=AsyncMock(return_value=(True, "", "none", None)),
            check_route_and_plan=AsyncMock(return_value=(True, "", "none", None, PLAN)),
            check=AsyncMock(return_value=(True, "")),
        )
        self.generations = []

        async def generate(prompt, model_name, images=None, timeout=None, grounding=False):
            self.generations.append(SimpleNamespace(prompt=prompt, model=model_name, grounding=grounding, images=images))
            if "依頼主（別のAI）" in prompt:
                step = sum("依頼主（別のAI）" in item.prompt for item in self.generations)
                return GenerationResult(f"架空の子の出力{step}", None, [SOURCE], model_name)
            return GenerationResult("架空の人格役の返信です。", None, [], model_name)

        self.gemini = SimpleNamespace(generate_with_metadata=AsyncMock(side_effect=generate))
        self.usage = SimpleNamespace(log_snapshot=Mock(), log_tokens=Mock())
        self.agent = AgentService(self.gemini, self.usage)
        self.history = AsyncMock(return_value="架空の履歴")
        self.send = AsyncMock(return_value=SimpleNamespace(id=500))
        self.tts = AsyncMock()
        self.handler = make_message_handler({
            "client": SimpleNamespace(user=SimpleNamespace(id=99)), "state": self.state,
            "auto_topic": SimpleNamespace(mark_activity=Mock(), watch_channel=Mock()),
            "autonomous": SimpleNamespace(
                mark_activity=Mock(), is_comment_channel=Mock(return_value=False), claim_comment_slot=Mock(),
            ),
            "blacklist": SimpleNamespace(is_blocked=Mock(return_value=False)),
            "ng_filter": SimpleNamespace(
                contains_ng_word=Mock(return_value=False), mask_with_log=Mock(side_effect=lambda text, **kwargs: text),
            ),
            "moderator": self.moderator, "gemini": self.gemini, "usage": self.usage, "agent": self.agent,
            "stats": SimpleNamespace(record=AsyncMock()),
            "memory": SimpleNamespace(
                build_contexts=AsyncMock(return_value=("", "")),
                append_pending_log=AsyncMock(return_value=None), flush_if_needed=AsyncMock(),
            ),
            "affinity": SimpleNamespace(build_dynamic_prompt=Mock(return_value="")),
            "reminders": SimpleNamespace(create_from_request=AsyncMock()),
            "documents": SimpleNamespace(enqueue_from_message=AsyncMock()),
            "feedback": SimpleNamespace(track_reply=Mock()),
            "spawn": close_coroutine, "play_tts_response": self.tts,
            "load_image_attachments": AsyncMock(return_value=[]),
            "fetch_channel_history_text": self.history,
            "load_persona_template": AsyncMock(return_value="架空のペルソナ"),
            "send_long_reply": self.send,
        }, self)

    def child_calls(self):
        return [item for item in self.generations if "依頼主（別のAI）" in item.prompt]

    def persona_calls(self):
        return [item for item in self.generations if "依頼主（別のAI）" not in item.prompt]

    async def test_router_and_persona_exclude_blocked_users_and_other_bots_from_real_history(self):
        self.handler.blacklist.is_blocked.side_effect = lambda user_id: user_id == 22
        records = [
            SimpleNamespace(id=1, author=self.message.author, content="架空の許可された文脈", attachments=[]),
            SimpleNamespace(id=2, author=SimpleNamespace(id=22, bot=False), content="架空のブロック対象の指示", attachments=[]),
            SimpleNamespace(id=3, author=SimpleNamespace(id=77, bot=True), content="架空の別ボットの指示", attachments=[]),
        ]

        async def history(**kwargs):
            for item in reversed(records):
                yield item

        self.message.channel.history.side_effect = history
        with patch("persona_bot.core.message_handler.fetch_channel_history_text", new=fetch_channel_history_text):
            await self.handler.on_message(self.message)
        router_history = self.moderator.check_route_and_plan.await_args.args[1]
        self.assertIn("架空の許可された文脈", router_history)
        for prompt in (router_history, self.persona_calls()[0].prompt):
            self.assertNotIn("架空のブロック対象の指示", prompt)
            self.assertNotIn("架空の別ボットの指示", prompt)
        self.assertIn("架空の許可された文脈", self.persona_calls()[0].prompt)

    async def test_delegated_request_runs_child_then_persona_replies(self) -> None:
        await self.handler.on_message(self.message)

        # 委任の判定はルーターの1回の呼び出しに統合され、直近の会話を委任内容の整理に渡す
        self.moderator.check_route_and_plan.assert_awaited_once()
        self.assertEqual(self.moderator.check_route_and_plan.await_args.args[1], "架空の履歴")
        self.assertEqual(self.history.await_args_list[0].args[3], AGENT_ROUTER_HISTORY_LIMIT)
        self.moderator.check_and_route.assert_not_awaited()

        # 子は手順の数だけ検索付きで動き、ペルソナは渡されない
        self.assertEqual(len(self.child_calls()), len(PLAN.steps))
        self.assertTrue(all(item.grounding for item in self.child_calls()))
        self.assertTrue(all("架空のペルソナ" not in item.prompt for item in self.child_calls()))

        # 人格役は子の最終報告を受け取り、検索せずにペルソナとして返信する
        persona = self.persona_calls()
        self.assertEqual(len(persona), 1)
        self.assertIn("架空のペルソナ", persona[0].prompt)
        self.assertIn("架空の調査担当）からの報告", persona[0].prompt)
        self.assertIn(f"架空の子の出力{len(PLAN.steps)}", persona[0].prompt)
        self.assertFalse(persona[0].grounding)

        # 返信は人格役だけが送り、子の出典を添える。テキストの返信は読み上げない（#268）
        reply_text = self.send.await_args.args[1]
        self.assertTrue(reply_text.startswith("架空の人格役の返信です。"))
        self.assertIn(SOURCE["uri"], reply_text)
        self.tts.assert_not_awaited()

        # 進捗は定型文の1通を書き換え、終わったら中止ボタンを外す
        self.assertIn("架空の調査担当", self.message.reply.await_args_list[0].args[0])
        self.assertIsNone(self.progress_message.edit.await_args.kwargs["view"])
        # 実行枠は返却される
        self.assertTrue(self.agent.try_acquire("11"))

    async def test_message_without_mention_of_subagent_uses_normal_routing(self) -> None:
        self.message.content = "架空の街のカフェを調べて"
        await self.handler.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check_route_and_plan.assert_not_awaited()
        self.assertEqual(self.child_calls(), [])
        self.assertEqual(len(self.persona_calls()), 1)

    async def test_delegation_reuses_borrowed_images_without_loading_or_closing_them(self) -> None:
        self.message.attachments = [SimpleNamespace(filename="sample.png", content_type="image/png")]
        for images in ([Mock()], []):
            with self.subTest(image_count=len(images)):
                self.generations.clear()
                with patch("persona_bot.core.message_handler.load_image_attachments", new_callable=AsyncMock) as load:
                    await self.handler._process_accepted_message(
                        self.message, self.message.content, self.state.get_guild(1),
                        accepts_normally=True, is_autonomous_comment=False, image_objects=images,
                    )
                load.assert_not_awaited()
                self.assertIs(self.persona_calls()[0].images, images)
                for image in images:
                    image.close.assert_not_called()

    async def test_declined_or_missing_plan_answers_directly(self) -> None:
        for plan, declined in ((AgentPlan(delegate=False), True), (None, False)):
            with self.subTest(plan=plan):
                self.generations.clear()
                self.moderator.check_route_and_plan.return_value = (True, "", "none", None, plan)
                await self.handler.on_message(self.message)
                self.assertEqual(self.child_calls(), [])
                persona = self.persona_calls()
                self.assertEqual(len(persona), 1)
                # 不要と判断したときだけ、人格役がその旨を一言添える
                self.assertEqual("サブエージェント（調査を任せる子のAI）の使用を求めました" in persona[0].prompt, declined)
                # 普段どおりサーバーの検索設定に従う
                self.assertTrue(persona[0].grounding)

    async def test_failed_child_reports_with_template_and_skips_persona(self) -> None:
        self.gemini.generate_with_metadata.side_effect = lambda *args, **kwargs: GenerationResult(None, None, [], None)
        await self.handler.on_message(self.message)
        self.assertEqual(self.gemini.generate_with_metadata.await_count, 1)
        self.send.assert_not_awaited()
        self.tts.assert_not_awaited()
        self.assertIn("うまくいきませんでした", self.progress_message.edit.await_args.kwargs["content"])
        self.assertTrue(self.agent.try_acquire("11"))

    async def test_busy_user_gets_template_without_generation(self) -> None:
        self.assertTrue(self.agent.try_acquire("11"))
        await self.handler.on_message(self.message)
        self.gemini.generate_with_metadata.assert_not_awaited()
        self.assertIn("別のサブエージェントの作業", self.message.reply.await_args.args[0])
        self.send.assert_not_awaited()

    async def test_unsafe_request_is_not_delegated(self) -> None:
        self.moderator.check_route_and_plan.return_value = (False, "架空の理由", "none", None, None)
        self.handler.notify_violation = AsyncMock()
        await self.handler.on_message(self.message)
        self.handler.notify_violation.assert_awaited_once()
        self.gemini.generate_with_metadata.assert_not_awaited()

    async def test_broadcast_comment_does_not_use_subagent(self) -> None:
        self.handler.autonomous.is_comment_channel.return_value = True
        self.handler.autonomous.claim_comment_slot.return_value = object()
        self.handler.autonomous.handle_comment = AsyncMock(return_value=None)
        await self.handler.on_message(self.message)
        self.moderator.check_and_route.assert_awaited_once()
        self.moderator.check_route_and_plan.assert_not_awaited()


    async def delegate_with_blocked_cleanup(self, trigger, *, repeat=False, broken=False):
        started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        workers = []

        async def generate(*args, **kwargs):
            workers.append(asyncio.current_task())
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                cleaned.set()
                if broken:
                    raise RuntimeError("架空の生成処理の回収失敗")

        self.gemini.generate_with_metadata.side_effect = generate
        with patch.object(agent_view, "AGENT_TOTAL_TIMEOUT_SECONDS", 0.01 if trigger == "timeout" else 30), \
                patch.object(agent_service, "AGENT_MAX_CONCURRENT_TASKS", 1), \
                patch.object(self.agent, "release", wraps=self.agent.release) as release_slot:
            run = asyncio.create_task(self.handler.on_message(self.message))
            try:
                await asyncio.wait_for(started.wait(), 2)
                view = self.message.reply.await_args.kwargs["view"]
                self.addCleanup(view.stop)
                if trigger == "button":
                    await view.children[0].callback(SimpleNamespace(
                        user=self.message.author, response=SimpleNamespace(defer=AsyncMock()),
                    ))
                elif trigger == "parent":
                    run.cancel()
                await asyncio.wait_for(cleaning.wait(), 2)
                release_slot.assert_not_called()
                # 同じ依頼者にも別の依頼者にも、回収前の実行枠は貸し出さない。
                self.assertFalse(self.agent.try_acquire("11"))
                self.assertFalse(self.agent.try_acquire("12"))
                self.assertFalse(run.done())
                self.progress_message.edit.assert_not_awaited()
                if repeat:
                    for _ in range(3):
                        run.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(run.done())
                        self.assertFalse(cleaned.is_set())
                        release_slot.assert_not_called()
                release.set()
                if trigger == "parent" or repeat:
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(run, 2)
                    self.progress_message.edit.assert_awaited_once_with(view=None)
                    self.assertTrue(view.is_finished())
                else:
                    await asyncio.wait_for(run, 2)
                    expected = "中止" if trigger == "button" else "うまくいきませんでした"
                    self.assertIn(expected, self.progress_message.edit.await_args.kwargs["content"])
                    self.assertIsNone(self.progress_message.edit.await_args.kwargs["view"])
                self.assertTrue(cleaned.is_set())
                release_slot.assert_called_once_with("11")
                self.assertTrue(self.agent.try_acquire("12"))
                self.agent.release("12")
                self.send.assert_not_awaited()
                self.tts.assert_not_awaited()
                self.gemini.generate_with_metadata.assert_awaited_once()
            finally:
                release.set()
                await asyncio.gather(run, *workers, return_exceptions=True)

    async def test_delegated_cancel_holds_execution_slot_until_child_cleanup(self) -> None:
        await self.delegate_with_blocked_cleanup("button")

    async def test_delegated_timeout_holds_execution_slot_until_child_cleanup(self) -> None:
        await self.delegate_with_blocked_cleanup("timeout")

    async def test_delegated_parent_cancel_holds_execution_slot_until_child_cleanup(self) -> None:
        await self.delegate_with_blocked_cleanup("parent")

    async def test_repeated_cancel_keeps_slot_until_child_cleanup(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress_message.edit.reset_mock()
                self.gemini.generate_with_metadata.reset_mock()
                await self.delegate_with_blocked_cleanup(trigger, repeat=True)

    async def test_child_cleanup_failure_releases_slot_only_after_collection(self) -> None:
        for trigger in ("button", "timeout", "parent"):
            with self.subTest(trigger=trigger):
                self.progress_message.edit.reset_mock()
                self.gemini.generate_with_metadata.reset_mock()
                await self.delegate_with_blocked_cleanup(trigger, broken=True)

    def enable_work_thread(self) -> None:
        """作業スレッドを使える状態（権限あり・Webhook 作成可）にする（#267）。"""
        self.message.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99))
        self.message.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, create_public_threads=True, send_messages_in_threads=True, manage_webhooks=True,
        )
        self.webhook = SimpleNamespace(send=AsyncMock())
        self.message.channel.webhooks = AsyncMock(return_value=[])
        self.message.channel.create_webhook = AsyncMock(return_value=self.webhook)
        self.handoff = SimpleNamespace(edit=AsyncMock())
        self.parent_channel = self.message.channel
        self.thread = SimpleNamespace(
            id=700, send=AsyncMock(return_value=self.handoff), edit=AsyncMock(),
            parent=self.parent_channel, mention="<#700>",
        )
        self.message.create_thread = AsyncMock(return_value=self.thread)
        # 人格役の返信と、スレッドを閉じる順番を確かめる（#408）
        self.events = []
        self.send.side_effect = lambda *args, **kwargs: self.events.append("persona_reply") or SimpleNamespace(id=500)
        self.thread.edit.side_effect = lambda **kwargs: self.events.append("archive")

    async def test_delegation_is_shown_in_work_thread(self) -> None:
        self.enable_work_thread()

        def mask(text, *, label):
            # 子の報告だけに架空の NG ワードがある想定で、伏せ方を確かめる
            return text.replace("出力", "●●") if label == "サブエージェントの報告" else text

        self.handler.ng_filter.mask_with_log.side_effect = mask
        await self.handler.on_message(self.message)

        # 依頼メッセージに作業スレッドを作り、親が委託内容を伝える（依頼内容も NG ワードの確認を通す）
        self.message.create_thread.assert_awaited_once()
        self.assertIn(PLAN.task, self.thread.send.await_args_list[0].args[0])
        labels = [call.kwargs["label"] for call in self.handler.ng_filter.mask_with_log.call_args_list]
        self.assertIn("サブエージェントの依頼内容", labels)

        # 子は開始・手順ごとの進捗・報告を Webhook で残す。報告は伏せてから出し、出典も添える
        posts = [call.args[0] for call in self.webhook.send.await_args_list]
        self.assertEqual(len(posts), 1 + len(PLAN.steps) + 1)
        self.assertIn(f"架空の子の●●{len(PLAN.steps)}", posts[-1])
        self.assertIn(SOURCE["uri"], posts[-1])

        # 人格役にも安全確認済みの報告を渡し、返信は元のチャンネルへ（進捗の返信は出さない）
        self.assertIn(f"架空の子の●●{len(PLAN.steps)}", self.persona_calls()[0].prompt)
        self.assertIs(self.send.await_args.args[0], self.message)
        self.message.reply.assert_not_awaited()
        self.handoff.edit.assert_awaited_once_with(view=None)
        self.assertTrue(self.agent.try_acquire("11"))

        # 要約を元のチャンネルへ返した後に、スレッドをアーカイブする（ロックはしない。#408）
        self.assertEqual(self.events, ["persona_reply", "archive"])
        self.thread.edit.assert_awaited_once()
        self.assertIs(self.thread.edit.await_args.kwargs["archived"], True)
        self.assertNotIn("locked", self.thread.edit.await_args.kwargs)

    async def test_work_thread_stays_open_when_persona_cannot_reply(self) -> None:
        """人格役が要約を返せなかったら、報告の残るスレッドを閉じない（#408）。"""
        self.enable_work_thread()

        async def generate(prompt, model_name, *args, **kwargs):
            self.generations.append(SimpleNamespace(prompt=prompt, model=model_name, grounding=kwargs.get("grounding")))
            if "依頼主（別のAI）" in prompt:
                return GenerationResult("架空の子の出力", None, [], model_name)
            return GenerationResult(None, None, [], None)

        self.gemini.generate_with_metadata.side_effect = generate
        await self.handler.on_message(self.message)
        self.send.assert_not_awaited()
        # 生成できなかった旨の返信はするが、スレッドは閉じない
        self.assertIn("エラーが発生しました", self.message.reply.await_args.args[0])
        self.thread.edit.assert_not_awaited()

    async def test_work_thread_stays_open_when_persona_reply_fails_to_send(self) -> None:
        self.enable_work_thread()
        self.send.side_effect = discord.HTTPException(Mock(status=500), "架空の送信失敗")
        with self.assertRaises(discord.HTTPException):
            await self.handler.on_message(self.message)
        self.thread.edit.assert_not_awaited()
        self.assertTrue(self.agent.try_acquire("11"))

    async def test_archive_failure_is_told_in_main_channel(self) -> None:
        self.enable_work_thread()
        self.thread.edit.side_effect = discord.Forbidden(Mock(status=403), "架空の内部の詳細")
        await self.handler.on_message(self.message)
        # 要約は返したうえで、閉じられなかった理由を元のチャンネルへ定型文で伝え、スレッドは残す
        self.send.assert_awaited_once()
        notice = self.message.reply.await_args.args[0]
        self.assertIn("作業スレッドを閉じられませんでした", notice)
        self.assertIn("権限", notice)
        self.assertNotIn("架空の内部の詳細", notice)
        self.assertTrue(self.agent.try_acquire("11"))

    def thread_message(self, content: str, *, mentioned: bool = False):
        """閉じた作業スレッドへの書き込み（Discord が再開する）。"""
        channel = Mock(spec=discord.Thread, id=self.thread.id)
        channel.parent = self.parent_channel
        channel.mention = "<#700>"

        @asynccontextmanager
        async def typing():
            yield

        channel.typing.side_effect = typing
        channel.edit = AsyncMock()
        bot = self.handler.client.user
        progress_message = SimpleNamespace(edit=AsyncMock())
        return SimpleNamespace(
            author=SimpleNamespace(id=12, bot=False, display_name="架空の別の利用者"),
            guild=self.message.guild, channel=channel,
            content=f"<@{bot.id}> {content}" if mentioned else content,
            attachments=[], mentions=[bot] if mentioned else [],
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            reply=AsyncMock(return_value=progress_message),
        )

    async def test_reopened_work_thread_confirms_additional_requests_once(self) -> None:
        self.enable_work_thread()
        await self.handler.on_message(self.message)
        self.thread.edit.assert_awaited_once()
        self.state.get_guild(1).current_respond_mode = "mention"
        self.generations.clear()

        # ボット宛てでない書き込みでも、追加の依頼があるかを一度だけ確かめる（AI は呼ばない）
        first = self.thread_message("架空の追記です")
        await self.handler.on_message(first)
        first.reply.assert_awaited_once()
        self.assertIn("追加のご依頼はありますか", first.reply.await_args.args[0])
        self.assertEqual(first.reply.await_args.kwargs["allowed_mentions"].to_dict()["parse"], [])
        self.assertEqual(self.generations, [])

        second = self.thread_message("架空の追記の続きです")
        await self.handler.on_message(second)
        second.reply.assert_not_awaited()

    async def test_reopened_thread_with_explicit_subagent_request_delegates_and_archives_again(self) -> None:
        self.enable_work_thread()
        await self.handler.on_message(self.message)
        self.state.get_guild(1).current_respond_mode = "mention"
        self.generations.clear()

        follow_up = self.thread_message("サブエージェントを使って、架空の続きも調べて", mentioned=True)
        await self.handler.on_message(follow_up)
        # 委任の明記があれば重ねて尋ねず、スレッドの中で返信1通の進捗表示のまま委任する
        replies = [call.args[0] for call in follow_up.reply.await_args_list]
        self.assertFalse(any("追加のご依頼はありますか" in text for text in replies))
        self.assertEqual(len(self.child_calls()), len(PLAN.steps))
        self.assertIs(self.send.await_args.args[0], follow_up)
        # 完了したら、再開した作業スレッドを閉じ直す。次の書き込みでまた確かめる
        follow_up.channel.edit.assert_awaited_once()
        self.assertIs(follow_up.channel.edit.await_args.kwargs["archived"], True)
        third = self.thread_message("架空の追記です")
        await self.handler.on_message(third)
        self.assertIn("追加のご依頼はありますか", third.reply.await_args.args[0])

    async def test_reopen_confirmation_is_not_sent_for_ng_words_or_unclosed_threads(self) -> None:
        self.enable_work_thread()
        self.state.get_guild(1).current_respond_mode = "mention"
        # 閉じていない（失敗して残した）スレッドへの書き込みは確かめない
        self.gemini.generate_with_metadata.side_effect = lambda *args, **kwargs: GenerationResult(None, None, [], None)
        await self.handler.on_message(self.message)
        self.thread.edit.assert_not_awaited()
        kept = self.thread_message("架空の追記です")
        await self.handler.on_message(kept)
        kept.reply.assert_not_awaited()

        # 閉じたスレッドでも、NG ワードを含む書き込みには返さない
        self.handler._agent_work_threads.mark_archived(self.thread.id)
        self.handler.ng_filter.contains_ng_word.return_value = True
        bad = self.thread_message("架空の不適切な追記")
        await self.handler.on_message(bad)
        bad.reply.assert_not_awaited()

    async def cancel_work_thread(self, phase, *, broken_cleanup=False, repeat=False) -> None:
        self.enable_work_thread()
        started, editing, release, cleaned = (asyncio.Event() for _ in range(4))

        async def wait_for_cancel(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        if phase == "worker":
            self.gemini.generate_with_metadata.side_effect = wait_for_cancel
        else:
            self.moderator.check.side_effect = wait_for_cancel

        async def edit(**kwargs):
            editing.set()
            await release.wait()
            cleaned.set()
            if broken_cleanup:
                raise RuntimeError("架空のボタン回収失敗")

        self.handoff.edit.side_effect = edit
        with patch("builtins.print"):
            run = asyncio.create_task(self.handler.on_message(self.message))
            try:
                await asyncio.wait_for(started.wait(), 2)
                run.cancel()
                await asyncio.wait_for(editing.wait(), 2)
                view = self.thread.send.await_args_list[0].kwargs["view"]
                self.assertTrue(view.is_finished())
                self.assertFalse(run.done())
                self.assertFalse(self.agent.try_acquire("11"))
                if repeat:
                    for _ in range(3):
                        run.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(run.done())
                        self.assertFalse(cleaned.is_set())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(run, 2)
                self.assertTrue(cleaned.is_set())
                self.handoff.edit.assert_awaited_once_with(view=None)
                self.assertTrue(self.agent.try_acquire("11"))
                self.agent.release("11")
                self.send.assert_not_awaited()
                self.tts.assert_not_awaited()
            finally:
                release.set()
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)

    async def test_external_cancel_removes_work_thread_button_before_releasing_slot(self) -> None:
        await self.cancel_work_thread("worker", repeat=True)

    async def test_cancel_during_child_report_screening_removes_work_thread_button(self) -> None:
        await self.cancel_work_thread("screening")

    async def test_button_cleanup_failure_does_not_replace_external_cancellation(self) -> None:
        await self.cancel_work_thread("worker", broken_cleanup=True)

    async def test_failed_delegation_in_work_thread_is_told_in_channel(self) -> None:
        self.enable_work_thread()
        self.gemini.generate_with_metadata.side_effect = lambda *args, **kwargs: GenerationResult(None, None, [], None)
        await self.handler.on_message(self.message)
        self.assertIn("うまくいきませんでした", self.thread.send.await_args.args[0])
        self.assertIn("うまくいきませんでした", self.message.reply.await_args.args[0])
        self.send.assert_not_awaited()
        self.tts.assert_not_awaited()
        # 失敗した子の報告は出さない（開始と手順1の進捗だけ）
        self.assertEqual(self.webhook.send.await_count, 2)
        # 失敗したスレッドは閉じずに残す（#408）
        self.thread.edit.assert_not_awaited()

    async def test_unsafe_child_report_is_never_posted_to_work_thread_or_parent_prompt(self):
        self.enable_work_thread()
        self.moderator.check.return_value = (False, "架空の子報告拒否")
        await self.handler.on_message(self.message)
        self.assertEqual(self.persona_calls(), [])
        self.send.assert_not_awaited()
        for call in self.webhook.send.await_args_list:
            self.assertNotIn("架空の子の出力", call.args[0])
            self.assertNotIn(SOURCE["uri"], call.args[0])
        self.assertIn("うまくいきませんでした", self.thread.send.await_args.args[0])
        self.assertTrue(self.agent.try_acquire("11"))
        self.thread.edit.assert_not_awaited()

    async def test_child_report_and_sources_are_masked_before_moderation_and_publication(self):
        self.enable_work_thread()
        source = {"title": "架空NGの参考資料", "uri": "https://example.invalid/架空NG"}
        self.handler.ng_filter.mask_with_log.side_effect = lambda text, **kwargs: text.replace("架空NG", "●●●●")

        async def generate(prompt, model_name, *args, **kwargs):
            self.generations.append(SimpleNamespace(prompt=prompt, model=model_name, grounding=kwargs.get("grounding")))
            if "依頼主（別のAI）" in prompt:
                return GenerationResult("架空NGの子報告", None, [source], model_name)
            return GenerationResult("架空の親返信", None, [], model_name)

        self.gemini.generate_with_metadata.side_effect = generate
        await self.handler.on_message(self.message)
        checked = self.moderator.check.await_args_list[0].args[0]
        self.assertIn("●●●●の子報告", checked)
        self.assertIn("●●●●の参考資料", checked)
        self.assertIn("https://example.invalid/●●●●", checked)
        for call in self.webhook.send.await_args_list:
            self.assertNotIn("架空NG", call.args[0])
        self.assertNotIn("架空NG", self.send.await_args.args[1])
        self.assertEqual(source["title"], "架空NGの参考資料")

    async def test_parent_rejection_can_only_leave_independently_screened_child_report(self):
        self.enable_work_thread()
        self.moderator.check.side_effect = [(True, ""), (False, "架空の親応答拒否")]
        await self.handler.on_message(self.message)
        self.assertEqual(self.moderator.check.await_count, 2)
        self.assertIn("架空の子の出力", self.moderator.check.await_args_list[0].args[0])
        self.assertIn("適切な応答を生成できませんでした", self.send.await_args.args[1])
        self.assertTrue(any("架空の子の出力" in call.args[0] for call in self.webhook.send.await_args_list))

    async def test_child_exception_reports_failure_and_releases_slot(self) -> None:
        self.gemini.generate_with_metadata.side_effect = RuntimeError("架空の子の作業失敗")
        await self.handler.on_message(self.message)
        self.assertIn("うまくいきませんでした", self.progress_message.edit.await_args.kwargs["content"])
        self.assertTrue(self.agent.try_acquire("11"))
        self.agent.release("11")
        self.send.assert_not_awaited()
        self.tts.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
