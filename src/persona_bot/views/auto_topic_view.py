"""自発会話パネル（/functions → 💬 自発会話）。

「ボットが自分から話しかける」系の設定をこの1枚にまとめている。

  ・**自発的な会話**（`auto_topic_service`）… 無会話が続いたテキストチャンネルへ
    話題を1つ投稿する。
  ・**自律行動**（`autonomous_service`）… 「話したいゲージ」が満タンになったら、
    自分からボイス/ステージチャンネルへ入って独り言を始め、そのチャンネルの
    チャットに届いたコメントへ配信者のように応答する。

**この2つは別々のトグルだが、同じパネルで切り替える。** 別パネルに分けると
「ボットが勝手に喋る設定」の入口が2つになって競合し、どちらを切ったのか
分からなくなるため（/functions のハブは row 0〜4 を使い切っていて、
新しいボタンを足す余地が無いという事情もある）。

無会話しきい値は両方で共用する — 自発的な会話では「何時間黙っていたら話題を
投げるか」、自律行動では「何時間で話したいゲージが 100% になるか」。
"""

import discord
from discord import ui

from persona_bot.core.app_state import GuildSettings
from persona_bot.services.autonomous_service import compute_talk_desire, describe_quiet_hours
from persona_bot.config.choices import AUTO_TOPIC_INTERVAL_CHOICES, AUTO_TOPIC_INTERVAL_DISPLAY_NAMES
from persona_bot.config.constants import AUTONOMOUS_IDLE_STOP_MINUTES
from persona_bot.views.view_parts import (
    authorize_administrator_operation, edit_panel_message, BackButton,
    HubFactory, PanelView,
)

# 自律行動ではボットがボイス/ステージへ自分から入るため、サーバーでは管理者だけが確定できる（#398）。
# DM の設定は全 DM で共有の1つで、従来どおり本人が操作できる（キャラ設定と同じ扱い）。
AUTO_TOPIC_ADMIN_NOTICE = "サーバーの自発会話・自律行動の設定を確定できるのは管理者のみです。設定は変更されていません。"

# 「話したいゲージ」を文字で描くときの目盛りの数。
_GAUGE_BAR_WIDTH = 10


def _gauge_bar(percent: float) -> str:
    """0〜100 の値を ▓░ のバーにする（画像を作らずに一目で分かるようにするため）。"""
    filled = max(0, min(_GAUGE_BAR_WIDTH, round(percent / 100 * _GAUGE_BAR_WIDTH)))
    return "▓" * filled + "░" * (_GAUGE_BAR_WIDTH - filled)


def _build_status_text(
    guild_settings: GuildSettings,
    auto_topic_enabled: bool,
    autonomous_enabled: bool,
    interval_hours: int,
    *,
    confirmed: bool = False,
) -> str:
    """自発会話パネルのステータス表示テキストを組み立てる。

    Args:
        guild_settings: 表示のうち「現在の実行時状態」（監視中チャンネル数・
            話したいゲージ・配信中かどうか）を読むために使う。設定値そのものは
            確定前の一時選択値を見せたいので、下の引数から受け取る。
        auto_topic_enabled / autonomous_enabled / interval_hours: 表示する設定値
            （確定前は View の一時選択値、確定後は反映済みの値）。
    """
    auto_topic_display = "✅ 有効" if auto_topic_enabled else "❌ 無効"
    autonomous_display = "✅ 有効" if autonomous_enabled else "❌ 無効"
    interval_display = AUTO_TOPIC_INTERVAL_DISPLAY_NAMES.get(interval_hours, f"{interval_hours}時間")
    desire = compute_talk_desire(guild_settings)

    if confirmed:
        header = "✅ 自発会話・自律行動の設定を更新しました。"
    else:
        header = "💬 **自発会話パネル** — ボットが自分から話し出す機能の設定です。"

    lines = [
        header,
        f"・**自発的な会話（テキスト）**: {auto_topic_display}",
        "　└ 会話が途切れたチャンネルへ、ボットから話題を投稿します。",
        f"・**自律行動（ボイス/ステージ）**: {autonomous_display}",
        "　└ ボットが自分からボイス/ステージへ入って独り言を始め、"
        "そのチャンネルのチャットに書き込むと配信者のように応答します。",
        f"・**無会話しきい値**: {interval_display}"
        "（自律行動では、この時間で「話したいゲージ」が 100% になります）",
        f"・**監視中チャンネル**: {len(guild_settings.auto_topic_channels)}件"
        "（ボットが会話したチャンネルが自動で対象になります）",
        f"・**話したいゲージ**: {_gauge_bar(desire)} {desire:.0f}%",
    ]

    session = guild_settings.autonomous_session
    if session is not None:
        kind = "ステージ" if session.is_stage else "ボイスチャンネル"
        # チャンネル名の解決に client を持ち込まずに済むよう、Discord 側で
        # 名前に展開されるチャンネルメンションで表示する。
        lines.append(f"・**配信中**: 🎙️ <#{session.channel_id}>（{kind}）")

    lines.append("※ 一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しません。")
    if not confirmed:
        lines.append("※ サーバーの設定の確定は管理者のみ行えます。DMでは本人が変更できます。")

    # 抑制する時間帯は「実際に使われているタイムゾーン名つき」で出す。
    # tzdata の無い環境では resolve_timezone が UTC へ落ちるため、定数の
    # "Asia/Tokyo" を書いてしまうと表示と実挙動が9時間ずれる（describe_quiet_hours 参照）。
    quiet_hours = describe_quiet_hours()
    autonomous_notice = f"※ 自律行動は {quiet_hours} は動きません。" if quiet_hours else "※ 自律行動は、"
    lines.append(
        f"{autonomous_notice}"
        f"コメントが {AUTONOMOUS_IDLE_STOP_MINUTES:.0f}分 途絶えると自動で退出します"
        "。権限・発言者状態を失った場合も退出します（`/leave` でいつでも停止）。"
    )
    return "\n".join(lines)


class AutoTopicIntervalSelect(ui.Select):
    """無会話しきい値のセレクトメニュー。

    自発的な会話では「何時間会話が無ければ話題提供するか」、
    自律行動では「何時間で話したいゲージが 100% になるか」を兼ねる。
    """

    def __init__(self, current_value: int) -> None:
        options = [
            discord.SelectOption(
                label=choice.name,
                # SelectOption の value は文字列のみ。int は str に変換して保持する。
                value=str(choice.value),
                default=(choice.value == current_value),
            )
            for choice in AUTO_TOPIC_INTERVAL_CHOICES
        ]
        super().__init__(placeholder="🕒 無会話しきい値を選択…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_interval_hours", int(self.values[0])))


class AutoTopicToggleButton(ui.Button):
    """自発的な会話（テキストチャンネルへの話題提供）の ON/OFF トグルボタン。"""

    def __init__(self, enabled: bool) -> None:
        label = "💬 自発的な会話: ON" if enabled else "💬 自発的な会話: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_auto_topic_enabled", not view.temp_auto_topic_enabled))


class AutonomousToggleButton(ui.Button):
    """自律行動（ボイス/ステージでの配信）の ON/OFF トグルボタン。"""

    def __init__(self, enabled: bool) -> None:
        label = "🎙️ 自律行動: ON" if enabled else "🎙️ 自律行動: OFF"
        style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        await view.update_preview(interaction, lambda: setattr(view, "temp_autonomous_enabled", not view.temp_autonomous_enabled))


class AutoTopicConfirmButton(ui.Button):
    """確定ボタン。ここで初めて GuildSettings に反映する。"""

    def __init__(self) -> None:
        super().__init__(label="確定", style=discord.ButtonStyle.success, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "AutoTopicView" = self.view  # type: ignore[assignment]
        # 押した時点の写しで即時に断り、保留の後に最新の Member で管理者かを確かめてから書き込む。
        if not await authorize_administrator_operation(
            view, interaction, allow_dm=True, notice=AUTO_TOPIC_ADMIN_NOTICE,
        ):
            return

        # 一時選択値をこのサーバー（ギルド）専用の設定に反映し、DB へ保存する。
        # 自律行動を OFF にした場合、進行中の配信は見回り（autonomous_loop）が
        # 次の巡回で気づいて後片付けする（ここでボイス切断まで行うと、
        # UI 層が接続の面倒まで見ることになりサービスとの責務が混ざるため）。
        view.guild_settings.auto_topic_enabled = view.temp_auto_topic_enabled
        view.guild_settings.autonomous_enabled = view.temp_autonomous_enabled
        view.guild_settings.auto_topic_interval_hours = view.temp_interval_hours

        # 配信の開始に失敗し続けていると、次に試すまでの待ち時間（最長4時間）が
        # 設定されている。権限を直してからこのパネルで確定したのに何時間も動かない
        # のでは直したか確かめようがないため、確定操作でその待ち時間を解除する。
        view.guild_settings.autonomous_start_retry_at = None
        view.guild_settings.autonomous_start_failures = 0

        await view.guild_settings.persist()
        if not await view.ensure_settings_current(interaction):
            return

        confirmed_text = _build_status_text(
            view.guild_settings,
            view.temp_auto_topic_enabled,
            view.temp_autonomous_enabled,
            view.temp_interval_hours,
            confirmed=True,
        )
        await edit_panel_message(interaction, deferred=True, content=confirmed_text, view=None)


class AutoTopicView(PanelView):
    """自発会話・自律行動の設定パネル全体を表す View。

    しきい値セレクトと2つの ON/OFF トグルを保持し、確定を押すまで
    GuildSettings を変更しない（SettingsView と同じ一時選択値方式）。
    パネル自体はエフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        guild_settings: GuildSettings,
        *,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 自発会話パネルは時間切れになりました。設定は変更されていません。"
                "再度 /functions を実行してください。"
            )
        )
        self.guild_settings = guild_settings
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        # 一時選択値（現在の設定を初期値として保存）
        self.temp_auto_topic_enabled: bool = guild_settings.auto_topic_enabled
        self.temp_autonomous_enabled: bool = guild_settings.autonomous_enabled
        self.temp_interval_hours: int = guild_settings.auto_topic_interval_hours

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UIパーツを View に追加する。選択・トグル更新時にも使用する。"""
        self.add_item(AutoTopicIntervalSelect(self.temp_interval_hours))
        self.add_item(AutoTopicToggleButton(self.temp_auto_topic_enabled))
        self.add_item(AutonomousToggleButton(self.temp_autonomous_enabled))
        self.add_item(AutoTopicConfirmButton())
        self.add_item(BackButton(row=2))

    def build_preview(self) -> str:
        """現在の一時選択値からパネル表示テキストを組み立てる。"""
        return _build_status_text(
            self.guild_settings,
            self.temp_auto_topic_enabled,
            self.temp_autonomous_enabled,
            self.temp_interval_hours,
        )
