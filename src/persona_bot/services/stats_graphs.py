"""会話統計の matplotlib 描画（#469）。

DB・Discord・Gemini に依存せず、正規化済みの値を受け取って PNG のバッファを返す。
`matplotlib.use("Agg")` はここ（描画の入口）で行う。全関数は `@serialized_plot` で共通ロックの下に置くため、
**これらの関数どうしを入れ子で呼ばないこと**（ロックは再入できない）。呼び出し側のワーカースレッドから
1回ずつ呼ぶ。
"""

import io

import matplotlib

matplotlib.use("Agg")
import numpy as np

from persona_bot.config.constants import STATS_HOURLY_BUCKETS, STATS_TOPIC_COUNT
from persona_bot.services.stats_normalization import (
    BIG_FIVE_LABELS,
    normalize_big_five,
    normalize_hourly,
    normalize_topics,
)
from persona_bot.utils.graph_rendering import plot_figure, save_figure_to_buffer, serialized_plot


@serialized_plot
def render_hourly_graph(hourly_raw: object, timezone_name: str) -> io.BytesIO:
    """時間帯別の発言回数ヒストグラム（24時間）。"""
    hourly = normalize_hourly(hourly_raw)
    with plot_figure(figsize=(10, 5)) as (figure, axis):
        x = np.arange(STATS_HOURLY_BUCKETS)
        axis.bar(x, hourly, color="tab:blue")
        axis.set_xticks(x)
        # 集計に実際に使った tz を表示する（定数のままだと、tzdata が無く
        # UTC へフォールバックした環境で「Asia/Tokyo」と偽って表示してしまう）。
        axis.set_xlabel(f"時刻（{timezone_name}）")
        axis.set_ylabel("発言回数")
        axis.set_title("よく話す時間帯")
        axis.grid(True, axis="y", alpha=0.3)
        return save_figure_to_buffer(figure, bbox_inches="tight")


@serialized_plot
def render_topic_graph(topics: list[dict]) -> io.BytesIO | None:
    """話題（トピック）の重み付き横棒グラフ。"""
    cleaned = [(topic["label"], topic["weight"]) for topic in normalize_topics(topics)]
    if not cleaned:
        return None
    cleaned.sort(key=lambda t: t[1], reverse=True)
    cleaned = cleaned[:STATS_TOPIC_COUNT][::-1]  # 横棒は下から上へ並ぶため反転
    labels = [c[0] for c in cleaned]
    weights = [c[1] for c in cleaned]
    with plot_figure(figsize=(10, max(3, len(labels) * 0.6))) as (figure, axis):
        axis.barh(range(len(labels)), weights, color="tab:green")
        axis.set_yticks(range(len(labels)))
        axis.set_yticklabels(labels)
        axis.set_xlabel("重要度")
        axis.set_title("よく話す話題")
        axis.grid(True, axis="x", alpha=0.3)
        return save_figure_to_buffer(figure, bbox_inches="tight")


@serialized_plot
def render_bigfive_radar(big_five: dict) -> io.BytesIO | None:
    """ビッグファイブ性格推定のレーダーチャート（5軸・0〜100）。"""
    keys = list(BIG_FIVE_LABELS.keys())
    normalized = normalize_big_five(big_five)
    values = [normalized.get(k, 0.0) for k in keys]
    if not any(values):
        return None
    labels = [BIG_FIVE_LABELS[k] for k in keys]
    angles = np.linspace(0, 2 * np.pi, len(keys), endpoint=False).tolist()
    # レーダーを閉じるため先頭要素を末尾に足す
    values_closed = values + values[:1]
    angles_closed = angles + angles[:1]
    with plot_figure(figsize=(6, 6), subplot_kw={"polar": True}) as (figure, axis):
        axis.plot(angles_closed, values_closed, color="tab:purple", marker="o")
        axis.fill(angles_closed, values_closed, color="tab:purple", alpha=0.25)
        axis.set_xticks(angles)
        axis.set_xticklabels(labels)
        axis.set_ylim(0, 100)
        axis.set_title("性格推定（ビッグファイブ）", pad=20)
        return save_figure_to_buffer(figure, bbox_inches="tight")


@serialized_plot
def render_ranking_graph(items: list[tuple[str, int]], top_n: int) -> io.BytesIO:
    """発言回数のユーザー別ランキング横棒グラフ。"""
    items.sort(key=lambda t: t[1], reverse=True)
    items = items[:top_n][::-1]  # 横棒は下から上へ並ぶため反転
    names = [i[0] for i in items]
    counts = [i[1] for i in items]
    with plot_figure(figsize=(10, max(3, len(names) * 0.5))) as (figure, axis):
        axis.barh(range(len(names)), counts, color="tab:orange")
        axis.set_yticks(range(len(names)))
        axis.set_yticklabels(names)
        axis.set_xlabel("発言回数")
        axis.set_title("発言回数ランキング")
        axis.grid(True, axis="x", alpha=0.3)
        return save_figure_to_buffer(figure, bbox_inches="tight")
