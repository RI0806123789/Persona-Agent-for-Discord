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
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

from persona_bot.config.constants import (
    DECISION_FAILURE_THRESHOLD, DECISION_MODEL_NAME, DECISION_SUSPEND_SECONDS, DECISION_TIMEOUT_SECONDS,
    DECISION_UNAVAILABLE_STATUS_CODES,
)
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


_USAGE_TOKEN_KEYS = ("total_input_tokens", "total_output_tokens", "total_tokens")


def _usage_info(usage: Any) -> dict[str, int] | None:
    """Decisions API の usage を、利用量の記録（UsageTracker）が読む形へ直す。"""
    if not isinstance(usage, dict):
        return None

    def read_int(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    values = {key: read_int(key) for key in _USAGE_TOKEN_KEYS}
    if all(value is None for value in values.values()):
        return None
    input_tokens = values["total_input_tokens"] or 0
    output_tokens = values["total_output_tokens"] or 0
    total_tokens = max(values["total_tokens"] or 0, input_tokens + output_tokens)
    return {
        "promptTokenCount": input_tokens,
        "candidatesTokenCount": output_tokens,
        "totalTokenCount": total_tokens,
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
        failure_threshold: int = DECISION_FAILURE_THRESHOLD,
        suspend_seconds: float = DECISION_SUSPEND_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._genai_client = genai_client
        self._model_name = model_name
        self._timeout = timeout
        # 判断のたびの入力トークンを記録する。None なら記録しない。
        self._usage = usage
        # 使えない状態が続くとき、会話のたびに問い合わせ・ログを繰り返さないための状態（#496）。
        # メモリだけに持つ（再起動すれば最初から試す）。
        self._failure_threshold = max(1, failure_threshold)
        self._suspend_seconds = suspend_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._suspended_until: float | None = None
        # 失敗が続いている間は True。ログは、これが切り替わったとき（失敗し始め・復旧）だけ出す。
        self._failing = False
        self._suspend_logged = False

    def _record_success(self) -> None:
        """判断モデルが応答した（入力の拒否を含む）。失敗の記録を戻し、復旧したときだけログを出す。"""
        self._consecutive_failures = 0
        self._suspended_until = None
        if self._failing:
            self._failing = False
            logger.info("判断モデル: 問い合わせが再び通りました。")

    def _record_failure(
        self, label: str, *, status_code: int | None, error: BaseException | None, timed_out: bool = False,
    ) -> None:
        """判断モデルが使えなかった。警告は、失敗し始めたときと、問い合わせを止めるとき（1回の失敗の連なりで1回）だけ。"""
        self._consecutive_failures += 1
        suspend = status_code in DECISION_UNAVAILABLE_STATUS_CODES or self._consecutive_failures >= self._failure_threshold
        if suspend:
            self._suspended_until = self._clock() + self._suspend_seconds
        status = "期限内に応答なし" if timed_out else (status_code if status_code is not None else "不明")
        if not self._failing:
            self._failing = True
            self._suspend_logged = suspend
            logger.warning(
                "判断モデル（%s）: 問い合わせに失敗したため、判定せず生成モデルの判定に従います（状態=%s）。%s",
                label, status, self._suspend_notice() if suspend else "", exc_info=error,
            )
        elif suspend and not self._suspend_logged:
            self._suspend_logged = True
            logger.warning("判断モデル（%s）: 失敗が続いています（状態=%s）。%s", label, status, self._suspend_notice())
        else:
            logger.debug("判断モデル（%s）: 問い合わせの失敗が続いています（状態=%s）。", label, status)

    def _suspend_notice(self) -> str:
        return f"{self._suspend_seconds:.0f} 秒間は問い合わせず、生成モデルの判定だけで進みます。"

    def _is_suspended(self) -> bool:
        """問い合わせを止めている間か。期限が過ぎたら止めるのをやめ、次の1回を試せるようにする。"""
        if self._suspended_until is None:
            return False
        if self._clock() < self._suspended_until:
            return True
        # 期限切れ。次の問い合わせの結果で、復旧か再停止かが決まる（試すのは同時に1回に限らないが、
        # 失敗すれば直ちに再停止する）。
        self._suspended_until = None
        self._consecutive_failures = max(0, self._failure_threshold - 1)
        return False

    async def decide_binary(
        self, text: str, questions: Mapping[str, BinaryQuestion], *, label: str = "判断",
    ) -> dict[str, BinaryAnswer] | None:
        if not text or not text.strip() or not questions:
            return None
        if self._is_suspended():
            # 使えない状態が続いている間は、問い合わせも待ち時間もログも出さない（#496）。
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
            self._record_failure(label, status_code=None, error=None, timed_out=True)
            return None
        except Exception as error:
            block_code = _block_error_code(error)
            if block_code is not None:
                # 入力そのものの拒否は、判断モデルが使えないことではない（応答は来ている）。
                self._record_success()
                logger.warning("判断モデル（%s）: 入力が拒否されたため判定しません（%s）。", label, block_code)
            else:
                self._record_failure(label, status_code=error_status_code(error), error=error)
            return None

        # 応答を受け取った時点で記録する（拒否された入力も API を消費している）。
        # 会話件数（User ( 行）には数えず、記録の失敗は判定に影響させない。
        log_tokens_safely(self._usage, f"判断モデル（{label}）", _usage_info(result.get("usage")))

        feedback = result.get("prompt_feedback")
        if isinstance(feedback, dict) and feedback.get("block_reason"):
            self._record_success()
            logger.warning("判断モデル（%s）: 入力が拒否されたため判定しません。", label)
            return None
        answers = read_binary_answers(result.get("answers"), questions.keys())
        if answers is None:
            # 応答の形が想定と違う（API の仕様変更など）。使えない状態として数える。
            self._record_failure(label, status_code=None, error=None)
            return None
        self._record_success()
        return answers
