from typing import Callable

import discord
from discord import ui

from persona_bot.core.app_state import GuildSettings
from persona_bot.core.background import run_in_thread
from persona_bot.config.choices import (
    MODEL_CHOICES,
    MODEL_DISPLAY_NAMES,
    RESPOND_MODE_CHOICES,
    RESPOND_MODE_DISPLAY_NAMES,
)
from persona_bot.config.constants import (
    DISCORD_SELECT_MAX_OPTIONS,
    PERSONA_SETTINGS_TEMPLATE_FILE_NAME,
    PROMPT_TEMPLATE_FILE_NAME,
)
from persona_bot.config.personas import (
    FOLLOW_DEFAULT_PERSONA, PersonaCatalog, current_persona_catalog, is_selectable_persona,
    refresh_persona_catalog,
)
from persona_bot.utils.discord_helpers import (
    is_bot_owner, is_current_administrator, is_current_bot_owner,
)
from persona_bot.views.autonomous_channel_view import AutonomousChannelView
from persona_bot.views.view_parts import (
    authorize_administrator_operation, confirm_administrator_operation, begin_panel_operation, edit_panel_message, BackButton,
    HubFactory, PanelView, select_option_text, swap_panel,
)


def _build_status_text(
    persona_file: str,
    model_name: str,
    respond_mode: str,
    ng_count: int,
    blacklist_status: str,
    *,
    confirmed: bool = False,
    catalog: PersonaCatalog | None = None,
) -> str:
    """キャラ設定パネルのステータス表示テキストを組み立てる。"""
    catalog = current_persona_catalog() if catalog is None else catalog
    persona_display = _persona_status(catalog, persona_file)
    model_display = MODEL_DISPLAY_NAMES.get(model_name, f"不明 ({model_name})")
    mode_display = RESPOND_MODE_DISPLAY_NAMES.get(respond_mode, f"不明 ({respond_mode})")

    if confirmed:
        header = "✅ キャラ設定を更新しました。"
    else:
        header = "⚙️ **キャラ設定パネル** — 変更したい項目を選択してください。"

    return (
        f"{header}\n"
        f"・**ペルソナ**: {persona_display}\n"
        f"・**モデル**: {model_display}\n"
        f"・**応答モード**: {mode_display}\n"
        f"・**NGワード**: {ng_count}件\n"
        f"・**ブラックリスト**: {blacklist_status}"
        f"{_persona_notice(catalog)}"
        "\n\nサーバーの設定確定は管理者のみ行えます。DMでは本人が変更できます。"
    )


def _persona_status(catalog: PersonaCatalog, persona_file: str) -> str:
    """ペルソナの表示。未選択は既定の名前を、見つからないファイルは既定で応答中と添える。"""
    default_name = catalog.display_name(catalog.default_value)
    if persona_file == FOLLOW_DEFAULT_PERSONA:
        return f"既定（{default_name}）"
    if not catalog.contains(persona_file):
        return f"⚠️ {catalog.display_name(persona_file)}（ファイルが見つからないため、既定の{default_name}で応答中）"
    return catalog.display_name(persona_file)


def _persona_notice(catalog: PersonaCatalog) -> str:
    """ペルソナを作っていない・多すぎて全部を選択肢に出せないときの案内。"""
    if not catalog.personas:
        return (
            "\n\nℹ️ ペルソナが未登録のため、組み込みの標準プロンプトで応答しています。"
            f"`prompts/{PROMPT_TEMPLATE_FILE_NAME}` をコピーして `prompts/<名前>.txt` を作ると、"
            "このパネルを開き直したときに選べます（作り方は README の「ペルソナを作る」）。"
            f"表示名と声は `prompts/{PERSONA_SETTINGS_TEMPLATE_FILE_NAME}` を `prompts/personas.json` に"
            "コピーして決められます。"
        )
    if len(catalog.personas) > DISCORD_SELECT_MAX_OPTIONS:
        return f"\n\nℹ️ ペルソナが多いため、先頭の{DISCORD_SELECT_MAX_OPTIONS}件だけを選択肢に表示しています。"
    return ""


class PersonaSelect(ui.Select):
    """ペルソナ選択用セレクトメニュー。"""

    def __init__(self, current_value: str, catalog: PersonaCatalog) -> None:
        # 選択肢は prompts/ のファイルから作る（Discord の上限を超える分は出さない）
        options = [
            discord.SelectOption(
                label=select_option_text(persona.name, fallback=persona.value),
                value=persona.value,
                default=(persona.value == current_value),
            )
            for persona in catalog.personas[:DISCORD_SELECT_MAX_OPTIONS]
        ]
        super().__init__(
            placeholder="👤 ペルソナを選択…",
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_persona", self.values[0]))


class ModelSelect(ui.Select):
    """AIモデル選択用セレクトメニュー。"""

    def __init__(self, current_value: str) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=(choice.value == current_value),
            )
            for choice in MODEL_CHOICES
        ]
        super().__init__(
            placeholder="🤖 AIモデルを選択…",
            options=options,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_model", self.values[0]))


class RespondModeSelect(ui.Select):
    """応答モード選択用セレクトメニュー。"""

    def __init__(self, current_value: str) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                value=choice.value,
                default=(choice.value == current_value),
            )
            for choice in RESPOND_MODE_CHOICES
        ]
        super().__init__(
            placeholder="💬 応答モードを選択…",
            options=options,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_respond_mode", self.values[0]))


class ConfirmButton(ui.Button):
    """確定ボタン。"""

    def __init__(self) -> None:
        super().__init__(
            label="確定",
            style=discord.ButtonStyle.success,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        notice = "サーバーのキャラ設定を確定できるのは管理者のみです。設定は変更されていません。"
        if not await authorize_administrator_operation(view, interaction, allow_dm=True, notice=notice, refresh=False):
            return

        # ペルソナを変えたときだけ、選んだファイルが prompts/ にあるかを確かめる。
        # 変えていなければ、見つからない保存値もそのまま残す（ファイルを戻せば元に戻る）。
        persona_changed = view.temp_persona != view.guild_settings.current_prompt_file
        if persona_changed and not await run_in_thread(is_selectable_persona, view.temp_persona):
            await edit_panel_message(interaction, deferred=True,
                content=f"エラー: ファイル「{view.temp_persona}」が見つかりませんでした。設定は変更されていません。",
                view=None,
            )
            return

        # 一時選択値をこのサーバー（ギルド）専用の設定に反映し、DB へ保存する
        if not await confirm_administrator_operation(view, interaction, allow_dm=True, notice=notice):
            return
        view.guild_settings.current_prompt_file = view.temp_persona
        view.guild_settings.current_model_name = view.temp_model
        view.guild_settings.current_respond_mode = view.temp_respond_mode
        await view.guild_settings.persist()
        if not await view.ensure_settings_current(interaction):
            return

        confirmed_text = _build_status_text(
            view.temp_persona,
            view.temp_model,
            view.temp_respond_mode,
            view.ng_count,
            view.blacklist_status,
            confirmed=True,
            catalog=view.catalog,
        )

        await edit_panel_message(interaction, deferred=True, content=confirmed_text, view=None)


class ResetButton(ui.Button):
    """会話履歴リセットボタン。"""

    def __init__(self) -> None:
        super().__init__(
            label="会話リセット",
            style=discord.ButtonStyle.secondary,
            emoji="🔄",
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]
        operation = begin_panel_operation(view, interaction)
        if operation is not None and not operation.is_current():
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return

        channel = interaction.channel
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            await edit_panel_message(interaction,
                content="エラー: チャンネルが取得できませんでした。",
                view=None,
            )
            return

        # パネル作成時ではなく、ボタン押下時点を境界にする。
        # Interaction ID もメッセージと同じ Snowflake なので履歴の after に使える。
        edited = await edit_panel_message(interaction,
            content="🔄 会話履歴をリセットしました。ここから新しい会話を始めます。",
            view=None,
        )
        if edited and view.guild_settings.is_active:
            view.guild_settings.channel_reset_points[int(channel_id)] = interaction.id


class OpenAutonomousChannelButton(ui.Button):
    """自律行動の配信先パネル（AutonomousChannelView）を開くボタン（管理者専用）。

    キャラ設定パネルはセレクト3つで row 0〜2 を使っていて、配信先のセレクトを
    足す余地が無いため、同じメッセージを専用パネルへ差し替える。
    """

    def __init__(self) -> None:
        super().__init__(label="配信先", style=discord.ButtonStyle.primary, emoji="🎙️", row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: SettingsView = self.view  # type: ignore[assignment]

        # ボタンは管理者にしか出さないが、押した時点の権限でも確かめる（DM には配信先が無い）。
        guild = interaction.guild
        if not await authorize_administrator_operation(
            view, interaction, notice="この機能はサーバー管理者のみ利用できます。",
        ):
            return
        async def make_settings(back_interaction: discord.Interaction) -> SettingsView:
            await back_interaction.response.defer(ephemeral=True)
            # 確定前の選択値は引き継ぐ（戻る操作では未確定の配信先を保存しない）。
            return await build_settings_view(
                back_interaction, view.guild_settings,
                ng_count_provider=view.ng_count_provider, blacklist_provider=view.blacklist_provider,
                make_hub=view.make_hub, fallback_ng_count=view.ng_count, previous=view,
            )

        channel_view = AutonomousChannelView(
            view.guild_settings, guild, make_hub=view.make_hub, make_settings=make_settings,
        )
        # 同じ（エフェメラルな）メッセージを配信先パネルに差し替える
        await swap_panel(interaction, view, channel_view, channel_view.build_preview())


BLACKLIST_PRIVATE_STATUS = "非公開（ボット所有者のみ）"


async def build_settings_view(
    interaction: discord.Interaction,
    guild_settings: GuildSettings,
    *,
    ng_count_provider: Callable[[], int] | None,
    blacklist_provider: Callable[[], list] | None,
    make_hub: HubFactory | None,
    fallback_ng_count: int = 0,
    previous: "SettingsView | None" = None,
) -> "SettingsView":
    """キャラ設定パネルを開く・開き直すときの共通の組み立て（#415）。

    ハブからの 1 回目と、配信先パネルから戻るときの両方で、次を同じ手順にそろえる:
      ・prompts/ のペルソナ一覧を読み直す（再起動なしで選べるように）
      ・全体の違反件数は、最新のボット所有者だけに見せる（サーバー管理者にも開示しない）
      ・管理者専用の 🎙️ 配信先 を出すかは、キャッシュの写しではなく最新の Member で決める
    応答の保留（defer）は呼び出し側で済ませてから呼ぶこと。previous を渡すと、確定前の選択値を引き継ぐ。
    """
    await refresh_persona_catalog()
    blacklist_status = BLACKLIST_PRIVATE_STATUS
    if is_bot_owner(interaction) and await is_current_bot_owner(interaction):
        if blacklist_provider is not None:
            blocked_users = blacklist_provider()
            blacklist_status = f"{len(blocked_users)}人" if blocked_users else "なし"
    settings = SettingsView(
        guild_settings,
        ng_count_provider() if ng_count_provider is not None else fallback_ng_count,
        blacklist_status,
        make_hub=make_hub,
        is_admin=await is_current_administrator(interaction),
        ng_count_provider=ng_count_provider,
        blacklist_provider=blacklist_provider,
    )
    if previous is not None:
        settings.temp_persona = previous.temp_persona
        settings.temp_model = previous.temp_model
        settings.temp_respond_mode = previous.temp_respond_mode
        settings.refresh_items()
    return settings


class SettingsView(PanelView):
    """キャラ設定パネル全体を表す View。

    3つのセレクトメニューと確定/戻るボタンを保持し、一時選択値を管理する。
    Google検索トグルは機能パネル（ハブ）側にある。管理者には、自律行動の
    配信先パネルを開く 🎙️ 配信先 も出す。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        ng_count: int,
        blacklist_status: str,
        *,
        make_hub: HubFactory | None = None,
        is_admin: bool = False,
        ng_count_provider: Callable[[], int] | None = None,
        blacklist_provider: Callable[[], list] | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ キャラ設定パネルは時間切れになりました。設定は変更されていません。"
                "再度 /functions を実行してください。"
            )
        )
        self.guild_settings = guild_settings
        self.ng_count = ng_count
        self.ng_count_provider = ng_count_provider
        self.blacklist_status = blacklist_status
        self.blacklist_provider = blacklist_provider
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub
        # 開いた時点で管理者か。管理者以外には 🎙️ 配信先 のボタン自体を出さない。
        self.is_admin = is_admin

        # パネルを開いた時点のペルソナ一覧。選択肢と表示をこの一覧にそろえる
        # （開いている間に別のパネルが一覧を読み直しても、選択肢が入れ替わらないように）。
        self.catalog = current_persona_catalog()

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_persona: str = guild_settings.current_prompt_file
        self.temp_model: str = guild_settings.current_model_name
        self.temp_respond_mode: str = guild_settings.current_respond_mode

        # UIパーツを View に追加
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。セレクト更新時にも使用する。

        セレクト類（row0〜2）、確定/戻る（row3）、会話リセットと管理者用の配信先（row4）の
        順に並べる。ペルソナが1つも無いときはペルソナのセレクトを出さない（選択肢0件の
        セレクトは Discord に拒否され、パネルごと開けなくなるため）。
        """
        if self.catalog.personas:
            self.add_item(PersonaSelect(self.temp_persona, self.catalog))
        self.add_item(ModelSelect(self.temp_model))
        self.add_item(RespondModeSelect(self.temp_respond_mode))
        self.add_item(ConfirmButton())
        self.add_item(BackButton(row=3))
        self.add_item(ResetButton())
        if self.is_admin:
            self.add_item(OpenAutonomousChannelButton())

    def build_preview(self) -> str:
        """現在の一時選択値からプレビュー用テキストを組み立てる。"""
        text = _build_status_text(
            self.temp_persona,
            self.temp_model,
            self.temp_respond_mode,
            self.ng_count,
            self.blacklist_status,
            catalog=self.catalog,
        )
        if self.is_admin:
            text += "\n\n🎙️ **配信先**（管理者のみ）… 自律行動でボットが出ていくボイス／ステージチャンネルを指定します。"
        return text
