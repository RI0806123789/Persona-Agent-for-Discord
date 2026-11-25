"""記憶の可視化（#484）のグラフ構築と、添付する HTML の安全性を架空の記憶で検証する。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.config.constants import ANNIVERSARY_PROFILE_KEY
from persona_bot.services import memory_graph
from persona_bot.services.memory_graph import (
    DATA_PLACEHOLDER,
    MemorySnapshot,
    build_memory_graph,
    build_memory_graph_document,
    graph_payload,
    load_memory_graph_template,
    render_memory_graph_html,
)
from persona_bot.utils.timeutil import resolve_timezone

TOKYO = resolve_timezone("Asia/Tokyo")
NOW = datetime(2026, 11, 19, 3, 0, tzinfo=timezone.utc)
_DATA_SCRIPT = re.compile(r'<script type="application/json" id="memory-data">(.*?)</script>', re.S)


def snapshot(**overrides) -> MemorySnapshot:
    values = {"profile": {}, "projects": {}, "document_summaries": [], "pending_count": 0}
    values.update(overrides)
    return MemorySnapshot(**values)


def by_label(graph):
    return {node.label: node for node in graph.nodes}


class BuildMemoryGraphTests(unittest.TestCase):
    def test_no_memory_returns_none_even_with_pending_conversations(self):
        self.assertIsNone(build_memory_graph(snapshot(), tz=TOKYO))
        self.assertIsNone(build_memory_graph(snapshot(pending_count=5), tz=TOKYO))
        # 記念日のキーが空のリストだけなら、覚えている記憶は無い。
        self.assertIsNone(build_memory_graph(snapshot(profile={ANNIVERSARY_PROFILE_KEY: []}), tz=TOKYO))

    def test_tree_follows_storage_structure_only(self):
        graph = build_memory_graph(snapshot(
            profile={"呼び名": "架空の太郎", "所属": {"学校": "架空大学", "学年": 2},
                     ANNIVERSARY_PROFILE_KEY: [{"label": "誕生日", "month": 5, "day": 1}]},
            projects={"架空の研究": {"手法": ["手法A", "手法B"]}},
            document_summaries=[{"document_name": "架空の資料.pdf", "summary": "要約"}],
            pending_count=3,
        ), tz=TOKYO)
        nodes = by_label(graph)
        ids = {node.id for node in graph.nodes}
        self.assertEqual(len(ids), len(graph.nodes))
        root = graph.nodes[0]
        self.assertEqual((root.label, root.kind, root.parent, root.depth), ("あなた", "root", None, 0))
        # 線は親への1本だけで、親は必ず先に作られている（木構造）。
        seen = set()
        for node in graph.nodes:
            if node.parent is not None:
                self.assertIn(node.parent, seen)
            seen.add(node.id)
        for category in ("プロフィール", "記念日", "プロジェクト", "文書の要約"):
            self.assertEqual(nodes[category].kind, "category")
            self.assertEqual(nodes[category].parent, root.id)
        self.assertEqual(nodes["まとめる前の会話: 3件"].kind, "info")
        self.assertEqual(nodes["呼び名: 架空の太郎"].parent, nodes["プロフィール"].id)
        self.assertEqual(nodes["学校: 架空大学"].parent, nodes["所属"].id)
        self.assertEqual(nodes["学年: 2"].path, "プロフィール › 所属 › 学年")
        self.assertEqual(nodes["手法A"].parent, nodes["手法"].id)
        self.assertEqual(nodes["誕生日（5月1日）"].parent, nodes["記念日"].id)
        self.assertEqual(nodes["架空の資料.pdf"].kind, "document")
        # 記念日はプロフィールの項目としては重ねて出さない。
        self.assertNotIn(ANNIVERSARY_PROFILE_KEY, nodes)
        self.assertEqual(graph.pending_count, 3)
        self.assertFalse(graph.truncated)
        # 本人・分類・件数の案内を除いた記憶の項目数。
        # プロフィール4（呼び名・所属・学校・学年）＋記念日1＋プロジェクト4＋文書1。
        self.assertEqual(graph.memory_node_count, 10)

    def test_list_items_use_name_fields_and_scalars_are_shown_as_is(self):
        graph = build_memory_graph(snapshot(projects={
            "機能": [{"name": "架空の機能", "状態": "作業中"}, {"状態": "完了"}, None, True, ""],
        }), tz=TOKYO)
        nodes = by_label(graph)
        for label in ("架空の機能", "2件目", "（なし）", "はい", "（空）", "状態: 作業中"):
            self.assertIn(label, nodes)

    def test_depth_limit_collapses_deep_values_into_detail(self):
        deep = {"a": {"b": {"c": {"d": {"e": {"f": "最下層"}}}}}}
        graph = build_memory_graph(snapshot(projects=deep), max_depth=4, tz=TOKYO)
        self.assertLessEqual(max(node.depth for node in graph.nodes), 4)
        collapsed = [node for node in graph.nodes if node.label.endswith("…")]
        self.assertEqual(len(collapsed), 1)
        self.assertIn("最下層", collapsed[0].detail)

    def test_node_limit_truncates_but_keeps_root_and_pending_notice(self):
        graph = build_memory_graph(snapshot(
            projects={f"話題{i}": [f"項目{i}-{n}" for n in range(5)] for i in range(20)},
            pending_count=2,
        ), max_nodes=10, tz=TOKYO)
        self.assertEqual(len(graph.nodes), 10)
        self.assertTrue(graph.truncated)
        self.assertEqual(graph.nodes[0].kind, "root")
        self.assertEqual(graph.nodes[1].kind, "info")

    def test_labels_are_single_line_and_details_are_bounded(self):
        hidden = "架空​の‮名前\x07"
        graph = build_memory_graph(snapshot(profile={
            "メモ": "一行目\r\n二行目\t字下げ" + "あ" * 100,
            hidden: "値",
        }), label_max=12, detail_max=20, tz=TOKYO)
        nodes = list(graph.nodes)
        for node in nodes:
            self.assertNotIn("\n", node.label)
            self.assertLessEqual(len(node.label), 12)
            self.assertIsNone(re.search(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮]", node.label + node.detail))
        memo = next(node for node in nodes if node.label.startswith("メモ"))
        self.assertTrue(memo.label.endswith("…"))
        self.assertIn("一行目\n二行目", memo.detail)
        self.assertTrue(memo.detail.endswith("…（長いため省略）"))
        self.assertTrue(any(node.label.startswith("架空 の 名前") for node in nodes))

    def test_anniversaries_show_invalid_entries_and_non_list_values(self):
        graph = build_memory_graph(snapshot(profile={ANNIVERSARY_PROFILE_KEY: [
            {"label": "誕生日", "month": 2, "day": 29}, {"label": "壊れた", "month": 13, "day": 1}, "手書き途中",
        ]}), tz=TOKYO)
        nodes = by_label(graph)
        self.assertIn("誕生日（2月29日）", nodes)
        unreadable = [node for node in graph.nodes if node.label == "読み取れない記念日"]
        self.assertEqual(len(unreadable), 2)
        self.assertIn("13", unreadable[0].detail)

        graph = build_memory_graph(snapshot(profile={ANNIVERSARY_PROFILE_KEY: "架空の文字列"}), tz=TOKYO)
        nodes = by_label(graph)
        self.assertNotIn("記念日", nodes)
        self.assertIn(f"{ANNIVERSARY_PROFILE_KEY}: 架空の文字列", nodes)

    def test_documents_are_newest_first_without_internal_fields(self):
        graph = build_memory_graph(snapshot(document_summaries=[
            {"document_name": "古い.pdf", "summary": "古い要約", "document_hash": "架空のハッシュ",
             "created_at": "2026-11-01T03:00:00+00:00"},
            {"document_name": " ", "summary": "名前なし", "key_points": ["要点1", 2], "warnings": ["注意1"]},
            "壊れた要約",
        ]), tz=TOKYO)
        documents = [node for node in graph.nodes if node.kind == "document"]
        self.assertEqual([node.label for node in documents], ["読み取れない要約", "名前のない文書", "古い.pdf"])
        self.assertNotIn("架空のハッシュ", documents[2].detail)
        self.assertIn("保存日時: 2026-11-01 12:00（Asia/Tokyo）", documents[2].detail)
        self.assertIn("【要点】\n・要点1\n・2", documents[1].detail)
        self.assertIn("【注意】\n・注意1", documents[1].detail)

    def test_document_detail_skips_booleans_and_non_finite_numbers(self):
        graph = build_memory_graph(snapshot(document_summaries=[
            {"document_name": "架空.pdf", "key_points": [True, False, float("nan"), float("inf"), 3, 1.5, "要点"]},
        ]), tz=TOKYO)
        detail = by_label(graph)["架空.pdf"].detail
        self.assertIn("・3\n・1.5\n・要点", detail)
        for word in ("True", "False", "nan", "inf"):
            self.assertNotIn(word, detail)

    def test_category_without_room_for_a_child_is_not_left_empty(self):
        # 根の次に分類を足すと残りが0になる上限（根 + 分類 = 2 のとき、子を置けない）。
        graph = build_memory_graph(snapshot(profile={"好み": "猫"}), max_nodes=2, tz=TOKYO)
        self.assertIsNone(graph)  # 記憶のノードを1つも置けない図は出さない
        graph = build_memory_graph(snapshot(profile={"好み": "猫"}, projects={"話題": "値"}), max_nodes=3, tz=TOKYO)
        self.assertEqual([node.kind for node in graph.nodes], ["root", "category", "value"])
        self.assertTrue(graph.truncated)
        for node in graph.nodes:
            if node.kind == "category":
                self.assertTrue(any(child.parent == node.id for child in graph.nodes))

    def test_entry_without_room_for_a_child_collapses_into_leaf_and_keeps_applied_max_nodes(self):
        """残り1枠の辞書・配列は空の key にせず葉ノードへ畳み、meta.maxNodes と pending_count も整合させる（#509）。"""
        snap = snapshot(profile={"家族": {"ペット": "架空の犬"}, "予備": "値"}, pending_count=True)
        graph = build_memory_graph(snap, max_nodes=3, tz=TOKYO)
        self.assertEqual([node.kind for node in graph.nodes], ["root", "category", "value"])
        self.assertTrue(graph.truncated)
        self.assertEqual(graph.max_nodes, 3)
        self.assertEqual(graph.pending_count, 0)
        self.assertIn("ペット", graph.nodes[-1].detail)
        payload = graph_payload(graph, generated_at=NOW, tz=TOKYO)
        self.assertEqual(payload["meta"]["maxNodes"], 3)
        self.assertEqual(payload["meta"]["pendingCount"], 0)
        for bad_pending in (True, False, "不正", float("nan"), float("inf"), -3):
            doc = build_memory_graph_document(snapshot(pending_count=bad_pending), generated_at=NOW)
            self.assertEqual(doc.pending_count, 0)

    def test_payload_has_only_graph_fields_and_metadata(self):
        graph = build_memory_graph(snapshot(profile={"呼び名": "架空"}, pending_count=1), tz=TOKYO)
        payload = graph_payload(graph, generated_at=NOW, tz=TOKYO)
        self.assertEqual(set(payload), {"meta", "nodes"})
        self.assertEqual(payload["meta"]["generatedAt"], "2026-11-19 12:00")
        self.assertEqual(payload["meta"]["timezone"], "Asia/Tokyo")
        self.assertEqual(payload["meta"]["pendingCount"], 1)
        for node in payload["nodes"]:
            self.assertEqual(set(node), {"id", "label", "kind", "parent", "depth", "path", "detail"})


class MemoryGraphHtmlTests(unittest.TestCase):
    def render(self, snap: MemorySnapshot) -> str:
        graph = build_memory_graph(snap, tz=TOKYO)
        return render_memory_graph_html(graph, template=load_memory_graph_template(), generated_at=NOW, tz=TOKYO)

    def test_template_has_one_placeholder_and_never_loads_external_resources(self):
        template = load_memory_graph_template()
        self.assertEqual(template.count(DATA_PLACEHOLDER), 1)
        self.assertIn("default-src 'none'", template)
        # ファイルを開いただけで外部へ通信させない（スクリプト・スタイル・画像・フォントを読み込まない）。
        self.assertIsNone(re.search(r"https?://(?!www\.w3\.org/2000/svg)", template))
        self.assertIsNone(re.search(r"\bsrc\s*=|@import|url\(|<link\b|fetch\(|XMLHttpRequest|WebSocket", template))
        # 記憶の文字列を HTML として解釈させる経路を使わない。
        self.assertIsNone(re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", template))

    def test_memory_text_cannot_close_the_data_script(self):
        attack = "</script><script>alert(1)</script><!-- &     終わり"
        html = self.render(snapshot(profile={attack: attack}))
        self.assertNotIn(DATA_PLACEHOLDER, html)
        self.assertEqual(html.lower().count("</script"), 2)
        self.assertNotIn("<script>alert", html)
        payload = json.loads(_DATA_SCRIPT.search(html).group(1))
        self.assertTrue(any(node["label"].startswith("</script>") for node in payload["nodes"]))
        self.assertTrue(any(attack in node["detail"] for node in payload["nodes"]))

    def test_render_requires_exactly_one_placeholder(self):
        graph = build_memory_graph(snapshot(profile={"a": "b"}), tz=TOKYO)
        for template in ("<html></html>", DATA_PLACEHOLDER * 2):
            with self.subTest(template=template), self.assertRaises(ValueError):
                render_memory_graph_html(graph, template=template, generated_at=NOW, tz=TOKYO)

    def test_document_is_utf8_html_or_none(self):
        empty = build_memory_graph_document(snapshot(pending_count=4), generated_at=NOW)
        self.assertIsNone(empty.html)
        self.assertEqual((empty.memory_node_count, empty.pending_count, empty.truncated), (0, 4, False))

        document = build_memory_graph_document(snapshot(profile={"呼び名": "架空の太郎"}), generated_at=NOW)
        text = document.html.decode("utf-8")
        self.assertTrue(text.startswith("<!doctype html>"))
        self.assertIn("架空の太郎", text)
        self.assertEqual(document.memory_node_count, 1)

    def test_template_path_is_next_to_module(self):
        self.assertEqual(memory_graph.TEMPLATE_PATH.parent, Path(memory_graph.__file__).parent)
        self.assertTrue(memory_graph.TEMPLATE_PATH.is_file())


if __name__ == "__main__":
    unittest.main()
