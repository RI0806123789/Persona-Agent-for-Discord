import logging
import asyncio
import re
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any, Callable, NamedTuple, Sequence

from google.genai import types

from persona_bot.config.constants import (
    DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
    DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
    GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS,
    GROUNDING_MODEL_NAME,
    GROUNDING_REQUEST_TIMEOUT_SECONDS,
    GROUNDING_SUBMODEL_RETRY_ENABLED,
    MAX_GROUNDING_SOURCES,
)
from persona_bot.utils.grounding import normalize_sources
from persona_bot.services.genai_client import generate_content, generate_json_content
from persona_bot.services.prompting import build_document_summary_prompt
from persona_bot.utils.storage import (
    contains_internal_response_text,
    read_json_response,
    safe_response_text,
)

logger = logging.getLogger(__name__)


def _remaining_timeout(deadline: float | None) -> float | None:
    """生成開始時の締切から残り時間を求め、使い切っていれば次の通信を止める。"""
    if deadline is None:
        return None
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise asyncio.TimeoutError()
    return remaining


def _grounding_is_unsupported(status_code: Any, message: str, *, multimodal: bool = False) -> bool:
    """認証・利用枠・サーバー障害と、検索ツールの非対応を区別する。

    検索付きの試行で渡すツールは Google 検索だけなので、「ツール（tool/tools）が使えない」という
    400 も検索の非対応とみなす（#398。画像と検索ツールの併用を断られた場合など）。画像を
    添えた試行では「multimodal」への言及も同じに扱う。「media」「image」だけの失敗は画像そのものの
    問題で、検索を外して試し直しても通らないため、代替の生成を増やさないよう対象にしない。
    """
    if status_code not in (400, 404, 422):
        return False
    normalized = message.lower()
    names = ["google_search", "google search", "grounding", "search_retrieval", "tool"]
    if multimodal:
        names.append("multimodal")
    return any(name in normalized for name in names) and any(reason in normalized for reason in (
        "unsupported", "not supported", "not available", "not enabled", "not found",
        "unknown name", "not permitted", "not allowed",
    ))


def _error_status_code(error: BaseException) -> int | None:
    """SDK と HTTP クライアントの例外から HTTP ステータスを読み取る。"""
    for code in (
        getattr(error, "code", None), getattr(error, "status_code", None),
        getattr(getattr(error, "response", None), "status_code", None),
    ):
        if isinstance(code, str) and re.fullmatch(r"[0-9]{3}", code.strip()):
            code = int(code.strip())
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
            return code
    return None


def _grounding_quota_exhausted(status_code: int | None, error: BaseException) -> bool:
    """検索付きの試行が利用枠（429）で断られたか。

    検索の利用枠はモデルの世代ごとに別に数えられ、Gemini 3 以降の検索は無料枠に含まれない。
    そのため選択モデルで断られても、検索用のサブモデルや検索なしの生成は通りうる。
    """
    if status_code is not None:
        return status_code == 429
    # 型付きのステータスが無い旧形式だけを文面で補う。認証・サーバー障害のコードを優先する。
    message = " ".join(str(part) for part in (
        getattr(error, "status", ""), getattr(error, "code", ""), error,
    )).lower()
    return (
        "resource_exhausted" in message or "resource exhausted" in message
        or re.search(r"(?<!\d)429(?!\d)", message) is not None
    )


class GenerationResult(NamedTuple):
    """本文と採用した生成のメタデータ。選択設定とは独立して保持する。"""

    text: str | None
    usage: Any | None
    sources: list[dict[str, str]]
    model_name: str | None


class GeminiService:
    """共有クライアントを使い、呼び出し元が選んだモデルで非同期生成する。"""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def generate(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
        grounding: bool = False,
    ) -> tuple[str | None, Any | None, list[dict[str, str]]]:
        """本文・使用量・出典を返す既存の入口。生成モデルが必要なら別の入口を使う。"""
        result = await self.generate_with_metadata(prompt, model_name, images, timeout, grounding)
        return result.text, result.usage, result.sources

    async def generate_with_metadata(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None = None,
        timeout: float | None = None,
        grounding: bool = False,
    ) -> GenerationResult:
        """採用した本文の生成モデルも返す。失敗時のモデルは未設定とする。"""
        # フォールバックのたびに待ち時間をリセットせず、生成全体で予算を共有する。
        budget = GROUNDING_REQUEST_TIMEOUT_SECONDS if grounding and timeout is None else timeout
        deadline = monotonic() + budget if budget is not None else None
        try:
            remaining = _remaining_timeout(deadline)
            request = self._generate(prompt, model_name, images, deadline, grounding)
            result = await asyncio.wait_for(request, timeout=remaining) if remaining is not None else await request
            # 締切を越えて戻った応答も成功として返さない。
            _remaining_timeout(deadline)
            return result
        except asyncio.TimeoutError:
            logger.warning("Gemini生成: 制限時間に達したため打ち切りました。", exc_info=True)
            return GenerationResult(None, None, [], None)

    async def _generate(
        self,
        prompt: str,
        model_name: str,
        images: Sequence[Any] | None,
        deadline: float | None,
        grounding: bool,
    ) -> GenerationResult:
        contents: list[Any] = [prompt]
        if images:
            contents.extend(images)
        attempts = [(model_name, grounding)]
        if grounding:
            if GROUNDING_SUBMODEL_RETRY_ENABLED and model_name != GROUNDING_MODEL_NAME:
                attempts.append((GROUNDING_MODEL_NAME, True))
            attempts.append((model_name, False))

        regenerations_left = GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS
        index = 0
        while index < len(attempts):
            effective_model, use_grounding = attempts[index]
            remaining = _remaining_timeout(deadline)
            try:
                options: dict[str, Any] = {}
                if use_grounding:
                    options["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                    logger.info("Google検索グラウンディング: 有効")
                response = await generate_content(
                    self._client, model=effective_model, contents=contents, timeout=remaining, **options,
                )
                text = safe_response_text(response)
                if text is not None and contains_internal_response_text(text):
                    # Discord・読み上げ・会話記憶・ログへ内部形式を流さないよう、ここで応答ごと捨てる。
                    # 診断には本文・プロンプト・検索語を含めず、モデルと応答の構造だけを残す。
                    logger.warning("Gemini生成: 内部形式が混入した応答を破棄しました（検索: %s）。", "有効" if use_grounding else "無効")
                    if regenerations_left <= 0:
                        logger.error("Gemini生成: 再生成の上限に達したため、応答を返しません。")
                        return GenerationResult(None, None, [], None)
                    regenerations_left -= 1
                    # 同じ条件で生成し直す（index は進めない）。待ち時間は生成全体の残りから使う。
                    continue
                sources = self._grounding_sources(response) if use_grounding else []
                return GenerationResult(
                    text, getattr(response, "usage_metadata", None), sources,
                    effective_model if text is not None else None,
                )
            except asyncio.TimeoutError:
                raise
            except Exception as error:
                status_code = _error_status_code(error)
                if use_grounding and _grounding_is_unsupported(status_code, str(error), multimodal=bool(images)):
                    logger.warning("検索ツールが非対応のため、残り時間内で代替の生成を試します。", exc_info=error)
                    index += 1
                    continue
                if use_grounding and _grounding_quota_exhausted(status_code, error):
                    logger.warning("検索の利用枠を超えたため、残り時間内で代替の生成を試します。", exc_info=error)
                    index += 1
                    continue
                # 通信・認証・サーバー障害と、検索なしの生成の利用枠超過では追加のAPI呼び出しを行わない。
                logger.error("Gemini生成に失敗しました。", exc_info=error)
                return GenerationResult(None, None, [], None)
        return GenerationResult(None, None, [], None)

    @staticmethod
    def _grounding_sources(response: Any) -> list[dict[str, str]]:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return []
        metadata = getattr(candidates[0], "grounding_metadata", None)
        chunks = getattr(metadata, "grounding_chunks", None) or []
        sources = []
        for chunk in chunks:
            web = getattr(chunk, "web", None)
            sources.append({"title": getattr(web, "title", None), "uri": getattr(web, "uri", None)})
        return normalize_sources(sources)

    async def upload_file(self, file_path: Path) -> Any:
        # テキスト系はOSごとのMIME推定に任せず、モデルが受け取れるtext/plainへ揃える。
        mime_type = "application/pdf" if file_path.suffix.lower() == ".pdf" else "text/plain"
        return await asyncio.wait_for(
            self._client.aio.files.upload(
                file=file_path,
                config=types.UploadFileConfig(
                    mime_type=mime_type,
                    http_options=types.HttpOptions(
                        timeout=ceil(DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                ),
            ),
            timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
        )

    async def delete_file(self, uploaded_file: Any) -> None:
        uploaded_name = getattr(uploaded_file, "name", None)
        if not uploaded_name:
            return
        try:
            await asyncio.wait_for(
                self._client.aio.files.delete(
                    name=uploaded_name,
                    config=types.DeleteFileConfig(http_options=types.HttpOptions(
                        timeout=ceil(DOCUMENT_UPLOAD_TIMEOUT_SECONDS * 1000),
                        retry_options=types.HttpRetryOptions(attempts=1),
                    )),
                ),
                timeout=DOCUMENT_UPLOAD_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.error("Geminiファイル削除に失敗しました。", exc_info=error)

    async def summarize_document(
        self, uploaded_file: Any, document_name: str, model_name: str, *,
        on_usage: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        prompt = build_document_summary_prompt(document_name)
        response = await generate_json_content(
            generate_content, self._client,
            model=model_name,
            contents=[prompt, uploaded_file],
            timeout=DOCUMENT_SUMMARY_TIMEOUT_SECONDS,
        )
        if on_usage is not None:
            on_usage(getattr(response, "usage_metadata", None))
        _, _, parsed = read_json_response(response, required_keys=("summary",))
        if not parsed:
            raise ValueError("要約JSONを取得できませんでした。")
        summary = parsed.get("summary")
        summary_text = summary.strip() if isinstance(summary, str) else ""
        if not summary_text:
            raise ValueError("要約テキストが空です。")
        return {
            "document_name": document_name,
            "summary": summary_text,
            "key_points": parsed.get("key_points", []) if isinstance(parsed.get("key_points", []), list) else [],
            "warnings": parsed.get("warnings", []) if isinstance(parsed.get("warnings", []), list) else [],
            "usage_notes": parsed.get("usage_notes", []) if isinstance(parsed.get("usage_notes", []), list) else [],
        }
