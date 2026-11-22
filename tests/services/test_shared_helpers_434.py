"""#434 で集約した共通ヘルパー（選択肢の絞り込み・ペルソナ読込・Gemini エラー判定ほか）の確認。"""
import asyncio
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np

from persona_bot.audio import live_audio
from persona_bot.config.paths import load_persona_or_default, load_prompt_template
from persona_bot.core.background import TaskOwner
from persona_bot.services.genai_client import error_status_code, is_quota_exhausted
from persona_bot.services.live_session import is_quota_error
from persona_bot.utils import graph_rendering
from persona_bot.utils.select_options import limit_keeping_selected
from persona_bot.views.view_parts import OwnedView


class LimitKeepingSelectedTests(unittest.TestCase):
    def test_short_lists_are_returned_unchanged(self) -> None:
        self.assertEqual(limit_keeping_selected([1, 2, 3], lambda item: item == 3, 5), [1, 2, 3])

    def test_selected_item_beyond_the_limit_replaces_the_last_slot(self) -> None:
        self.assertEqual(limit_keeping_selected(list(range(10)), lambda item: item == 8, 4), [0, 1, 2, 8])

    def test_selected_item_inside_the_limit_is_not_moved(self) -> None:
        self.assertEqual(limit_keeping_selected(list(range(10)), lambda item: item == 1, 4), [0, 1, 2, 3])

    def test_missing_selection_and_repeated_use_are_stable(self) -> None:
        once = limit_keeping_selected(list(range(10)), lambda item: item == 9, 4)
        self.assertEqual(limit_keeping_selected(once, lambda item: item == 9, 4), once)
        self.assertEqual(limit_keeping_selected(list(range(10)), lambda item: item == 99, 4), [0, 1, 2, 3])

    def test_zero_limit_is_empty(self) -> None:
        self.assertEqual(limit_keeping_selected([1, 2], lambda item: True, 0), [])


class LoadPersonaOrDefaultTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_loaded_text(self) -> None:
        loader = AsyncMock(return_value="架空のペルソナ")
        result = await load_persona_or_default(
            "persona.txt", logger=logging.getLogger("test"), message="警告", loader=loader,
        )
        self.assertEqual(result, "架空のペルソナ")
        loader.assert_awaited_once_with("persona.txt")

    async def test_missing_file_warns_with_the_callers_logger_and_returns_default(self) -> None:
        for default in (None, ""):
            with self.subTest(default=default), self.assertLogs("test.caller", level="WARNING") as logs:
                result = await load_persona_or_default(
                    "persona.txt", logger=logging.getLogger("test.caller"), message="架空の警告",
                    loader=AsyncMock(side_effect=FileNotFoundError("persona.txt")), default=default,
                )
            self.assertEqual(result, default)
            self.assertEqual(logs.records[0].getMessage(), "架空の警告")

    async def test_unreadable_file_is_treated_like_a_missing_one(self) -> None:
        """権限がない・UTF-8 でない（Shift_JIS 保存など）ファイルも、見つからないときと同じく見送る（#496）。"""
        for error in (PermissionError("架空"), IsADirectoryError("架空"), UnicodeDecodeError("utf-8", b"\x82", 0, 1, "架空")):
            with self.subTest(error=type(error).__name__), self.assertLogs("test.caller", level="WARNING") as logs:
                result = await load_persona_or_default(
                    "persona.txt", logger=logging.getLogger("test.caller"), message="架空の警告",
                    loader=AsyncMock(side_effect=error), default=None,
                )
            self.assertIsNone(result)
            self.assertEqual(logs.records[0].getMessage(), "架空の警告")
            self.assertNotIn("persona.txt", logs.records[0].getMessage())

    async def test_shift_jis_persona_file_is_not_readable_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "persona.txt"
            path.write_bytes("あなたは架空のキャラクターです。".encode("cp932"))

            async def loader(_name: str) -> str:
                return load_prompt_template(path)

            with self.assertLogs("test.caller", level="WARNING"):
                result = await load_persona_or_default(
                    "persona.txt", logger=logging.getLogger("test.caller"), message="架空の警告", loader=loader,
                    default="標準",
                )
        self.assertEqual(result, "標準")

    async def test_errors_other_than_reading_the_file_propagate(self) -> None:
        with self.assertRaises(RuntimeError):
            await load_persona_or_default(
                "persona.txt", logger=logging.getLogger("test"), message="警告",
                loader=AsyncMock(side_effect=RuntimeError()),
            )


class ErrorStatusTests(unittest.TestCase):
    def test_status_is_read_from_code_status_code_and_response(self) -> None:
        self.assertEqual(error_status_code(SimpleNamespace(code=429)), 429)
        self.assertEqual(error_status_code(SimpleNamespace(code=None, status_code="503")), 503)
        self.assertEqual(error_status_code(SimpleNamespace(response=SimpleNamespace(status_code=401))), 401)
        self.assertIsNone(error_status_code(SimpleNamespace(code=True)))
        self.assertIsNone(error_status_code(SimpleNamespace(code=1011)))
        self.assertEqual(error_status_code(SimpleNamespace(code=1011), max_code=4999), 1011)

    def test_quota_uses_status_first_and_text_only_without_one(self) -> None:
        self.assertTrue(is_quota_exhausted(SimpleNamespace(code=429)))
        self.assertFalse(is_quota_exhausted(SimpleNamespace(code=500, status="RESOURCE_EXHAUSTED")))
        self.assertTrue(is_quota_exhausted(RuntimeError("RESOURCE_EXHAUSTED: quota")))
        self.assertTrue(is_quota_exhausted(RuntimeError("HTTP 429 too many")))
        self.assertFalse(is_quota_exhausted(RuntimeError("HTTP 4290 unrelated")))

    def test_live_keeps_its_wider_quota_words_and_websocket_codes(self) -> None:
        self.assertTrue(is_quota_error(SimpleNamespace(code=429)))
        self.assertTrue(is_quota_error(SimpleNamespace(code=1011, message="You exceeded your current quota")))
        self.assertFalse(is_quota_error(SimpleNamespace(code=1007, message="invalid argument")))


class PlotFigureTests(unittest.TestCase):
    def test_figure_is_closed_even_when_drawing_fails(self) -> None:
        import matplotlib.pyplot as plt

        before = set(plt.get_fignums())
        with self.assertRaises(RuntimeError):
            with graph_rendering.plot_figure(figsize=(2, 2)) as (figure, _axis):
                self.assertNotIn(figure.number, before)
                raise RuntimeError("架空の描画失敗")
        self.assertEqual(set(plt.get_fignums()), before)

    def test_figure_is_closed_after_a_normal_save(self) -> None:
        import matplotlib.pyplot as plt

        before = set(plt.get_fignums())
        with graph_rendering.plot_figure(figsize=(2, 2)) as (figure, _axis):
            buffer = graph_rendering.save_figure_to_buffer(figure)
        buffer.close()
        self.assertEqual(set(plt.get_fignums()), before)


class DownsamplerTests(unittest.TestCase):
    def test_filter_taps_are_shared_and_read_only(self) -> None:
        first, second = live_audio.Downsampler(), live_audio.Downsampler()
        self.assertIs(first._taps, second._taps)
        self.assertFalse(first._taps.flags.writeable)

    def test_non_integer_ratio_is_rejected(self) -> None:
        with patch.object(live_audio, "LIVE_INPUT_SAMPLE_RATE", 44100):
            with self.assertRaises(ValueError):
                live_audio.Downsampler()

    def test_output_is_unchanged_by_the_shared_taps(self) -> None:
        pcm = (np.sin(np.arange(960) / 7) * 8000).astype(np.int16)
        stereo = np.repeat(pcm, 2).tobytes()
        self.assertEqual(len(live_audio.Downsampler().process(stereo)), 320 * 2)


class HistoryTextTests(unittest.TestCase):
    def test_messages_without_text_or_attachments_leave_no_empty_line(self) -> None:
        from persona_bot.utils.discord_helpers import build_history_text

        user = SimpleNamespace(id=1, bot=False)
        messages = [
            SimpleNamespace(author=user, content="こんにちは", attachments=[]),
            SimpleNamespace(author=user, content="   ", attachments=[]),
            SimpleNamespace(author=user, content="", attachments=[SimpleNamespace(filename="a.pdf", content_type="application/pdf")]),
        ]
        text = build_history_text(messages, SimpleNamespace(id=99))
        self.assertEqual(text.splitlines(), ["ユーザーの質問: こんにちは", "ユーザーの質問:  [添付ファイルあり]"])


class OwnedViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_stopping_owner_rejects_new_dispatch(self) -> None:
        owner = TaskOwner()
        view = OwnedView(timeout=None, task_owner=owner)
        self.addCleanup(view.stop)
        self.assertFalse(view.is_owner_stopping)
        owner.stopping = True
        self.assertTrue(view.is_owner_stopping)
        self.assertIsNone(view._dispatch_item(SimpleNamespace(), SimpleNamespace(data={})))

    async def test_run_owned_without_owner_just_awaits(self) -> None:
        view = OwnedView(timeout=None, task_owner=None)
        self.addCleanup(view.stop)

        async def work() -> int:
            return 7

        self.assertEqual(await view.run_owned(work()), 7)

    async def test_nested_owner_run_keeps_the_outer_task_tracked(self) -> None:
        owner = TaskOwner()
        inner_seen: list[bool] = []

        async def inner() -> None:
            inner_seen.append(asyncio.current_task() in owner._tasks)

        async def outer() -> None:
            await owner.run(inner())
            # 内側の run が終わっても、外側の追跡は外れない。
            inner_seen.append(asyncio.current_task() in owner._tasks)

        await asyncio.create_task(owner.run(outer()))
        self.assertEqual(inner_seen, [True, True])
        self.assertEqual(owner._tasks, {})


if __name__ == "__main__":
    unittest.main()
