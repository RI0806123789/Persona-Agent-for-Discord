import logging
import asyncio
import hashlib
import codecs
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import BotState
from persona_bot.core.background import run_in_thread, spawn, wait_for_cleanup
from persona_bot.config.constants import (
    DOCUMENT_FAILURE_NOTICE,
    DOCUMENT_MEMORY_CONTEXT_LIMIT,
    DOCUMENT_ONLY_QUESTION,
    DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER,
    DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL,
    DOCUMENT_WORKER_IDLE_SECONDS,
    GEMINI_RESPONSE_TIMEOUT_SECONDS,
    HISTORY_LIMIT,
    MAX_DOCUMENT_SIZE_BYTES,
    QUEUE_MAX_FINISHED_TASKS,
    TEXT_VALIDATION_PREVIEW_BYTES,
)
from persona_bot.services.content_moderator import ContentModerator, screen_bot_response_with_status
from persona_bot.core.database import Database
from persona_bot.utils.discord_helpers import (
    REPLY_ALLOWED_MENTIONS,
    clean_message_content,
    fetch_channel_history_text,
    format_grounding_sources,
    is_supported_document_attachment,
    is_supported_message_channel,
    send_long_reply,
)
from persona_bot.utils.timeutil import utc_now_iso
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.memory_service import MemoryService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_persona_template
from persona_bot.services.prompting import build_full_prompt, build_memory_sections
from persona_bot.utils.usage_graph import UsageTracker

logger = logging.getLogger(__name__)


# 処理が終わった（もう再処理しない）タスクのステータス
_TERMINAL_STATUSES = {"completed", "failed"}


class DocumentUserError(ValueError):
    """利用者が対処でき、文面をそのまま失敗の返信へ載せてよい文書処理の失敗（#220）。

    返信は公開チャンネルに出るため、文面は固定の案内と利用者自身の添付のファイル名だけで作り、
    例外の本文・内部のパス・接続先を含めないこと。これ以外の例外（一般の ValueError を含む）は
    内容に関わらず定型文だけを返し、本文は失敗記録（queue_tasks.error）にだけ残す。
    運用ログには共通formatterで例外の型と発生位置だけを残す。
    """


def build_failure_notice(error: BaseException) -> str:
    """失敗の返信の本文を作る。DocumentUserError 以外の例外の文面は載せない。"""
    if isinstance(error, DocumentUserError):
        return f"{DOCUMENT_FAILURE_NOTICE}\n詳細: {error}"
    return DOCUMENT_FAILURE_NOTICE


def describe_error(error: BaseException) -> str:
    """保護された失敗記録へ残す診断（例外の型と本文）。返信や運用ログには使わない。"""
    return f"{type(error).__name__}: {error}"


class DocumentService:
    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        memory: MemoryService,
        gemini: GeminiService,
        usage: UsageTracker,
        db: Database,
        ng_filter: NgWordFilter | None = None,
        moderator: ContentModerator | None = None,
        *,
        is_blocked: Callable[[int], bool] | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._memory = memory
        self._gemini = gemini
        self._usage = usage
        self._db = db
        self._ng_filter = ng_filter
        self._moderator = moderator
        self._is_blocked = is_blocked
        self._worker: asyncio.Task[Any] | None = None
        self._stopping = False

    def start_worker(self) -> None:
        """終了時に回収できるよう、このサービスのワーカーだけを保持する。"""
        if not self._stopping and (self._worker is None or self._worker.done()):
            self._worker = spawn(self.run_worker(), label="文書処理ワーカー")

    async def stop_worker(self) -> None:
        """Gemini・Discord・DBが有効な間に、中断とファイル回収を完了する。"""
        self._stopping = True
        await wait_for_cleanup(self._stop_worker(), label="文書ワーカーの終了")

    async def _stop_worker(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            finally:
                self._worker = None

    async def enqueue_from_message(
        self,
        message: discord.Message,
        question: str,
        attachments: Sequence[discord.Attachment],
    ) -> str | None:
        """文書タスクを受け付けてタスクIDを返す。未処理件数の上限に達していれば追加せず None を返す。"""
        now = datetime.now(timezone.utc)
        # 取り出し順は ORDER BY created_at, task_id で決まり、task_id の先頭はチャンネルID。
        # 秒精度にすると同じ1秒に届いた別チャンネルの文書が到着順に処理されなくなるため、
        # この列だけはマイクロ秒まで残す（他の時刻は timeutil.utc_now_iso の秒精度）。
        now_iso = now.isoformat(timespec="microseconds")
        task_id = f"{message.channel.id}-{message.id}-{int(now.timestamp() * 1000)}"
        result = await self._enqueue(
            {
                "task_id": task_id,
                "status": "pending",
                "channel_id": message.channel.id,
                "message_id": message.id,
                "author_id": message.author.id,
                "question": question,
                "attachment_count": len(attachments),
                "created_at": now_iso,
                "updated_at": now_iso,
            }
        )
        if result != "accepted":
            scope = "利用者別" if result == "user_limit" else "全体"
            logger.warning("文書キュー: 処理待ちの上限（%s）に達したため、新しい文書を受け付けませんでした。", scope)
            return None
        return task_id

    async def run_worker(self) -> None:
        # 起動時の復旧が完了してから通常処理へ進む。成功後は再実行しない。
        while not self._stopping:
            try:
                await self._recover_stuck_tasks()
            except Exception as error:
                logger.warning("文書ワーカー: 中断タスクの復旧に失敗しました。待機後に再試行します。", exc_info=error)
                await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)
            else:
                break

        while not self._stopping:
            # キューの読み書きで予期しない例外（一時的な DB ロック等）が
            # 発生してもワーカーが停止しないよう、ループ全体を保護してリトライする。
            try:
                task = await self._claim_next_task()
                if task is None:
                    await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)
                    continue
                await self._process_task(task)
            except Exception as error:
                logger.error("文書ワーカー: 予期しないエラーが発生しました。", exc_info=error)
                await asyncio.sleep(DOCUMENT_WORKER_IDLE_SECONDS)

    async def _recover_stuck_tasks(self) -> None:
        """再起動前に "processing" のまま中断されたタスクを "pending" に戻す。

        これがないと、処理中に Bot が落ちたタスクは二度と拾われず放置される。
        """
        now_iso = utc_now_iso()
        recovered = await run_in_thread(self._db.queue_recover_stuck, now_iso)
        if recovered:
            logger.info("文書ワーカー: 中断された processing タスク %d 件を pending に戻しました。", recovered)

    async def _enqueue(self, task: dict[str, Any]) -> str:
        # 件数の確認と追加は DB 側で1つのトランザクションにまとめる（並行した受付でも上限を超えない）。
        return await run_in_thread(
            self._db.queue_try_enqueue, task,
            DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER, DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL,
        )

    async def _update_task(self, task_id: str, **updates: Any) -> None:
        now_iso = utc_now_iso()
        await run_in_thread(self._db.queue_update_task, task_id, updates, now_iso)
        # 完了/失敗タスクが際限なく溜まらないよう、終了時に古いものから削除する
        if updates.get("status") in _TERMINAL_STATUSES:
            try:
                await run_in_thread(self._db.queue_prune_finished, QUEUE_MAX_FINISHED_TASKS)
            except Exception as error:
                # 終端状態の保存は成功済み。掃除の失敗で二重返信やfailedへの転落を起こさない。
                logger.error("文書ワーカー: 終了タスクの掃除に失敗しました。", exc_info=error)

    async def _claim_next_task(self) -> dict[str, Any] | None:
        now_iso = utc_now_iso()
        return await run_in_thread(self._db.queue_claim_next_pending, now_iso)

    async def _process_task(self, task: dict[str, Any]) -> None:
        task_id = str(task.get("task_id", ""))
        message: discord.Message | None = None

        try:
            message = await self._fetch_message_from_task(task)
            attachments = [
                attachment
                for attachment in message.attachments
                if is_supported_document_attachment(attachment)
            ]
            if not attachments:
                raise DocumentUserError("対応する文書の添付が見つかりませんでした。")

            # 文書タスクは投稿者の記憶スペースに紐付けて処理する（ユーザー別記憶）。
            author_id = str(task.get("author_id") or message.author.id)
            if author_id == "0":
                author_id = str(message.author.id)
            # 質問は、受付時に NG 判定と入力モデレーションを通した保存済みの値だけを使う（#203）。
            # 再取得した投稿の本文は、受付後に編集されていても誰も検査していないため生成へ渡さない。
            # 添付は編集で外すことしかできず差し替えられないので、再取得した一覧を使ってよい。
            question = str(task.get("question", "")).strip()
            if clean_message_content(message, self._client.user) != question:
                logger.info("文書タスク: 受付後に本文が編集されたため、受付時に検査した質問で回答します。")
            question = question or DOCUMENT_ONLY_QUESTION

            # ペルソナ/モデル/グラウンディングはメッセージが投稿されたサーバー（DM ならDM共有設定）のものを使う。
            guild_id = message.guild.id if message.guild is not None else None
            guild_settings = await self._state.get_guild_async(guild_id)
            response_settings = guild_settings.snapshot_response()

            prompt = await load_persona_template(response_settings.prompt_file)
            summaries = [
                await self._process_attachment(attachment, author_id, response_settings.model_name)
                for attachment in attachments
            ]
            summaries = [self._mask_summary_data(summary) for summary in summaries]
            summary_notice, summaries_accepted = await screen_bot_response_with_status(
                json.dumps(summaries, ensure_ascii=False), None, self._moderator, log_label="文書要約",
            )
            if not summaries_accepted:
                await wait_for_cleanup(
                    self._send_and_complete(task_id, message, summary_notice, [], author_id,
                                            question, summary_notice, False),
                    label="文書返信の送信と完了記録",
                )
                return

            memory_category = await self._memory.route_category(question)
            memory_context, document_context = await self._memory.build_contexts(
                memory_category, author_id, limit=max(DOCUMENT_MEMORY_CONTEXT_LIMIT, len(attachments)),
                pending_summaries=summaries,
            )
            # 元の投稿は履歴から除く。受付後に編集された本文が履歴経由でプロンプトへ入ると、
            # 保存済みの質問だけを使っても入力検査を迂回できてしまう（#203）。
            history_text = await fetch_channel_history_text(
                message.channel,
                guild_settings.channel_reset_points.get(message.channel.id),
                self._client.user,
                HISTORY_LIMIT,
                exclude_message_id=message.id,
                is_blocked=self._is_blocked,
            )
            full_prompt = build_full_prompt(
                prompt,
                build_memory_sections(memory_category, memory_context, document_context),
                history_text,
                question,
            )
            response_text, usage_info, grounding_sources = await self._gemini.generate(
                full_prompt,
                response_settings.model_name,
                timeout=GEMINI_RESPONSE_TIMEOUT_SECONDS,
                grounding=response_settings.grounding_enabled,
            )

            if not response_text:
                raise ValueError("Gemini APIの呼び出しに失敗しました。")

            # 文書経路では親密度を更新せず、共通関数で感情タグだけを除去する。
            response_text = strip_affinity_tag(response_text)

            # 生成に成功した時点で消費したトークンを記録する。タグ除去後に空になった場合や
            # 後段のモデレーションで却下された場合も、Gemini を呼んだ分は統計に含める（#432）。
            self._usage.log_snapshot(
                "文書タスク",
                question,
                f"文書添付: {len(attachments)}件",
                full_prompt,
                response_text,
                usage_info,
            )

            # タグのみの応答は、空本文や参考リンクだけを送らず生成失敗として扱う。
            if not response_text:
                raise ValueError("Gemini APIの応答に本文がありませんでした。")

            response_text, accepted = await screen_bot_response_with_status(
                response_text, self._ng_filter, self._moderator, log_label="文書応答"
            )
            if accepted:
                await self._memory.store_document_summaries(author_id, summaries)

            # 参考リンクは送信用テキストにのみ追記する（長期記憶にはノイズになるため含めない）
            reply_text = response_text
            if accepted and grounding_sources:
                reply_text += format_grounding_sources(grounding_sources, self._ng_filter)

            # DiscordとDBは同じトランザクションにできない。送信開始を先に記録し、
            # 強制終了で配送結果が不明なら自動再送を止める。通常のキャンセルは記録開始から
            # 送信・完了保存までを一括で保護し、すべて終えてから伝える。
            await wait_for_cleanup(
                self._send_and_complete(task_id, message, reply_text, grounding_sources, author_id,
                                        question, response_text, accepted),
                label="文書返信の送信と完了記録",
            )
        except Exception as error:
            if self._stopping:
                # 終了中の中断は processing のまま残し、次回起動時に復旧する。
                return
            # 診断本文は保護された失敗記録だけに残し、運用ログと公開の返信には含めない。
            diagnosis = describe_error(error)
            logger.error("文書タスク処理エラーが発生しました。", exc_info=error)
            try:
                await self._update_task(
                    task_id,
                    status="failed",
                    finished_at=utc_now_iso(),
                    error=diagnosis,
                )
            except Exception as update_error:
                logger.error("文書タスク: 失敗状態の保存に失敗しました。", exc_info=update_error)
            if message is not None:
                try:
                    # 返信に載せるファイル名は利用者の入力なので、返信先の本人以外へは通知させない。
                    notice = build_failure_notice(error)
                    if self._ng_filter is not None:
                        notice = self._ng_filter.mask_with_log(notice, label="文書タスクの失敗通知")
                    await message.reply(notice, allowed_mentions=REPLY_ALLOWED_MENTIONS)
                except Exception as reply_error:
                    logger.error("文書タスク失敗通知に失敗しました。", exc_info=reply_error)
            return

    def _mask_summary_data(self, value: Any) -> Any:
        """要約と補足項目を保存前にマスクし、JSONのキーと構造は保つ。"""
        if isinstance(value, str) and self._ng_filter is not None:
            return self._ng_filter.mask_with_log(value, label="文書要約")
        if isinstance(value, list):
            return [self._mask_summary_data(item) for item in value]
        if isinstance(value, dict):
            # ハッシュは本文ではなく重複排除の識別子なので、元の指紋を保持する。
            return {key: item if key == "document_hash" else self._mask_summary_data(item)
                    for key, item in value.items()}
        return value

    async def _send_and_complete(
        self, task_id: str, message: discord.Message, reply_text: str, grounding_sources: list,
        author_id: str, question: str, response_text: str, accepted: bool,
    ) -> None:
        await self._update_task(task_id, status="sending")
        await send_long_reply(message, reply_text, suppress_embeds=bool(grounding_sources))
        # 回答は送信済み。完了記録を先に試み、保存の失敗を再送や二重のエラー返信へ変えない。
        try:
            await self._update_task(task_id, status="completed", finished_at=utc_now_iso())
        except Exception as error:
            logger.error("文書タスク: 回答後の完了状態の保存に失敗しました。", exc_info=error)
        try:
            if accepted:
                await self._memory.append_pending_log(author_id, question, response_text)
                spawn(self._memory.flush_if_needed(), label="記憶圧縮（文書処理便乗）")
        except Exception as error:
            logger.error("文書タスク: 回答後の記憶追記に失敗しました。", exc_info=error)

    async def _fetch_message_from_task(self, task: dict[str, Any]) -> discord.Message:
        channel_id = int(task.get("channel_id", 0))
        message_id = int(task.get("message_id", 0))
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)
        # on_message 側で受け付けるチャンネル種別（DM 含む）と判定を揃える
        if not is_supported_message_channel(channel):
            raise ValueError("文書処理はメッセージ取得に対応したチャンネルでのみ実行できます。")
        return await channel.fetch_message(message_id)

    async def _process_attachment(
        self,
        attachment: discord.Attachment,
        author_id: str,
        model_name: str,
    ) -> dict[str, Any]:
        if attachment.size > MAX_DOCUMENT_SIZE_BYTES:
            raise DocumentUserError(f"ファイルサイズが上限を超えています: {attachment.filename}")

        suffix = Path(attachment.filename).suffix.lower()
        temp_path: Path | None = None
        uploaded_file: Any = None
        try:
            temp_path = await self._download_to_temp_file(attachment, suffix)
            await validate_document_file(temp_path, suffix)

            uploaded_file = await self._gemini.upload_file(temp_path)
            summary_data = await self._gemini.summarize_document(
                uploaded_file, attachment.filename, model_name, on_usage=self._record_summary_usage,
            )
            summary_data["document_hash"] = await run_in_thread(self._document_hash, temp_path)
            return summary_data
        finally:
            try:
                if uploaded_file is not None:
                    await wait_for_cleanup(
                        self._gemini.delete_file(uploaded_file), label="文書アップロードの削除",
                    )
            finally:
                # アップロード先の削除が失敗しても、ローカルの回収を実行する。
                if temp_path is not None:
                    self._remove_temp_file(temp_path)

    @staticmethod
    def _document_hash(path: Path) -> str:
        """ファイル内容だけの指紋を、同期ワーカーで算出する。"""
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _record_summary_usage(self, usage_info: Any) -> None:
        """要約内容をログへ出さず、トークン数のみ記録する。"""
        if self._usage is None or usage_info is None:
            return
        try:
            self._usage.log_tokens("文書要約", usage_info)
        except Exception as error:
            logger.warning("文書要約のトークン記録に失敗しました: %s", type(error).__name__)

    async def _download_to_temp_file(self, attachment: discord.Attachment, suffix: str) -> Path:
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_path = Path(temp_file.name)
        downloaded = False
        try:
            temp_file.close()
            temp_bytes = await attachment.read()
            if len(temp_bytes) > MAX_DOCUMENT_SIZE_BYTES:
                raise DocumentUserError(f"ファイルサイズが上限を超えています: {attachment.filename}")
            # 書き込み完了まで待ち、キャンセル時も回収後の再作成を防ぐ。
            await run_in_thread(temp_path.write_bytes, temp_bytes)
            downloaded = True
            return temp_path
        finally:
            # 正常に返した時点で呼び出し側へ所有権を渡す。それまではここで回収する。
            if not downloaded:
                self._remove_temp_file(temp_path)

    @staticmethod
    def _remove_temp_file(temp_path: Path) -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as error:
            logger.error("文書タスク: 一時ファイルの削除に失敗しました。", exc_info=error)


def validate_pdf_file(temp_path: Path) -> None:
    # pypdf は PDF の検証にしか使わないため、ここで読み込む。モジュールの先頭で import すると、
    # pypdf の無い環境では起動・通常のチャット・回帰テストの検出まで ImportError で止まる。
    try:
        from pypdf import PdfReader
    except ImportError as error:
        # 利用者には対処できないサーバー側の不足なので返信には載せない（定型文になる。#220）。
        # ログと失敗記録で原因が分かるよう、英語の例外ではなく原因が分かる文にする。
        raise ValueError("PDFの検証に必要な pypdf がインストールされていません。") from error
    with temp_path.open("rb") as file:
        reader = PdfReader(file)
        if reader.is_encrypted:
            raise DocumentUserError("PDFが暗号化されています。パスワードを外してから送ってください。")


def validate_text_file(temp_path: Path) -> None:
    with open(temp_path, "rb") as file:
        preview_bytes = file.read(TEXT_VALIDATION_PREVIEW_BYTES)
    # プレビューの末尾がマルチバイト文字（日本語は3バイト）の途中になり得るため、
    # 途中で切れた場合は末尾の不完全な文字を許容してデコード検証する。
    # 単純な decode() だと正常な日本語ファイルが境界位置次第で拒否される。
    truncated = len(preview_bytes) >= TEXT_VALIDATION_PREVIEW_BYTES
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        decoder.decode(preview_bytes, final=not truncated)
    except UnicodeDecodeError as error:
        # 英語のデコードエラーの文面ではなく、利用者が直せる案内を返す（元の例外は __cause__ に残る）。
        raise DocumentUserError(
            "テキストを UTF-8 として読み取れませんでした。UTF-8 で保存し直してから送ってください。"
        ) from error


async def validate_document_file(temp_path: Path, suffix: str) -> None:
    if suffix == ".pdf":
        await run_in_thread(validate_pdf_file, temp_path)
    else:
        await run_in_thread(validate_text_file, temp_path)
