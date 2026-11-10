"""サブエージェントの実行枠・報告検査・親応答の橋渡し。"""

from __future__ import annotations

import logging
import discord

from persona_bot.config.personas import current_persona_catalog
from persona_bot.core.app_state import ResponseSettings
from persona_bot.services.agent_plan import AgentPlan, requests_subagent
from persona_bot.services.agent_service import render_agent_line
from persona_bot.services.prompting import build_agent_report_section
from persona_bot.views.agent_thread import WorkThreadEntry, archive_work_thread, send_reopen_confirmation

logger = logging.getLogger(__name__)


async def handle_agent_request(
    self,
    message: discord.Message,
    question: str,
    memory_category: str,
    plan: AgentPlan,
    response_settings: ResponseSettings,
    *,
    image_objects: list | None = None,
    start_progress,
) -> str | None:
    """子（サブエージェント）に作業させ、報告を人格役がペルソナの口調で言い直して返信する（#170）。

    進捗は定型文（案A）で伝える。使える場面では依頼メッセージに作業スレッドを作り、親が
    委託内容を伝えて子が Webhook で進捗と報告を残す（#267）。使えなければ1通のメッセージを
    書き換える。子の失敗・期限切れ・中止は定型文で伝えて終える（人格役の生成はしない）。
    返信の後処理（親密度・モデレーション・評価・記憶）は通常の会話と同じ handle_chat_message に任せる。
    image_objects は読込済み画像の借用として人格役へ渡し、この経路では閉じない。

    完了して人格役が要約を返信できたら、作業スレッドを閉じる（#408）。再開した作業スレッドの中で
    受けた追加の依頼（返信1通の進捗表示になる）も、完了したらそのスレッドを閉じ直す。
    失敗・中止・期限切れや、人格役が返信できなかった場合は閉じずに残す（報告を見られるように）。
    """
    user_id = str(message.author.id)
    catalog = current_persona_catalog()
    lines = catalog.agent_lines(response_settings.prompt_file)
    if not self.agent.try_acquire(user_id):
        logger.info('サブエージェント: 実行中の依頼があるため受け付けませんでした (user_id=<ID>)。')
        try:
            await message.reply(render_agent_line(lines, "busy"), allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException as error:
            logger.error(f'サブエージェント: 受付できない旨を送れませんでした: {type(error).__name__}', exc_info=error)
        return None
    progress = None
    progress_finished = False
    try:
        # ロール・依頼内容・出力形式・手順名は進捗や作業スレッドへそのまま出るため、NG ワードを伏せておく。
        plan = AgentPlan(
            delegate=True,
            role=self.ng_filter.mask_with_log(plan.role, label="サブエージェントのロール"),
            task=self.ng_filter.mask_with_log(plan.task, label="サブエージェントの依頼内容"),
            expected_output=self.ng_filter.mask_with_log(
                plan.expected_output, label="サブエージェントの出力形式",
            ),
            steps=tuple(self.ng_filter.mask_with_log(step, label="サブエージェントの手順") for step in plan.steps),
        )
        logger.info('サブエージェント: 委任します（手順: %d）。', len(plan.steps))
        progress = await start_progress(
            message, lines, plan, webhooks=self._agent_webhooks,
            avatar_url=catalog.agent_avatar_url(response_settings.prompt_file),
        )
        work_thread = progress.work_thread
        if work_thread is not None:
            self._agent_work_threads.register(work_thread.id, lines, plan.role)
        elif self._agent_work_threads.is_work_thread(message.channel.id):
            # 再開した作業スレッドの中で受けた追加の依頼。完了したら、このスレッドを閉じ直す
            work_thread = message.channel
        outcome, report = await progress.run(
            self.agent.run(plan, response_settings.model_name, progress.update)
        )
        if report is not None:
            # 親の返信とは独立して公開されるため、出典も含めて投稿前に安全を確認する。
            report_text, sources, _, accepted = await self._screen_response(
                report.text, report.sources, log_label="サブエージェントの報告", screen_sources=True,
            )
            report = report._replace(text=report_text, sources=sources)
            if not accepted:
                await progress.finish("failed")
                progress_finished = True
                return None
        await progress.finish(outcome, report if progress.shows_report else None)
        progress_finished = True
        if report is None:
            return None
        reply = await self.handle_chat_message(
            message, question, memory_category, response_settings=response_settings,
            delegation_section=build_agent_report_section(plan.role, report.text),
            delegated_sources=report.sources,
            image_objects=image_objects,
        )
        # ★ 要約を元のチャンネルへ返せた後に閉じる（返せなかったときは、報告の残るスレッドを開いたままにする）。
        # handle_chat_message は返信できなかった経路（生成の失敗など）で None を返す。
        if reply is not None and work_thread is not None:
            await archive_work_thread(work_thread, message, lines, plan.role, self._agent_work_threads)
        return reply
    finally:
        try:
            if progress is not None and not progress_finished:
                await progress.close()
        finally:
            self.agent.release(user_id)


async def confirm_reopened_work_thread(
    self, message: discord.Message, question: str, entry: WorkThreadEntry, *, accepts_normally: bool,
) -> None:
    """ボットが閉じた作業スレッドが書き込みで再開されたら、追加の依頼があるかを確かめる（#408）。

    LLM を呼ばない定型文を1回だけ返す（再開ごとに1回。記録の更新は呼び出し側が済ませている）。
    - ボット宛ての発言で「サブエージェント」の明記があれば、確かめずにそのまま委任へ進める（重ねて尋ねない）。
    - NG ワードを含む発言には返さない（ボット宛てなら、この後の関門で従来どおり違反として扱う）。
    ボット宛てでない発言も確かめる（メンションが無いと普段は無視されるため、再開に気づけるようにする）。
    """
    if accepts_normally and requests_subagent(question):
        return
    if question and self.ng_filter.contains_ng_word(question):
        return
    logger.info("サブエージェント: 閉じた作業スレッドが再開されたため、追加の依頼を確かめます。")
    await send_reopen_confirmation(message, entry)
