"""サブエージェントへの委任内容（ルーターの判定結果）と、その正規化。

ルーター（`content_moderator.check_route_and_plan`）と子の実行（`agent_service.py`）の
両方が使うため、どちらにも依存しない小さなモジュールに分けている
（`agent_service` は応答の後処理で `content_moderator` を使うので、ここへ置かないと循環する）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from persona_bot.config.constants import (
    AGENT_ENABLED,
    AGENT_EXPECTED_OUTPUT_MAX_CHARS,
    AGENT_MAX_STEPS,
    AGENT_ROLE_MAX_CHARS,
    AGENT_STEP_NAME_MAX_CHARS,
    AGENT_TASK_MAX_CHARS,
    AGENT_TRIGGER_PATTERN,
)
from persona_bot.utils.normalization import normalize_bool_flag, normalize_extracted_text

_TRIGGER = re.compile(AGENT_TRIGGER_PATTERN, re.IGNORECASE)


@dataclass(frozen=True)
class AgentPlan:
    """ルーターが決めた委任内容。

    delegate が False のときは「サブエージェントの使用を求められたが、人格役だけで
    答えられる」と判断したことを表し、他の項目は空になる。
    """

    delegate: bool
    role: str = ""
    task: str = ""
    expected_output: str = ""
    steps: tuple[str, ...] = ()


def requests_subagent(text: str) -> bool:
    """発言にサブエージェントの使用が明記されているか（Gemini は呼ばない）。

    明記がある発言だけ、ルーターに委任の判定を足す。明記があっても依頼とは限らない
    （「サブエージェントって何？」など）ため、最終的な判断はルーターが行う。
    """
    return AGENT_ENABLED and bool(text) and _TRIGGER.search(text) is not None


def normalize_agent_plan(value: Any) -> AgentPlan | None:
    """モデルが返す agent を検証する。読めない値は None（委任の判定なし）にする。

    委任すると決めたのに依頼内容が空なら、子へ渡せないので「判定なし」に倒し、
    通常の会話として答える（委任の失敗を利用者に見せるより、普段どおり答える方が自然なため）。
    手順が無ければ依頼内容そのものを1手順として扱い、多すぎる分は AGENT_MAX_STEPS で切る。
    """
    if not isinstance(value, dict):
        return None
    delegate = normalize_bool_flag(value.get("delegate"))
    if delegate is None:
        return None
    if not delegate:
        return AgentPlan(delegate=False)
    role = normalize_extracted_text(value.get("role"), AGENT_ROLE_MAX_CHARS)
    task = normalize_extracted_text(value.get("task"), AGENT_TASK_MAX_CHARS)
    if not task:
        return None
    raw_steps = value.get("steps")
    steps = tuple(
        step for step in (
            normalize_extracted_text(item, AGENT_STEP_NAME_MAX_CHARS)
            for item in (raw_steps if isinstance(raw_steps, list) else [])
        ) if step
    )[:AGENT_MAX_STEPS]
    return AgentPlan(
        delegate=True,
        role=role or "調査担当",
        task=task,
        expected_output=normalize_extracted_text(value.get("expected_output"), AGENT_EXPECTED_OUTPUT_MAX_CHARS),
        steps=steps or ("依頼された作業",),
    )
