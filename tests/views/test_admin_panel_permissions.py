"""開いたパネルで管理者権限を失ったときの表示とブロック解除を検証する（#255）。"""

import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import discord
from persona_bot.core.app_state import BotState, GuildSettings
from persona_bot.views.auto_topic_view import AUTO_TOPIC_ADMIN_NOTICE, AutoTopicConfirmButton, AutoTopicView
from persona_bot.views.functions_view import (
    GROUNDING_ADMIN_NOTICE, FunctionsView, GroundingToggleButton, UnblockButton,
)
from persona_bot.views.stats_view import (
    AGGREGATION_BUSY_NOTICE, FeedbackButton, RankingButton, StatsItemSelect, StatsUserSelect, StatsView,
)
from persona_bot.views.unblock_view import UnblockSelect, UnblockView
from persona_bot.views.view_parts import BackButton
from tests.helpers.member_helpers import make_member, make_owner_client


class AdministratorPanelPermissionsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.stats = SimpleNamespace(
            get_user_options=Mock(return_value=[("11", "架空の本人", 1), ("22", "架空の対象者", 2)]),
            display_name=Mock(return_value="架空の対象者"),
        )
        self.blacklist = SimpleNamespace(
            unblock=AsyncMock(return_value=True), is_blocked=Mock(return_value=False),
        )

    def track(self, view):
        self.addCleanup(view.stop)
        return view

    def stats_panel(self, *, is_admin=True, make_hub=None):
        view = self.track(StatsView(
            self.stats, viewer_id="11", is_admin=is_admin, feedback=Mock(), make_hub=make_hub,
        ))
        view.temp_target_id = "22"
        self.stats.get_user_options.reset_mock()
        self.stats.display_name.reset_mock()
        return view

    def unblock_panel(self):
        return self.track(UnblockView(
            self.blacklist, [{"user_id": 22, "user_name": "架空の対象者", "count": 3}],
        ))

    @staticmethod
    def component(view, kind):
        return next(item for item in view.children if isinstance(item, kind))

    def interaction(self, *, admin=True):
        response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock())

        async def respond(**kwargs):
            response.is_done.return_value = True

        response.defer = AsyncMock(side_effect=respond)
        response.edit_message = AsyncMock(side_effect=respond)
        return SimpleNamespace(
            user=make_member(admin=admin), response=response, extras={},
            client=make_owner_client(),
            edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
            original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        )

    def assert_owner_only(self, view, interaction):
        self.assertFalse(view.is_admin)
        self.assertEqual(view.temp_target_id, view.viewer_id)
        self.assertFalse(any(isinstance(item, (StatsUserSelect, RankingButton, FeedbackButton))
                             for item in view.children))
        self.stats.get_user_options.assert_not_called()
        self.stats.display_name.assert_not_called()
        edit = interaction.response.edit_message.await_args.kwargs
        self.assertIs(edit["view"], view)
        self.assertIn("あなた自身", edit["content"])
        self.assertNotIn("架空の対象者", edit["content"])

    async def test_item_selection_removes_other_users_after_permission_revocation(self):
        view = self.stats_panel()
        interaction = self.interaction(admin=False)
        select = self.component(view, StatsItemSelect)
        select._values = ["topic"]
        await select.callback(interaction)
        self.assert_owner_only(view, interaction)
        self.assertEqual(view.temp_item, "topic")

    async def test_user_selection_removes_other_users_after_permission_revocation(self):
        view = self.stats_panel()
        interaction = self.interaction(admin=False)
        select = self.component(view, StatsUserSelect)
        select._values = ["22"]
        await select.callback(interaction)
        self.assert_owner_only(view, interaction)

    async def test_item_selection_in_dm_cannot_keep_an_old_admin_panel(self):
        view = self.stats_panel()
        interaction = self.interaction()
        interaction.user = Mock(spec=discord.User, id=11)
        select = self.component(view, StatsItemSelect)
        select._values = ["time"]
        await select.callback(interaction)
        self.assert_owner_only(view, interaction)

    async def test_current_admin_keeps_selection_and_non_admin_stays_on_self(self):
        for admin in (True, False):
            for kind, value in ((StatsItemSelect, "personality"), (StatsUserSelect, "22")):
                with self.subTest(admin=admin, select=kind.__name__):
                    view = self.stats_panel()
                    interaction = self.interaction(admin=admin)
                    select = self.component(view, kind)
                    select._values = [value]
                    await select.callback(interaction)
                    if admin:
                        self.assertTrue(view.is_admin)
                        self.assertEqual(view.temp_target_id, "22")
                        self.assertTrue(any(isinstance(item, StatsUserSelect) for item in view.children))
                        self.assertIn("架空の対象者", interaction.response.edit_message.await_args.kwargs["content"])
                    else:
                        self.assert_owner_only(view, interaction)

    async def test_back_builds_hub_using_current_permissions_in_both_directions(self):
        for before, now in ((True, False), (False, True), (True, True), (False, False)):
            with self.subTest(before=before, now=now):
                dependencies = (GuildSettings(), Mock(), Mock(), self.blacklist,
                                Mock(), Mock(), self.stats, Mock())
                hub = self.track(FunctionsView(*dependencies, is_admin=before))
                view = self.stats_panel(is_admin=before, make_hub=hub.clone)
                interaction = self.interaction(admin=now)
                interaction.client = make_owner_client(owner_id=11 if now else 99)
                await self.component(view, BackButton).callback(interaction)
                returned = self.track(interaction.response.edit_message.await_args.kwargs["view"])
                self.assertIsInstance(returned, FunctionsView)
                self.assertEqual(returned.is_admin, now)
                self.assertEqual(any(isinstance(item, UnblockButton) for item in returned.children), now)
                self.assertEqual("ブロック解除" in returned.build_preview(), now)
                self.assertTrue(view.is_finished())
                self.assertIs(returned.message, interaction.original_response.return_value)
                for name, dependency in zip(
                    ("guild_settings", "usage", "ng_filter", "blacklist", "gemini", "moderator", "stats", "reminders"),
                    dependencies,
                ):
                    self.assertIs(getattr(returned, name), dependency)

    async def test_unblock_rechecks_permissions_after_ephemeral_defer(self):
        for revoked in (True, False):
            with self.subTest(revoked=revoked):
                self.blacklist.unblock.reset_mock()
                view = self.unblock_panel()
                interaction = self.interaction()
                select = self.component(view, UnblockSelect)
                select._values = ["22"]

                async def defer(**kwargs):
                    interaction.response.is_done.return_value = True
                    # ログイン時の情報はそのまま、最新の所有者だけが変更される。
                    interaction.client.application_info.return_value = SimpleNamespace(
                        owner=SimpleNamespace(id=99 if revoked else 11), team=None,
                    )

                interaction.response.defer.side_effect = defer
                await select.callback(interaction)
                interaction.response.defer.assert_awaited_once_with(ephemeral=True)
                interaction.client.application_info.assert_awaited_once_with()
                content = interaction.edit_original_response.await_args.kwargs["content"]
                if revoked:
                    self.blacklist.unblock.assert_not_awaited()
                    self.assertIn("ボット所有者のみ", content)
                    self.assertNotIn("架空の対象者", content)
                else:
                    self.blacklist.unblock.assert_awaited_once_with(22)
                    self.assertIn("解除しました", content)
                self.assertIsNone(interaction.edit_original_response.await_args.kwargs["view"])

    async def test_unblock_denies_when_current_member_cannot_be_obtained(self):
        view = self.unblock_panel()
        interaction = self.interaction()
        interaction.client.application_info.side_effect = RuntimeError("架空の取得失敗")
        select = self.component(view, UnblockSelect)
        select._values = ["22"]
        await select.callback(interaction)
        self.blacklist.unblock.assert_not_awaited()
        self.assertIn("ボット所有者のみ", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_unblock_cancellation_during_permission_lookup_is_propagated(self):
        view = self.unblock_panel()
        interaction = self.interaction()
        interaction.client.application_info.side_effect = asyncio.CancelledError
        select = self.component(view, UnblockSelect)
        select._values = ["22"]
        with self.assertRaises(asyncio.CancelledError):
            await select.callback(interaction)
        self.blacklist.unblock.assert_not_awaited()

    async def test_non_owner_cannot_start_unblock(self):
        view = self.unblock_panel()
        interaction = self.interaction(admin=False)
        interaction.client = make_owner_client(owner_id=99)
        select = self.component(view, UnblockSelect)
        select._values = ["22"]
        await select.callback(interaction)
        self.blacklist.unblock.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once_with(
            "この機能はボット所有者のみ利用できます。", ephemeral=True,
        )


class ServerSettingPermissionTests(unittest.IsolatedAsyncioTestCase):
    """🔍 Google検索の切り替えと 💬 自発会話の確定は、サーバーでは管理者だけ（#398）。"""

    def panel(self, kind, settings):
        if kind == "grounding":
            view = FunctionsView(settings, Mock(), Mock(), Mock(), Mock(), Mock(), Mock(), Mock())
            view.blocked_user_check = None
            button = GroundingToggleButton
        else:
            view = AutoTopicView(settings)
            # 確定で書き込まれる値を、既定（どちらも OFF）から変えておく
            view.temp_auto_topic_enabled = view.temp_autonomous_enabled = True
            button = AutoTopicConfirmButton
        self.addCleanup(view.stop)
        return view, next(item for item in view.children if isinstance(item, button))

    @staticmethod
    def snapshot(settings):
        return (settings.grounding_enabled, settings.auto_topic_enabled, settings.autonomous_enabled)

    @staticmethod
    def interaction(user, *, guild=True):
        response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock(), edit_message=AsyncMock())

        async def defer(**kwargs):
            response.is_done.return_value = True

        response.defer = AsyncMock(side_effect=defer)
        return SimpleNamespace(
            user=user, response=response, extras={}, edit_original_response=AsyncMock(),
            guild=SimpleNamespace(id=1) if guild else None, followup=SimpleNamespace(send=AsyncMock()),
        )

    def settings(self, guild_id=1):
        settings = BotState().get_guild(guild_id)
        settings.persist = AsyncMock()
        return settings

    async def test_non_administrator_is_refused_before_any_change(self):
        for kind, notice in (("grounding", GROUNDING_ADMIN_NOTICE), ("auto", AUTO_TOPIC_ADMIN_NOTICE)):
            with self.subTest(kind=kind):
                settings = self.settings()
                before = self.snapshot(settings)
                _view, button = self.panel(kind, settings)
                interaction = self.interaction(make_member(admin=False))
                await button.callback(interaction)
                interaction.response.send_message.assert_awaited_once_with(notice, ephemeral=True)
                interaction.response.defer.assert_not_awaited()
                settings.persist.assert_not_awaited()
                self.assertEqual(self.snapshot(settings), before)

    async def test_role_removed_before_the_write_is_refused_with_the_current_member(self):
        for kind, notice in (("grounding", GROUNDING_ADMIN_NOTICE), ("auto", AUTO_TOPIC_ADMIN_NOTICE)):
            for lookup_fails in (False, True):
                with self.subTest(kind=kind, lookup_fails=lookup_fails):
                    settings = self.settings()
                    before = self.snapshot(settings)
                    _view, button = self.panel(kind, settings)
                    member = make_member(admin=True, fresh_admin=False)
                    if lookup_fails:
                        member.guild.fetch_member.side_effect = RuntimeError("架空の取得失敗")
                    interaction = self.interaction(member)
                    await button.callback(interaction)
                    interaction.response.defer.assert_awaited_once()
                    self.assertEqual(interaction.edit_original_response.await_args.kwargs["content"], notice)
                    settings.persist.assert_not_awaited()
                    self.assertEqual(self.snapshot(settings), before)

    async def test_current_administrator_changes_and_saves_the_setting(self):
        for kind in ("grounding", "auto"):
            with self.subTest(kind=kind):
                settings = self.settings()
                before = self.snapshot(settings)
                _view, button = self.panel(kind, settings)
                await button.callback(self.interaction(make_member(admin=True)))
                settings.persist.assert_awaited_once()
                self.assertNotEqual(self.snapshot(settings), before)

    async def test_dm_settings_stay_operable_by_the_user(self):
        for kind in ("grounding", "auto"):
            with self.subTest(kind=kind):
                settings = self.settings(None)
                before = self.snapshot(settings)
                _view, button = self.panel(kind, settings)
                interaction = self.interaction(Mock(spec=discord.User, id=11), guild=False)
                await button.callback(interaction)
                interaction.response.send_message.assert_not_awaited()
                settings.persist.assert_awaited_once()
                self.assertNotEqual(self.snapshot(settings), before)


class AggregationRepeatPressTests(unittest.IsolatedAsyncioTestCase):
    """全体比較・フィードバックの集計を、実行中にもう一度押しても重ねない（#398）。"""

    def panel(self, stats, feedback):
        view = StatsView(stats, viewer_id="11", is_admin=True, feedback=feedback)
        self.addCleanup(view.stop)
        return view

    @staticmethod
    def interaction():
        return SimpleNamespace(
            user=make_member(admin=True), guild_id=1, extras={},
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def test_second_press_while_running_is_refused_and_the_button_frees_afterwards(self):
        for kind in ("ranking", "feedback"):
            with self.subTest(kind=kind):
                release = asyncio.Event()

                async def slow(*args):
                    await release.wait()
                    return None if kind == "ranking" else "架空の集計"

                stats = SimpleNamespace(get_user_options=Mock(return_value=[]), build_ranking_graph=AsyncMock(side_effect=slow))
                feedback = SimpleNamespace(build_summary_text=AsyncMock(side_effect=slow))
                view = self.panel(stats, feedback)
                button = next(item for item in view.children
                              if isinstance(item, RankingButton if kind == "ranking" else FeedbackButton))
                work = stats.build_ranking_graph if kind == "ranking" else feedback.build_summary_text

                first = asyncio.create_task(button.callback(self.interaction()))
                await asyncio.sleep(0)
                second = self.interaction()
                await button.callback(second)
                second.response.send_message.assert_awaited_once_with(AGGREGATION_BUSY_NOTICE, ephemeral=True)
                second.response.defer.assert_not_awaited()
                self.assertEqual(work.await_count, 1)

                release.set()
                await first
                # 終わったら、また押せる（失敗・キャンセルでも finally で印を外す）
                third = self.interaction()
                await button.callback(third)
                third.response.defer.assert_awaited_once()
                self.assertEqual(work.await_count, 2)

    async def test_failure_or_cancellation_frees_the_button(self):
        stats = SimpleNamespace(get_user_options=Mock(return_value=[]),
                                build_ranking_graph=AsyncMock(side_effect=asyncio.CancelledError))
        view = self.panel(stats, None)
        button = next(item for item in view.children if isinstance(item, RankingButton))
        with self.assertRaises(asyncio.CancelledError):
            await button.callback(self.interaction())
        self.assertTrue(view.try_begin_aggregation("ranking"))


if __name__ == "__main__":
    unittest.main()
