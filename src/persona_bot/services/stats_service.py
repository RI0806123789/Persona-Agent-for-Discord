import logging
import asyncio
import io
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import matplotlib

matplotlib.use("Agg")
import numpy as np

from persona_bot.config.constants import (
    ROUTER_MODEL_NAME,
    STATS_ANALYSIS_TIMEOUT_SECONDS,
    STATS_ANALYSIS_TTL_HOURS,
    STATS_HOURLY_BUCKETS,
    STATS_MAX_USERS_IN_SELECT,
    STATS_OPT_OUT_DEFAULT,
    STATS_PERSONALITY_MIN_SAMPLES,
    STATS_RANKING_TOP_N,
    STATS_RECENT_SAMPLES_MAX,
    STATS_TIMEZONE,
    STATS_TOPIC_COUNT,
)
from persona_bot.core.background import run_in_thread, spawn, wait_for_cleanup
from persona_bot.utils.graph_rendering import plot_figure, save_figure_to_buffer, serialized_plot
from persona_bot.utils.select_options import limit_keeping_selected
from persona_bot.core.database import Database
from persona_bot.services.prompting import build_stats_analysis_prompt
from persona_bot.utils.storage import extract_json_object
from persona_bot.utils.timeutil import parse_iso_datetime, resolve_timezone, timezone_label, utc_now_iso

logger = logging.getLogger(__name__)

# ビッグファイブの軸（英語キー → 日本語表示名）。レーダーチャートの軸ラベルに使う。
BIG_FIVE_LABELS = {
    "openness": "開放性",
    "conscientiousness": "誠実性",
    "extraversion": "外向性",
    "agreeableness": "協調性",
    "neuroticism": "神経症傾向",
}


# ---- LLM の解析結果・保存済みの値の正規化 ----------------------------------
# 解析結果は LLM の出力なので、型が指示どおりとは限らない（"weight": "高" など）。
# 保存時に揃えるだけでなく、描画時にも同じ関数を通す（以前の版で保存された
# キャッシュが DB に残っていても、描画が例外で落ち続けないようにするため）。

def _finite_number(value: Any) -> float | None:
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
            weight = 0.0 if raw_weight is None else _finite_number(raw_weight)
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
        number = _finite_number(big_five.get(key))
        if number is not None:
            normalized[key] = min(100.0, max(0.0, number))
    return normalized


def normalize_hourly(hourly: Any) -> list[float]:
    """時間帯別の発言回数を STATS_HOURLY_BUCKETS 個の 0 以上の有限数に揃える（不足は 0）。"""
    values = hourly if isinstance(hourly, list) else []
    counts = [max(0.0, _finite_number(value) or 0.0) for value in values[:STATS_HOURLY_BUCKETS]]
    return counts + [0.0] * (STATS_HOURLY_BUCKETS - len(counts))


@dataclass
class _AnalysisFlight:
    """同じ入力の解析を共有し、最新の要求だけに保存を許可する。"""

    samples: list[str]
    version: int
    task: asyncio.Task | None = None
    waiters: int = 0
    saved: bool = False


class StatsService:
    """ユーザー別の会話統計（発言回数・時間帯・話題・性格推定）を集計・可視化する。

    集計対象は「ボットへの発言のみ」。データは SQLite（data/memory/bot.db の
    user_stats テーブル）に永続化する。保存は asyncio.Lock で直列化する
    （単一プロセス前提）。

    本人が収集そのものを停止できる（opt-out）。停止判定は record() /
    ensure_analysis() の内部で行い、呼び出し側には判断させない
    （呼び出し箇所が増えたときのチェック漏れを防ぐため）。
    """

    def __init__(self, db: Database, gemini: Any) -> None:
        self._db = db
        self.gemini = gemini
        # { user_id: { user_name, total_count, hourly[24], first_seen, last_seen,
        #              recent_samples[], analysis{ topics, big_five, personality, computed_at },
        #              opt_out } }
        try:
            self.cache: dict[str, dict] = db.stats_load_all()
        except Exception as error:
            logger.error("会話統計の読み込みに失敗しました。", exc_info=error)
            self.cache = {}
        self._save_lock = asyncio.Lock()
        # 削除・収集設定の変更より前に開始した解析結果を書き戻さないための世代。
        self._analysis_versions: dict[str, int] = {}
        self._analysis_tasks: dict[str, _AnalysisFlight] = {}
        self._tz = resolve_timezone(STATS_TIMEZONE, purpose="会話統計の時間帯集計")

    # ---- 記録 -------------------------------------------------------------

    @staticmethod
    def _new_entry(user_name: str) -> dict:
        now_iso = utc_now_iso()
        return {
            "user_name": user_name,
            "total_count": 0,
            "hourly": [0] * STATS_HOURLY_BUCKETS,
            "first_seen": now_iso,
            "last_seen": now_iso,
            "recent_samples": [],
            "analysis": None,
            # 収集停止（opt-out）の既定値。定数から引くことで、運用方針を
            # 「原則収集しない（オプトイン）」へ切り替えられるようにしている。
            "opt_out": STATS_OPT_OUT_DEFAULT,
        }

    @staticmethod
    def _entry_opted_out(entry: dict) -> bool:
        """エントリが収集停止中かを返す（キーが無い旧形式は既定値で扱う）。"""
        return bool(entry.get("opt_out", STATS_OPT_OUT_DEFAULT))

    @staticmethod
    def _total_count(entry: dict) -> int:
        """発言回数を int に揃える。壊れた／None の値は 0 として扱い、例外を出さない。"""
        value = entry.get("total_count", 0)
        if isinstance(value, bool):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _to_local_hour(self, when: datetime) -> int:
        """発言時刻（UTC aware 想定）を集計用タイムゾーンの「時」に変換する。"""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(self._tz).hour

    async def record(self, user_id: str, user_name: str, text: str, when: datetime) -> bool:
        """1件を集計・保存し、成否を返す。失敗してもチャット本体は止めない。"""
        try:
            # 更新から保存までを直列化し、古いスナップショットによる上書きを防ぐ。
            async with self._save_lock:
                if self.is_opted_out(user_id):
                    return True
                entry = deepcopy(self.cache.get(user_id))
                if entry is None:
                    entry = self._new_entry(user_name)
                entry["user_name"] = user_name
                entry["total_count"] = self._total_count(entry) + 1
                hourly = entry.get("hourly")
                if not isinstance(hourly, list) or len(hourly) != STATS_HOURLY_BUCKETS:
                    hourly = [0] * STATS_HOURLY_BUCKETS
                    entry["hourly"] = hourly
                hourly[self._to_local_hour(when)] += 1
                entry["last_seen"] = utc_now_iso()
                text = (text or "").strip()
                if text:
                    samples = entry.get("recent_samples")
                    if not isinstance(samples, list):
                        samples = []
                        entry["recent_samples"] = samples
                    samples.append(text)
                    if len(samples) > STATS_RECENT_SAMPLES_MAX:
                        del samples[: len(samples) - STATS_RECENT_SAMPLES_MAX]
                if not await self._save_user_locked(user_id, entry):
                    return False
                self.cache[user_id] = entry
                return True
        except Exception as error:
            logger.error("会話統計の記録に失敗しました。", exc_info=error)
            return False

    async def save_background(self, user_id: str) -> bool:
        """対象ユーザーの現在値だけを保存し、成否を返す。"""
        async with self._save_lock:
            return await self._save_user_locked(user_id)

    async def _save_user_locked(self, user_id: str, entry: dict | None = None) -> bool:
        """候補を保存する。失敗・中断時は公開済みキャッシュを変更しない。"""
        previous = deepcopy(self.cache.get(user_id))
        entry = previous if entry is None else entry
        if entry is None:
            return True
        try:
            await run_in_thread(self._db.stats_save_user, user_id, deepcopy(entry))
        except asyncio.CancelledError:
            # 同期ワーカーは中断できず、ここへ戻る時点では保存済みの場合もある。
            # ロックを離す前に元行へ戻し、後続の更新へ未確定の値を引き継がない。
            await wait_for_cleanup(
                self._restore_user_locked(user_id, previous), label="会話統計の中断後の復元",
            )
            raise
        except Exception as error:
            logger.error("会話統計の保存に失敗しました。", exc_info=error)
            return False
        return True

    async def _restore_user_locked(self, user_id: str, previous: dict | None) -> None:
        """中断した書き込みを対象1行だけ補償し、繰り返しキャンセルでも回収する。"""
        try:
            if previous is None:
                await run_in_thread(self._db.stats_delete_user, user_id)
            else:
                await run_in_thread(self._db.stats_save_user, user_id, previous)
        except Exception as error:
            # 保存先自体に障害があれば完全な復元は保証できない。成功とは扱わず、
            # 呼出元には元のキャンセルを伝え、復元失敗を運用ログに残す。
            logger.error("会話統計の中断後の復元に失敗しました。", exc_info=error)

    # ---- 収集停止（opt-out） ----------------------------------------------

    def is_opted_out(self, user_id: str) -> bool:
        """そのユーザーが統計収集を停止しているかを返す。

        on_message の主経路（record）から毎回呼ばれるため、DB へは行かず
        キャッシュだけを見る。一度も発言していないユーザーはエントリが無いので、
        既定値 STATS_OPT_OUT_DEFAULT を返す（True にすればオプトイン運用になる）。
        """
        entry = self.cache.get(user_id)
        if entry is None:
            return STATS_OPT_OUT_DEFAULT
        return self._entry_opted_out(entry)

    async def set_opt_out(self, user_id: str, user_name: str, opted_out: bool) -> bool:
        """統計収集の停止/再開を設定する。保存できたかを返す。

        一度も発言していないユーザーはエントリが無いため、その場で作ってから
        フラグを立てる（「発言してからでないと停止できない」を避けるため）。
        """
        async with self._save_lock:
            entry = deepcopy(self.cache.get(user_id))
            if entry is None:
                entry = self._new_entry(user_name)
            elif user_name:
                entry["user_name"] = user_name
            entry["opt_out"] = bool(opted_out)
            if not await self._save_user_locked(user_id, entry):
                return False
            self.cache[user_id] = entry
            self._analysis_versions[user_id] = self._analysis_versions.get(user_id, 0) + 1
            return True

    async def clear_user_data(self, user_id: str) -> bool:
        """そのユーザーの蓄積データ（集計値・発言サンプル・解析キャッシュ）を消す。

        **行ごとは消さない。** del self.cache[user_id] にすると収集停止の設定まで
        一緒に消えてしまうため、フィールドだけをリセットして opt_out は残す。
        保存できたかを返す（プライバシー操作の失敗は隠さずユーザーへ伝える）。
        """
        async with self._save_lock:
            entry = deepcopy(self.cache.get(user_id))
            if entry is None:
                return True
            now_iso = utc_now_iso()
            entry["user_name"] = ""
            entry["total_count"] = 0
            entry["hourly"] = [0] * STATS_HOURLY_BUCKETS
            entry["recent_samples"] = []
            entry["analysis"] = None
            entry["first_seen"] = now_iso
            entry["last_seen"] = now_iso
            if not await self._save_user_locked(user_id, entry):
                return False
            self.cache[user_id] = entry
            self._analysis_versions[user_id] = self._analysis_versions.get(user_id, 0) + 1
            return True

    # ---- 参照ヘルパー -----------------------------------------------------

    def has_data(self) -> bool:
        return any(self._total_count(d) > 0 and not self._entry_opted_out(d) for d in self.cache.values())

    def display_name(self, user_id: str) -> str:
        entry = self.cache.get(user_id)
        if entry and entry.get("user_name"):
            return entry["user_name"]
        return str(user_id)

    def get_user_options(self, current_user_id: str | None = None) -> list[tuple[str, str, int]]:
        """管理者向けユーザー選択メニュー用に (user_id, 表示名, 発言回数) を回数の多い順で返す。

        収集停止中のユーザーは載せない（「収集を止めたい」という意思表示には
        「他人に見られたくない」が含まれると解釈する）。まだ発言していない人も
        同じく載らないため、外形上は両者を区別できない＝停止したこと自体は漏れない。
        """
        items = [
            (uid, data.get("user_name") or uid, self._total_count(data))
            for uid, data in self.cache.items()
            if self._total_count(data) > 0 and not self._entry_opted_out(data)
        ]
        items.sort(key=lambda t: t[2], reverse=True)
        return limit_keeping_selected(
            items, lambda item: current_user_id is not None and item[0] == current_user_id,
            STATS_MAX_USERS_IN_SELECT,
        )

    # ---- 話題・性格推定の LLM 解析 ---------------------------------------

    def _is_fresh(self, computed_at: str | None) -> bool:
        """解析キャッシュが TTL 内かどうかを判定する。"""
        if not isinstance(computed_at, str) or not computed_at:
            return False
        try:
            when = parse_iso_datetime(computed_at)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        elapsed_hours = (datetime.now(timezone.utc) - when).total_seconds() / 3600.0
        return elapsed_hours < STATS_ANALYSIS_TTL_HOURS

    @staticmethod
    def _lacks_personality(cached: dict, sample_count: int) -> bool:
        """件数不足の時点の解析しか無いが、今は性格推定に足りる件数があるか。

        件数不足で解析したときは性格推定を保存しないため、件数が揃ったら TTL 内でも
        1回だけ解析し直す。sample_count を持たない以前の版のキャッシュは従来どおり扱う。
        """
        analyzed = _finite_number(cached.get("sample_count"))
        if analyzed is None:
            return False
        return analyzed < STATS_PERSONALITY_MIN_SAMPLES <= sample_count

    async def ensure_analysis(self, user_id: str, force: bool = False) -> dict | None:
        """話題・性格推定の解析結果を返す。TTL 内ならキャッシュ、なければ LLM で再計算する。

        LLM 呼び出しやパースに失敗した場合は、既存のキャッシュ（あれば）にフォールバックする。
        force=Trueも同じ入力の実行中処理を共有し、完了後の次のforce指定で再計算する。
        """
        observed_flight = self._analysis_tasks.get(user_id)
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if not entry:
                return None
            cached = deepcopy(entry.get("analysis"))
            if self._entry_opted_out(entry):
                return cached
            samples = list(entry.get("recent_samples") or [])
            if not samples:
                return cached
            if (not force and cached and self._analysis_usable(cached, len(samples))
                    and self._is_fresh(cached.get("computed_at"))
                    and not self._lacks_personality(cached, len(samples))):
                return cached
            version = self._analysis_versions.get(user_id, 0)
            flight = self._analysis_tasks.get(user_id)
            if (flight is None and observed_flight is not None
                    and observed_flight.version == version and observed_flight.samples == samples
                    and observed_flight.task.done() and not observed_flight.task.cancelled()
                    and observed_flight.saved):
                # 保存ロックを待つ間に完了した解析も、この要求の実行中処理として共有する。
                return deepcopy(entry.get("analysis"))
            if (flight is None or flight.version != version or flight.samples != samples
                    or flight.task.done()):
                flight = _AnalysisFlight(samples=samples, version=version)
                self._analysis_tasks[user_id] = flight
                flight.task = spawn(self._run_analysis(user_id, flight), label="会話統計の解析")
            flight.waiters += 1

        try:
            # 1枚のパネルの中断で、ほかのパネルが待つ解析を中断しない。
            return deepcopy(await asyncio.shield(flight.task))
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if self._analysis_tasks.get(user_id) is flight:
                    self._analysis_tasks.pop(user_id)
                if not flight.task.done():
                    flight.task.cancel()
                    # 保存中の同期処理も回収してから最後の呼出元へキャンセルを返す。
                    await wait_for_cleanup(
                        self._drain_analysis(flight.task), label="会話統計の解析中断待機",
                    )

    @staticmethod
    async def _drain_analysis(task: asyncio.Task) -> None:
        await asyncio.gather(task, return_exceptions=True)

    async def _run_analysis(self, user_id: str, flight: _AnalysisFlight) -> dict | None:
        try:
            return await self._generate_analysis(user_id, flight)
        finally:
            if self._analysis_tasks.get(user_id) is flight:
                self._analysis_tasks.pop(user_id)

    async def _generate_analysis(self, user_id: str, flight: _AnalysisFlight) -> dict | None:
        samples = flight.samples

        # プロンプト文面は prompting.py に集約している（STATS_TOPIC_COUNT のような
        # 定数は prompting が constants を import しない規約のため引数で渡す）。
        prompt = build_stats_analysis_prompt(samples, STATS_TOPIC_COUNT)
        parsed = None
        try:
            # 話題・性格推定はユーザーが選択中のペルソナ/モデルとは無関係な内部解析
            # タスクのため、記憶ルーティング等と同じ固定モデル（ROUTER_MODEL_NAME）を使う。
            text, _usage, _sources = await self.gemini.generate(
                prompt,
                ROUTER_MODEL_NAME,
                images=None,
                timeout=STATS_ANALYSIS_TIMEOUT_SECONDS,
                grounding=False,
            )
            parsed = extract_json_object(text or "", required_keys=("topics", "big_five", "personality"))
            if not parsed:
                logger.warning("会話統計の解析結果を JSON として解釈できませんでした。")
        except Exception as error:
            logger.error("会話統計の解析呼び出しに失敗しました。", exc_info=error)

        analysis = None
        if parsed:
            # 少数のサンプルからの性格推定は不正確なため保存しない。件数が揃ったら
            # _lacks_personality により TTL 内でも解析し直す。
            enough = len(samples) >= STATS_PERSONALITY_MIN_SAMPLES
            personality = parsed.get("personality")
            analysis = {
                "topics": normalize_topics(parsed.get("topics")),
                "big_five": normalize_big_five(parsed.get("big_five")) if enough else {},
                "personality": personality.strip() if enough and isinstance(personality, str) else "",
                "sample_count": len(samples),
                "computed_at": utc_now_iso(),
            }
            if not self._analysis_usable(analysis, len(samples)):
                logger.warning("会話統計: 話題または性格値が不足したため解析結果を保存しません。")
                analysis = None
        async with self._save_lock:
            entry = self.cache.get(user_id)
            if entry is None:
                return None
            # LLM待機中の削除・停止・再開をまたいだ結果は反映しない。
            if (analysis is None or flight.version != self._analysis_versions.get(user_id, 0)
                    or self._entry_opted_out(entry)):
                return deepcopy(entry.get("analysis"))
            if self._analysis_tasks.get(user_id) is not flight:
                # 新しい解析の実行に交代された。計算済みの新しい結果を優先して返し、
                # 古いキャッシュは解析が失敗した（analysis が None）ときだけ使う。
                return deepcopy(analysis or entry.get("analysis"))
            candidate = deepcopy(entry)
            candidate["analysis"] = analysis
            if not await self._save_user_locked(user_id, candidate):
                return deepcopy(entry.get("analysis"))
            self.cache[user_id] = candidate
            flight.saved = True
        return analysis

    @staticmethod
    def _analysis_usable(analysis: dict, sample_count: int) -> bool:
        """失敗表示になる空の解析を、TTLの有効なキャッシュとして扱わない。"""
        return bool(normalize_topics(analysis.get("topics"))) and (
            sample_count < STATS_PERSONALITY_MIN_SAMPLES
            or any(normalize_big_five(analysis.get("big_five")).values())
        )

    # ---- グラフ生成 -------------------------------------------------------

    @serialized_plot
    def build_hourly_graph(self, entry: dict) -> io.BytesIO:
        """時間帯別の発言回数ヒストグラム（24時間）。"""
        hourly = normalize_hourly(entry.get("hourly"))
        with plot_figure(figsize=(10, 5)) as (figure, axis):
            x = np.arange(STATS_HOURLY_BUCKETS)
            axis.bar(x, hourly, color="tab:blue")
            axis.set_xticks(x)
            # 集計に実際に使った tz を表示する（定数のままだと、tzdata が無く
            # UTC へフォールバックした環境で「Asia/Tokyo」と偽って表示してしまう）。
            axis.set_xlabel(f"時刻（{timezone_label(self._tz)}）")
            axis.set_ylabel("発言回数")
            axis.set_title("よく話す時間帯")
            axis.grid(True, axis="y", alpha=0.3)
            return save_figure_to_buffer(figure, bbox_inches="tight")

    @serialized_plot
    def build_topic_graph(self, topics: list[dict]) -> io.BytesIO | None:
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
    def build_bigfive_radar(self, big_five: dict) -> io.BytesIO | None:
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

    async def build_ranking_graph(self, top_n: int = STATS_RANKING_TOP_N) -> io.BytesIO | None:
        """発言回数のユーザー別ランキング横棒グラフ（管理者用の全体比較）。

        収集停止中のユーザーは、データが残っていてもランキングに出さない
        （get_user_options と同じ理由）。
        """
        snapshot = {
            uid: (data.get("user_name") or uid, self._total_count(data))
            for uid, data in self.cache.items()
            if self._total_count(data) > 0 and not self._entry_opted_out(data)
        }
        if not snapshot:
            return None
        versions = {uid: self._analysis_versions.get(uid, 0) for uid in snapshot}
        graph = await run_in_thread(self._render_ranking_graph, list(snapshot.values()), top_n)
        # 描画中に停止・削除したユーザーの情報を、管理者へ後から送信しない。
        if any(uid not in self.cache or self._entry_opted_out(self.cache[uid])
               or not self.cache[uid].get("total_count")
               or versions[uid] != self._analysis_versions.get(uid, 0) for uid in snapshot):
            graph.close()
            return None
        return graph

    @serialized_plot
    def _render_ranking_graph(self, items: list[tuple[str, int]], top_n: int) -> io.BytesIO:
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

    # ---- ダッシュボード組み立て ------------------------------------------

    async def build_dashboard(
        self, user_id: str, item: str, viewer_id: str | None = None
    ) -> tuple[str, list[tuple[str, io.BytesIO]]]:
        """指定ユーザー・指定項目のダッシュボード（説明文＋グラフ画像群）を組み立てる。

        戻り値 (caption, images):
            caption … エフェメラルに表示する説明テキスト
            images  … (ファイル名, PNG バッファ) のリスト。呼び出し側が File 化して送る。

        viewer_id は「誰が見ているか」。収集停止中のユーザーのデータは、本人には
        注記つきで見せるが、他人（管理者）には「まだありません」と同じ文言を返して
        停止していること自体を露見させない。省略時（None）は本人とみなす。
        UIからは必ずviewer_idを渡す。描画中の削除や収集設定変更も世代で検出する。
        """
        entry = deepcopy(self.cache.get(user_id))
        version = self._analysis_versions.get(user_id, 0)
        name = self.display_name(user_id)
        opted_out = bool(entry) and self._entry_opted_out(entry)
        # 他人が停止中のユーザーを指定した場合は、データの有無にかかわらず
        # 「まだありません」と同一の文言を返す（専用の文言を出すと停止が露見する）。
        if opted_out and viewer_id is not None and viewer_id != user_id:
            return (f"{name} さんの会話データはまだありません。", [])
        if not entry or self._total_count(entry) == 0:
            return (f"{name} さんの会話データはまだありません。", [])

        images: list[tuple[str, io.BytesIO]] = []
        try:
            lines = [
                f"📈 **会話統計 — {name} さん**",
                f"・累計発言回数: {self._total_count(entry)} 回",
            ]
            if opted_out:
                # 本人向けの注記。停止後も残っているデータを見ていることが分かるようにする。
                lines.insert(1, "🔕 現在、統計収集を停止しています。（表示中のデータは停止前の記録です）")

            want_time = item in ("all", "time")
            want_topic = item in ("all", "topic")
            want_personality = item in ("all", "personality")

            if want_time:
                images.append(("stats_hourly.png", await run_in_thread(self.build_hourly_graph, entry)))

            samples = entry.get("recent_samples") or []
            enough_samples = len(samples) >= STATS_PERSONALITY_MIN_SAMPLES
            analysis = None
            # 性格推定だけを見る場合、件数が足りなければ解析（Gemini 呼び出し）自体を行わない。
            # 先に解析すると、使わない結果のために API を呼び、件数不足の推定を残してしまう。
            if want_topic or (want_personality and enough_samples):
                analysis = await self.ensure_analysis(user_id)

            if want_topic:
                topic_graph = await run_in_thread(self.build_topic_graph, deepcopy(analysis.get("topics", []))) if analysis else None
                if topic_graph is not None:
                    images.append(("stats_topics.png", topic_graph))
                else:
                    lines.append("・話題: 解析できるデータがありませんでした。")

            if want_personality:
                if not enough_samples:
                    lines.append(
                        f"・性格推定: データ不足のため表示できません"
                        f"（発言 {len(samples)} 件 / 必要 {STATS_PERSONALITY_MIN_SAMPLES} 件）。"
                    )
                else:
                    radar = await run_in_thread(self.build_bigfive_radar, deepcopy(analysis.get("big_five", {}))) if analysis else None
                    if radar is not None:
                        images.append(("stats_personality.png", radar))
                        summary = analysis.get("personality") if analysis else ""
                        if isinstance(summary, str) and summary:
                            lines.append(f"・性格推定: {summary}")
                        lines.append("※ これは発言からの推定であり、確定的な性格診断ではありません。")
                    elif opted_out:
                        lines.append("・性格推定: 統計収集を停止中のため、新しい解析は行っていません。")
                    else:
                        lines.append("・性格推定: 解析に失敗しました。時間をおいて再度お試しください。")

            current = self.cache.get(user_id)
            if (not current or not current.get("total_count")
                    or version != self._analysis_versions.get(user_id, 0)
                    or (viewer_id is not None and viewer_id != user_id and self._entry_opted_out(current))):
                for _, buffer in images:
                    buffer.close()
                return (f"{name} さんの会話データはまだありません。", [])
            return ("\n".join(lines), images)
        except BaseException:
            for _, buffer in images:
                buffer.close()
            raise
