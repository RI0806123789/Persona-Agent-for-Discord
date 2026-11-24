import logging
import time
import math
import asyncio
import re

from persona_bot.config.constants import (
    AFFINITY_HALF_LIFE_SECONDS,
    AFFINITY_LOWER_THRESHOLD,
    AFFINITY_MAX,
    AFFINITY_MIN,
    AFFINITY_NEUTRAL,
    AFFINITY_RAISE_THRESHOLD,
)
from persona_bot.core.database import Database
from persona_bot.services.prompting import build_affinity_prompt
from persona_bot.utils.user_persistence import save_user_background

logger = logging.getLogger(__name__)

# モデルが返答の末尾に付ける感情タグ `[V:X, A:Y]`。
# `[V: +5, A: -10]` のように空白を挟むことがあるため各要素の前後に \s* を許容する。
# `[v:+5, a:-10]` のような小文字のゆらぎも出るため大文字・小文字を区別しない
# （どちらも緩めないとタグが除去されずユーザーへそのまま露出し、感情も更新されない）。
# チャット返信・自発的な会話・会話要約のいずれでも同じ形を扱うため、
# 正規表現とその操作はここに集約している。
_AFFINITY_TAG_PATTERN = re.compile(
    r"\[V:\s*([+-]?\d+)\s*,\s*A:\s*([+-]?\d+)\s*\]",
    re.IGNORECASE,
)
# 読み上げ用。一般の角括弧表記まで消さないよう、感情名と数値タグだけを扱う。
_SPEECH_EMOTION_TAG_PATTERN = re.compile(
    r"\[\s*(?:(?:happy|sad|angry|neutral|excited|calm|surprised|scared)"
    r"|(?:arousal|valence|affinity)\s*:\s*[+-]?\d+"
    r"(?:\s*,\s*(?:arousal|valence|affinity)\s*:\s*[+-]?\d+)*)\s*\]",
    re.IGNORECASE,
)


def extract_affinity_changes(text: str) -> tuple[str, tuple[int, int] | None]:
    """感情タグを取り除いた本文と、その変化量を返す。

    Returns:
        (本文, (機嫌の変化量, テンションの変化量))。タグが無ければ変化量は None を返し、
        本文は元のまま（余計な strip をしない）。
    """
    # 本文中の引用は残し、末尾に連続するタグだけをメタデータとして扱う。
    matches = list(_AFFINITY_TAG_PATTERN.finditer(text))
    if not matches or text[matches[-1].end():].strip():
        return text, None
    match = matches[-1]
    start = match.start()
    for previous in reversed(matches[:-1]):
        if text[previous.end():start].strip():
            break
        start = previous.start()
    cleaned = text[:start].strip()
    return cleaned, (int(match.group(1)), int(match.group(2)))


def strip_affinity_tag(text: str) -> str:
    """感情タグだけを取り除く（変化量を使わない経路用）。

    自発的な会話・記念日のお祝い・会話要約は特定の相手への返信ではないため
    親密度を更新しない。ペルソナが指示に反してタグを付けた場合の保険として使う。
    """
    return _AFFINITY_TAG_PATTERN.sub("", text).strip()


def strip_speech_tags(text: str) -> str:
    """読み上げ前に感情名・親密度・感情変化の内部タグを取り除く。"""
    return _SPEECH_EMOTION_TAG_PATTERN.sub("", strip_affinity_tag(text)).strip()


class AffinityService:
    def __init__(self, db: Database):
        self._db = db
        self.cache: dict[str, dict] = {}
        self._save_lock = asyncio.Lock()
        self._save_versions: dict[str, int] = {}
        self._saved_versions: dict[str, int] = {}
        self._load()

    def _load(self):
        # 起動時に DB の affinity テーブルからキャッシュへ読み込む
        try:
            self.cache = self._db.affinity_load_all()
        except Exception as error:
            logger.error("親密度データの読み込みに失敗しました。", exc_info=error)
            self.cache = {}

    def get_user_data(self, user_id: str) -> dict:
        cached = self.cache.get(user_id)
        if cached is not None:
            return self._apply_decay(cached.copy())
        return {
            "affinity": AFFINITY_NEUTRAL,
            "valence": AFFINITY_NEUTRAL,
            "arousal": AFFINITY_NEUTRAL,
            "last_interaction": time.time()
        }

    def _apply_decay(self, data: dict) -> dict:
        current_time = time.time()
        last_time = data.get("last_interaction", current_time)
        elapsed = max(0.0, current_time - last_time)

        if elapsed > 0:
            decay_factor = math.pow(0.5, elapsed / AFFINITY_HALF_LIFE_SECONDS)
            data["valence"] = AFFINITY_NEUTRAL + (data["valence"] - AFFINITY_NEUTRAL) * decay_factor
            data["arousal"] = AFFINITY_NEUTRAL + (data["arousal"] - AFFINITY_NEUTRAL) * decay_factor
            data["valence"] = max(AFFINITY_MIN, min(AFFINITY_MAX, int(round(data["valence"]))))
            data["arousal"] = max(AFFINITY_MIN, min(AFFINITY_MAX, int(round(data["arousal"]))))

        return data

    def update_emotion(self, user_id: str, v_change: int, a_change: int) -> dict:
        data = self.get_user_data(user_id)
        data["valence"] += v_change
        data["arousal"] += a_change

        data["valence"] = max(AFFINITY_MIN, min(AFFINITY_MAX, data["valence"]))
        data["arousal"] = max(AFFINITY_MIN, min(AFFINITY_MAX, data["arousal"]))

        if data["valence"] > AFFINITY_RAISE_THRESHOLD:
            data["affinity"] = min(AFFINITY_MAX, data["affinity"] + 1)
        elif data["valence"] < AFFINITY_LOWER_THRESHOLD:
            data["affinity"] = max(AFFINITY_MIN, data["affinity"] - 1)

        data["last_interaction"] = time.time()
        self.cache[user_id] = data
        return data

    async def save_background(self, user_id: str) -> None:
        """対象ユーザーだけを保存し、後続保存が古い値に巻き戻されないよう世代合流と直列化を行う。"""
        await save_user_background(
            user_id,
            lock=self._save_lock,
            save_versions=self._save_versions,
            saved_versions=self._saved_versions,
            cache=self.cache,
            write_fn=self._db.affinity_upsert,
            logger=logger,
            error_message="親密度データの保存に失敗しました。",
        )

    def build_dynamic_prompt(self, user_id: str) -> str:
        # 文面そのものは prompting.py に集約している（ここは現在値を渡すだけ）。
        data = self.get_user_data(user_id)
        return build_affinity_prompt(data["affinity"], data["valence"], data["arousal"])
