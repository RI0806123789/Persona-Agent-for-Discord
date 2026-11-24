import io
import logging
from collections import deque
from typing import Any

import matplotlib

matplotlib.use("Agg")
import numpy as np

from persona_bot.core.background import run_in_thread
from persona_bot.utils.graph_rendering import plot_figure, save_figure_to_buffer, serialized_plot
from persona_bot.config.constants import MAX_USAGE_HISTORY


logger = logging.getLogger(__name__)


class UsageTracker:
    def __init__(self) -> None:
        # 長時間稼働でメモリと描画コストが際限なく増えないよう、
        # 直近 MAX_USAGE_HISTORY 件のみを保持するリングバッファにする。
        self.input_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)
        self.output_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)
        self.total_tokens: deque[int] = deque(maxlen=MAX_USAGE_HISTORY)

    def has_data(self) -> bool:
        return bool(self.total_tokens)

    @staticmethod
    def _read_token_count(usage_info: Any, attr_name: str, dict_key: str) -> int:
        """SDK オブジェクト（snake_case 属性）と REST API の dict（camelCase キー）の
        両形式から トークン数を取り出す。"""
        if isinstance(usage_info, dict):
            return int(usage_info.get(dict_key, 0) or 0)
        return int(getattr(usage_info, attr_name, 0) or 0)

    def _read_output_token_count(self, usage_info: Any) -> int:
        """出力トークン数を読む。生成 API は candidates_token_count、Gemini Live の usage_metadata
        （types.UsageMetadata）は response_token_count に入れるので、どちらの形も読む（#496）。"""
        return (
            self._read_token_count(usage_info, "candidates_token_count", "candidatesTokenCount")
            or self._read_token_count(usage_info, "response_token_count", "responseTokenCount")
        )

    def record(self, usage_info: Any) -> None:
        self.input_tokens.append(self._read_token_count(usage_info, "prompt_token_count", "promptTokenCount"))
        self.output_tokens.append(self._read_output_token_count(usage_info))
        self.total_tokens.append(self._read_token_count(usage_info, "total_token_count", "totalTokenCount"))

    def log_conversation(self, kind: str = "会話") -> None:
        """利用者の発言・発話への応答が完了したことを計数用の見出しとして記録する（#496）。

        トークン数が別メッセージで届く音声会話（Gemini Live）でも、1ターンの応答完了を管理コンソールの
        「会話」件数と「最後の会話」に数えるために使う。本文や話者は出さない。
        """
        logger.info("User (%s)：会話を処理しました。", kind)

    def log_tokens(self, context_label: str, usage_info: Any | None) -> None:
        """会話ではない Gemini 呼び出し（サブエージェントの作業など）のトークンだけを記録する。

        log_snapshot の「User (…)：」行を出さないため、管理コンソールの会話件数には数えず、
        「Total token count：」行で Gemini の呼び出し回数とトークン数にだけ数える。本文は出さない。
        """
        if not usage_info:
            return
        self.record(usage_info)
        logger.info("[内部処理] Prompt token count：%d", self.input_tokens[-1])
        logger.info("Total token count：%d", self.total_tokens[-1])

    def log_snapshot(
        self,
        context_label: str,
        question: str,
        attachment_summary: str,
        full_prompt: str,
        response_text: str | None,
        usage_info: Any | None,
    ) -> None:
        # 計数用の見出しは維持するが、場所・質問・添付名・応答本文は既定ログへ出さない。
        logger.info("User (会話)：会話を処理しました。")
        logger.info("Prompt text count：%d", len(full_prompt))
        if usage_info:
            self.record(usage_info)
            logger.info("Prompt token count：%d", self.input_tokens[-1])
            logger.info("Response token count：%d", self.output_tokens[-1])
            logger.info("Total token count：%d", self.total_tokens[-1])
        logger.info("AI：%s", "応答を生成しました。" if response_text else "応答がありません。")

    async def build_graph_buffer(self) -> io.BytesIO:
        # 3列を同じ時点でコピーする。描画待機中のrecordで系列の長さが食い違わない。
        snapshot = (list(self.input_tokens), list(self.output_tokens), list(self.total_tokens))
        return await run_in_thread(self._render_graph_buffer, *snapshot)

    @serialized_plot
    def _render_graph_buffer(
        self, input_tokens: list[int], output_tokens: list[int], total_tokens: list[int],
    ) -> io.BytesIO:
        with plot_figure(figsize=(10, 6)) as (figure, axis):
            x = np.arange(1, len(total_tokens) + 1)
            axis.plot(x, input_tokens, label="入力トークン数", marker="o", color="red")
            axis.plot(x, output_tokens, label="出力トークン数", marker="o", color="blue")
            axis.plot(x, total_tokens, label="合計トークン数", marker="o", color="green")
            axis.set_xlabel("利用回数")
            axis.set_ylabel("トークン数")
            axis.set_title("Gemini API 利用結果のトークン数推移")
            axis.legend()
            axis.grid(True)

            return save_figure_to_buffer(figure)


def log_tokens_safely(usage: UsageTracker | None, context_label: str, usage_info: Any | None) -> None:
    """内部の Gemini 呼び出しのトークンを記録する。記録の失敗や未接続で呼出元の処理を止めない。

    モデレーション・記憶・文書など、会話の本処理に付随する呼び出し元が使う。
    UsageTracker.log_tokens は「User (」行を出さないので会話件数には数えない。
    計測は付随的な処理のため、usage が None（未接続）でも usage_info が空でも何もせず、
    記録で例外が出てもログに型名だけを残して戻る（本文・値は出さない）。
    特に ContentModerator._judge_verdict は全体を except Exception で包んでおり、
    記録の失敗がそのまま「判定不能」に化けるため、この入口を通して判定と切り離す。
    """
    if usage is None or not usage_info:
        return
    try:
        usage.log_tokens(context_label, usage_info)
    except Exception as error:
        logger.warning("トークンの記録に失敗しました: %s", type(error).__name__)


def log_conversation_safely(usage: UsageTracker | None, kind: str = "会話") -> None:
    """音声会話などの1ターン完了を会話件数へ安全に記録する。未接続や失敗で本処理を止めない（#496）。"""
    if usage is None:
        return
    log_fn = getattr(usage, "log_conversation", None)
    if not callable(log_fn):
        return
    try:
        log_fn(kind)
    except Exception as error:
        logger.warning("会話の記録に失敗しました: %s", type(error).__name__)
