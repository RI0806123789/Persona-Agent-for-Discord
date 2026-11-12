import logging
import asyncio
from dataclasses import dataclass
import io
from inspect import isawaitable
from typing import Any, Callable, Optional

import discord
from discord import ui

from persona_bot.config.constants import DISCORD_SELECT_LABEL_MAX_LENGTH, VIEW_TIMEOUT_SECONDS
from persona_bot.core.background import TaskOwner, _current_owner
from persona_bot.core.app_state import GuildSettings
from persona_bot.utils.discord_helpers import is_administrator, is_current_administrator, reject_blocked_interaction, split_message


logger = logging.getLogger(__name__)


async def authorize_panel_operation(view, interaction, *, allowed, recheck, notice: str) -> bool:
    """押下時と待機後の権限を確かめ、拒否時は適切な応答を返す。"""
    if not allowed:
        await interaction.response.send_message(notice, ephemeral=True)
        return False
    operation = begin_panel_operation(view, interaction)
    await interaction.response.defer(ephemeral=True)
    if operation is not None and not operation.is_current():
        # すでに閉じた・別の画面へ移ったパネルの操作は、何も実行しない。
        return False
    if not await confirm_panel_permission(interaction, await recheck(interaction), notice=notice):
        return False
    # 権限の再確認を待つ間に「戻る」などで画面が移っていたら、副作用を起こさせない。
    return panel_operation_is_current(view, interaction)


def _is_personal_settings_operation(view, interaction, allow_dm: bool) -> bool:
    return (
        allow_dm and getattr(interaction, "guild", None) is None
        and getattr(getattr(view, "guild_settings", None), "guild_id", None) is None
    )


async def confirm_administrator_operation(view, interaction, *, allow_dm: bool = False, notice: str) -> bool:
    """長い待機の後に管理者を再取得し、失効した操作・設定を保存させない。"""
    allowed = _is_personal_settings_operation(view, interaction, allow_dm) or (
        getattr(interaction, "guild", None) is not None and await is_current_administrator(interaction)
    )
    if not await confirm_panel_permission(interaction, allowed, notice=notice):
        return False
    return panel_operation_is_current(view, interaction) and await view.ensure_settings_current(interaction)


async def authorize_administrator_operation(
    view, interaction, *, allow_dm: bool = False, notice: str, refresh: bool = True,
) -> bool:
    """写しの権限で即時拒否し、保留後の再確認を共通化する。DM許可は明示する。"""
    allowed = _is_personal_settings_operation(view, interaction, allow_dm) or (
        getattr(interaction, "guild", None) is not None and is_administrator(getattr(interaction, "user", None))
    )

    async def recheck(_interaction):
        return not refresh or _is_personal_settings_operation(view, interaction, allow_dm) or await is_current_administrator(interaction)

    if not await authorize_panel_operation(view, interaction, allowed=allowed, recheck=recheck, notice=notice):
        return False
    return panel_operation_is_current(view, interaction) and await view.ensure_settings_current(interaction)


async def confirm_panel_permission(interaction, allowed: bool, *, notice: str, followup: bool = False) -> bool:
    """待機後の判定結果を返し、画面の更新先に応じて拒否を案内する。"""
    if allowed:
        return True
    if followup:
        await interaction.followup.send(notice, ephemeral=True)
    else:
        await edit_panel_message(interaction, deferred=True, content=notice, view=None)
    return False

# ハブ（機能パネル）を再生成する呼び出し可能オブジェクトの型。
# functions_view を直接 import すると循環参照になるため、呼び出し側から
# FunctionsView.clone のような生成関数を受け取る形にする。
HubFactory = Callable[[discord.Interaction], Any]


class _PanelState:
    def __init__(self, view: "PanelView") -> None:
        self.current: PanelView | None = view
        self.generation = 0
        self.edit_lock = asyncio.Lock()


@dataclass(frozen=True)
class _PanelOperation:
    source: "PanelView"
    state: _PanelState
    generation: int

    def is_current(self) -> bool:
        # 元Viewが遷移で停止しても、受付済みの最新操作は次の遷移を完了できる。
        # 停止済みViewそのものの再表示は、編集時に別途拒否する。
        return self.generation == self.state.generation


def begin_panel_operation(view: ui.View | None, interaction: discord.Interaction) -> _PanelOperation | None:
    """DB待機より前に操作の世代を確保する。同じInteractionでは二重に進めない。"""
    if not isinstance(view, PanelView):
        return None
    extras = getattr(interaction, "extras", None)
    if not isinstance(extras, dict):
        extras = interaction.extras = {}
    previous = extras.get("persona_panel_operation")
    if isinstance(previous, _PanelOperation) and previous.source is view:
        return previous
    state = view._panel_state
    if state.current is not view or view.is_finished():
        operation = _PanelOperation(view, state, -1)
    else:
        state.generation += 1
        operation = _PanelOperation(view, state, state.generation)
    extras["persona_panel_operation"] = operation
    return operation


def select_option_text(text: Any, *, fallback: str = "") -> str:
    """選択肢の表示文を1行・上限以内にし、空なら代わりの表示名を使う。"""
    single_line = " ".join(("" if text is None else str(text)).split())
    return single_line[:DISCORD_SELECT_LABEL_MAX_LENGTH].rstrip() or fallback


def panel_operation_is_current(view: ui.View | None, interaction: discord.Interaction) -> bool:
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation):
        operation = begin_panel_operation(view, interaction)
    return operation is None or operation.is_current()


async def _edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None, deferred: bool,
) -> None:
    if deferred or getattr(interaction.response, "is_done", lambda: not hasattr(interaction.response, "edit_message"))():
        await interaction.edit_original_response(content=content, view=view)
    else:
        await interaction.response.edit_message(content=content, view=view)


async def edit_panel_message(
    interaction: discord.Interaction, *, content: str, view: ui.View | None = None,
    source: ui.View | None = None, deferred: bool = False,
) -> bool:
    """同じパネルの編集を直列化し、後から始まった操作がある旧更新を破棄する。"""
    extras = getattr(interaction, "extras", {})
    operation = extras.get("persona_panel_operation") if isinstance(extras, dict) else None
    if not isinstance(operation, _PanelOperation) or (source is not None and operation.source is not source):
        operation = begin_panel_operation(source if source is not None else view, interaction)
    if operation is None:
        await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
        return True
    state = operation.state
    try:
        # 先行するHTTP編集が遅くても、ロック待ちでInteractionを失効させない。
        if state.edit_lock.locked() and not getattr(interaction.response, "is_done", lambda: False)():
            await interaction.response.defer(ephemeral=True)
        async with state.edit_lock:
            if not operation.is_current() or (view is not None and view.is_finished()):
                if view is not None and view is not state.current:
                    view.stop()
                if not getattr(interaction.response, "is_done", lambda: False)():
                    await interaction.response.defer(ephemeral=True)
                return False
            previous = state.current
            # 閉じる編集の通信中は新しい操作を受け付けない。後発のセレクトが
            # 実行中の世代を無効にし、両方の結果が破棄されることを防ぐ。
            if view is None:
                state.current = None
            try:
                await _edit_panel_message(interaction, content=content, view=view, deferred=deferred)
            except BaseException:
                if view is None and operation.is_current() and previous is not None and not previous.is_finished():
                    state.current = previous
                raise
            if previous is not view:
                state.current = view
                if isinstance(view, PanelView):
                    view._panel_state = state
                if previous is not None:
                    previous.stop()
            return True
    except BaseException:
        # ロック待ちの取り消しでも、不採用の遷移先を残さない。
        if view is not None and view is not state.current:
            view.stop()
        raise


async def bind_panel_message(
    interaction: discord.Interaction, panel: ui.View, *, previous_message: Any = None,
) -> None:
    """編集後の参照取得に失敗しても、直前のWebhook参照で時間切れ処理を保つ。"""
    panel.message = previous_message
    try:
        panel.message = await interaction.original_response()
    except Exception as error:
        logger.warning("パネルの応答参照の取得に失敗しました。", exc_info=error)


PANEL_BUSY_NOTICE = "処理中です。結果が届くまでお待ちください。"


class PanelView(ui.View):
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
        super().__init__(timeout=timeout)
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
        # UI コールバックを終了処理の回収対象にするための所有者。
        # 未指定時は作成時のコンテキストから自動取得する（コマンド実行中なら設定される）。
        self._task_owner: TaskOwner | None = task_owner if task_owner is not None else _current_owner.get()

    def _dispatch_item(self, item: discord.ui.Item[Any], interaction: discord.Interaction) -> Optional[asyncio.Task[None]]:
        """終了後の新規操作を拒否し、処理の開始は discord.py に任せる。"""
        if self._task_owner is not None and self._task_owner.stopping:
            return None
        return super()._dispatch_item(item, interaction)

    async def _scheduled_task(self, item: discord.ui.Item[Any], interaction: discord.Interaction) -> None:
        """ライブラリの処理全体を所有者の終了待機に含める。"""
        task = super()._scheduled_task(item, interaction)
        if self._task_owner is None:
            await task
        else:
            await self._task_owner.run(task)

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

    def stop(self) -> None:
        if self._panel_state.current is self:
            self._panel_state.current = None
            self._panel_state.generation += 1
        super().stop()

    async def update_preview(self, interaction: discord.Interaction, update: Callable[[], None]) -> None:
        """最新の操作だけが一時状態とプレビューを更新する。"""
        operation = begin_panel_operation(self, interaction)
        if operation is not None and not operation.is_current():
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
            return
        update()
        self.refresh_items()
        await edit_panel_message(interaction, content=self.build_preview(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """パネルを開いた後にブロックされた利用者も、操作のたびに拒否する。"""
        if self.is_finished() or self._panel_state.current is not self:
            return False
        if self._task_owner is not None and self._task_owner.stopping:
            return False
        if self.blocked_user_check is not None and await reject_blocked_interaction(interaction, self.blocked_user_check):
            return False
        if not await self.ensure_settings_current(interaction):
            return False
        # グラフ等の別メッセージへの操作では、保留中のパネル更新を失効させない。
        # 画面を更新するコールバックだけが、待機前に操作世代を確保する。
        return not self.is_finished() and self._panel_state.current is self

    async def ensure_settings_current(self, interaction: discord.Interaction) -> bool:
        """退出前の設定を使う操作は、受付時と非同期待機後に拒否する。"""
        settings = getattr(self, "guild_settings", None)
        if not isinstance(settings, GuildSettings) or settings.is_active:
            return True
        await edit_panel_message(
            interaction,
            content="このパネルはサーバー退出により無効になりました。再度 /functions を実行して開き直してください。",
            view=None,
            source=self,
        )
        return False

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item[Any], /
    ) -> None:
        """パネルや操作内容の repr を出さず、例外は共通ログの診断へ渡す。"""
        logger.error("パネル操作中にエラーが発生しました。", exc_info=error)

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

        begin_panel_operation(view, interaction)
        if isinstance(view, PanelView) and not await view.ensure_settings_current(interaction):
            return
        hub = make_parent(interaction)
        if isawaitable(hub):
            hub = await hub
        if isinstance(view, PanelView) and not await view.ensure_settings_current(interaction):
            # 生成済みの戻り先は表示されないまま捨てる。止めないとタイムアウトの
            # タイマーが残る（swap_panel と同じ扱い）。
            hub.stop()
            return
        hub.blocked_user_check = getattr(view, "blocked_user_check", None)
        if not await edit_panel_message(interaction, content=hub.build_preview(), view=hub, source=view):
            hub.stop()
            return
        if view is not None:
            view.stop()
        # エフェメラルはチャンネル経由では編集できないためWebhookの参照を保持する。
        await bind_panel_message(interaction, hub, previous_message=getattr(view, "message", None))


async def swap_panel(
    interaction: discord.Interaction,
    hub: ui.View,
    panel: PanelView,
    content: str,
) -> None:
    """ハブが開いている（エフェメラルな）メッセージを、サブパネルへ差し替える。

    Discord の View は最大5行までのため機能を1枚に合体できない。そこで
    「同じメッセージの中身だけを入れ替える」方式で各パネルを行き来する。

    引数:
        interaction: ハブのボタンが受け取った Interaction。
        hub:         差し替え元のハブ View（差し替え後に停止する）。
        panel:       差し替え先のサブパネル。
        content:     差し替え後のメッセージ本文（通常は panel.build_preview()）。
    """
    if isinstance(hub, PanelView) and not await hub.ensure_settings_current(interaction):
        panel.stop()
        return
    panel.blocked_user_check = getattr(hub, "blocked_user_check", None)
    if not hasattr(panel, "guild_settings") and hasattr(hub, "guild_settings"):
        panel.guild_settings = hub.guild_settings
    if not await edit_panel_message(interaction, content=content, view=panel, source=hub):
        return
    # ハブは役目を終えたので停止する。止めないとハブのタイムアウト処理が
    # 後からメッセージを誤って上書きする（同じメッセージを共有しているため、
    # 表示中のサブパネルが「機能パネルは時間切れです」に化けてしまう）。
    hub.stop()
    await bind_panel_message(interaction, panel, previous_message=getattr(hub, "message", None))


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
