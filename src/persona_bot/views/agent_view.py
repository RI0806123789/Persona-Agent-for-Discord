"""サブエージェントの進捗メッセージと中止ボタン（#170）。

子が作業している間の進捗は、次の2つの形のどちらかで伝える（文面はどちらもペルソナの定型文で、
`agent_service.render_agent_line` が組み立てる。案A。LLM を呼ばない）。

- 作業スレッド（`agent_thread.py`。#267）: 依頼メッセージにスレッドを作り、親が委託内容を伝え、
  子が Webhook で進捗と報告を残す。使える場面ではこちらを優先する。
- 返信1通の書き換え（このモジュールの `AgentProgress`）: スレッドを使えない場面の切り替え先。
  依頼への返信として進捗メッセージを1通出し、手順ごとに書き換える。

共通の部分（中止ボタン・全体の期限・子のタスクの回収）は `AgentProgressBase` が持つ。

- 書き換えは `AGENT_PROGRESS_MIN_INTERVAL_SECONDS` より短い間隔では行わない（Discord のレート制限）。
  終了時の書き換えだけは間隔に関わらず必ず行う。
- 進捗の送信・書き換えの失敗はログに残すだけで、子の作業は止めない（フェイルオープン）。
- 中止ボタンは依頼した本人だけが押せる。全体の期限（`AGENT_TOTAL_TIMEOUT_SECONDS`）もここで管理する。

機能パネル（`PanelView`）ではなく、チャンネルに出す1通のメッセージに付ける部品なので
`ui.View` を直接継承する（パネルの差し替え・操作世代の仕組みは使わない）。
"""

from __future__ import annotations

import logging
import asyncio
from time import monotonic
from typing import Any, Coroutine, Mapping

import discord
from discord import ui

from persona_bot.config.constants import AGENT_PROGRESS_MIN_INTERVAL_SECONDS, AGENT_TOTAL_TIMEOUT_SECONDS
from persona_bot.core.background import TaskOwner, _current_owner, spawn, wait_for_cleanup
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport, render_agent_line


logger = logging.getLogger(__name__)


class AgentCancelView(ui.View):
    """進捗メッセージに付ける中止ボタン。押せるのは依頼した本人だけ。"""

    def __init__(
        self, requester_id: int, cancel_event: asyncio.Event, *, task_owner: TaskOwner | None = None,
    ) -> None:
        # 作業の期限より少し長く待つ（期限で終わったときは呼び出し側がボタンを外す）。
        super().__init__(timeout=AGENT_TOTAL_TIMEOUT_SECONDS + 60)
        self.requester_id = requester_id
        self.cancel_event = cancel_event
        # Discordが直接起動するコールバックにも、作成時の所有者を引き継ぐ。
        self._task_owner = task_owner if task_owner is not None else _current_owner.get()

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: ui.Item[Any], /
    ) -> None:
        """中止ボタンもSDK既定の repr 付きログへ渡さない。"""
        logger.error("サブエージェント: 中止ボタンの操作中にエラーが発生しました。", exc_info=error)

    @ui.button(label="中止", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: ui.Button) -> None:
        if self._task_owner is None:
            await self._cancel(interaction)
        else:
            await self._task_owner.run(self._cancel(interaction))

    async def _cancel(self, interaction: discord.Interaction) -> None:
        try:
            if interaction.user.id != self.requester_id:
                await interaction.response.send_message("依頼した人だけが中止できます。", ephemeral=True)
                return
            # 書き換えは作業側が行う。応答失敗時も、受け付けた中止要求は維持する。
            self.cancel_event.set()
            await interaction.response.defer()
        except discord.HTTPException as error:
            logger.warning("サブエージェント: 中止操作への応答を送れませんでした。", exc_info=error)


class AgentProgressBase:
    """依頼1件分の進捗表示の共通部分。update（手順ごと）→ finish の順に使う。

    表示の形ごとの部分（送り先と文面の並べ方）は派生クラスが決める。
    """

    # 子の報告そのものを表示するか（作業スレッドだけが表示する）。
    shows_report = False
    # 依頼のために作った作業スレッド（作業スレッドの表示だけが持つ）。完了後に閉じる対象（#408）。
    work_thread: discord.Thread | None = None

    def __init__(self, requester_id: int, lines: Mapping[str, str], plan: AgentPlan) -> None:
        self._lines = lines
        self._plan = plan
        self._cancel_event = asyncio.Event()
        self._view = AgentCancelView(requester_id, self._cancel_event)

    def _line(self, key: str, *, index: int = 0, step: str = "") -> str:
        return render_agent_line(
            self._lines, key, role=self._plan.role, step=step, current=index + 1, total=len(self._plan.steps),
        )

    async def update(self, index: int, step: str) -> None:
        """手順の開始を表示する（手順の番号は 0 始まり）。"""
        raise NotImplementedError

    async def finish(self, key: str, report: AgentReport | None = None) -> None:
        """終了の定型文（done / failed / canceled）を伝え、中止ボタンを外す。

        Args:
            report: 子の報告（done のときだけ）。表示する形（shows_report）では、
                呼び出し側が NG ワードを伏せてから渡す。
        """
        raise NotImplementedError

    async def close(self) -> None:
        """途中の中断でもViewを止め、既知のメッセージから中止ボタンを取り除く。"""
        self._view.stop()

        async def remove_button() -> None:
            try:
                await self._remove_cancel_button()
            except Exception as error:
                # 編集の失敗で本来のキャンセルや処理例外を置き換えない。
                logger.warning("サブエージェント: 中止ボタンを回収できませんでした。", exc_info=error)

        await wait_for_cleanup(remove_button(), label="サブエージェントの中止ボタンの回収")

    async def _remove_cancel_button(self) -> None:
        raise NotImplementedError

    async def run(self, work: Coroutine[Any, Any, AgentReport | None]) -> tuple[str, AgentReport | None]:
        """子の作業を、全体の期限と中止ボタンを見ながら待つ。

        Returns:
            ("done", 報告) / ("failed", None) / ("canceled", None)。
            呼び出し側のキャンセル（ボットの終了など）はそのまま伝える。
        """
        worker = spawn(work, label="サブエージェントの作業")
        canceled = spawn(self._cancel_event.wait(), label="サブエージェントの中止待ち")
        try:
            await asyncio.wait(
                {worker, canceled}, timeout=AGENT_TOTAL_TIMEOUT_SECONDS, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            self._view.stop()
            # 中止後の後片付けが報告を返しても、正常に完了した作業と取り違えない。
            worker_finished = worker.done()
            cancel_requested = self._cancel_event.is_set()
            for task in (worker, canceled):
                if not task.done():
                    task.cancel()

            async def collect_children() -> None:
                # 子の例外も回収し、片方の失敗で他方の終了待ちを打ち切らない。
                await asyncio.gather(worker, canceled, return_exceptions=True)

            # 実行枠の解放前に両方を回収する。追加のキャンセルも回収後に伝える。
            await wait_for_cleanup(collect_children(), label="サブエージェントの終了待ち")
        if cancel_requested:
            # ★ 中止の要求を最優先にする。中止ボタンと作業の完了が同じ周回で重なると、
            # 先に完了を採用して中止を黙って無視してしまう（本人は中止したつもりでいる）。
            logger.info("サブエージェント: 依頼した本人が中止しました。")
            return "canceled", None
        if worker_finished and not worker.cancelled():
            error = worker.exception()
            if error is not None:
                logger.error("サブエージェント: 作業中にエラーが発生しました。", exc_info=error)
                return "failed", None
            report = worker.result()
            return ("done", report) if report is not None else ("failed", None)
        logger.warning("サブエージェント: 期限に達したため中止しました。")
        return "failed", None


class AgentProgress(AgentProgressBase):
    """依頼への返信1通を書き換える進捗表示。start → update（手順ごと）→ finish の順に使う。

    作業スレッドを使えない場面（DM・スレッドの中・権限不足など）の切り替え先。
    """

    def __init__(self, message: discord.Message, lines: Mapping[str, str], plan: AgentPlan) -> None:
        super().__init__(message.author.id, lines, plan)
        self._source = message
        self._message: discord.Message | None = None
        self._last_edit = 0.0
        self._header = render_agent_line(lines, "start", role=plan.role, total=len(plan.steps))

    async def start(self) -> None:
        """依頼への返信として進捗メッセージを出す（失敗しても作業は続ける）。"""
        first_step = self._plan.steps[0]
        content = f"{self._header}\n{self._line('progress', step=first_step)}"
        try:
            self._message = await self._source.reply(
                content, view=self._view, allowed_mentions=discord.AllowedMentions.none(),
            )
            self._last_edit = monotonic()
        except discord.DiscordException as error:
            logger.warning("サブエージェント: 進捗メッセージを送れませんでした。", exc_info=error)
            self._view.stop()

    async def update(self, index: int, step: str) -> None:
        """手順の開始を表示する。最初の手順は start で表示済み。間隔が短ければ見送る。"""
        if index == 0 or self._message is None:
            return
        if monotonic() - self._last_edit < AGENT_PROGRESS_MIN_INTERVAL_SECONDS:
            return
        await self._edit(f"{self._header}\n{self._line('progress', index=index, step=step)}", view=self._view)

    async def finish(self, key: str, report: AgentReport | None = None) -> None:
        """終了の定型文（done / failed / canceled）に書き換え、中止ボタンを外す（報告は表示しない）。"""
        self._view.stop()
        if self._message is None:
            # 進捗メッセージを出せなかった場合も、失敗と中止だけは伝える（完了は人格役の返信で分かる）。
            if key != "done":
                try:
                    await self._source.reply(self._line(key), allowed_mentions=discord.AllowedMentions.none())
                except discord.DiscordException as error:
                    logger.warning("サブエージェント: 終了の案内を送れませんでした。", exc_info=error)
            return
        await self._edit(f"{self._header}\n{self._line(key)}", view=None)

    async def _edit(self, content: str, *, view: ui.View | None) -> None:
        try:
            await self._message.edit(content=content, view=view)
            self._last_edit = monotonic()
        except discord.DiscordException as error:
            logger.warning("サブエージェント: 進捗メッセージを更新できませんでした。", exc_info=error)

    async def _remove_cancel_button(self) -> None:
        if self._message is not None:
            # 中断時は進捗本文を変えず、操作できないボタンだけを外す。
            await self._message.edit(view=None)
