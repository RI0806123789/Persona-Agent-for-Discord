"""判断専用モデル（文章を作らず「判断」だけを返すモデル）の共通の入口（#450）。

ボットには、入力が不適切か・返信すべきかのように、文章の生成ではなく判断だけが要る場面がある。
判断は `Decider`（Protocol）越しに行い、実装には Gemini Decision 1（Gemini Decisions API）を使う
`GeminiDecider` を `bot.py` で渡す。将来ほかの判断モデル（セルフホストのモデルなど）へ替えるときは、
同じ Protocol を満たす実装を渡せばよく、呼び出し側は変えなくてよい。

判断の結果は「はい（true）である確率」と「確信度」（どちらも 0〜1）で返す。
**判断できなかったとき（通信の失敗・期限切れ・入力の拒否・応答の形の誤り）は None を返し、
例外を外へ出さない。** モデレーションの主経路から呼ばれ、呼び出し側は None なら従来の判定へ戻る。
"""

import asyncio
import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from persona_bot.config.constants import DECISION_MODEL_NAME, DECISION_TIMEOUT_SECONDS
from persona_bot.services.genai_client import create_decision, error_status_code
from persona_bot.utils.usage_graph import UsageTracker, log_tokens_safely

logger = logging.getLogger(__name__)

# 入力が Google 側の安全フィルタで拒否されたことを示す、Decisions API のエラーコード。
# ログにはこの決まった語だけを出す（API の本文や入力は出さない）。
_BLOCK_ERROR_CODES = frozenset({"safety", "prohibited_content", "blocklist", "image_safety", "content_blocked"})


@dataclass(frozen=True)
class BinaryQuestion:
    """「はい・いいえ」で答える判断の問い。

    モデルは問いのキーを見ないため、問いの内容はすべて instructions に書く。
    true_description / false_description は「はい」「いいえ」に当たる条件の説明（任意）。
    """

    instructions: str
    true_description: str = ""
    false_description: str = ""


@dataclass(frozen=True)
class BinaryAnswer:
    """判断の結果。probability は「はい」である確率、confidence は確信度（どちらも 0〜1）。"""

    probability: float
    confidence: float


class Decider(Protocol):
    """判断モデルの共通の入口。"""

    async def decide_binary(
        self, text: str, questions: Mapping[str, BinaryQuestion], *, label: str = "判断",
    ) -> dict[str, BinaryAnswer] | None:
        """text について各問いへ答える。判断できなければ None（例外は出さない）。

        label はログと利用量の記録に使う用途名（固定の語。利用者の文章を入れないこと）。
        """
        ...


def _question_payload(question: BinaryQuestion) -> dict[str, Any]:
    """問いを Decisions API の noul（はい・いいえ）の問いの形にする。"""
    payload: dict[str, Any] = {"type": "noul", "instructions": question.instructions}
    criteria = {
        key: value for key, value in (
            ("true_description", question.true_description),
            ("false_description", question.false_description),
        ) if value
    }
    if criteria:
        payload["criteria"] = criteria
    return payload


def _read_unit_interval(value: Any) -> float | None:
    """0〜1 の有限の数値だけを受け取る（bool・NaN・範囲外は読めない値として扱う）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


def read_binary_answers(answers: Any, keys: Iterable[str]) -> dict[str, BinaryAnswer] | None:
    """応答の answers から、問うたすべてのキーの答えを読む。1つでも読めなければ None。"""
    if not isinstance(answers, dict):
        return None
    result: dict[str, BinaryAnswer] = {}
    for key in keys:
        answer = answers.get(key)
        if not isinstance(answer, dict) or answer.get("type", "noul") != "noul":
            return None
        probability = _read_unit_interval(answer.get("noul"))
        if probability is None:
            return None
        confidence = _read_unit_interval(answer.get("confidence"))
        # 確信度は API の定義では max(p, 1 - p)。欠けていても確率から同じ値を作れる。
        result[key] = BinaryAnswer(
            probability, max(probability, 1.0 - probability) if confidence is None else confidence,
        )
    return result


def _usage_info(usage: Any) -> dict[str, int] | None:
    """Decisions API の usage を、利用量の記録（UsageTracker）が読む形へ直す。"""
    if not isinstance(usage, dict):
        return None

    def count(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    return {
        "promptTokenCount": count("total_input_tokens"),
        "candidatesTokenCount": count("total_output_tokens"),
        "totalTokenCount": count("total_tokens"),
    }


def _block_error_code(error: BaseException) -> str | None:
    """HTTP 400 の拒否のエラーコード（決まった語のときだけ）を返す。"""
    details = getattr(error, "details", None)
    inner = details.get("error") if isinstance(details, dict) else None
    code = inner.get("code") if isinstance(inner, dict) else None
    return code if isinstance(code, str) and code in _BLOCK_ERROR_CODES else None


class GeminiDecider:
    """Gemini Decision 1（Gemini Decisions API）で判断する `Decider` の実装。"""

    def __init__(
        self,
        genai_client: Any,
        *,
        model_name: str = DECISION_MODEL_NAME,
        timeout: float = DECISION_TIMEOUT_SECONDS,
        usage: UsageTracker | None = None,
    ) -> None:
        self._genai_client = genai_client
        self._model_name = model_name
        self._timeout = timeout
        # 判断のたびの入力トークンを記録する。None なら記録しない。
        self._usage = usage

    async def decide_binary(
        self, text: str, questions: Mapping[str, BinaryQuestion], *, label: str = "判断",
    ) -> dict[str, BinaryAnswer] | None:
        if not text or not text.strip() or not questions:
            return None
        try:
            result = await create_decision(
                self._genai_client,
                model=self._model_name,
                input=text,
                questions={key: _question_payload(question) for key, question in questions.items()},
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("判断モデル（%s）: 期限内に応答が無かったため判定しません。", label)
            return None
        except Exception as error:
            block_code = _block_error_code(error)
            if block_code is not None:
                logger.warning("判断モデル（%s）: 入力が拒否されたため判定しません（%s）。", label, block_code)
            else:
                logger.warning(
                    "判断モデル（%s）: 問い合わせに失敗したため判定しません（状態=%s）。",
                    label, error_status_code(error) or "不明", exc_info=error,
                )
            return None

        # 応答を受け取った時点で記録する（拒否された入力も API を消費している）。
        # 会話件数（User ( 行）には数えず、記録の失敗は判定に影響させない。
        log_tokens_safely(self._usage, f"判断モデル（{label}）", _usage_info(result.get("usage")))

        feedback = result.get("prompt_feedback")
        if isinstance(feedback, dict) and feedback.get("block_reason"):
            logger.warning("判断モデル（%s）: 入力が拒否されたため判定しません。", label)
            return None
        answers = read_binary_answers(result.get("answers"), questions.keys())
        if answers is None:
            logger.warning("判断モデル（%s）: 応答の形が想定と違うため判定しません。", label)
        return answers
