"""統計画像の所有権、生成失敗の案内、パネル操作時の権限を検証する。"""

import asyncio
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.views import stats_view
from persona_bot.views.unblock_view import UnblockSelect, UnblockView
from persona_bot.utils.discord_helpers import is_current_administrator
from persona_bot.views.stats_view import FeedbackButton, RankingButton, RunStatsButton, StatsUserSelect, StatsView
from tests.helpers.member_helpers import make_member, make_owner_client


class StatsDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.buffers = [io.BytesIO(b"png-1"), io.BytesIO(b"png-2")]
        for buffer in self.buffers:
            self.addCleanup(buffer.close)
        self.stats = SimpleNamespace(
            build_dashboard=AsyncMock(return_value=("架空の統計", [(f"{i}.png", b) for i, b in enumerate(self.buffers)])),
            get_user_options=Mock(return_value=[("11", "架空の本人", 1), ("22", "架空の対象", 2)]),
            display_name=Mock(return_value="架空の対象"),
        )
        self.view = StatsView(self.stats, viewer_id="11", is_admin=True)
        self.addCleanup(self.view.stop)
        self.button = next(item for item in self.view.children if isinstance(item, RunStatsButton))
        self.interaction = SimpleNamespace(
            user=make_member(admin=True),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_success_and_send_failure_release_files_before_buffers(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                buffers = [io.BytesIO(b"png") for _ in range(2)]
                self.stats.build_dashboard.return_value = ("架空の統計", [(f"{i}.png", b) for i, b in enumerate(buffers)])
                self.interaction.followup.send.side_effect = RuntimeError("架空の送信失敗") if fails else None
                await self.button.callback(self.interaction)
                self.assertTrue(all(buffer.closed for buffer in buffers))
                for file in self.interaction.followup.send.await_args_list[-2 if fails else -1].kwargs["files"]:
                    self.assertTrue(file.fp.closed)

    async def test_followup_failures_preserve_the_first_result_and_report_privately(self) -> None:
        for caption, images in (("あ" * 2100, []), ("架空の統計", [("graph.png", self.buffers[0])])):
            with self.subTest(graph=bool(images)):
                self.stats.build_dashboard.return_value = (caption, images)
                self.interaction.edit_original_response.reset_mock()
                self.interaction.followup.send.reset_mock()
                self.interaction.followup.send.side_effect = [RuntimeError("架空の送信失敗"), None]
                await self.button.callback(self.interaction)
                self.interaction.edit_original_response.assert_awaited_once_with(content=caption[:2000], view=None)
                self.assertIn("失敗", self.interaction.followup.send.await_args.args[0])
                self.assertTrue(self.interaction.followup.send.await_args.kwargs["ephemeral"])

    async def test_failed_failure_notice_keeps_result_and_closes_images(self) -> None:
        self.interaction.followup.send.side_effect = RuntimeError("架空の送信失敗")
        await self.button.callback(self.interaction)
        self.interaction.edit_original_response.assert_awaited_once_with(content="架空の統計", view=None)
        self.assertTrue(all(buffer.closed for buffer in self.buffers))

    async def test_generation_failure_replaces_busy_panel_with_error(self) -> None:
        self.stats.build_dashboard.side_effect = RuntimeError("架空の生成失敗")
        await self.button.callback(self.interaction)
        self.assertTrue(self.view.is_finished())
        self.assertIn("失敗", self.interaction.edit_original_response.await_args.kwargs["content"])
        self.assertNotIn("集計中", self.interaction.edit_original_response.await_args.kwargs["content"])

    async def test_partial_file_creation_failure_releases_every_buffer(self) -> None:
        original = discord.File
        calls = []

        def create(buffer, **kwargs):
            calls.append(buffer)
            if len(calls) == 2:
                raise RuntimeError("架空のファイル生成失敗")
            return original(buffer, **kwargs)

        with patch.object(stats_view.discord, "File", side_effect=create):
            await self.button.callback(self.interaction)
        self.assertTrue(all(buffer.closed for buffer in self.buffers))
        self.assertIn("失敗", self.interaction.edit_original_response.await_args.kwargs["content"])

    async def test_send_cancellation_releases_files_and_propagates(self) -> None:
        self.interaction.followup.send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.button.callback(self.interaction)
        self.assertTrue(all(buffer.closed for buffer in self.buffers))

    async def test_revoked_admin_cannot_view_another_user(self) -> None:
        self.view.temp_target_id = "22"
        self.interaction.user.guild_permissions.administrator = False
        await self.button.callback(self.interaction)
        self.stats.build_dashboard.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.assertTrue(self.interaction.response.send_message.await_args.kwargs["ephemeral"])

    async def test_non_admin_still_views_self_and_viewer_is_passed(self) -> None:
        self.view.is_admin = False
        self.view.temp_target_id = "22"
        self.interaction.user.guild_permissions.administrator = False
        await self.button.callback(self.interaction)
        self.stats.build_dashboard.assert_awaited_once_with("11", "all", viewer_id="11")

    async def test_revoked_admin_cannot_change_target(self) -> None:
        select = next(item for item in self.view.children if isinstance(item, StatsUserSelect))
        select._values = ["22"]
        self.interaction.user.guild_permissions.administrator = False
        await select.callback(self.interaction)
        self.assertEqual(self.view.temp_target_id, "11")
        self.assertFalse(self.view.is_admin)
        self.interaction.response.edit_message.assert_awaited_once()
        self.assertIn("あなた自身", self.interaction.response.edit_message.await_args.kwargs["content"])
        self.interaction.response.send_message.assert_not_awaited()

    async def test_permission_revoked_during_generation_does_not_send_other_users_graph(self) -> None:
        self.view.temp_target_id = "22"
        result = self.stats.build_dashboard.return_value

        async def generate(*args, **kwargs):
            # 押した時点の Member は更新されない。最新の Member だけがロール剥奪を反映する。
            self.interaction.user.fresh.guild_permissions.administrator = False
            await asyncio.sleep(0)
            return result

        self.stats.build_dashboard.side_effect = generate
        await self.button.callback(self.interaction)
        self.interaction.followup.send.assert_not_awaited()
        self.assertNotEqual(self.interaction.edit_original_response.await_args.kwargs["content"], "架空の統計")
        self.assertTrue(all(buffer.closed for buffer in self.buffers))


class AdministratorRecheckTests(unittest.IsolatedAsyncioTestCase):
    """送信直前の管理者の再確認が、押した時点の写しではなく最新の Member で行われることを確かめる（#208）。"""

    async def test_helper_uses_fetched_member_not_the_interaction_snapshot(self) -> None:
        for snapshot, fresh, expected in ((True, False, False), (False, True, True), (True, True, True), (False, False, False)):
            with self.subTest(snapshot=snapshot, fresh=fresh):
                user = make_member(admin=snapshot, fresh_admin=fresh)
                self.assertIs(await is_current_administrator(SimpleNamespace(user=user)), expected)
                user.guild.fetch_member.assert_awaited_once_with(11)

    async def test_helper_denies_when_member_cannot_be_confirmed(self) -> None:
        response = SimpleNamespace(status=404, reason="架空")
        for error in (discord.NotFound(response, "脱退済み"), discord.HTTPException(response, "取得失敗"), RuntimeError("想定外")):
            with self.subTest(error=type(error).__name__):
                user = make_member(admin=True)
                user.guild.fetch_member.side_effect = error
                self.assertFalse(await is_current_administrator(SimpleNamespace(user=user)))
        # DM の User は Member ではないので、サーバーの権限を確認できない。
        self.assertFalse(await is_current_administrator(SimpleNamespace(user=Mock(spec=discord.User))))

    async def test_helper_propagates_cancellation(self) -> None:
        user = make_member(admin=True)
        user.guild.fetch_member.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await is_current_administrator(SimpleNamespace(user=user))

    def interaction(self, member):
        return SimpleNamespace(
            user=member, guild_id=1,
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock()),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_other_user_stats_denied_when_member_lookup_fails(self) -> None:
        buffers = [io.BytesIO(b"png-1"), io.BytesIO(b"png-2")]
        stats = SimpleNamespace(
            build_dashboard=AsyncMock(return_value=("架空の統計", [("a.png", buffers[0]), ("b.png", buffers[1])])),
            get_user_options=Mock(return_value=[("11", "架空の本人", 1), ("22", "架空の対象", 2)]),
            display_name=Mock(return_value="架空の対象"),
        )
        view = StatsView(stats, viewer_id="11", is_admin=True)
        self.addCleanup(view.stop)
        view.temp_target_id = "22"
        member = make_member(admin=True)
        member.guild.fetch_member.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="架空"), "障害")
        interaction = self.interaction(member)
        await next(item for item in view.children if isinstance(item, RunStatsButton)).callback(interaction)
        interaction.followup.send.assert_not_awaited()
        self.assertNotEqual(interaction.edit_original_response.await_args.kwargs["content"], "架空の統計")
        self.assertTrue(all(buffer.closed for buffer in buffers))

    async def test_own_stats_do_not_need_administrator_lookup(self) -> None:
        stats = SimpleNamespace(
            build_dashboard=AsyncMock(return_value=("架空の統計", [])),
            get_user_options=Mock(return_value=[("11", "架空の本人", 1)]), display_name=Mock(return_value="架空の本人"),
        )
        view = StatsView(stats, viewer_id="11", is_admin=False)
        self.addCleanup(view.stop)
        member = make_member(admin=False)
        interaction = self.interaction(member)
        await next(item for item in view.children if isinstance(item, RunStatsButton)).callback(interaction)
        member.guild.fetch_member.assert_not_awaited()
        self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"], "架空の統計")

    def ranking_button(self, buffer) -> RankingButton:
        """実際の StatsView に載った全体比較ボタン（実行中の印をパネルが持つため。#398）。"""
        stats = SimpleNamespace(get_user_options=Mock(return_value=[]), build_ranking_graph=AsyncMock(return_value=buffer))
        view = StatsView(stats, viewer_id="11", is_admin=True, feedback=None)
        self.addCleanup(view.stop)
        return next(item for item in view.children if isinstance(item, RankingButton))

    async def test_ranking_is_not_sent_after_role_removed_during_rendering(self) -> None:
        for lookup_fails in (False, True):
            with self.subTest(lookup_fails=lookup_fails):
                buffer = io.BytesIO(b"png")
                member = make_member(admin=True)
                if lookup_fails:
                    member.guild.fetch_member.side_effect = RuntimeError("架空の障害")
                else:
                    member.fresh.guild_permissions.administrator = False
                interaction = self.interaction(member)
                button = self.ranking_button(buffer)
                with patch.object(stats_view, "send_graph_ephemeral", new=AsyncMock()) as sending:
                    await button.callback(interaction)
                sending.assert_not_awaited()
                self.assertTrue(buffer.closed)
                self.assertIn("管理者のみ", interaction.followup.send.await_args.args[0])

    async def test_ranking_sends_for_a_current_administrator(self) -> None:
        buffer = io.BytesIO(b"png")
        interaction = self.interaction(make_member(admin=True))
        button = self.ranking_button(buffer)
        with patch.object(stats_view, "send_graph_ephemeral", new=AsyncMock()) as sending:
            await button.callback(interaction)
        sending.assert_awaited_once()

    async def test_feedback_denied_when_member_lookup_fails(self) -> None:
        feedback = SimpleNamespace(build_summary_text=AsyncMock(return_value="公開してはいけない架空の集計"))
        view = StatsView(SimpleNamespace(get_user_options=Mock(return_value=[("11", "架空の本人", 1)]),
                                         display_name=Mock(return_value="架空の本人")),
                         viewer_id="11", is_admin=True, feedback=feedback)
        self.addCleanup(view.stop)
        member = make_member(admin=True)
        member.guild.fetch_member.side_effect = RuntimeError("架空の障害")
        interaction = self.interaction(member)
        await next(item for item in view.children if isinstance(item, FeedbackButton)).callback(interaction)
        interaction.followup.send.assert_awaited_once_with("この機能はサーバー管理者のみ利用できます。", ephemeral=True)


class UnblockPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_is_rechecked_before_unblocking(self) -> None:
        for owner in (False, True):
            with self.subTest(owner=owner):
                blacklist = SimpleNamespace(unblock=AsyncMock(return_value=True), is_blocked=Mock(return_value=False))
                view = UnblockView(blacklist, [{"user_id": 22, "user_name": "架空の対象"}])
                self.addCleanup(view.stop)
                select = next(item for item in view.children if isinstance(item, UnblockSelect))
                select._values = ["22"]
                interaction = SimpleNamespace(
                    user=make_member(admin=True),
                    client=make_owner_client(owner_id=11 if owner else 99),
                    response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )
                await select.callback(interaction)
                if owner:
                    blacklist.unblock.assert_awaited_once_with(22)
                else:
                    blacklist.unblock.assert_not_awaited()
                    interaction.response.defer.assert_not_awaited()
                    interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
