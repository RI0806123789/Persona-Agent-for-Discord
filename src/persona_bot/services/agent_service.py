"""サブエージェント（委任型）の子の実行と、同時実行の管理（#170）。

会話の中で「サブエージェントを使って」と明記された依頼のうち、ルーター
（`content_moderator.check_route_and_plan`）が委任すると決めたものだけを、ここで子に作業させる。

- 子は人格役（ペルソナ）とは別の、独立したクリーンな文脈で動く。渡すのはルーターが整理した
  依頼内容（ロール・依頼内容・期待する出力形式・手順）だけで、ペルソナ・会話履歴・長期記憶は渡さない。
- 1手順につき検索付きの生成を1回行う。手順の数（`AGENT_MAX_STEPS` まで）が子のツール実行の上限。
- 子は Markdown の報告を返すだけで、利用者へ返答するのは常に人格役（`MessageHandler`）。
- 進捗の表示は LLM を呼ばない定型文（案A）。文面は `render_agent_line` が組み立てる。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, NamedTuple

from persona_bot.config.constants import (
    AGENT_DEFAULT_LINES,
    AGENT_MAX_CONCURRENT_TASKS,
    AGENT_STEP_NOTE_MAX_CHARS,
    AGENT_STEP_TIMEOUT_SECONDS,
    AGENT_TIMEZONE,
)
from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.prompting import build_agent_worker_prompt
from persona_bot.utils.grounding import normalize_sources
from persona_bot.utils.timeutil import format_prompt_now, resolve_timezone

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from persona_bot.services.gemini_service import GeminiService
    from persona_bot.utils.usage_graph import UsageTracker


class AgentReport(NamedTuple):
    """子の最終報告（Markdown）と、検索で参照した出典。"""

    text: str
    sources: list[dict[str, str]]


def render_agent_line(lines: Mapping[str, str], key: str, **values: Any) -> str:
    """進捗の定型文に差し込みを埋める。書き間違いがあれば既定の文面に戻す。

    ペルソナごとの文面は読み込み時（personas.py）に点検済みだが、表示のたびに例外で
    進捗が止まらないよう、ここでも既定の文面へ倒す。
    """
    values = {"role": "", "step": "", "current": 0, "total": 0, **values}
    try:
        return lines[key].format(**values)
    except (KeyError, IndexError, ValueError, AttributeError, TypeError):
        return AGENT_DEFAULT_LINES[key].format(**values)


class AgentService:
    """子（サブエージェント）を手順ごとに実行し、同時に動く数を制限する。"""

    def __init__(self, gemini: "GeminiService", usage: "UsageTracker") -> None:
        self._gemini = gemini
        self._usage = usage
        # 実行中の依頼者。同じ人の依頼は1件ずつ、全体でも AGENT_MAX_CONCURRENT_TASKS 件まで。
        self._running_users: set[str] = set()
        self._tz = resolve_timezone(AGENT_TIMEZONE, purpose="サブエージェントへ伝える現在日時")

    def try_acquire(self, user_id: str) -> bool:
        """実行枠を確保する。判定と登録の間に await を挟まない（同時に届いた2件を両方通さない）。"""
        if user_id in self._running_users or len(self._running_users) >= AGENT_MAX_CONCURRENT_TASKS:
            return False
        self._running_users.add(user_id)
        return True

    def release(self, user_id: str) -> None:
        self._running_users.discard(user_id)

    def _now_text(self) -> str:
        return format_prompt_now(self._tz, include_zone=True)

    async def run(
        self,
        plan: AgentPlan,
        model_name: str,
        on_step: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> AgentReport | None:
        """子に手順を順に実行させ、最後の手順の報告を返す。どれかが失敗したら None。

        全体の期限と中止は呼び出し側（進捗表示）が管理する。ここでは例外を外へ出さない
        （通信の失敗は GeminiService が None に変える）。

        Args:
            on_step: 各手順を始める前に呼ぶ（手順の番号は 0 始まり）。進捗の表示に使う。
        """
        notes: list[str] = []
        report = ""
        sources: list[dict[str, str]] = []
        now_text = self._now_text()
        for index, step in enumerate(plan.steps):
            if on_step is not None:
                await on_step(index, step)
            prompt = build_agent_worker_prompt(
                plan.role, plan.task, plan.expected_output, plan.steps, index, notes, now_text,
            )
            result = await self._gemini.generate_with_metadata(
                prompt, model_name, timeout=AGENT_STEP_TIMEOUT_SECONDS, grounding=True,
            )
            self._usage.log_tokens("サブエージェント", result.usage)
            # 子には感情タグを禁じているが、混ざっても人格役へ渡す前に取り除く。
            text = strip_affinity_tag(result.text or "").strip()
            if not text:
                logger.error("サブエージェント: 手順%d/%dの生成に失敗したため中止します。", index + 1, len(plan.steps))
                return None
            logger.info("サブエージェント: 手順%d/%dを終えました。", index + 1, len(plan.steps))
            report = text
            notes.append(text[:AGENT_STEP_NOTE_MAX_CHARS])
            sources = normalize_sources([*sources, *result.sources])
        # 最後の手順は、メモと合わせた最終報告を書いている（build_agent_worker_prompt）。
        return AgentReport(report, sources) if report else None
