import logging
import asyncio
import io
from inspect import isawaitable
from typing import Any, Callable, Coroutine, Optional

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, VIEW_TIMEOUT_SECONDS
from persona_bot.core.background import TaskOwner, _current_owner
from persona_bot.core.app_state import GuildSettings
from persona_bot.utils.discord_helpers import reject_blocked_interaction, split_message
# 操作世代と画面更新の直列化は panel_state.py、権限確認は panel_authorization.py（#470）。
# 従来の import 先（テストや他のビューが使う名前）を保つため、ここから再公開する。
from persona_bot.views.panel_authorization import (  # noqa: F401（再公開）
    authorize_administrator_operation,
    authorize_panel_operation,
    begin_current_panel_operation,
    confirm_administrator_operation,
    confirm_panel_not_blocked,
    confirm_panel_permission,
)
from persona_bot.views.panel_state import (  # noqa: F401（再公開）
    _edit_panel_message,
    _PanelOperation,
    _PanelState,
    begin_panel_operation,
    bind_panel_message,
    edit_panel_message,
    panel_operation_is_current,
)


logger = logging.getLogger(__name__)


async def defer_and_render_graph(
    interaction: discord.Interaction, render: Callable[[], Coroutine[Any, Any, Any]],
    *, failure_log: str = "グラフ生成中にエラーが発生しました。",
    failure_notice: str = "エラー: グラフの生成に失敗しました。",
) -> tuple[bool, Any]:
    """応答を保留してグラフを生成する。`(成功したか, 生成結果)` を返す（#453）。

    生成結果は画像のバッファか、描画できるデータが無いときの None。失敗したときはログに残し、
    本人だけに失敗を伝えて `(False, None)` を返す（呼び出し側は何も送らずに戻ること）。
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        return True, await render()
    except Exception as error:
        logger.error(failure_log, exc_info=error)
        await interaction.followup.send(failure_notice, ephemeral=True)
        return False, None


# ハブ（機能パネル）を再生成する呼び出し可能オブジェクトの型。
# functions_view を直接 import すると循環参照になるため、呼び出し側から
# FunctionsView.clone のような生成関数を受け取る形にする。
HubFactory = Callable[[discord.Interaction], Any]


def select_option_text(text: Any, *, fallback: str = "") -> str:
    """選択肢の表示文を1行・上限以内にし、空なら代わりの表示名を使う。"""
    single_line = " ".join(("" if text is None else str(text)).split())
    return single_line[:DISCORD_SELECT_LABEL_MAX_LENGTH].rstrip() or fallback



PANEL_BUSY_NOTICE = "処理中です。結果が届くまでお待ちください。"
# 管理者専用の操作を、管理者でない人が押したときの案内（統計・キャラ設定で共有）。
ADMIN_ONLY_NOTICE = "この機能はサーバー管理者のみ利用できます。"


class OwnedView(ui.View):
    """終了処理の回収対象になる View の共通の土台（#434）。

    次の3つをここに集約する。機能パネルの `PanelView` のほか、チャンネルへ出す1通のメッセージに付ける
    `AgentCancelView`・`VoiceConsentView` も継承する。

    ・`TaskOwner` の保持（未指定なら作成時のコンテキストから取得する）
    ・終了開始後の新規操作の拒否（`_dispatch_item`）と、受付全体の回収（`_scheduled_task`）
    ・`on_error` で SDK 既定の View/Item の repr をログへ出さない

    ★ ボタンのコールバックを直接呼ぶ経路（テストなど）でも回収できるよう、必要な View は
    `run_owned` でコールバック本体を包む。`TaskOwner.run` は入れ子でも追跡を外さない。
    """

    # on_error のログの出力先と文面。モジュールごとのロガー名を保つため、サブクラスが差し替える。
    _error_logger: logging.Logger = logger
    _error_log_message = "パネル操作中にエラーが発生しました。"

    def __init__(self, *, timeout: float | None, task_owner: TaskOwner | None = None) -> None:
        super().__init__(timeout=timeout)
        # UI コールバックを終了処理の回収対象にするための所有者。
        # 未指定時は作成時のコンテキストから自動取得する（コマンド実行中なら設定される）。
        self._task_owner: TaskOwner | None = task_owner if task_owner is not None else _current_owner.get()

    @property
    def is_owner_stopping(self) -> bool:
        """ボットの終了が始まり、新しい操作を受け付けない状態か。"""
        return self._task_owner is not None and self._task_owner.stopping

    async def run_owned(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """コールバック本体を所有者の終了待機に含めて実行する（所有者が無ければそのまま待つ）。"""
        if self._task_owner is None:
            return await coro
        return await self._task_owner.run(coro)

    def _dispatch_item(self, item: discord.ui.Item[Any], interaction: discord.Interaction) -> Optional[asyncio.Task[None]]:
        """終了後の新規操作を拒否し、処理の開始は discord.py に任せる。"""
        if self.is_owner_stopping:
            return None
        return super()._dispatch_item(item, interaction)

    async def _scheduled_task(self, item: discord.ui.Item[Any], interaction: discord.Interaction) -> None:
        """ライブラリの処理全体を所有者の終了待機に含める。"""
        await self.run_owned(super()._scheduled_task(item, interaction))

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any], /
    ) -> None:
        """View や操作内容の repr を出さず、例外は共通ログの診断へ渡す。"""
        self._error_logger.error(self._error_log_message, exc_info=error)


class PanelView(OwnedView):
    """/functions のハブと各サブパネルに共通する土台となる View。

    各パネルにコピーされていた次の定型をここへ集約する:
      ・タイムアウト時に UI を取り除いて時間切れ表示へ差し替える処理（on_timeout）
      ・パネルメッセージの参照（message）
      ・操作待ち時間のマジックナンバー（constants.VIEW_TIMEOUT_SECONDS）

    サブクラスは UI パーツの追加を _add_all_items に実装し、__init__ の
    最後で一度だけ自分で呼ぶ（基底では呼ばない — サブクラス固有の属性が
    まだ初期化されていない段階で呼ぶと AttributeError になるため）。
    """

    def __init__(
        self,
        *,
        timeout: float = VIEW_TIMEOUT_SECONDS,
        timeout_notice: str,
        task_owner: TaskOwner | None = None,
    ) -> None:
        super().__init__(timeout=timeout, task_owner=task_owner)
        # タイムアウト時に UI を取り除くためのパネルメッセージ参照（送信後に設定される）。
        self.message: discord.InteractionMessage | None = None
        # タイムアウトしたときにユーザーへ表示する文言（パネルごとに変える）。
        self.timeout_notice: str = timeout_notice
        self.blocked_user_check: Callable[[int], bool] | None = None
        self._panel_state = _PanelState(self)
        # 実行中の重い処理（集計・グラフ描画・再読込）。連打で同じ処理を重ねず、同じ結果を何通も
        # 送らないため（#398、#415）。パネルを残したまま結果を別の followup で送るボタンは、
        # パネルの操作世代では止められないので、名前ごとにここで排他する。
        self._running_aggregations: set[str] = set()

    def try_begin_aggregation(self, name: str) -> bool:
        """同じ名前の処理が実行中なら False を返す。判定と登録の間に await を挟まないこと。

        True を返したら、必ず `finally` で `end_aggregation` を呼ぶ。
        """
        if name in self._running_aggregations:
            return False
        self._running_aggregations.add(name)
        return True

    def end_aggregation(self, name: str) -> None:
        self._running_aggregations.discard(name)

    async def run_exclusive(
        self, interaction: discord.Interaction, name: str, work: Callable[[], Coroutine[Any, Any, Any]],
        *, busy_notice: str = PANEL_BUSY_NOTICE,
    ) -> bool:
        """同じ名前の処理が実行中なら本人だけに断り、そうでなければ `work` を実行して必ず解除する（#470）。

        「`try_begin_aggregation` → 実行中の案内 → `try/finally` で `end_aggregation`」の定型を1か所にまとめたもの。
        実行したら True、実行中で断ったら False を返す。判定と登録の間に await を挟まない
        （`try_begin_aggregation` の約束）。`work` の例外はそのまま呼び出し側へ伝わる。
        """
        if not self.try_begin_aggregation(name):
            await interaction.response.send_message(busy_notice, ephemeral=True)
            return False
        try:
            await work()
        finally:
            self.end_aggregation(name)
        return True

    def stop(self) -> None:
        if self._panel_state.current is self:
            self._panel_state.current = None
            self._panel_state.generation += 1
        super().stop()

    async def update_preview(self, interaction: discord.Interaction, update: Callable[[], None]) -> None:
        """最新の操作だけが一時状態とプレビューを更新する。"""
        if not await begin_current_panel_operation(self, interaction):
            return
        update()
        self.refresh_items()
        await edit_panel_message(interaction, content=self.build_preview(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """パネルを開いた後にブロックされた利用者も、操作のたびに拒否する。"""
        if self.is_finished() or self._panel_state.current is not self:
            return False
        if self.is_owner_stopping:
            return False
        if self.blocked_user_check is not None and await reject_blocked_interaction(interaction, self.blocked_user_check):
            return False
        if not await self.ensure_settings_current(interaction):
            return False
        # グラフ等の別メッセージへの操作では、保留中のパネル更新を失効させない。
        # 画面を更新するコールバックだけが、待機前に操作世代を確保する。
        return not self.is_finished() and self._panel_state.current is self

    async def ensure_settings_current(
        self, interaction: discord.Interaction, *, deferred: bool = False,
    ) -> bool:
        """退出前の設定を使う操作は、受付時と非同期待機後に拒否する。"""
        settings = getattr(self, "guild_settings", None)
        if not isinstance(settings, GuildSettings) or settings.is_active:
            return True
        await edit_panel_message(
            interaction,
            deferred=deferred,
            content="このパネルはサーバー退出により無効になりました。再度 /functions を実行して開き直してください。",
            view=None,
            source=self,
        )
        return False

    async def on_timeout(self) -> None:
        """タイムアウト時に、操作できなくなった UI をメッセージから取り除く。"""
        state = self._panel_state
        if state.current is not self:
            return
        if self.message is None:
            # メッセージ参照が無い（送信前・差し替え済みなど）ときは何もしない。
            self.stop()
            return
        state.generation += 1
        generation = state.generation
        async with state.edit_lock:
            if state.current is not self or state.generation != generation:
                return
            try:
                await self.message.edit(content=self.timeout_notice, view=None)
            except discord.HTTPException as error:
                logger.warning("パネルのタイムアウト処理に失敗しました。", exc_info=error)
            finally:
                self.stop()

    def refresh_items(self) -> None:
        """UI パーツを組み直す（セレクト選択やトグル操作のあとに呼ぶ）。

        Select/Button のコールバックから `view._add_all_items()` とプライベート
        メソッドを直接呼んでいた8か所を、公開 API 1本に置き換えるためのもの。
        別クラスから他クラスの非公開メソッドを触る形を無くし、組み直しの手順
        （クリア → 再追加）をこの1か所に閉じ込める。
        """
        self.clear_items()
        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。サブクラスが実装する。

        パーツを組み直さないパネルでも、refresh_items が呼ばれたときに
        黙って何も起きない状態を避けるため、基底では例外にしている。
        """
        raise NotImplementedError(
            f"{type(self).__name__} は _add_all_items() を実装していません。"
        )


class BackButton(ui.Button):
    """親パネルへ戻るボタン。指定が無ければ機能パネル（ハブ）へ戻る。

    make_parent または親 View が保持する make_hub で戻り先を再生成し、
    同じメッセージを差し替える。どちらも無い場合はパネルを閉じる。
    非同期の生成関数は、待機前にDiscordへの応答を保留する。
    """

    def __init__(self, *, row: int, make_parent: HubFactory | None = None) -> None:
        super().__init__(label="戻る", style=discord.ButtonStyle.secondary, emoji="↩️", row=row)
        self.make_parent = make_parent

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        make_parent = self.make_parent if self.make_parent is not None else getattr(view, "make_hub", None)
        if make_parent is None:
            # エフェメラルメッセージは delete() できないため、内容を編集して閉じる。
            edited = await edit_panel_message(interaction, content="パネルを閉じました。", view=None, source=view)
            if edited and view is not None:
                view.stop()
            return

        if not await begin_current_panel_operation(view, interaction):
            return
        if isinstance(view, PanelView) and not await view.ensure_settings_current(interaction):
            return
        hub = make_parent(interaction)
        if isawaitable(hub):
            hub = await hub
        await swap_panel(interaction, view, hub)


async def swap_panel(
    interaction: discord.Interaction,
    hub: ui.View | None,
    panel: PanelView,
    content: str | None = None,
) -> None:
    """ハブが開いている（エフェメラルな）メッセージを、別のパネルへ差し替える。

    Discord の View は最大5行までのため機能を1枚に合体できない。そこで
    「同じメッセージの中身だけを入れ替える」方式で各パネルを行き来する。

    引数:
        interaction: ボタンが受け取った Interaction。
        hub:         差し替え元の View（差し替え後に停止する）。
        panel:       差し替え先のパネル（不採用・中断時はこの関数内で停止する）。
        content:     差し替え後のメッセージ本文。省略時は `panel.build_preview()`。
    """
    adopted = False
    try:
        if isinstance(hub, PanelView) and not await hub.ensure_settings_current(interaction):
            return
        panel.blocked_user_check = getattr(hub, "blocked_user_check", None)
        if not hasattr(panel, "guild_settings") and hasattr(hub, "guild_settings"):
            panel.guild_settings = hub.guild_settings
        resolved_content = panel.build_preview() if content is None else content
        if not await edit_panel_message(interaction, content=resolved_content, view=panel, source=hub):
            return
        adopted = True
    finally:
        # 生成済みの遷移先が不採用・例外・中断で表示されなかった場合は止めてタイマーを残さない。
        if not adopted and hasattr(panel, "stop"):
            panel.stop()
    # 差し替え元は役目を終えたので停止する。止めないと元のタイムアウト処理が
    # 後からメッセージを誤って上書きする（同じメッセージを共有しているため、
    # 表示中のパネルが「時間切れです」に化けてしまう）。
    if hub is not None:
        hub.stop()
    # エフェメラルはチャンネル経由では編集できないためWebhookの参照を保持する。
    await bind_panel_message(interaction, panel, previous_message=getattr(hub, "message", None))


async def edit_panel_or_followup(
    interaction: discord.Interaction,
    *,
    content: str,
    view: ui.View | None = None,
    source: ui.View | None = None,
) -> bool:
    """保留済みのパネルを更新し、時間切れや画面遷移で失効していれば本人限定の followup で伝える（#511）。"""
    if await edit_panel_message(interaction, deferred=True, content=content, view=view, source=source):
        return True
    await interaction.followup.send(content, ephemeral=True)
    return False


async def send_panel_result(interaction: discord.Interaction, text: str, shown: list[bool], *, fallback: str = "") -> bool:
    """先頭をパネルに表示し、残りは本人向けに送る。"""
    chunks = split_message(text) or [fallback]
    if not await edit_panel_message(interaction, deferred=True, content=chunks[0]):
        return False
    shown[0] = True
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)
    return True


async def report_panel_failure(interaction: discord.Interaction, notice: str, *, keep_result: bool, label: str) -> None:
    """表示済みの結果を保持しつつ、失敗を本人に伝える。"""
    if not panel_operation_is_current(None, interaction):
        return
    if not keep_result:
        try:
            await edit_panel_message(interaction, deferred=True, content=notice, view=None)
            return
        except Exception as error:
            logger.warning("%s: 失敗の案内をパネルに表示できませんでした。", label, exc_info=error)
    try:
        await interaction.followup.send(notice, ephemeral=True)
    except Exception as error:
        logger.error("%s: 失敗の案内を送れませんでした。", label, exc_info=error)


async def send_graph_ephemeral(
    interaction: discord.Interaction,
    buffer: io.BytesIO,
    filename: str,
    caption: str,
    *,
    followup: bool = False,
    error_log: str | None = None,
    error_notice: str | None = None,
) -> None:
    """グラフ画像を、本人だけに見えるエフェメラルの新規メッセージとして送る。

    パネル自体は残したまま結果だけを別メッセージで返す用途（📊 グラフ /
    📊 全体比較）に使う。送信に失敗した場合はエラー文だけを返し、例外は
    呼び出し側へ伝播させない（パネル操作を止めないため）。

    **buffer の所有権はこの関数にある。** 呼び出し側は buffer を渡したら
    close してはならない — 成否にかかわらずこの関数が必ず close する。
    （元の GraphButton / RankingButton が、送信直後の finally で自分の
    buffer を閉じていた挙動をそのまま引き継いだもの。discord.File は
    ファイルオブジェクトを渡した場合その所有権を持たず閉じてくれない。）

    引数:
        interaction: ボタンが受け取った Interaction。
        buffer:      PNG 画像を保持する BytesIO。この関数が閉じる。
        filename:    Discord 上でのファイル名（例: "usage_graph.png"）。
        caption:     画像に添えるメッセージ本文。
        followup:    defer済みの場合はTrueにし、followupで送信する。
        error_log:   互換性のため保持する旧ログ文脈名。ログには固定の文面を使う。
        error_notice: 送信失敗時の案内文。省略時は固定の案内を使う。
    """
    picture = None
    try:
        picture = discord.File(buffer, filename=filename)
        sender = interaction.followup.send if followup else interaction.response.send_message
        await sender(caption, file=picture, ephemeral=True)
    except Exception as error:
        logger.error("グラフの送信中にエラーが発生しました。", exc_info=error)
        # 送信が始まる前に失敗した場合のみ、同じ Interaction でエラーを返せる。
        if followup or not interaction.response.is_done():
            sender = interaction.followup.send if followup else interaction.response.send_message
            try:
                await sender(error_notice or "エラー: グラフの送信に失敗しました。", ephemeral=True)
            except Exception as notice_error:
                logger.error("グラフ送信失敗の案内も送れませんでした。", exc_info=notice_error)
    finally:
        # discord.Fileが差し替えたbuffer.closeを元へ戻してから閉じる。
        if picture is not None:
            picture.close()
        buffer.close()
