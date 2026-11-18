"""会話統計の LLM 解析結果・保存済みの値の正規化（#469）。

解析結果は LLM の出力なので、型が指示どおりとは限らない（"weight": "高" など）。
保存時に揃えるだけでなく、描画時にも同じ関数を通す（以前の版で保存された
キャッシュが DB に残っていても、描画が例外で落ち続けないようにするため）。

DB・Discord・matplotlib に依存しない純粋関数だけを置く。`stats_service` が従来の名前で再公開する。
"""

import math
from typing import Any

from persona_bot.config.constants import STATS_HOURLY_BUCKETS

# ビッグファイブの軸（英語キー → 日本語表示名）。レーダーチャートの軸ラベルに使う。
BIG_FIVE_LABELS = {
    "openness": "開放性",
    "conscientiousness": "誠実性",
    "extraversion": "外向性",
    "agreeableness": "協調性",
    "neuroticism": "神経症傾向",
}


def finite_number(value: Any) -> float | None:
    """有限の数値に変換する。変換できなければ None。

    bool は int の一種だが数値として扱わない（True が 1 点として描かれるのを防ぐ）。
    "0.8" のような数字の文字列は受け付け、"高" や NaN・無限大は捨てる。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def normalize_topics(topics: Any) -> list[dict[str, Any]]:
    """話題を {"label": 空でない文字列, "weight": 0 以上の有限数} のリストに揃える。

    LLM は ["ゲーム"] のように文字列だけのリストを返すことがあるため、その場合は
    重み 1.0 の話題として扱う。重みが無い要素は従来どおり 0、数値でない要素は捨てる。
    """
    if not isinstance(topics, list):
        return []
    normalized: list[dict[str, Any]] = []
    for topic in topics:
        if isinstance(topic, str):
            label, weight = topic.strip(), 1.0
        elif isinstance(topic, dict) and isinstance(topic.get("label"), str):
            label = topic["label"].strip()
            raw_weight = topic.get("weight")
            weight = 0.0 if raw_weight is None else finite_number(raw_weight)
        else:
            continue
        if label and weight is not None:
            normalized.append({"label": label, "weight": max(0.0, weight)})
    return normalized


def normalize_big_five(big_five: Any) -> dict[str, float]:
    """ビッグファイブを既知の5軸だけにし、0〜100 の有限数へ丸める。数値でない軸は捨てる。"""
    if not isinstance(big_five, dict):
        return {}
    normalized: dict[str, float] = {}
    for key in BIG_FIVE_LABELS:
        number = finite_number(big_five.get(key))
        if number is not None:
            normalized[key] = min(100.0, max(0.0, number))
    return normalized


def normalize_hourly(hourly: Any) -> list[float]:
    """時間帯別の発言回数を STATS_HOURLY_BUCKETS 個の 0 以上の有限数に揃える（不足は 0）。"""
    values = hourly if isinstance(hourly, list) else []
    counts = [max(0.0, finite_number(value) or 0.0) for value in values[:STATS_HOURLY_BUCKETS]]
    return counts + [0.0] * (STATS_HOURLY_BUCKETS - len(counts))
