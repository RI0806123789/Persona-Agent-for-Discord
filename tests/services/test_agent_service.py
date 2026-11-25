"""子（サブエージェント）の手順実行・同時実行の制限・定型文の組み立てを検証する（#170）。"""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import AGENT_DEFAULT_LINES, AGENT_MAX_CONCURRENT_TASKS, MAX_GROUNDING_SOURCES
from persona_bot.services.agent_plan import AgentPlan
from persona_bot.services.agent_service import AgentReport, AgentService, render_agent_line
from persona_bot.services.gemini_service import GenerationResult

PLAN = AgentPlan(True, "架空の調査担当", "架空の街のカフェを調べる", "店名と特徴を箇条書きで", ("候補の検索", "特徴の比較"))


def result(text, sources=()):
    return GenerationResult(text, {"total_token_count": 1}, list(sources), "架空のモデル" if text else None)


class AgentRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.gemini = SimpleNamespace(generate_with_metadata=AsyncMock())
        self.usage = SimpleNamespace(log_tokens=Mock())
        self.service = AgentService(self.gemini, self.usage)

    async def test_steps_run_in_order_and_last_step_is_the_report(self) -> None:
        self.gemini.generate_with_metadata.side_effect = [
            result("- 架空のメモ1", [{"title": "A", "uri": "https://example.invalid/a"}]),
            result("# 架空の最終報告 [V:+1, A:0]", [
                {"title": "A", "uri": "https://example.invalid/a"}, {"title": "B", "uri": "https://example.invalid/b"},
            ]),
        ]
        steps = []

        async def on_step(index, step):
            steps.append((index, step))

        report = await self.service.run(PLAN, "架空の選択モデル", on_step)
        # 感情タグは取り除き、出典は重複を除いて集める
        self.assertEqual(report, AgentReport("# 架空の最終報告", [
            {"title": "A", "uri": "https://example.invalid/a"}, {"title": "B", "uri": "https://example.invalid/b"},
        ]))
        self.assertEqual(steps, [(0, "候補の検索"), (1, "特徴の比較")])
        calls = self.gemini.generate_with_metadata.await_args_list
        self.assertEqual(len(calls), len(PLAN.steps))
        for call in calls:
            # 子は検索を使え、選択モデルで手順ごとの期限つきで動く
            self.assertEqual(call.args[1], "架空の選択モデル")
            self.assertTrue(call.kwargs["grounding"])
            self.assertIsNotNone(call.kwargs["timeout"])
        first_prompt, last_prompt = calls[0].args[0], calls[1].args[0]
        self.assertIn("架空の調査担当", first_prompt)
        self.assertIn("最終報告はまだ書かない", first_prompt)
        # 前の手順のメモを次の手順へ引き継ぎ、最後の手順で期待する出力の報告を書かせる
        self.assertIn("- 架空のメモ1", last_prompt)
        self.assertIn("最終報告を Markdown で書いて", last_prompt)
        self.assertEqual(self.usage.log_tokens.call_count, len(PLAN.steps))

    async def test_failed_step_stops_without_further_calls(self) -> None:
        self.gemini.generate_with_metadata.side_effect = [result(None), result("呼ばれない")]
        with patch("builtins.print"):
            self.assertIsNone(await self.service.run(PLAN, "架空のモデル"))
        self.gemini.generate_with_metadata.assert_awaited_once()

    async def test_sources_are_capped(self) -> None:
        sources = [{"title": str(index), "uri": f"https://example.invalid/{index}"} for index in range(MAX_GROUNDING_SOURCES + 3)]
        self.gemini.generate_with_metadata.side_effect = [result("メモ", sources), result("報告")]
        with patch("builtins.print"):
            report = await self.service.run(PLAN, "架空のモデル")
        self.assertEqual(len(report.sources), MAX_GROUNDING_SOURCES)


class ConcurrencyTests(unittest.TestCase):
    def test_same_user_and_global_limits(self) -> None:
        service = AgentService(SimpleNamespace(), SimpleNamespace())
        self.assertTrue(service.try_acquire("1"))
        self.assertFalse(service.try_acquire("1"))
        for index in range(2, AGENT_MAX_CONCURRENT_TASKS + 1):
            self.assertTrue(service.try_acquire(str(index)))
        self.assertFalse(service.try_acquire("他の架空の利用者"))
        service.release("1")
        self.assertTrue(service.try_acquire("他の架空の利用者"))


class LineTests(unittest.TestCase):
    def test_placeholders_are_filled_and_broken_lines_fall_back(self) -> None:
        lines = dict(
            AGENT_DEFAULT_LINES,
            progress="{step}だよ（{current}/{total}）",
            start="{壊れた",
            done="{role.missing}",
        )
        self.assertEqual(render_agent_line(lines, "progress", step="検索", current=1, total=2), "検索だよ（1/2）")
        self.assertEqual(
            render_agent_line(lines, "start", role="架空の担当"),
            AGENT_DEFAULT_LINES["start"].format(role="架空の担当"),
        )
        self.assertEqual(
            render_agent_line(lines, "done", role="架空の担当"),
            AGENT_DEFAULT_LINES["done"].format(role="架空の担当"),
        )


if __name__ == "__main__":
    unittest.main()
