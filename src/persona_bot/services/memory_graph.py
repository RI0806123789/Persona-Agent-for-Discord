"""記憶の可視化（#484）: 本人の記憶をノードグラフにし、自己完結した HTML を組み立てる。

`/functions` → 🔒 統計プライバシー → 🧠 記憶を見る から使う。ボットが利用者について覚えていること
（プロフィール記憶・プロジェクト記憶・文書の要約・記念日）を、本人だけに届く HTML ファイルにする。

利用者の決定（Issue #484 のコメント）:
・届け方は「HTML ファイルを本人だけ（エフェメラル）に添付」。ボットは Web サーバーを立てない
  （本番は OCI 上で、localhost のリンクは利用者のブラウザから開けないため）。
・閲覧のみ。削除・修正はできない。
・線は**保存されている記憶の構造（本人 → 分類 → 項目 → 中身）だけ**を表す。LLM で関連を推定しない
  （ボットが実際には持っていない関連を描かないため）。

このモジュールは DB・Discord・Gemini に触れない。読むファイルは同じフォルダの HTML テンプレートだけで、
呼び出し側（パネル）が `run_in_thread` で `build_memory_graph_document` を呼ぶ。

★ 壊れやすい点:
・HTML へ埋め込む記憶は利用者の発言や LLM の出力から作られた任意の文字列。`<script type="application/json">`
  に入れる JSON は `_json_for_script` で `<` `>` `&` と行区切り文字をエスケープし、テンプレート側は
  `textContent` だけで描画する（`innerHTML` を使わない）。どちらかを外すと、記憶の文字列で HTML が壊れたり
  スクリプトが動いたりする。
・テンプレートは外部のスクリプト・フォント・画像を読み込まない（CSP の `default-src 'none'` でも塞ぐ）。
  ファイルを開いただけで外部へ通信し、記憶を持っていることが第三者へ漏れないようにするため。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from persona_bot.config.constants import (
    ANNIVERSARY_PROFILE_KEY,
    DOCUMENT_SUMMARY_LIMIT,
    MEMORY_GRAPH_DETAIL_MAX_CHARS,
    MEMORY_GRAPH_LABEL_MAX_CHARS,
    MEMORY_GRAPH_MAX_DEPTH,
    MEMORY_GRAPH_MAX_NODES,
    MEMORY_GRAPH_TIMEZONE,
)
from persona_bot.utils.anniversaries import parse_anniversary
from persona_bot.utils.timeutil import parse_iso_datetime, resolve_timezone, timezone_label

TEMPLATE_PATH = Path(__file__).with_name("memory_graph_template.html")
# テンプレートの中で、記憶のデータ（JSON）に置き換える目印。テンプレートにちょうど1回だけ書く。
DATA_PLACEHOLDER = "__MEMORY_GRAPH_DATA__"

# 表示名から取り除く文字（制御文字・ゼロ幅文字・双方向の上書き）。改行とタブは詳細でだけ残す。
_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_PATH_SEPARATOR = " › "
# 配列の要素に、そのまま見出しにできる名前の項目があればそれを使う（無ければ「N件目」）。
_ITEM_NAME_KEYS = ("name", "label", "title", "名前", "名称", "タイトル")
# 文書の要約で、詳細に載せる項目とその見出し（document_service の要約 JSON のキー）。
_DOCUMENT_LIST_FIELDS = (("key_points", "要点"), ("warnings", "注意"), ("usage_notes", "使い方"))

_CATEGORY_DETAILS = {
    "profile": "会話からまとめた、あなた自身についての記憶です（呼び名・好み・状況など）。",
    "anniversaries": "誕生日など、毎年お祝いする日の記憶です。",
    "projects": "会話からまとめた、あなたが取り組んでいることについての記憶です。",
    "documents": f"送ってくれた文書（PDF など）の要約です。新しいものから最大 {DOCUMENT_SUMMARY_LIMIT} 件を覚えています。",
}
_ROOT_DETAIL = (
    "ボットがあなたについて覚えていることの全体です。"
    "線は「分類 → 項目 → 中身」という記憶の保存のされ方を表しています（ボットが推測した関連ではありません）。"
)
_PENDING_DETAIL = (
    "最近の会話のうち、まだ長期記憶へまとめていないものの件数です。会話の本文はこの図に載せていません。"
    "一定の件数がたまるか、しばらく会話が途切れたときに、上の記憶へまとめます。"
)


@dataclass(frozen=True)
class MemorySnapshot:
    """本人1人分の記憶の写し（`MemoryService.load_user_snapshot` が作る）。"""

    profile: dict[str, Any]
    projects: dict[str, Any]
    document_summaries: list[Any]
    # まだ記憶へ圧縮していない会話の件数（本文は持たない）。
    pending_count: int = 0


@dataclass(frozen=True)
class GraphNode:
    """図の1ノード。線は `parent` への1本だけ（木構造）。"""

    id: str
    label: str
    kind: str
    parent: str | None
    depth: int
    path: str
    detail: str


@dataclass
class MemoryGraph:
    nodes: list[GraphNode] = field(default_factory=list)
    # ノード数の上限に達して、記憶の一部を図に載せられなかったか。
    truncated: bool = False
    pending_count: int = 0

    @property
    def memory_node_count(self) -> int:
        """記憶の中身を表すノードの数（本人・分類・件数の案内を除く）。"""
        return sum(1 for node in self.nodes if node.kind not in ("root", "category", "info"))


@dataclass(frozen=True)
class MemoryGraphDocument:
    """パネルへ返す結果。記憶が1つも無ければ `html` は None。"""

    html: bytes | None
    memory_node_count: int
    pending_count: int
    truncated: bool


def _one_line(text: str, limit: int) -> str:
    """図に出す1行の表示名へ整える（不可視文字を除き、空白をまとめ、上限で切る）。"""
    collapsed = " ".join(_INVISIBLE.sub(" ", text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def _detail_text(text: str, limit: int) -> str:
    """詳細に出す文へ整える（改行は残し、不可視文字を除き、上限で切る）。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "\n".join(
        _INVISIBLE.sub("", line.replace("\t", "    ")).rstrip() for line in normalized.split("\n")
    ).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "…（長いため省略）"


def _scalar_text(value: Any) -> str:
    """入れ子でない値を表示用の文字列にする。"""
    if value is None:
        return "（なし）"
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    if isinstance(value, str):
        return value if value.strip() else "（空）"
    if isinstance(value, (dict, list)):
        return "（空）"
    return str(value)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _list_item_name(index: int, item: Any) -> str:
    if isinstance(item, dict):
        for key in _ITEM_NAME_KEYS:
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    return f"{index}件目"


def _format_saved_at(raw: Any, tz) -> str | None:
    """文書要約の保存日時（UTC の ISO 8601）を表示用のタイムゾーンで示す。読めなければ None。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        moment = parse_iso_datetime(raw.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


class _GraphBuilder:
    def __init__(self, *, max_nodes: int, max_depth: int, label_max: int, detail_max: int) -> None:
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._label_max = label_max
        self._detail_max = detail_max
        self.graph = MemoryGraph()
        self._depths: dict[str, int] = {}

    def add(self, label: str, kind: str, parent: str | None, detail: str, path: Iterable[str]) -> str | None:
        """ノードを1つ足して ID を返す。上限に達したら足さずに None を返す。"""
        if len(self.graph.nodes) >= self._max_nodes:
            self.graph.truncated = True
            return None
        node_id = f"n{len(self.graph.nodes)}"
        depth = 0 if parent is None else self._depths[parent] + 1
        self._depths[node_id] = depth
        self.graph.nodes.append(GraphNode(
            id=node_id,
            label=_one_line(label, self._label_max) or "（名前なし）",
            kind=kind,
            parent=parent,
            depth=depth,
            path=_PATH_SEPARATOR.join(_one_line(part, self._label_max) for part in path),
            detail=_detail_text(detail, self._detail_max),
        ))
        return node_id

    def add_entry(
        self, parent: str, name: str, value: Any, path: tuple[str, ...], *, show_name: bool = True,
    ) -> None:
        """1つの値を、入れ子なら枝として、入れ子でなければ葉として足す。"""
        node_path = path + (name,)
        depth = self._depths[parent] + 1
        if isinstance(value, (dict, list)) and value:
            if depth >= self._max_depth:
                # これ以上は枝を伸ばさず、中身をまとめて詳細に載せる（図が細かくなりすぎないため）。
                self.add(f"{name} …", "value", parent, _json_text(value), node_path)
                return
            count = f"{len(value)} 項目" if isinstance(value, dict) else f"{len(value)} 件"
            node = self.add(name, "key", parent, count, node_path)
            if node is None:
                return
            if isinstance(value, dict):
                for key, child in value.items():
                    self.add_entry(node, str(key), child, node_path)
            else:
                for index, child in enumerate(value, 1):
                    self.add_entry(
                        node, _list_item_name(index, child), child, node_path,
                        show_name=isinstance(child, (dict, list)),
                    )
            return
        text = _scalar_text(value)
        label = f"{name}: {text}" if show_name else text
        self.add(label, "value", parent, text, node_path)


def _add_anniversaries(builder: _GraphBuilder, root: str, entries: list[Any]) -> None:
    category = builder.add("記念日", "category", root, _CATEGORY_DETAILS["anniversaries"], ("記念日",))
    if category is None:
        return
    for index, entry in enumerate(entries, 1):
        parsed = parse_anniversary(entry)
        if parsed is None:
            # 記念日の形になっていない要素も、覚えている内容として隠さずに見せる。
            builder.add(
                "読み取れない記念日", "anniversary", category, _json_text(entry), ("記念日", f"{index}件目"),
            )
            continue
        label, month, day = parsed
        builder.add(
            f"{label}（{month}月{day}日）", "anniversary", category,
            f"{label}: 毎年 {month}月{day}日", ("記念日", label),
        )


def _document_detail(summary: dict[str, Any], tz) -> str:
    lines: list[str] = []
    text = summary.get("summary")
    if isinstance(text, str) and text.strip():
        lines.append(text.strip())
    for key, heading in _DOCUMENT_LIST_FIELDS:
        values = summary.get(key)
        if isinstance(values, list):
            items = [str(item).strip() for item in values if isinstance(item, (str, int, float)) and str(item).strip()]
            if items:
                lines.append(f"\n【{heading}】")
                lines.extend(f"・{item}" for item in items)
    saved_at = _format_saved_at(summary.get("created_at"), tz)
    if saved_at is not None:
        lines.append(f"\n保存日時: {saved_at}（{timezone_label(tz)}）")
    return "\n".join(lines) or "（要約の本文がありません）"


def _add_documents(builder: _GraphBuilder, root: str, summaries: list[Any], tz) -> None:
    category = builder.add("文書の要約", "category", root, _CATEGORY_DETAILS["documents"], ("文書の要約",))
    if category is None:
        return
    # 新しいものほど後ろに保存されている。図では新しい順に並べる。
    for summary in reversed(summaries):
        if not isinstance(summary, dict):
            builder.add("読み取れない要約", "document", category, _json_text(summary), ("文書の要約", "読み取れない要約"))
            continue
        raw_name = summary.get("document_name")
        name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "名前のない文書"
        builder.add(name, "document", category, _document_detail(summary, tz), ("文書の要約", name))


def build_memory_graph(
    snapshot: MemorySnapshot,
    *,
    max_nodes: int = MEMORY_GRAPH_MAX_NODES,
    max_depth: int = MEMORY_GRAPH_MAX_DEPTH,
    label_max: int = MEMORY_GRAPH_LABEL_MAX_CHARS,
    detail_max: int = MEMORY_GRAPH_DETAIL_MAX_CHARS,
    tz=None,
) -> MemoryGraph | None:
    """記憶の写しから図を作る。覚えている記憶が1つも無ければ None を返す。

    件数の案内（まとめる前の会話）だけでは図を作らない（呼び出し側が文章で案内する）。
    """
    profile = snapshot.profile if isinstance(snapshot.profile, dict) else {}
    projects = snapshot.projects if isinstance(snapshot.projects, dict) else {}
    documents = snapshot.document_summaries if isinstance(snapshot.document_summaries, list) else []
    anniversaries = profile.get(ANNIVERSARY_PROFILE_KEY)
    anniversaries = anniversaries if isinstance(anniversaries, list) else []
    other_profile = {key: value for key, value in profile.items() if key != ANNIVERSARY_PROFILE_KEY}
    # 記念日のキーが list 以外の値で保存されていても、隠さずにプロフィールの項目として見せる。
    if ANNIVERSARY_PROFILE_KEY in profile and not isinstance(profile[ANNIVERSARY_PROFILE_KEY], list):
        other_profile[ANNIVERSARY_PROFILE_KEY] = profile[ANNIVERSARY_PROFILE_KEY]
    if not (other_profile or anniversaries or projects or documents):
        return None

    tz = tz if tz is not None else resolve_timezone(MEMORY_GRAPH_TIMEZONE, purpose="記憶の可視化")
    builder = _GraphBuilder(max_nodes=max(max_nodes, 2), max_depth=max(max_depth, 2),
                            label_max=max(label_max, 2), detail_max=max(detail_max, 1))
    builder.graph.pending_count = max(int(snapshot.pending_count or 0), 0)
    root = builder.add("あなた", "root", None, _ROOT_DETAIL, ("あなた",))
    if root is None:  # 上限は2以上にしてあるので通らない（念のため）。
        return None
    if builder.graph.pending_count:
        # 上限に達したときも落ちないよう、記憶の中身より先に足す。
        builder.add(
            f"まとめる前の会話: {builder.graph.pending_count}件", "info", root, _PENDING_DETAIL,
            ("まとめる前の会話",),
        )

    if other_profile:
        category = builder.add("プロフィール", "category", root, _CATEGORY_DETAILS["profile"], ("プロフィール",))
        if category is not None:
            for key, value in other_profile.items():
                builder.add_entry(category, str(key), value, ("プロフィール",))
    if anniversaries:
        _add_anniversaries(builder, root, anniversaries)
    if projects:
        category = builder.add("プロジェクト", "category", root, _CATEGORY_DETAILS["projects"], ("プロジェクト",))
        if category is not None:
            for key, value in projects.items():
                builder.add_entry(category, str(key), value, ("プロジェクト",))
    if documents:
        _add_documents(builder, root, documents, tz)
    return builder.graph


def _json_for_script(payload: Any) -> str:
    """`<script type="application/json">` の中へ安全に置ける JSON 文字列にする。

    `</script>` や `<!--` で要素が閉じられないよう、`<` `>` `&` を Unicode エスケープにする
    （`JSON.parse` は元の文字へ戻す）。U+2028/U+2029 も念のためエスケープする。
    """
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    )


def graph_payload(graph: MemoryGraph, *, generated_at: datetime, tz) -> dict[str, Any]:
    """HTML へ埋め込むデータ。利用者 ID や表示名は入れない（ファイルを渡されたときに漏れないため）。"""
    return {
        "meta": {
            "generatedAt": generated_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            "timezone": timezone_label(tz),
            "pendingCount": graph.pending_count,
            "truncated": graph.truncated,
            "maxNodes": MEMORY_GRAPH_MAX_NODES,
            "memoryNodeCount": graph.memory_node_count,
        },
        "nodes": [asdict(node) for node in graph.nodes],
    }


def load_memory_graph_template() -> str:
    """同じフォルダの HTML テンプレートを読む（同期 I/O。呼び出し側がワーカーで呼ぶ）。"""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def render_memory_graph_html(graph: MemoryGraph, *, template: str, generated_at: datetime, tz) -> str:
    """テンプレートの目印をデータに置き換えた HTML を返す。目印が1つでなければ例外にする。"""
    if template.count(DATA_PLACEHOLDER) != 1:
        raise ValueError("記憶の可視化のテンプレートに、データの目印がちょうど1つありません。")
    return template.replace(DATA_PLACEHOLDER, _json_for_script(graph_payload(graph, generated_at=generated_at, tz=tz)))


def build_memory_graph_document(snapshot: MemorySnapshot, *, generated_at: datetime) -> MemoryGraphDocument:
    """記憶の写しから、添付する HTML（UTF-8）を作る。テンプレートを読むのでワーカーで呼ぶこと。"""
    tz = resolve_timezone(MEMORY_GRAPH_TIMEZONE, purpose="記憶の可視化")
    graph = build_memory_graph(snapshot, tz=tz)
    pending = max(int(snapshot.pending_count or 0), 0)
    if graph is None:
        return MemoryGraphDocument(html=None, memory_node_count=0, pending_count=pending, truncated=False)
    html = render_memory_graph_html(graph, template=load_memory_graph_template(), generated_at=generated_at, tz=tz)
    return MemoryGraphDocument(
        html=html.encode("utf-8"),
        memory_node_count=graph.memory_node_count,
        pending_count=graph.pending_count,
        truncated=graph.truncated,
    )
