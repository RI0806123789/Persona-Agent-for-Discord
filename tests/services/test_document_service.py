"""文書応答の感情タグ除去と失敗の返信、一時ファイルの回収、pypdf が無い環境での読み込みを検証する。"""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona_bot.config import paths
from persona_bot.core.background import spawn
from persona_bot.core.app_state import BotState
from persona_bot.services import document_service
from persona_bot.services.document_service import DocumentService, DocumentUserError
from tests.helpers.persona_helpers import FICTIONAL_PERSONAS, install_persona_catalog


class DocumentTemporaryFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        original = tempfile.NamedTemporaryFile
        factory = patch.object(
            document_service.tempfile,
            "NamedTemporaryFile",
            side_effect=lambda **kwargs: original(dir=self.root, **kwargs),
        )
        factory.start()
        self.addCleanup(factory.stop)
        self.uploaded = SimpleNamespace(name="fictional-upload")
        self.gemini = SimpleNamespace(
            upload_file=AsyncMock(return_value=self.uploaded),
            summarize_document=AsyncMock(return_value={"summary": "架空の要約"}),
            delete_file=AsyncMock(),
        )
        self.memory = SimpleNamespace(store_document_summary=AsyncMock())
        self.service = DocumentService(None, None, self.memory, self.gemini, None, None)
        self.attachment = SimpleNamespace(
            filename="fictional.txt", size=10, read=AsyncMock(return_value=b"fictional text")
        )

    async def process(self):
        return await self.service._process_attachment(self.attachment, "test-user", "test-model")

    def assert_no_files(self) -> None:
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_read_failure_removes_temporary_file(self) -> None:
        self.attachment.read.side_effect = OSError("架空の読み込み失敗")
        with self.assertRaisesRegex(OSError, "読み込み失敗"):
            await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_actual_download_size_is_checked_and_temporary_file_is_removed(self) -> None:
        self.attachment.read.return_value = b"12345"
        self.attachment.size = 1
        with patch.object(document_service, "MAX_DOCUMENT_SIZE_BYTES", 4), patch.object(Path, "write_bytes") as write:
            with self.assertRaisesRegex(DocumentUserError, "上限"):
                await self.process()
        self.assert_no_files()
        write.assert_not_called()
        self.gemini.upload_file.assert_not_awaited()

    async def test_actual_download_at_size_limit_is_accepted(self) -> None:
        self.attachment.read.return_value = b"1234"
        self.attachment.size = 1
        with patch.object(document_service, "MAX_DOCUMENT_SIZE_BYTES", 4):
            await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_awaited_once()

    async def test_summary_records_tokens_without_conversation_snapshot(self) -> None:
        usage = SimpleNamespace(total_token_count=17)
        self.service._usage = SimpleNamespace(log_tokens=Mock(), log_snapshot=Mock())

        async def summarize(*args, on_usage):
            on_usage(usage)
            return {"summary": "架空の要約"}

        self.gemini.summarize_document.side_effect = summarize
        result = await self.process()
        self.service._usage.log_tokens.assert_called_once_with("文書要約", usage)
        self.service._usage.log_snapshot.assert_not_called()
        self.assertNotIn("usage", result)

    async def test_partial_write_failure_removes_temporary_file(self) -> None:
        original = Path.write_bytes

        def fail_write(path, data):
            original(path, data[:2])
            raise OSError("架空の書き込み失敗")

        with patch.object(Path, "write_bytes", fail_write):
            with self.assertRaisesRegex(OSError, "書き込み失敗"):
                await self.process()
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_read_cancellation_removes_temporary_file(self) -> None:
        self.attachment.read.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.process()
        self.assert_no_files()

    async def check_write_cancellation(self, cancel_count: int) -> None:
        original = Path.write_bytes
        started = asyncio.Event()
        release = threading.Event()
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()

        def delayed_write(path, data):
            loop.call_soon_threadsafe(started.set)
            try:
                if not release.wait(timeout=5):
                    raise TimeoutError("テストの書き込み待機が終了しませんでした")
                return original(path, data)
            finally:
                loop.call_soon_threadsafe(finished.set)

        with patch.object(Path, "write_bytes", delayed_write):
            task = spawn(self.process(), label="一時ファイルのキャンセル検証")
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                for _ in range(cancel_count):
                    task.cancel()
                    await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                await asyncio.wait_for(finished.wait(), timeout=5)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_write_cancellation_does_not_recreate_file(self) -> None:
        await self.check_write_cancellation(1)

    async def test_repeated_write_cancellation_does_not_recreate_file(self) -> None:
        await self.check_write_cancellation(2)

    async def test_success_passes_file_to_upload_and_removes_it(self) -> None:
        async def upload(path):
            self.assertEqual(path.read_bytes(), b"fictional text")
            return self.uploaded

        self.gemini.upload_file.side_effect = upload
        result = await self.process()
        self.assert_no_files()
        self.assertEqual(result["summary"], "架空の要約")
        self.assertEqual(len(result["document_hash"]), 64)
        self.memory.store_document_summary.assert_not_awaited()
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)

    async def test_validation_failure_removes_temporary_file(self) -> None:
        self.attachment.read.return_value = b"\xff"
        with self.assertRaisesRegex(DocumentUserError, "UTF-8") as raised:
            await self.process()
        self.assertIsInstance(raised.exception.__cause__, UnicodeDecodeError)
        self.assert_no_files()
        self.gemini.upload_file.assert_not_awaited()

    async def test_upload_failure_removes_temporary_file(self) -> None:
        self.gemini.upload_file.side_effect = OSError("架空のアップロード失敗")
        with self.assertRaisesRegex(OSError, "アップロード失敗"):
            await self.process()
        self.assert_no_files()

    async def test_summary_failure_removes_remote_and_local_files(self) -> None:
        self.gemini.summarize_document.side_effect = OSError("架空の要約失敗")
        with self.assertRaisesRegex(OSError, "要約失敗"):
            await self.process()
        self.assert_no_files()
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)

    async def test_remote_cleanup_failure_still_removes_local_file(self) -> None:
        self.gemini.delete_file.side_effect = OSError("架空のアップロード先削除失敗")
        with self.assertRaisesRegex(OSError, "アップロード先削除失敗"):
            await self.process()
        self.assert_no_files()

    async def test_repeated_cancellation_waits_for_remote_cleanup_before_local_removal(self) -> None:
        summarizing, deleting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def summarize(*args, **kwargs):
            summarizing.set()
            await asyncio.Event().wait()

        async def delete(*args):
            deleting.set()
            await release.wait()

        self.gemini.summarize_document.side_effect = summarize
        self.gemini.delete_file.side_effect = delete
        task = spawn(self.process(), label="文書削除中の再キャンセルの確認")
        try:
            await asyncio.wait_for(summarizing.wait(), 2)
            task.cancel()
            await asyncio.wait_for(deleting.wait(), 2)
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(len(list(self.root.iterdir())), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        self.gemini.delete_file.assert_awaited_once_with(self.uploaded)
        self.assert_no_files()

    async def test_pdf_validation_cancellation_waits_for_stream_to_close_before_removal(self) -> None:
        self.attachment.filename = "fictional.pdf"
        entered = asyncio.Event()
        release = threading.Event()
        streams = []
        loop = asyncio.get_running_loop()

        def read_pdf(stream):
            streams.append(stream)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(timeout=5):
                raise TimeoutError("PDF検証の待機が終了しませんでした")
            return SimpleNamespace(is_encrypted=False)

        with patch.dict(sys.modules, {"pypdf": SimpleNamespace(PdfReader=read_pdf)}):
            task = spawn(self.process(), label="PDF検証キャンセルの確認")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(streams[0].closed)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(streams[0].closed)
        self.assert_no_files()


class DocumentResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.memory = SimpleNamespace(
            store_document_summaries=AsyncMock(),
            route_category=AsyncMock(return_value="none"),
            build_contexts=AsyncMock(return_value=("", "")),
            append_pending_log=AsyncMock(),
            flush_if_needed=AsyncMock(),
        )
        self.gemini = SimpleNamespace(generate=AsyncMock())
        self.moderator = SimpleNamespace(check=AsyncMock(return_value=(True, "")))
        self.usage = SimpleNamespace(log_snapshot=Mock())
        self.is_blocked = Mock(return_value=False)
        self.service = DocumentService(
            SimpleNamespace(user=SimpleNamespace(id=99)),
            BotState(), self.memory, self.gemini, self.usage, None,
            moderator=self.moderator,
            is_blocked=self.is_blocked,
        )
        self.message = SimpleNamespace(
            id=30,
            content="文書を要約してください。",
            guild=SimpleNamespace(id=10),
            channel=SimpleNamespace(id=20),
            attachments=[SimpleNamespace(filename="fictional.txt")],
            reply=AsyncMock(),
        )
        self.service._fetch_message_from_task = AsyncMock(return_value=self.message)
        self.service._process_attachment = AsyncMock(return_value={
            "document_name": "fictional.txt", "summary": "架空の文書要約",
        })
        self.service._update_task = AsyncMock()
        self.sent = AsyncMock()
        self.tasks = []

        def start(coro, **kwargs):
            task = spawn(coro, **kwargs)
            self.tasks.append(task)
            return task

        for name, value in (
            ("send_long_reply", self.sent),
            ("fetch_channel_history_text", AsyncMock(return_value="架空の履歴")),
            ("load_persona_template", AsyncMock(return_value="架空のペルソナ")),
            ("spawn", start),
        ):
            replacement = patch.object(document_service, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def asyncTearDown(self) -> None:
        await asyncio.gather(*self.tasks)

    async def process_response(self, text: str, sources=None) -> None:
        self.gemini.generate.return_value = (text, None, sources or [])
        # 受付時は on_message が検査済みの本文を question として保存する（実際の流れと揃える）。
        await self.service._process_task(
            {"task_id": "fictional-task", "author_id": "test-user", "question": self.message.content},
        )

    def assert_clean_response(self, expected: str, *, sources: bool = False) -> None:
        self.sent.assert_awaited_once()
        self.assertEqual(self.sent.await_args.args[0], self.message)
        reply = self.sent.await_args.args[1]
        self.assertNotIn("[V:", reply)
        self.assertEqual(self.sent.await_args.kwargs, {"suppress_embeds": sources})
        if not sources:
            self.assertEqual(reply, expected)
        self.memory.append_pending_log.assert_awaited_once_with(
            "test-user", "文書を要約してください。", expected
        )
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "completed")
        self.message.reply.assert_not_awaited()

    async def test_edited_message_body_is_not_sent_to_generation(self) -> None:
        # 受付後に編集された本文は検査されていないので、受付時の質問で回答する（#203）。
        for stored, expected in (("文書を要約してください。", "文書を要約してください。"),
                                 ("", document_service.DOCUMENT_ONLY_QUESTION)):
            with self.subTest(stored=stored):
                self.gemini.generate.reset_mock()
                self.memory.route_category.reset_mock()
                self.message.content = "受付後に編集した架空の本文"
                self.gemini.generate.return_value = ("要約です。", None, [])
                await self.service._process_task(
                    {"task_id": "fictional-task", "author_id": "test-user", "question": stored},
                )
                prompt = self.gemini.generate.await_args.args[0]
                self.assertIn(expected, prompt)
                self.assertNotIn("受付後に編集した架空の本文", prompt)
                self.memory.route_category.assert_awaited_once_with(expected)
                # 元の投稿は履歴からも除く（編集後の本文が履歴経由で入らないように）。
                history = document_service.fetch_channel_history_text.await_args
                self.assertEqual(history.kwargs["exclude_message_id"], self.message.id)
                self.assertIs(history.kwargs["is_blocked"], self.is_blocked)

    async def test_affinity_tag_is_removed_before_screening_and_storage(self) -> None:
        await self.process_response("要約です。[V:1, A:2]")
        self.assert_clean_response("要約です。")
        self.assertEqual(self.moderator.check.await_args.args[0], "要約です。")
        self.assertEqual(self.moderator.check.await_count, 2)
        self.assertEqual(self.usage.log_snapshot.call_args.args[4], "要約です。")

    async def test_usage_is_recorded_when_the_response_is_rejected_or_has_only_tags(self) -> None:
        """生成に成功したら、後段で却下・空判定になってもトークンを記録する（#432）。"""
        self.moderator.check.side_effect = [(True, ""), (False, "架空の不適切判定")]
        await self.process_response("架空の不適切な回答")
        self.usage.log_snapshot.assert_called_once()
        self.usage.log_snapshot.reset_mock()
        self.moderator.check.side_effect = None
        self.moderator.check.return_value = (True, "")
        await self.process_response("[V:1, A:2]")
        self.usage.log_snapshot.assert_called_once()

    async def test_multiple_tags_with_spaces_are_removed(self) -> None:
        await self.process_response("[V: +1, A: -2]要約です。[V: -3, A: +4]")
        self.assert_clean_response("要約です。")

    async def test_response_without_tags_is_preserved(self) -> None:
        await self.process_response("**要点**\n- 架空の文書の内容です。")
        self.assert_clean_response("**要点**\n- 架空の文書の内容です。")

    async def test_rejected_response_is_sent_without_memory_or_sources_and_completes(self) -> None:
        self.moderator.check.side_effect = [(True, ""), (False, "架空の不適切判定")]
        await self.process_response("架空の不適切な回答", [{"title": "架空の資料", "uri": "https://example.com/source"}])
        self.sent.assert_awaited_once()
        self.assertEqual(self.sent.call_args.args[1], "申し訳ありませんが、適切な応答を生成できませんでした。")
        self.memory.append_pending_log.assert_not_awaited()
        self.memory.store_document_summaries.assert_not_awaited()
        self.memory.flush_if_needed.assert_not_awaited()
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "completed")
        self.message.reply.assert_not_awaited()

    async def test_reference_links_are_sent_but_not_saved_in_memory(self) -> None:
        await self.process_response(
            "要約です。[V:1, A:2]",
            [{"title": "参考資料", "uri": "https://example.com/reference"}],
        )
        self.assert_clean_response("要約です。", sources=True)
        self.assertEqual(
            self.sent.await_args.args[1],
            "要約です。\n\n🔍 **参考リンク:**\n・ [参考資料](<https://example.com/reference>)",
        )

    async def check_tag_only_response(self, sources=None) -> None:
        await self.process_response(" [V:1, A:2] ", sources)
        self.sent.assert_not_awaited()
        self.memory.append_pending_log.assert_not_awaited()
        self.moderator.check.assert_awaited_once()
        self.assertIn("架空の文書要約", self.moderator.check.await_args.args[0])
        self.memory.store_document_summaries.assert_not_awaited()
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "failed")
        self.message.reply.assert_awaited_once()
        self.assertTrue(self.message.reply.await_args.args[0].strip())
        self.assertNotIn("[V:", self.message.reply.await_args.args[0])

    async def test_tag_only_response_reports_failure_without_sending_empty_text(self) -> None:
        await self.check_tag_only_response()

    async def test_tag_only_response_does_not_send_links_without_body(self) -> None:
        await self.check_tag_only_response([{"uri": "https://example.com/reference"}])

    async def test_moderation_replacement_is_sent_without_saving_the_apology(self) -> None:
        self.moderator.check.return_value = (False, "架空の判定理由")
        await self.process_response("架空の応答。[V:1, A:2]")
        self.assertEqual(self.sent.call_args.args[1], "申し訳ありませんが、適切な応答を生成できませんでした。")
        self.memory.append_pending_log.assert_not_awaited()
        self.memory.flush_if_needed.assert_not_awaited()
        self.assertEqual(self.service._update_task.await_args.kwargs["status"], "completed")

    # --- 失敗の返信に内部の情報を出さない（#220） ---

    # 公開の返信へ出てはいけない架空の接続情報
    FICTIONAL_CONNECTION = "postgresql://fictional-user:fictional-pass@db.invalid:5432/bot"

    def failure_reply(self) -> str:
        self.sent.assert_not_awaited()
        self.message.reply.assert_awaited_once()
        # 利用者のファイル名を載せることがあるため、返信先の本人以外へは通知させない。
        self.assertIs(self.message.reply.await_args.kwargs["allowed_mentions"], document_service.REPLY_ALLOWED_MENTIONS)
        return self.message.reply.await_args.args[0]

    def saved_error(self) -> str:
        update = self.service._update_task.await_args
        self.assertEqual(update.kwargs["status"], "failed")
        return update.kwargs["error"]

    async def test_persona_read_failure_does_not_publish_internal_path(self) -> None:
        # 実際の読込経路（load_persona_template → read_text）で FileNotFoundError を起こす。
        install_persona_catalog(self)
        self.service._state.get_guild(10).current_prompt_file = FICTIONAL_PERSONAS[0]
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "fictional-internal-dir" / "missing-persona.txt"
            with patch.object(document_service, "load_persona_template", paths.load_persona_template), \
                    patch.object(paths, "resolve_prompt_path", return_value=missing):
                with self.assertLogs(document_service.logger, level="ERROR") as captured:
                    await self.process_response("使われない回答")
        reply = self.failure_reply()
        self.assertEqual(reply, document_service.DOCUMENT_FAILURE_NOTICE)
        for leaked in (directory, "fictional-internal-dir", "missing-persona.txt", "Errno", "FileNotFoundError"):
            self.assertNotIn(leaked, reply)
        # 原因の調査に要る診断は、内部の失敗記録にだけ残る。
        self.assertIn("FileNotFoundError", self.saved_error())
        self.assertIn("missing-persona.txt", self.saved_error())
        for record in captured.records:
            self.assertNotIn(directory, record.getMessage())
            self.assertNotIn("missing-persona.txt", record.getMessage())
        self.gemini.generate.assert_not_awaited()

    async def test_unexpected_errors_including_plain_value_error_get_fixed_notice(self) -> None:
        # 一般の ValueError も公開してよい検証エラーとはみなさない。
        for error in (
            ValueError(f"架空の内部エラー: {self.FICTIONAL_CONNECTION}"),
            RuntimeError(f"架空の接続失敗: {self.FICTIONAL_CONNECTION}"),
            OSError("架空の読込失敗: /srv/fictional-bot/data/memory/bot.db"),
        ):
            with self.subTest(error=type(error).__name__):
                self.message.reply.reset_mock()
                self.service._update_task.reset_mock()
                self.memory.build_contexts.side_effect = error
                with self.assertLogs(document_service.logger, level="ERROR") as captured:
                    await self.process_response("使われない回答")
                reply = self.failure_reply()
                self.assertEqual(reply, document_service.DOCUMENT_FAILURE_NOTICE)
                self.assertNotIn("fictional", reply)
                self.assertEqual(self.saved_error(), f"{type(error).__name__}: {error}")
                self.assertIs(captured.records[0].exc_info[1], error)
                for record in captured.records:
                    self.assertNotIn("fictional", record.getMessage())
                    self.assertNotIn(str(error), record.getMessage())

    async def test_gemini_failure_reply_is_fixed_notice(self) -> None:
        await self.process_response(None)
        self.assertEqual(self.failure_reply(), document_service.DOCUMENT_FAILURE_NOTICE)
        self.assertIn("Gemini", self.saved_error())

    async def test_user_fixable_errors_are_explained_without_internal_details(self) -> None:
        encrypted_pdf = {"pypdf": SimpleNamespace(PdfReader=lambda stream: SimpleNamespace(is_encrypted=True))}
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, encrypted_pdf):
            internal = Path(directory) / "fictional-internal-dir"
            internal.mkdir()
            invalid_text, pdf = internal / "fictional.txt", internal / "fictional.pdf"
            invalid_text.write_bytes(b"\xff\xfe fictional")
            pdf.write_bytes(b"%PDF fictional")

            def validate(validator, path):
                return lambda: setattr(self.service._process_attachment, "side_effect", lambda *args: validator(path))

            for name, prepare, expected in (
                # 実際の検証関数が出す文面をそのまま返信へ載せる。
                ("文字コード", validate(document_service.validate_text_file, invalid_text), "UTF-8 で保存し直して"),
                ("暗号化", validate(document_service.validate_pdf_file, pdf), "PDFが暗号化されています"),
                ("添付なし", lambda: setattr(self.message, "attachments", []), "対応する文書の添付が見つかりませんでした"),
            ):
                with self.subTest(name=name):
                    self.message.reply.reset_mock()
                    self.service._update_task.reset_mock()
                    self.service._process_attachment.side_effect = None
                    self.message.attachments = [SimpleNamespace(filename="fictional.txt")]
                    prepare()
                    await self.process_response("使われない回答")
                    reply = self.failure_reply()
                    self.assertTrue(reply.startswith(document_service.DOCUMENT_FAILURE_NOTICE + "\n詳細: "))
                    self.assertIn(expected, reply)
                    for leaked in (directory, "fictional-internal-dir", "codec", "UnicodeDecodeError"):
                        self.assertNotIn(leaked, reply)

    async def test_worker_continues_when_failure_save_and_notice_both_fail(self) -> None:
        self.service._update_task.side_effect = OSError("架空の状態保存失敗")
        self.message.reply.side_effect = OSError("架空の返信失敗")
        self.memory.build_contexts.side_effect = RuntimeError("架空の内部エラー")
        self.gemini.generate.return_value = ("使われない回答", None, [])
        pending = [{"task_id": f"fictional-task-{i}", "author_id": "test-user", "question": "架空の質問"}
                   for i in range(2)]
        outcomes = []
        original = self.service._process_task

        async def claim():
            if pending:
                return pending.pop(0)
            self.service._stopping = True
            return None

        async def process(task):
            try:
                await original(task)
            except BaseException as error:
                outcomes.append(error)
                raise
            outcomes.append(None)

        self.service._recover_stuck_tasks = AsyncMock()
        self.service._claim_next_task = AsyncMock(side_effect=claim)
        self.service._process_task = process
        with patch.object(document_service, "DOCUMENT_WORKER_IDLE_SECONDS", 0):
            await asyncio.wait_for(self.service.run_worker(), 2)
        # 保存と通知の失敗をタスク内で処理し、ワーカーまで例外を漏らさず次のタスクへ進む。
        self.assertEqual(outcomes, [None, None])
        self.assertEqual(self.message.reply.await_count, 2)
        self.assertEqual(self.service._update_task.await_count, 2)


class OptionalPdfDependencyTests(unittest.TestCase):
    """pypdf が無くても文書処理を読み込め、PDF だけが理由の分かる形で失敗する（#57）。"""

    # sys.modules の値を None にすると、その名前の import は ModuleNotFoundError になる
    WITHOUT_PYPDF = {"pypdf": None, "pypdf.errors": None}

    def load_without_pypdf(self):
        # import 済みのモジュールを壊さないよう、同じソースから別名の複製を作って読み込む
        spec = importlib.util.spec_from_file_location("document_service_without_pypdf", document_service.__file__)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, self.WITHOUT_PYPDF):
            spec.loader.exec_module(module)
        return module

    def test_module_is_importable_without_pypdf(self) -> None:
        module = self.load_without_pypdf()
        self.assertTrue(callable(module.DocumentService))

    def test_pdf_validation_reports_missing_pypdf(self) -> None:
        module = self.load_without_pypdf()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.pdf"
            path.write_bytes(b"%PDF-1.4 fictional")
            with patch.dict(sys.modules, self.WITHOUT_PYPDF):
                with self.assertRaisesRegex(ValueError, "pypdf") as raised:
                    module.validate_pdf_file(path)
        # 利用者には直せないサーバー側の不足なので、返信へ載せる検証エラーにはしない（#220）。
        self.assertNotIsInstance(raised.exception, module.DocumentUserError)

    def test_text_validation_does_not_need_pypdf(self) -> None:
        module = self.load_without_pypdf()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fictional.txt"
            path.write_text("架空の文書です。", encoding="utf-8")
            with patch.dict(sys.modules, self.WITHOUT_PYPDF):
                module.validate_text_file(path)



class DocumentQueueAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """受付が利用者別の上限で断られたらタスクを作らずに None を返すことを確認する（#202）。"""

    async def asyncSetUp(self) -> None:
        from persona_bot.core.database import Database
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = Database(Path(directory.name) / "bot.db")
        self.addCleanup(lambda: self.db.close())
        self.db.initialize()
        self.service = DocumentService(None, BotState(), None, None, None, self.db)

    def message(self, message_id: int, author_id: int = 11) -> SimpleNamespace:
        return SimpleNamespace(id=message_id, channel=SimpleNamespace(id=10), author=SimpleNamespace(id=author_id))

    async def test_enqueue_returns_none_without_task_when_user_limit_is_reached(self) -> None:
        with patch.object(document_service, "DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER", 1):
            self.assertIsNotNone(await self.service.enqueue_from_message(self.message(20), "架空の質問", [object()]))
            self.assertIsNone(await self.service.enqueue_from_message(self.message(21), "架空の質問", [object()]))
            self.assertIsNotNone(await self.service.enqueue_from_message(self.message(22, author_id=12), "架空の質問", [object()]))
        claimed = [self.db.queue_claim_next_pending("2026-01-01T00:00:00") for _ in range(3)]
        self.assertEqual([task["message_id"] for task in claimed if task], [20, 22])


if __name__ == "__main__":
    unittest.main()
