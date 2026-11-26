"""共通の日本語フォント設定と、OS別の候補による実PNG描画を検証する。"""

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import matplotlib
matplotlib.use("Agg")
from matplotlib import font_manager
import matplotlib.pyplot as plt

from persona_bot.config import constants
from persona_bot.utils import graph_rendering
from persona_bot.services.stats_service import StatsService
from persona_bot.utils.usage_graph import UsageTracker


class GraphFontConfigurationTests(unittest.TestCase):
    def test_candidates_include_linux_fonts_before_generic_fallback(self) -> None:
        families = constants.GRAPH_FONT_FAMILY
        self.assertIn("Noto Sans CJK JP", families)
        self.assertIn("Noto Sans JP", families)
        self.assertIn("IPAexGothic", families)
        self.assertEqual(families[-1], "sans-serif")

    def test_linux_only_inventory_excludes_missing_windows_fonts(self) -> None:
        with patch.object(font_manager.fontManager, "ttflist", [SimpleNamespace(name="Noto Sans CJK JP")]):
            self.assertEqual(graph_rendering.graph_font_context(), {
                "font.family": ["Noto Sans CJK JP", "sans-serif"],
            })

    def test_windows_fonts_keep_their_priority(self) -> None:
        installed = [SimpleNamespace(name=name) for name in ("Noto Sans CJK JP", "Meiryo", "Yu Gothic")]
        with patch.object(font_manager.fontManager, "ttflist", installed):
            self.assertEqual(graph_rendering.graph_font_context()["font.family"], [
                "Yu Gothic", "Meiryo", "Noto Sans CJK JP", "sans-serif",
            ])

    def test_missing_japanese_fonts_keep_generic_fallback(self) -> None:
        with patch.object(font_manager.fontManager, "ttflist", []):
            self.assertEqual(graph_rendering.graph_font_context(), {"font.family": ["sans-serif"]})


class JapaneseGraphRenderingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_graph_uses_shared_font_and_renders_japanese(self) -> None:
        available = {font.name for font in font_manager.fontManager.ttflist}
        # Linux系フォントを優先して、Windows専用フォントに頼らず描けることも検証する。
        linux = [name for name in constants.GRAPH_FONT_FAMILY
                 if name not in ("Yu Gothic", "Meiryo", "sans-serif") and name in available]
        japanese = linux or [name for name in ("Yu Gothic", "Meiryo") if name in available]
        if not japanese:
            if os.environ.get("REQUIRE_JAPANESE_GRAPH_FONT") == "1":
                self.fail("日本語フォントが必要です。紹介サイトの「運用」のページ（日本語グラフのフォント）の手順を確認してください。")
            self.skipTest("日本語フォント未導入のため、実描画の字形確認を省略します。")
        family = japanese[0]
        old_families = constants.GRAPH_FONT_FAMILY[:]
        constants.GRAPH_FONT_FAMILY[:] = [family]
        self.addCleanup(lambda: constants.GRAPH_FONT_FAMILY.__setitem__(slice(None), old_families))
        service = StatsService(SimpleNamespace(stats_load_all=lambda: {}), None)
        service.cache["test-user"] = {**service._new_entry("架空の利用者"), "total_count": 1}
        tracker = UsageTracker()
        tracker.record({"promptTokenCount": 1, "candidatesTokenCount": 2, "totalTokenCount": 3})
        figures = []
        original_subplots = plt.subplots

        def subplots(*args, **kwargs):
            figure, axis = original_subplots(*args, **kwargs)
            figures.append(figure)
            return figure, axis

        before = list(matplotlib.rcParams["font.family"])
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with patch.object(plt, "subplots", side_effect=subplots):
                images = [
                    service.build_hourly_graph(service.cache["test-user"]),
                    service.build_topic_graph([{"label": "架空の話題", "weight": 1}]),
                    service.build_bigfive_radar({key: 50 for key in ("openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism")}),
                    await service.build_ranking_graph(),
                    await tracker.build_graph_buffer(),
                ]
        for buffer in images:
            self.addCleanup(buffer.close)
            self.assertEqual(buffer.read(8), b"\x89PNG\r\n\x1a\n")
        self.assertEqual(len(figures), 5)
        for figure in figures:
            for axis in figure.axes:
                for text in (axis.title, axis.xaxis.label, axis.yaxis.label, *axis.get_xticklabels(), *axis.get_yticklabels()):
                    self.assertEqual(text.get_fontfamily(), [family])
        self.assertFalse([warning for warning in captured if "Glyph" in str(warning.message)
                          and "missing" in str(warning.message)])
        self.assertEqual(list(matplotlib.rcParams["font.family"]), before)


if __name__ == "__main__":
    unittest.main()
