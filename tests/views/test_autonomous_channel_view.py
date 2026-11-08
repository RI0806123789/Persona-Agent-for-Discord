"""自律行動の配信先パネル（⚙️ キャラ設定 → 🎙️ 配信先）の表示・権限・確定を確かめる（#266）。

Discord はモックで、DB には書き込まない（保存は persist を差し替えて呼ばれたかだけを見る）。
チャンネル名・ID はすべて架空のもの。
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona_bot.config.constants import DISCORD_MESSAGE_MAX_LENGTH, DISCORD_SELECT_MAX_OPTIONS
from persona_bot.core.app_state import BotState
from persona_bot.views.autonomous_channel_view import (
    NOT_ADMIN_NOTICE, AutonomousChannelConfirmButton, AutonomousChannelView, StageChannelSelect, VoiceChannelSelect,
)
from persona_bot.views.settings_view import OpenAutonomousChannelButton, SettingsView
from persona_bot.views.view_parts import BackButton
from tests.helpers import discord_mocks
from tests.helpers.member_helpers import make_member, make_owner_client
from tests.helpers.persona_helpers import install_empty_persona_catalog, install_persona_catalog


def make_channel(channel_id: int, name: str, **denied: bool):
    permissions = SimpleNamespace(
        view_channel=True, connect=True, speak=True, send_messages=True,
        mute_members=True, manage_channels=True, move_members=True,
    )
    for key, value in denied.items():
        setattr(permissions, key, value)
    return discord_mocks.make_channel(
        channel_id=channel_id, name=name, instance=None, permissions_for=Mock(return_value=permissions),
    )


def make_guild(*, stages: int = 2, voices: int = 2):
    return discord_mocks.make_guild(
        me=object(),
        stage_channels=[make_channel(2000 + index, f"架空のステージ{index}") for index in range(stages)],
        voice_channels=[make_channel(3000 + index, f"架空のボイス{index}") for index in range(voices)],
    )


def make_interaction(*, admin: bool = True, fresh_admin: bool | None = None, guild=None):
    interaction = discord_mocks.make_interaction(
        user=make_member(admin=admin, fresh_admin=fresh_admin),
        guild=guild,
        extras={},
        original_response=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock())),
        without=("followup",),
    )
    async def defer(**kwargs):
        interaction.response.is_done.return_value = True
    interaction.response.defer.side_effect = defer
    return interaction


def child(view, item_type):
    return next(item for item in view.children if isinstance(item, item_type))


class SettingsEntryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        install_empty_persona_catalog(self)
        refresh = patch("persona_bot.views.settings_view.refresh_persona_catalog", new=AsyncMock())
        self.refresh_catalog = refresh.start()
        self.addCleanup(refresh.stop)
        self.settings = BotState().get_guild(1)
        self.guild = make_guild()

    def settings_view(self, *, is_admin: bool) -> SettingsView:
        view = SettingsView(self.settings, 0, "なし", is_admin=is_admin)
        self.addCleanup(view.stop)
        return view

    async def test_button_is_shown_only_to_administrators(self) -> None:
        member_view = self.settings_view(is_admin=False)
        self.assertFalse(any(isinstance(item, OpenAutonomousChannelButton) for item in member_view.children))
        self.assertNotIn("配信先", member_view.build_preview())

        admin_view = self.settings_view(is_admin=True)
        button = child(admin_view, OpenAutonomousChannelButton)
        self.assertEqual(button.row, 4)
        self.assertIn("🎙️ **配信先**", admin_view.build_preview())
        # 選択を変えて組み直しても残る
        admin_view.refresh_items()
        child(admin_view, OpenAutonomousChannelButton)

    async def test_pressing_without_administrator_or_guild_is_rejected(self) -> None:
        for interaction in (make_interaction(admin=False, guild=self.guild), make_interaction(guild=None)):
            with self.subTest(guild=interaction.guild):
                view = self.settings_view(is_admin=True)
                await child(view, OpenAutonomousChannelButton).callback(interaction)
                interaction.response.send_message.assert_awaited_once()
                self.assertIn("管理者のみ", interaction.response.send_message.await_args.args[0])
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
                interaction.response.edit_message.assert_not_awaited()
                self.assertFalse(view.is_finished())

    async def test_hub_passes_press_time_administrator_to_settings_panel(self) -> None:
        from persona_bot.views import functions_view

        for admin in (False, True):
            with self.subTest(admin=admin):
                hub = functions_view.FunctionsView(
                    self.settings, Mock(), Mock(),
                    SimpleNamespace(is_blocked=Mock(return_value=False), get_all_blocked_users=Mock(return_value=[])),
                    Mock(), Mock(), Mock(), Mock(), is_admin=not admin,
                )
                self.addCleanup(hub.stop)
                interaction = make_interaction(admin=admin, guild=self.guild)
                interaction.response.is_done = Mock(return_value=True)
                with patch.object(functions_view, "refresh_persona_catalog", AsyncMock()):
                    await child(hub, functions_view.OpenSettingsButton).callback(interaction)
                panel = interaction.edit_original_response.await_args.kwargs["view"]
                self.addCleanup(panel.stop)
                # ハブを開いた時点ではなく、押した時点の権限で決める
                self.assertIs(panel.is_admin, admin)
                self.assertIs(any(isinstance(item, OpenAutonomousChannelButton) for item in panel.children), admin)

    async def test_revoked_administrator_cannot_open_channel_panel(self) -> None:
        view = self.settings_view(is_admin=True)
        interaction = make_interaction(guild=self.guild, fresh_admin=False)
        with patch("persona_bot.views.settings_view.AutonomousChannelView") as panel:
            await child(view, OpenAutonomousChannelButton).callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.user.guild.fetch_member.assert_awaited_once_with(interaction.user.id)
        panel.assert_not_called()
        edited = interaction.edit_original_response.await_args.kwargs
        self.assertIsNone(edited["view"])
        self.assertIn("管理者のみ", edited["content"])
        self.assertTrue(view.is_finished())

    async def test_member_lookup_failure_denies_channel_panel_after_defer(self) -> None:
        view = self.settings_view(is_admin=True)
        interaction = make_interaction(guild=self.guild)
        async def unavailable(user_id):
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)
            raise RuntimeError("架空の取得失敗")
        interaction.user.guild.fetch_member.side_effect = unavailable
        with patch("builtins.print"), patch("persona_bot.views.settings_view.AutonomousChannelView") as panel:
            await child(view, OpenAutonomousChannelButton).callback(interaction)
        panel.assert_not_called()
        self.assertIsNone(interaction.edit_original_response.await_args.kwargs["view"])

    async def test_administrator_swaps_to_panel_and_keeps_blacklist_check(self) -> None:
        view = self.settings_view(is_admin=True)
        blocked = Mock(return_value=False)
        view.blocked_user_check = blocked
        interaction = make_interaction(guild=self.guild)
        await child(view, OpenAutonomousChannelButton).callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.user.guild.fetch_member.assert_awaited_once_with(interaction.user.id)
        panel = interaction.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        self.assertIsInstance(panel, AutonomousChannelView)
        self.assertIs(panel.blocked_user_check, blocked)
        self.assertIs(panel.guild, self.guild)
        self.assertTrue(view.is_finished())
        self.assertIn("配信先パネル", interaction.edit_original_response.await_args.kwargs["content"])

    async def test_back_returns_to_settings_before_returning_to_hub(self) -> None:
        view = self.settings_view(is_admin=True)
        view.ng_count = 3
        view.blacklist_status = "架空の状態"
        view.blacklist_provider = Mock(return_value=[object(), object()])
        view.blocked_user_check = Mock(return_value=False)
        make_hub = Mock()
        view.make_hub = make_hub
        opening = make_interaction(guild=self.guild)
        await child(view, OpenAutonomousChannelButton).callback(opening)
        panel = opening.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        # 配信先の選択は「戻る」で保存されない。
        panel.temp_stage_channel_id = 2000
        panel.temp_voice_channel_id = 3000
        self.settings.persist = AsyncMock()

        returning = make_interaction(guild=self.guild)
        returning.client = make_owner_client(owner_id=returning.user.id)
        await child(panel, BackButton).callback(returning)
        parent = returning.edit_original_response.await_args.kwargs["view"]
        self.assertIsInstance(parent, SettingsView)
        self.addCleanup(parent.stop)
        self.assertIn("キャラ設定パネル", returning.edit_original_response.await_args.kwargs["content"])
        self.assertIs(parent.guild_settings, self.settings)
        self.assertEqual(parent.ng_count, 3)
        self.assertEqual(parent.blacklist_status, "2人")
        self.assertIs(parent.make_hub, make_hub)
        self.assertIs(parent.blocked_user_check, view.blocked_user_check)
        self.assertIs(parent.message, await returning.original_response())
        self.assertTrue(panel.is_finished())
        self.assertIsNone(self.settings.autonomous_stage_channel_id)
        self.assertIsNone(self.settings.autonomous_voice_channel_id)
        self.settings.persist.assert_not_awaited()
        make_hub.assert_not_called()

        from persona_bot.views.functions_view import FunctionsView
        hub = FunctionsView(
            self.settings, Mock(), Mock(),
            SimpleNamespace(is_blocked=Mock(return_value=False), get_all_blocked_users=Mock(return_value=[])),
            Mock(), Mock(), Mock(), Mock(), is_admin=True,
        )
        self.addCleanup(hub.stop)
        make_hub.return_value = hub
        to_hub = make_interaction(guild=self.guild)
        await child(parent, BackButton).callback(to_hub)
        self.assertIs(to_hub.response.edit_message.await_args.kwargs["view"], hub)
        make_hub.assert_called_once_with(to_hub)
        self.assertTrue(parent.is_finished())

    async def test_return_to_settings_uses_press_time_administrator(self) -> None:
        for admin in (False, True):
            with self.subTest(admin=admin):
                view = self.settings_view(is_admin=True)
                opening = make_interaction(guild=self.guild)
                await child(view, OpenAutonomousChannelButton).callback(opening)
                panel = opening.edit_original_response.await_args.kwargs["view"]
                self.addCleanup(panel.stop)

                returning = make_interaction(admin=admin, guild=self.guild)
                await child(panel, BackButton).callback(returning)
                parent = returning.edit_original_response.await_args.kwargs["view"]
                self.assertIsInstance(parent, SettingsView)
                self.addCleanup(parent.stop)
                self.assertIs(parent.is_admin, admin)
                self.assertIs(
                    any(isinstance(item, OpenAutonomousChannelButton) for item in parent.children), admin,
                )

    async def test_return_refreshes_catalog_and_counts_without_losing_unsaved_choices(self) -> None:
        from persona_bot.config.choices import MODEL_CHOICES
        original = install_persona_catalog(self)
        view = self.settings_view(is_admin=True)
        view.temp_persona = original.personas[-1].value
        view.temp_model = MODEL_CHOICES[-1].value
        view.temp_respond_mode = "all"
        ng = SimpleNamespace(count=3)
        view.ng_count_provider = lambda: ng.count
        opening = make_interaction(guild=self.guild)
        await child(view, OpenAutonomousChannelButton).callback(opening)
        panel = opening.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        updated = []

        async def refresh():
            updated.append(install_persona_catalog(self, "../prompts/new_fictional.txt"))

        self.refresh_catalog.side_effect = refresh
        ng.count = 7
        returning = make_interaction(guild=self.guild)
        await child(panel, BackButton).callback(returning)
        parent = returning.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(parent.stop)
        self.refresh_catalog.assert_awaited_once()
        self.assertIs(parent.catalog, updated[0])
        self.assertEqual(parent.ng_count, 7)
        self.assertEqual(parent.temp_persona, view.temp_persona)
        self.assertEqual(parent.temp_model, view.temp_model)
        self.assertEqual(parent.temp_respond_mode, view.temp_respond_mode)
        self.assertIn("ファイルが見つからない", parent.build_preview())
        self.assertNotEqual(self.settings.current_prompt_file, view.temp_persona)

    async def test_return_does_not_redisplay_blacklist_count_after_owner_change(self) -> None:
        view = self.settings_view(is_admin=True)
        view.blacklist_status = "3人"
        opening = make_interaction(guild=self.guild)
        await child(view, OpenAutonomousChannelButton).callback(opening)
        panel = opening.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(panel.stop)

        returning = make_interaction(guild=self.guild)
        returning.client = make_owner_client(owner_id=returning.user.id)
        returning.client.application_info.return_value = make_owner_client(owner_id=99).application
        await child(panel, BackButton).callback(returning)
        edited = returning.edit_original_response.await_args or returning.response.edit_message.await_args
        parent = edited.kwargs["view"]
        self.addCleanup(parent.stop)
        self.assertIn("非公開（ボット所有者のみ）", edited.kwargs["content"])
        self.assertNotIn("3人", edited.kwargs["content"])

    async def test_timeout_during_owner_lookup_does_not_reopen_settings(self) -> None:
        view = self.settings_view(is_admin=True)
        opening = make_interaction(guild=self.guild)
        await child(view, OpenAutonomousChannelButton).callback(opening)
        panel = opening.edit_original_response.await_args.kwargs["view"]
        self.addCleanup(panel.stop)
        returning = make_interaction(guild=self.guild)
        returning.client = make_owner_client(owner_id=returning.user.id)
        application = returning.client.application
        started, release = asyncio.Event(), asyncio.Event()

        async def lookup():
            started.set()
            await release.wait()
            return application

        returning.client.application_info.side_effect = lookup
        run = asyncio.create_task(child(panel, BackButton).callback(returning))
        try:
            await asyncio.wait_for(started.wait(), 2)
            returning.response.defer.assert_awaited_once_with(ephemeral=True)
            await panel.on_timeout()
            release.set()
            await asyncio.wait_for(run, 2)
            returning.edit_original_response.assert_not_awaited()
            returning.response.edit_message.assert_not_awaited()
            self.assertTrue(panel.is_finished())
        finally:
            release.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


class AutonomousChannelPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.settings = BotState().get_guild(1)
        self.settings.persist = AsyncMock()
        self.guild = make_guild()

    def panel(self, guild=None) -> AutonomousChannelView:
        view = AutonomousChannelView(self.settings, guild or self.guild)
        self.addCleanup(view.stop)
        return view

    async def choose(self, view, item_type, value: str):
        item = child(view, item_type)
        item._values = [value]
        interaction = make_interaction(guild=view.guild)
        await item.callback(interaction)
        return interaction

    async def test_unset_is_first_and_default(self) -> None:
        view = self.panel()
        for item_type, names in ((StageChannelSelect, "架空のステージ"), (VoiceChannelSelect, "架空のボイス")):
            options = child(view, item_type).options
            self.assertEqual(options[0].label, "指定なし（自動）")
            self.assertTrue(options[0].default)
            self.assertEqual([option.label for option in options[1:]], [f"{names}0", f"{names}1"])
        text = view.build_preview()
        self.assertEqual(text.count("指定なし（自動で選びます）"), 2)
        self.assertIn("手動でボットを別のチャンネルへ移すことは防げません", text)
        self.assertIn("移された場合は配信を終了します", text)

    async def test_selection_changes_nothing_until_confirm(self) -> None:
        view = self.panel()
        self.settings.autonomous_start_failures = 3
        self.settings.autonomous_start_retry_at = datetime.now(timezone.utc)
        interaction = await self.choose(view, StageChannelSelect, "2001")
        await self.choose(view, VoiceChannelSelect, "3000")
        self.assertIsNone(self.settings.autonomous_stage_channel_id)
        self.assertIsNone(self.settings.autonomous_voice_channel_id)
        self.settings.persist.assert_not_awaited()
        self.assertIn("<#2001>", interaction.response.edit_message.await_args.kwargs["content"])
        self.assertTrue(next(o for o in child(view, StageChannelSelect).options if o.value == "2001").default)

        confirm = make_interaction(guild=self.guild)
        await child(view, AutonomousChannelConfirmButton).callback(confirm)
        confirm.response.defer.assert_awaited_once_with(ephemeral=True)
        confirm.user.guild.fetch_member.assert_awaited_once()
        self.assertEqual(self.settings.autonomous_stage_channel_id, 2001)
        self.assertEqual(self.settings.autonomous_voice_channel_id, 3000)
        self.settings.persist.assert_awaited_once()
        # 開始失敗の待ち時間も解除する（💬 自発会話 の確定と同じ）
        self.assertEqual(self.settings.autonomous_start_failures, 0)
        self.assertIsNone(self.settings.autonomous_start_retry_at)
        edited = confirm.edit_original_response.await_args.kwargs
        self.assertIsNone(edited["view"])
        self.assertIn("✅ 自律行動の配信先を更新しました", edited["content"])

    async def test_choosing_unset_clears_designation(self) -> None:
        self.settings.autonomous_voice_channel_id = 3001
        view = self.panel()
        await self.choose(view, VoiceChannelSelect, "0")
        await child(view, AutonomousChannelConfirmButton).callback(make_interaction(guild=self.guild))
        self.assertIsNone(self.settings.autonomous_voice_channel_id)

    async def test_confirm_is_rejected_when_administrator_was_revoked(self) -> None:
        view = self.panel()
        await self.choose(view, StageChannelSelect, "2000")
        confirm = make_interaction(admin=True, fresh_admin=False, guild=self.guild)
        await child(view, AutonomousChannelConfirmButton).callback(confirm)
        self.assertIsNone(self.settings.autonomous_stage_channel_id)
        self.settings.persist.assert_not_awaited()
        self.assertEqual(confirm.edit_original_response.await_args.kwargs["content"], NOT_ADMIN_NOTICE)

    async def test_more_than_limit_is_truncated_but_keeps_selected_channel(self) -> None:
        guild = make_guild(stages=1, voices=40)
        selected = guild.voice_channels[-1].id
        self.settings.autonomous_voice_channel_id = selected
        view = self.panel(guild)
        options = child(view, VoiceChannelSelect).options
        self.assertEqual(len(options), DISCORD_SELECT_MAX_OPTIONS)
        self.assertEqual(options[-1].value, str(selected))
        self.assertTrue(options[-1].default)
        self.assertFalse(options[0].default)
        self.assertEqual(options[1].value, str(guild.voice_channels[0].id))
        text = view.build_preview()
        self.assertIn(f"ボイスチャンネルが多いため、先頭の{DISCORD_SELECT_MAX_OPTIONS - 1}件", text)
        self.assertNotIn("ステージチャンネルが多いため", text)
        self.assertLessEqual(len(text), DISCORD_MESSAGE_MAX_LENGTH)

    async def test_unusable_and_deleted_designations_are_explained(self) -> None:
        guild = make_guild()
        guild.stage_channels[1] = make_channel(2001, "権限の無いステージ", mute_members=False)
        self.settings.autonomous_stage_channel_id = 2001
        self.settings.autonomous_voice_channel_id = 3999  # 削除済み
        view = self.panel(guild)
        text = view.build_preview()
        self.assertIn("<#2001>（⚠️ 権限が足りないため、いまは使えません。自動で選びます）", text)
        self.assertIn("⚠️ 見つかりません（削除された可能性があります。自動で選びます）", text)
        stage_option = next(o for o in child(view, StageChannelSelect).options if o.value == "2001")
        self.assertIn("権限が足りない", stage_option.description)
        self.assertIsNone(next(o for o in child(view, StageChannelSelect).options if o.value == "2000").description)
        # 削除済みの指定は選択肢に出ないため「指定なし」も既定にしない（確定するまで指定を保つ）
        self.assertFalse(any(option.default for option in child(view, VoiceChannelSelect).options))

    async def test_channel_names_are_single_line_and_server_without_channels_still_opens(self) -> None:
        guild = make_guild(stages=0, voices=1)
        guild.voice_channels[0].name = "改行\nを含む  名前"
        view = self.panel(guild)
        self.assertEqual([o.label for o in child(view, StageChannelSelect).options], ["指定なし（自動）"])
        self.assertEqual(child(view, VoiceChannelSelect).options[1].label, "改行 を含む 名前")


if __name__ == "__main__":
    unittest.main()
