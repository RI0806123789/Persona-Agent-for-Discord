"""サブエージェントの作業スレッド（親子のやりとりの可視化）を検証する（#267）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord

from persona_bot.config.constants import (
    AGENT_CHILD_FALLBACK_NAME,
    AGENT_CHILD_NAME_MAX_CHARS,
    AGENT_DEFAULT_LINES,
    AGENT_THREAD_ARCHIVE_FAILURE_REASONS,
    AGENT_THREAD_NAME_MAX_CHARS,
    AGENT_WEBHOOK_NAME,
    DISCORD_MESSAGE_MAX_LENGTH,
)
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport
from persona_bot.views import agent_thread
from persona_bot.views.agent_thread import (
    AgentThreadProgress,
    AgentWebhooks,
    AgentWorkThreads,
    WorkThreadEntry,
    archive_work_thread,
    build_handoff_text,
    send_reopen_confirmation,
    start_agent_progress,
    thread_name,
    thread_unavailable_reason,
    webhook_username,
)
from persona_bot.views.agent_view import AgentProgress
from tests.helpers.discord_mocks import (
    ONLY_RESPONSE, make_guild, make_http_response, make_interaction, make_message, make_response,
)

BOT_ID = 99
PLAN = AgentPlan(True, "架空の調査担当", "架空の街のカフェを調べる", "箇条書き", ("候補の検索", "特徴の比較"))
LINES = dict(
    AGENT_DEFAULT_LINES,
    name="見習い{role}",
    handoff="{role}、お願い",
    child_start="引き受けました（{total}手順）",
    child_progress="{step}中（{current}/{total}）",
    child_report="架空の報告です",
    done="架空の完了",
    failed="架空の失敗",
)
SOURCE = {"title": "架空の出典", "uri": "https://example.invalid/cafe"}
ALL_PERMISSIONS = {
    "view_channel": True, "create_public_threads": True, "send_messages_in_threads": True, "manage_webhooks": True,
}


def own_webhook(**overrides):
    values = dict(
        type=discord.WebhookType.incoming, token="架空のトークン", user=SimpleNamespace(id=BOT_ID),
        name=AGENT_WEBHOOK_NAME, send=AsyncMock(),
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_channel(permissions=None, webhooks=()):
    channel = Mock(spec=discord.TextChannel, id=10)
    channel.permissions_for.return_value = SimpleNamespace(**(permissions or ALL_PERMISSIONS))
    channel.webhooks = AsyncMock(return_value=list(webhooks))
    channel.create_webhook = AsyncMock()
    return channel


def no_mentions(call):
    return call.kwargs["allowed_mentions"].to_dict()["parse"] == []


class NameAndTextTests(unittest.TestCase):
    def test_webhook_username_follows_discord_restrictions(self) -> None:
        name = webhook_username("見習いの Discord @bot #1: `x` CLYDE 担当")
        self.assertNotIn("discord", name.lower())
        self.assertNotIn("clyde", name.lower())
        for symbol in ("@", "#", ":", "`"):
            self.assertNotIn(symbol, name)
        self.assertIn("＠bot ＃1： ｀x｀", name)
        self.assertEqual(len(webhook_username("あ" * 200)), AGENT_CHILD_NAME_MAX_CHARS)
        for text in ("", "   ", "Discord", "everyone", "HERE", "disdiscordcord"):
            with self.subTest(text=text):
                self.assertEqual(webhook_username(text), AGENT_CHILD_FALLBACK_NAME)

    def test_thread_name_is_one_line_within_limit(self) -> None:
        self.assertIn("架空の調査担当", thread_name("架空の調査担当"))
        self.assertLessEqual(len(thread_name("あ" * 300)), AGENT_THREAD_NAME_MAX_CHARS)

    def test_handoff_lists_task_output_and_steps(self) -> None:
        text = build_handoff_text(LINES, PLAN)
        self.assertTrue(text.startswith("架空の調査担当、お願い"))
        for part in ("架空の街のカフェを調べる", "箇条書き", "1. 候補の検索", "2. 特徴の比較"):
            self.assertIn(part, text)
        # 出力形式が無ければ見出しごと省く
        without_output = build_handoff_text(LINES, AgentPlan(True, PLAN.role, PLAN.task, "", PLAN.steps))
        self.assertNotIn("期待する出力", without_output)
        long_plan = AgentPlan(True, PLAN.role, "い" * 5000, "", PLAN.steps)
        self.assertLessEqual(len(build_handoff_text(LINES, long_plan)), DISCORD_MESSAGE_MAX_LENGTH)


class EligibilityTests(unittest.TestCase):
    def message(self, **overrides):
        values = dict(guild=make_guild(me=SimpleNamespace(id=BOT_ID)), channel=make_channel())
        values.update(overrides)
        return make_message(**values)

    def test_text_channel_with_permissions_can_use_thread(self) -> None:
        self.assertIsNone(thread_unavailable_reason(self.message()))

    def test_places_without_threads_fall_back(self) -> None:
        cases = {
            "DM": self.message(guild=None),
            "スレッドの中": self.message(channel=Mock(spec=discord.Thread, id=10)),
            "ボイスのチャット": self.message(channel=Mock(spec=discord.VoiceChannel, id=10)),
            "メンバー情報なし": self.message(guild=make_guild(me=None)),
        }
        for name, message in cases.items():
            with self.subTest(case=name):
                self.assertIsNotNone(thread_unavailable_reason(message))

    def test_each_missing_permission_is_reported(self) -> None:
        for permission in ALL_PERMISSIONS:
            with self.subTest(permission=permission):
                channel = make_channel(dict(ALL_PERMISSIONS, **{permission: False}))
                reason = thread_unavailable_reason(self.message(channel=channel))
                self.assertIn(permission, reason)

    def test_disabled_by_constant(self) -> None:
        with patch.object(agent_thread, "AGENT_THREAD_ENABLED", False):
            self.assertIsNotNone(thread_unavailable_reason(self.message()))


class WebhookCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_own_webhook_and_caches_it(self) -> None:
        mine = own_webhook()
        others = [
            own_webhook(user=SimpleNamespace(id=1)), own_webhook(name="別の Webhook"),
            own_webhook(token=None), own_webhook(type=discord.WebhookType.channel_follower),
        ]
        channel = make_channel(webhooks=[*others, mine])
        webhooks = AgentWebhooks()
        self.assertIs(await webhooks.get(channel, BOT_ID), mine)
        self.assertIs(await webhooks.get(channel, BOT_ID), mine)
        channel.webhooks.assert_awaited_once()
        channel.create_webhook.assert_not_awaited()

    async def test_creates_one_webhook_when_missing_even_for_concurrent_requests(self) -> None:
        created = own_webhook()
        channel = make_channel()
        channel.create_webhook.return_value = created
        webhooks = AgentWebhooks()
        results = await asyncio.gather(*(webhooks.get(channel, BOT_ID) for _ in range(3)))
        self.assertTrue(all(result is created for result in results))
        channel.create_webhook.assert_awaited_once()
        self.assertEqual(channel.create_webhook.await_args.kwargs["name"], AGENT_WEBHOOK_NAME)

    async def test_failure_returns_none_and_retries_next_time(self) -> None:
        channel = make_channel()
        channel.webhooks.side_effect = [discord.Forbidden(Mock(status=403), "架空の拒否"), [own_webhook()]]
        webhooks = AgentWebhooks()
        with patch("persona_bot.views.agent_thread.logger"):
            self.assertIsNone(await webhooks.get(channel, BOT_ID))
        self.assertIsNotNone(await webhooks.get(channel, BOT_ID))

    async def test_forget_removes_only_the_same_webhook(self) -> None:
        first, second = own_webhook(), own_webhook()
        channel = make_channel(webhooks=[first])
        webhooks = AgentWebhooks()
        await webhooks.get(channel, BOT_ID)
        webhooks.forget(channel.id, second)
        self.assertIs(await webhooks.get(channel, BOT_ID), first)
        webhooks.forget(channel.id, first)
        channel.webhooks.return_value = [second]
        self.assertIs(await webhooks.get(channel, BOT_ID), second)


class ThreadProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.webhook = own_webhook()
        self.channel = make_channel(webhooks=[self.webhook])
        self.handoff = SimpleNamespace(edit=AsyncMock())
        self.thread = SimpleNamespace(id=700, send=AsyncMock(return_value=self.handoff))
        self.fallback_message = SimpleNamespace(edit=AsyncMock())
        self.message = make_message(
            author_id=11, guild=make_guild(me=SimpleNamespace(id=BOT_ID)),
            channel=self.channel, create_thread=AsyncMock(return_value=self.thread),
            reply=AsyncMock(return_value=self.fallback_message),
        )
        self.webhooks = AgentWebhooks()

    async def open(self, avatar_url="https://example.invalid/agent.png"):
        with patch("persona_bot.views.agent_thread.logger"):
            progress = await start_agent_progress(
                self.message, LINES, PLAN, webhooks=self.webhooks, avatar_url=avatar_url,
            )
        self.addCleanup(progress._view.stop)
        return progress

    def child_posts(self):
        return [call.args[0] for call in self.webhook.send.await_args_list]

    async def test_parent_hands_off_in_thread_and_child_accepts(self) -> None:
        progress = await self.open()
        self.assertIsInstance(progress, AgentThreadProgress)
        self.assertTrue(progress.shows_report)

        # 依頼メッセージにスレッドを作る。自動アーカイブの時間は指定しない（完了したらボットが閉じる。#408）
        kwargs = self.message.create_thread.await_args.kwargs
        self.assertIn("架空の調査担当", kwargs["name"])
        self.assertNotIn("auto_archive_duration", kwargs)
        self.assertIs(progress.work_thread, self.thread)

        # 親の名義で委託内容を伝え、そのメッセージに中止ボタンを付ける（通知は飛ばさない）
        handoff = self.thread.send.await_args_list[0]
        self.assertIn("架空の街のカフェを調べる", handoff.args[0])
        self.assertIs(handoff.kwargs["view"], progress._view)
        self.assertTrue(no_mentions(handoff))

        # 子は Webhook で名前とアイコンを変えて、同じスレッドへ投稿する
        self.assertEqual(self.child_posts(), ["引き受けました（2手順）"])
        child = self.webhook.send.await_args
        self.assertEqual(child.kwargs["username"], "見習い架空の調査担当")
        self.assertEqual(child.kwargs["avatar_url"], "https://example.invalid/agent.png")
        self.assertIs(child.kwargs["thread"], self.thread)
        self.assertTrue(child.kwargs["wait"])
        self.assertTrue(no_mentions(child))
        # 依頼したチャンネルには進捗の返信を出さない
        self.message.reply.assert_not_awaited()

    async def test_each_step_is_posted_and_kept(self) -> None:
        progress = await self.open(avatar_url=None)
        await progress.update(0, "候補の検索")
        await progress.update(1, "特徴の比較")
        # 書き換えずに1通ずつ残し、間隔による間引きもしない
        self.assertEqual(self.child_posts()[1:], ["候補の検索中（1/2）", "特徴の比較中（2/2）"])
        self.assertIsNone(self.webhook.send.await_args.kwargs["avatar_url"])

    async def test_done_posts_report_then_parent_status(self) -> None:
        progress = await self.open()
        await progress.finish("done", AgentReport("架空の報告の本文", [SOURCE]))
        # 中止ボタンを外す
        self.handoff.edit.assert_awaited_once_with(view=None)
        self.assertTrue(progress._view.is_finished())
        # 子が報告と参考リンクを残す（リンクの埋め込みは抑える）
        report = self.webhook.send.await_args
        self.assertTrue(report.args[0].startswith("架空の報告です\n\n架空の報告の本文"))
        self.assertIn(SOURCE["uri"], report.args[0])
        self.assertTrue(report.kwargs["suppress_embeds"])
        # 親が終了を伝える。完了の返信は人格役が行うので、チャンネルへは返信しない
        self.assertEqual(self.thread.send.await_args.args[0], "架空の完了")
        self.assertTrue(no_mentions(self.thread.send.await_args))
        self.message.reply.assert_not_awaited()

    async def test_long_report_is_split(self) -> None:
        progress = await self.open()
        await progress.finish("done", AgentReport("あ" * 4500, []))
        reports = self.child_posts()[1:]
        self.assertGreaterEqual(len(reports), 3)
        self.assertTrue(all(len(chunk) <= DISCORD_MESSAGE_MAX_LENGTH for chunk in reports))

    async def test_failed_or_canceled_is_told_in_thread_and_channel(self) -> None:
        for key in ("failed", "canceled"):
            with self.subTest(key=key):
                self.thread.send.reset_mock()
                self.message.reply.reset_mock()
                progress = await self.open()
                await progress.finish(key)
                status = LINES[key]
                self.assertEqual(self.thread.send.await_args.args[0], status)
                self.message.reply.assert_awaited_once()
                self.assertEqual(self.message.reply.await_args.args[0], status)
                self.assertTrue(no_mentions(self.message.reply.await_args))
                # 報告は出さない
                self.assertFalse(any("架空の報告です" in post for post in self.child_posts()))

    async def test_cancel_button_on_handoff_stops_the_worker(self) -> None:
        progress = await self.open()
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(3600)

        run = asyncio.create_task(progress.run(slow()))
        await started.wait()
        button = self.thread.send.await_args_list[0].kwargs["view"].children[0]
        await button.callback(make_interaction(user_id=11, response=make_response("defer"), without=ONLY_RESPONSE))
        with patch("persona_bot.views.agent_thread.logger"):
            self.assertEqual(await run, ("canceled", None))

    async def test_deleted_webhook_is_forgotten_and_does_not_stop_work(self) -> None:
        progress = await self.open()
        self.webhook.send.side_effect = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        replacement = own_webhook()
        self.channel.webhooks.return_value = [replacement]
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")
            await progress.update(1, "特徴の比較")
            await progress.finish("done", AgentReport("架空の報告の本文", []))
        # 同じ依頼の失敗した投稿から再送し、進捗と最終報告も新しいWebhookへ続ける。
        self.assertEqual(self.webhook.send.await_count, 2)
        self.assertIs(self.webhooks._cache[self.channel.id], replacement)
        self.assertEqual(replacement.send.await_count, 3)
        self.assertEqual(replacement.send.await_args_list[0].args[0], "候補の検索中（1/2）")
        self.assertIn("架空の報告の本文", replacement.send.await_args.args[0])
        self.assertEqual(self.thread.send.await_args.args[0], "架空の完了")

    async def test_webhook_refetch_uses_the_bot_id_kept_at_start_even_if_guild_me_is_gone(self) -> None:
        """長時間の作業の後に guild.me がキャッシュから外れても、再取得で AttributeError にしない（#412）。"""
        progress = await self.open()
        self.message.guild.me = None
        self.webhook.send.side_effect = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        replacement = own_webhook()
        self.channel.webhooks.return_value = [replacement]
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")
        self.assertIs(self.webhooks._cache[self.channel.id], replacement)
        replacement.send.assert_awaited_once()

    async def test_webhook_refetch_without_any_bot_id_gives_up_quietly(self) -> None:
        progress = await self.open()
        progress._bot_user_id = None
        self.webhook.send.side_effect = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")  # 例外にならず、作業は止まらない

    async def test_cached_webhook_deleted_before_start_is_recreated(self) -> None:
        self.webhooks._cache[self.channel.id] = self.webhook
        self.webhook.send.side_effect = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        replacement = own_webhook()
        self.channel.webhooks.return_value = []
        self.channel.create_webhook.return_value = replacement
        await self.open()
        self.channel.create_webhook.assert_awaited_once()
        self.assertEqual(replacement.send.await_args.args[0], "引き受けました（2手順）")

    async def test_deleted_webhook_retries_only_failed_chunk_and_preserves_send_options(self) -> None:
        progress = await self.open()
        missing = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        self.webhook.send.reset_mock()
        self.webhook.send.side_effect = [None, missing]
        replacement = own_webhook()
        self.channel.webhooks.return_value = [replacement]
        content = "あ" * 2100
        with patch("persona_bot.views.agent_thread.logger"):
            await progress._child_send(content, suppress_embeds=True)
        replacement.send.assert_awaited_once()
        sent = replacement.send.await_args
        self.assertEqual(sent.args[0], "あ" * 100)
        self.assertIs(sent.kwargs["thread"], self.thread)
        self.assertTrue(sent.kwargs["suppress_embeds"])
        self.assertTrue(sent.kwargs["wait"])
        self.assertTrue(no_mentions(sent))
        self.assertEqual(sent.kwargs["username"], "見習い架空の調査担当")

    async def test_second_not_found_stops_retrying_and_forgets_replacement(self) -> None:
        progress = await self.open()
        missing = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        self.webhook.send.side_effect = missing
        replacement = own_webhook(send=AsyncMock(side_effect=missing))
        self.channel.webhooks.reset_mock()
        self.channel.webhooks.return_value = [replacement]
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")
            await progress.update(1, "特徴の比較")
        self.channel.webhooks.assert_awaited_once()
        replacement.send.assert_awaited_once()
        self.assertNotIn(self.channel.id, self.webhooks._cache)

    async def test_webhook_recovery_failure_does_not_stop_work(self) -> None:
        progress = await self.open()
        self.webhook.send.side_effect = discord.NotFound(make_http_response(404, "Not Found"), "架空")
        self.channel.webhooks.side_effect = discord.Forbidden(make_http_response(403, "Forbidden"), "架空")
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")
            await progress.finish("done", AgentReport("架空の報告の本文", []))
        self.assertIsNone(progress._webhook)
        self.assertEqual(self.thread.send.await_args.args[0], "架空の完了")

    async def test_child_post_failure_is_logged_and_work_continues(self) -> None:
        progress = await self.open()
        self.webhook.send.side_effect = discord.DiscordException("架空の送信失敗")
        with patch("persona_bot.views.agent_thread.logger"):
            await progress.update(0, "候補の検索")
            await progress.finish("done", AgentReport("架空の報告の本文", []))
        self.assertEqual(self.thread.send.await_args.args[0], "架空の完了")
        self.assertIn(self.channel.id, self.webhooks._cache)

    async def assert_fallback(self) -> AgentProgress:
        progress = await self.open()
        self.assertIsInstance(progress, AgentProgress)
        self.assertFalse(progress.shows_report)
        # 返信1通の表示には、閉じる対象の作業スレッドが無い（#408）
        self.assertIsNone(progress.work_thread)
        # 依頼への返信1通に、中止ボタン付きの進捗を出す
        self.message.reply.assert_awaited_once()
        self.assertIs(self.message.reply.await_args.kwargs["view"], progress._view)
        return progress

    async def test_falls_back_when_webhook_is_unavailable(self) -> None:
        self.channel.webhooks.side_effect = discord.Forbidden(Mock(status=403), "架空の拒否")
        await self.assert_fallback()
        # Webhook を用意できないうちはスレッドを作らない
        self.message.create_thread.assert_not_awaited()

    async def test_falls_back_when_thread_cannot_be_created(self) -> None:
        self.message.create_thread.side_effect = discord.HTTPException(
            make_http_response(400, "Bad Request"), "架空の作成失敗",
        )
        await self.assert_fallback()

    async def test_falls_back_when_handoff_cannot_be_posted(self) -> None:
        self.thread.send.side_effect = discord.DiscordException("架空の送信失敗")
        await self.assert_fallback()
        # 委託内容を送れなかったスレッド側の中止ボタンは止める
        self.assertTrue(self.thread.send.await_args.kwargs["view"].is_finished())
        self.webhook.send.assert_not_awaited()

    async def test_falls_back_without_permissions(self) -> None:
        self.channel.permissions_for.return_value = SimpleNamespace(**dict(ALL_PERMISSIONS, manage_webhooks=False))
        await self.assert_fallback()
        self.channel.webhooks.assert_not_awaited()
        self.message.create_thread.assert_not_awaited()

    async def test_cancellation_while_opening_stops_cancel_button(self) -> None:
        self.webhook.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await AgentThreadProgress.open(self.message, LINES, PLAN, webhooks=self.webhooks, avatar_url=None)
        self.assertTrue(self.thread.send.await_args.kwargs["view"].is_finished())
        self.handoff.edit.assert_awaited_once_with(view=None)

    async def test_repeated_cancellation_while_opening_waits_for_button_removal(self) -> None:
        self.webhook.send.side_effect = asyncio.CancelledError()
        editing, release, cleaned = (asyncio.Event() for _ in range(3))

        async def edit(**kwargs):
            editing.set()
            await release.wait()
            cleaned.set()

        self.handoff.edit.side_effect = edit
        run = asyncio.create_task(AgentThreadProgress.open(
            self.message, LINES, PLAN, webhooks=self.webhooks, avatar_url=None,
        ))
        try:
            await asyncio.wait_for(editing.wait(), 2)
            self.assertTrue(self.thread.send.await_args.kwargs["view"].is_finished())
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
        finally:
            release.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    async def test_cancellation_cleanup_failure_keeps_the_original_cancellation(self) -> None:
        self.webhook.send.side_effect = asyncio.CancelledError()
        self.handoff.edit.side_effect = RuntimeError("架空の編集失敗")
        with patch("persona_bot.views.agent_thread.logger"), self.assertRaises(asyncio.CancelledError):
            await AgentThreadProgress.open(self.message, LINES, PLAN, webhooks=self.webhooks, avatar_url=None)
        self.assertTrue(self.thread.send.await_args.kwargs["view"].is_finished())
        self.handoff.edit.assert_awaited_once_with(view=None)

    async def test_cancellation_while_starting_fallback_stops_cancel_view(self) -> None:
        self.channel.permissions_for.return_value = SimpleNamespace(**dict(ALL_PERMISSIONS, manage_webhooks=False))
        self.message.reply.side_effect = asyncio.CancelledError()
        with patch("persona_bot.views.agent_thread.logger"), self.assertRaises(asyncio.CancelledError):
            await start_agent_progress(self.message, LINES, PLAN, webhooks=self.webhooks, avatar_url=None)
        self.assertTrue(self.message.reply.await_args.kwargs["view"].is_finished())


class WorkThreadRegistryTests(unittest.TestCase):
    """作業スレッドの記録（#408）。"""

    def test_reopen_is_reported_once_only_after_the_bot_archived_it(self) -> None:
        threads = AgentWorkThreads()
        threads.register(700, LINES, "架空の調査担当")
        self.assertTrue(threads.is_work_thread(700))
        self.assertFalse(threads.is_work_thread(701))
        # ボットが閉じる前（作業中・失敗して残したスレッド）は確かめない
        self.assertIsNone(threads.take_reopened(700))
        threads.mark_archived(700)
        entry = threads.take_reopened(700)
        self.assertEqual((entry.lines, entry.role), (LINES, "架空の調査担当"))
        # 再開ごとに1回だけ。もう一度閉じれば、次の再開でまた確かめる
        self.assertIsNone(threads.take_reopened(700))
        threads.mark_archived(700)
        self.assertIsNotNone(threads.take_reopened(700))
        self.assertTrue(threads.is_work_thread(700))

    def test_unknown_threads_are_ignored(self) -> None:
        threads = AgentWorkThreads()
        threads.mark_archived(800)
        self.assertIsNone(threads.take_reopened(800))
        self.assertFalse(threads.is_work_thread(800))

    def test_oldest_entries_are_forgotten_over_the_limit(self) -> None:
        threads = AgentWorkThreads(max_entries=2)
        for thread_id in (1, 2):
            threads.register(thread_id, LINES, "架空")
        # 閉じたスレッドは新しく使ったものとして扱い、先に忘れない
        threads.mark_archived(1)
        threads.register(3, LINES, "架空")
        self.assertTrue(threads.is_work_thread(1))
        self.assertFalse(threads.is_work_thread(2))
        self.assertTrue(threads.is_work_thread(3))


class ArchiveWorkThreadTests(unittest.IsolatedAsyncioTestCase):
    """完了した作業スレッドを閉じる（#408）。"""

    async def asyncSetUp(self) -> None:
        self.parent = SimpleNamespace(send=AsyncMock())
        self.thread = SimpleNamespace(id=700, edit=AsyncMock(), parent=self.parent, mention="<#700>")
        self.channel = make_channel()
        self.source = make_message(author_id=11, channel=self.channel, reply=AsyncMock())
        self.threads = AgentWorkThreads()
        self.threads.register(self.thread.id, LINES, PLAN.role)

    async def archive(self) -> bool:
        with patch("persona_bot.views.agent_thread.logger"):
            return await archive_work_thread(self.thread, self.source, LINES, PLAN.role, self.threads)

    async def test_archives_without_locking_and_remembers_it(self) -> None:
        self.assertTrue(await self.archive())
        self.thread.edit.assert_awaited_once()
        kwargs = self.thread.edit.await_args.kwargs
        self.assertIs(kwargs["archived"], True)
        # 閉じるのはアーカイブだけ（ロックすると利用者が追記できなくなる）
        self.assertNotIn("locked", kwargs)
        self.source.reply.assert_not_awaited()
        self.assertIsNotNone(self.threads.take_reopened(self.thread.id))

    async def test_failure_tells_fixed_reason_in_main_channel_and_keeps_thread(self) -> None:
        cases = {
            "forbidden": discord.Forbidden(make_http_response(403, "Forbidden"), "架空の内部の詳細"),
            "not_found": discord.NotFound(make_http_response(404, "Not Found"), "架空の内部の詳細"),
            "error": discord.HTTPException(make_http_response(500, "Server Error"), "架空の内部の詳細"),
        }
        for key, error in cases.items():
            with self.subTest(reason=key):
                self.source.reply.reset_mock()
                self.thread.edit.side_effect = error
                self.assertFalse(await self.archive())
                self.source.reply.assert_awaited_once()
                notice = self.source.reply.await_args.args[0]
                self.assertTrue(notice.startswith(LINES["archive_failed"]))
                self.assertIn(AGENT_THREAD_ARCHIVE_FAILURE_REASONS[key], notice)
                # 例外の本文は公開チャンネルに出さない
                self.assertNotIn("架空の内部の詳細", notice)
                self.assertTrue(no_mentions(self.source.reply.await_args))
                # 閉じていないので、再開としては扱わない
                self.assertIsNone(self.threads.take_reopened(self.thread.id))

    async def test_failure_in_reopened_thread_is_told_in_parent_channel(self) -> None:
        """再開したスレッドの中で受けた追加の依頼なら、スレッドの親（メインのチャンネル）へ伝える。"""
        self.source = make_message(author_id=11, channel=self.thread, reply=AsyncMock())
        self.thread.edit.side_effect = discord.Forbidden(make_http_response(403, "Forbidden"), "架空")
        self.assertFalse(await self.archive())
        self.parent.send.assert_awaited_once()
        notice = self.parent.send.await_args.args[0]
        self.assertIn(AGENT_THREAD_ARCHIVE_FAILURE_REASONS["forbidden"], notice)
        self.assertIn("<#700>", notice)
        self.assertTrue(no_mentions(self.parent.send.await_args))
        self.source.reply.assert_not_awaited()
        # 親チャンネルを取れなければ、スレッドの中へ伝える
        self.thread.parent = None
        self.assertFalse(await self.archive())
        self.source.reply.assert_awaited_once()

    async def test_failure_notice_that_cannot_be_sent_does_not_raise(self) -> None:
        self.thread.edit.side_effect = discord.Forbidden(make_http_response(403, "Forbidden"), "架空")
        self.source.reply.side_effect = discord.HTTPException(make_http_response(500, "Server Error"), "架空")
        self.assertFalse(await self.archive())

    async def test_reopen_confirmation_uses_the_persona_line(self) -> None:
        message = make_message(author_id=12, channel=self.thread, reply=AsyncMock())
        lines = dict(LINES, reopened="{role}の続きはある？")
        await send_reopen_confirmation(message, WorkThreadEntry(lines, PLAN.role))
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], "架空の調査担当の続きはある？")
        self.assertTrue(no_mentions(message.reply.await_args))
        # 送れなくても例外にしない
        message.reply.side_effect = discord.HTTPException(make_http_response(500, "Server Error"), "架空")
        with patch("persona_bot.views.agent_thread.logger"):
            await send_reopen_confirmation(message, WorkThreadEntry(lines, PLAN.role))


if __name__ == "__main__":
    unittest.main()
