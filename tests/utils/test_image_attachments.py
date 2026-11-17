"""画像の容量制限・ワーカーでの展開と中断時の回収を確認する。"""

import asyncio
import io
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PIL import Image

from persona_bot.core.background import spawn
from persona_bot.utils import discord_attachments
from tests.helpers.thread_probe import make_thread_probe


class ImageAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def attachment(self, data=b"fictional", *, size=None):
        return SimpleNamespace(
            filename="fictional.png", content_type="image/png",
            size=len(data) if size is None else size, read=AsyncMock(return_value=data),
        )

    @staticmethod
    def png(size):
        """小さな架空画像だけを圧縮し、実メモリを圧迫しない入力にする。"""
        with Image.new("RGBA", size) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            return buffer.getvalue()

    @staticmethod
    def header_image(size, *, image_format="PNG", thumbnail_size=None, draft_size=None):
        """画素領域を確保せず、ヘッダーの寸法と縮小結果だけを持つ入力を作る。"""
        source = MagicMock(size=size, format=image_format)
        source.__enter__.return_value = source

        def draft(*args):
            if draft_size is not None:
                source.size = draft_size

        def thumbnail(*args):
            if thumbnail_size is not None:
                source.size = thumbnail_size

        source.draft.side_effect = draft
        source.thumbnail.side_effect = thumbnail
        source.convert.return_value = Mock(size=thumbnail_size or size, mode="RGB")
        return source

    async def test_pixel_and_dimension_limits_reject_before_conversion(self):
        """圧縮サイズ内でも画素数・縦横の上限超過はRGB領域を作る前に拒否する。"""
        cases = (
            ("画素数", (4, 4), 4, 15),
            ("横幅", (4, 1), 3, 100),
            ("高さ", (1, 4), 3, 100),
            ("幅なし", (0, 2), 3, 100),
            ("高さなし", (2, 0), 3, 100),
        )
        for case, size, dimension, pixels in cases:
            with self.subTest(制限=case):
                source = MagicMock(size=size)
                source.__enter__.return_value = source
                with patch.object(discord_attachments.Image, "open", return_value=source) as opened, patch.multiple(
                    discord_attachments, MAX_IMAGE_SOURCE_DIMENSION=dimension, MAX_IMAGE_PIXELS=pixels,
                ):
                    self.assertEqual(await discord_attachments.load_image_attachments([self.attachment()]), [])
                source.convert.assert_not_called()
                source.draft.assert_not_called()
                source.thumbnail.assert_not_called()
                source.__exit__.assert_called_once()
                self.assertTrue(opened.call_args.args[0].closed)

    async def test_phone_photos_and_tall_screenshot_are_accepted_after_resize(self):
        """標準写真・4800万画素写真・縦長スクリーンショットを縮小して受け付ける。"""
        cases = (
            ("JPEG", (4032, 3024), (2048, 1536)),
            ("JPEG", (8064, 6048), (2048, 1536)),
            ("PNG", (1080, 8000), (276, 2048)),
        )
        for image_format, size, reduced in cases:
            with self.subTest(形式=image_format, 寸法=size):
                source = self.header_image(size, image_format=image_format, thumbnail_size=reduced)
                with patch.object(discord_attachments.Image, "open", return_value=source) as opened:
                    images = await discord_attachments.load_image_attachments([self.attachment()])
                self.addCleanup(discord_attachments.close_image_attachments, images)
                self.assertEqual([(image.mode, image.size) for image in images], [("RGB", reduced)])
                source.thumbnail.assert_called_once_with((2048, 2048))
                if image_format == "JPEG":
                    source.draft.assert_called_once_with("RGB", (2048, 1536))
                    names = [call[0] for call in source.mock_calls]
                    self.assertLess(names.index("draft"), names.index("thumbnail"))
                else:
                    source.draft.assert_not_called()
                source.convert.assert_called_once_with("RGB")
                source.__exit__.assert_called_once()
                self.assertTrue(opened.call_args.args[0].closed)

    def test_header_absolute_boundaries_are_format_specific(self):
        """形式別の画素数と元寸法の境界を、デコード・縮小より前に判定する。"""
        cases = (
            ("JPEG", (10000, 5000), True),
            ("JPEG", (10000, 5001), False),
            ("PNG", (4000, 3000), True),
            ("PNG", (4000, 3001), False),
            ("GIF", (4000, 3001), False),
            ("WEBP", (4000, 3001), False),
            ("JPEG", (16384, 1), True),
            ("PNG", (1, 16384), True),
            ("JPEG", (16385, 1), False),
            ("PNG", (1, 16385), False),
        )
        for image_format, size, accepted in cases:
            with self.subTest(形式=image_format, 寸法=size, 受付=accepted):
                source = self.header_image(size, image_format=image_format, thumbnail_size=(2, 1))
                with patch.object(discord_attachments.Image, "open", return_value=source) as opened:
                    if accepted:
                        image = discord_attachments._decode_image_bytes(b"fictional", 6)
                        self.assertEqual(image.size, (2, 1))
                        image.close()
                    else:
                        with self.assertRaisesRegex(ValueError, "寸法・画素数"):
                            discord_attachments._decode_image_bytes(b"fictional", 100)
                        source.draft.assert_not_called()
                        source.thumbnail.assert_not_called()
                        source.convert.assert_not_called()
                        source.load.assert_not_called()
                source.__exit__.assert_called_once()
                self.assertTrue(opened.call_args.args[0].closed)

    def test_jpeg_draft_uses_aspect_ratio_before_thumbnail_and_conversion(self):
        """縦長JPEGも短辺に沿ったdraft目標を渡し、縮小デコードの後でRGB変換する。"""
        source = self.header_image(
            (1000, 12000), image_format="JPEG", draft_size=(250, 3000), thumbnail_size=(171, 2048),
        )
        with patch.object(discord_attachments.Image, "open", return_value=source):
            image = discord_attachments._decode_image_bytes(b"fictional", 171 * 2048 * 3)
        source.draft.assert_called_once_with("RGB", (170, 2048))
        names = [call[0] for call in source.mock_calls]
        self.assertLess(names.index("draft"), names.index("thumbnail"))
        self.assertLess(names.index("thumbnail"), names.index("convert"))
        self.assertEqual(image.size, (171, 2048))
        image.close()

    def test_real_small_images_resize_and_remain_usable_after_source_closes(self):
        """小さな実画像でPillowの縮小・縦横比・RGB変換と入力解放を確認する。"""
        for image_format in ("JPEG", "PNG", "GIF", "WEBP"):
            with self.subTest(形式=image_format):
                with Image.new("RGB", (16, 8), (10, 20, 30)) as source, io.BytesIO() as buffer:
                    source.save(buffer, format=image_format)
                    data = buffer.getvalue()
                with patch.object(discord_attachments, "MAX_IMAGE_DIMENSION", 4):
                    image = discord_attachments._decode_image_bytes(data, 24)
                with image:
                    self.assertEqual((image.mode, image.size), ("RGB", (4, 2)))
                    image.getpixel((0, 0))

    async def test_cumulative_budget_counts_resized_images(self):
        """縮小前なら入らない画像も受け付け、縮小後の累計から残り容量を計算する。"""
        attachments = [self.attachment(self.png(size)) for size in ((8, 6), (8, 6), (2, 2))]
        original = discord_attachments._decode_image_bytes
        with patch.multiple(discord_attachments, MAX_IMAGE_DIMENSION=4, MAX_IMAGE_TOTAL_RGB_BYTES=48), patch.object(
            discord_attachments, "_decode_image_bytes", wraps=original,
        ) as decode:
            images = await discord_attachments.load_image_attachments(attachments)
        self.addCleanup(discord_attachments.close_image_attachments, images)
        self.assertEqual([image.size for image in images], [(4, 3), (2, 2)])
        self.assertEqual([call.args[1] for call in decode.call_args_list], [48, 12, 12])
        self.assertEqual(sum(image.width * image.height * 3 for image in images), 48)

    def test_resized_budget_uses_actual_dimensions_and_rejects_one_byte_short(self):
        """縮小の丸め後の実寸を使い、1バイト不足ならRGB変換前に入力を閉じる。"""
        source = self.header_image((1080, 8000), thumbnail_size=(277, 2048))
        with patch.object(discord_attachments.Image, "open", return_value=source) as opened:
            with self.assertRaisesRegex(ValueError, "合計容量"):
                discord_attachments._decode_image_bytes(b"fictional", 277 * 2048 * 3 - 1)
        source.thumbnail.assert_called_once()
        source.convert.assert_not_called()
        source.__exit__.assert_called_once()
        self.assertTrue(opened.call_args.args[0].closed)

    def test_resize_failure_closes_source_and_buffer(self):
        """縮小デコードや縮小そのものの失敗でも元画像と圧縮バッファを閉じる。"""
        for stage in ("draft", "thumbnail"):
            with self.subTest(失敗箇所=stage):
                source = self.header_image((4032, 3024), image_format="JPEG")
                getattr(source, stage).side_effect = OSError("架空の縮小失敗")
                with patch.object(discord_attachments.Image, "open", return_value=source) as opened:
                    with self.assertRaisesRegex(OSError, "架空の縮小失敗"):
                        discord_attachments._decode_image_bytes(b"fictional", 100)
                source.convert.assert_not_called()
                source.__exit__.assert_called_once()
                self.assertTrue(opened.call_args.args[0].closed)

    async def test_rgb_budget_rejects_before_conversion_and_closes_input(self):
        """1枚だけでも残り予算を1バイト超えれば変換せず入力を閉じる。"""
        source = MagicMock(size=(2, 3))
        source.__enter__.return_value = source
        with patch.object(discord_attachments.Image, "open", return_value=source) as opened:
            with self.assertRaisesRegex(ValueError, "合計容量"):
                discord_attachments._decode_image_bytes(b"fictional", 17)
        source.convert.assert_not_called()
        source.__exit__.assert_called_once()
        self.assertTrue(opened.call_args.args[0].closed)

    async def test_all_image_limits_accept_exact_boundary(self):
        """圧縮サイズ・画素数・寸法・枚数・RGB合計の境界値は受け付ける。"""
        data = self.png((2, 3))
        attachments = [self.attachment(data), self.attachment(data)]
        with patch.multiple(
            discord_attachments, MAX_IMAGE_SIZE_BYTES=len(data), MAX_IMAGE_PIXELS=6,
            MAX_IMAGE_DIMENSION=3, MAX_IMAGE_ATTACHMENTS=2, MAX_IMAGE_TOTAL_RGB_BYTES=36,
        ):
            images = await discord_attachments.load_image_attachments(attachments)
        self.addCleanup(discord_attachments.close_image_attachments, images)
        self.assertEqual([(image.mode, image.size) for image in images], [("RGB", (2, 3))] * 2)
        self.assertEqual(sum(image.width * image.height * 3 for image in images), 36)
        for attachment in attachments:
            attachment.read.assert_awaited_once()

    async def test_cumulative_budget_includes_previous_images(self):
        """個別には安全な画像でも保持済み画像と合計し、残り予算内の後続だけを読む。"""
        attachments = [self.attachment(self.png(size)) for size in ((2, 3), (2, 3), (1, 2))]
        original = discord_attachments._decode_image_bytes
        with patch.multiple(discord_attachments, MAX_IMAGE_PIXELS=6, MAX_IMAGE_TOTAL_RGB_BYTES=24), patch.object(
            discord_attachments, "_decode_image_bytes", wraps=original,
        ) as decode:
            images = await discord_attachments.load_image_attachments(attachments)
        self.addCleanup(discord_attachments.close_image_attachments, images)
        self.assertEqual([image.size for image in images], [(2, 3), (1, 2)])
        self.assertEqual([call.args[1] for call in decode.call_args_list], [24, 6, 6])

    async def test_exhausted_budget_skips_later_downloads(self):
        """累計予算が尽きた時点で後続画像の取得も止める。"""
        attachments = [self.attachment(self.png((2, 3))) for _ in range(2)]
        with patch.object(discord_attachments, "MAX_IMAGE_TOTAL_RGB_BYTES", 18):
            images = await discord_attachments.load_image_attachments(attachments)
        self.addCleanup(discord_attachments.close_image_attachments, images)
        self.assertEqual(len(images), 1)
        attachments[1].read.assert_not_awaited()

    async def test_attachment_count_skips_later_downloads(self):
        """画像以外を数えず、画像の受付枚数を超えた分は取得しない。"""
        attachments = [self.attachment(self.png((1, 1))) for _ in range(4)]
        attachments[0].filename, attachments[0].content_type = "架空.txt", "text/plain"
        with patch.object(discord_attachments, "MAX_IMAGE_ATTACHMENTS", 2):
            images = await discord_attachments.load_image_attachments(attachments)
        self.addCleanup(discord_attachments.close_image_attachments, images)
        self.assertEqual(len(images), 2)
        attachments[0].read.assert_not_awaited()
        attachments[-1].read.assert_not_awaited()

    async def test_failed_images_also_consume_attempt_limit(self):
        """不正画像や過大な申告でも試行枚数を消費し、取得を繰り返さない。"""
        too_large = self.attachment(size=discord_attachments.MAX_IMAGE_SIZE_BYTES + 1)
        invalid = self.attachment()
        later = self.attachment(self.png((1, 1)))
        with patch.object(discord_attachments, "MAX_IMAGE_ATTACHMENTS", 2):
            self.assertEqual(await discord_attachments.load_image_attachments([too_large, invalid, later]), [])
        too_large.read.assert_not_awaited()
        invalid.read.assert_awaited_once()
        later.read.assert_not_awaited()

    async def test_conversion_failure_closes_source_and_preserves_held_image(self):
        """変換失敗の入力を閉じ、成功した画像は受取側へ所有権を渡す。"""
        first = MagicMock(size=(2, 2))
        second = MagicMock(size=(2, 2))
        for source in (first, second):
            source.__enter__.return_value = source
        converted = Image.new("RGB", (2, 2))
        self.addCleanup(converted.close)
        first.convert.return_value = converted
        second.convert.side_effect = OSError("架空の変換失敗")
        with patch.object(discord_attachments.Image, "open", side_effect=[first, second]) as opened:
            images = await discord_attachments.load_image_attachments([self.attachment(), self.attachment()])
        self.assertEqual(images, [converted])
        converted.getpixel((0, 0))
        for source in (first, second):
            source.__exit__.assert_called_once()
        self.assertTrue(all(call.args[0].closed for call in opened.call_args_list))
        discord_attachments.close_image_attachments(images)
        with self.assertRaises(ValueError):
            converted.getpixel((0, 0))

    async def test_rejection_after_success_keeps_accepted_image_open(self):
        """上限超過した後続画像を拒否しても先行画像を勝手に閉じない。"""
        with patch.multiple(discord_attachments, MAX_IMAGE_PIXELS=4, MAX_IMAGE_DIMENSION=2):
            images = await discord_attachments.load_image_attachments([
                self.attachment(self.png((2, 2))), self.attachment(self.png((3, 2))),
            ])
        self.assertEqual(len(images), 1)
        images[0].getpixel((0, 0))
        discord_attachments.close_image_attachments(images)
        with self.assertRaises(ValueError):
            images[0].getpixel((0, 0))

    async def test_download_cancellation_closes_previously_decoded_images(self):
        """次の画像取得中のキャンセルでも取得済み画像を回収する。"""
        first = Image.new("RGB", (2, 2))
        later = self.attachment()
        later.read.side_effect = asyncio.CancelledError
        with patch.object(discord_attachments, "_decode_image_bytes", return_value=first):
            with self.assertRaises(asyncio.CancelledError):
                await discord_attachments.load_image_attachments([self.attachment(), later])
        with self.assertRaises(ValueError):
            first.getpixel((0, 0))

    async def test_open_failure_closes_input_buffer(self):
        """ヘッダーを開けなかった場合も圧縮データの入力バッファを閉じる。"""
        with patch.object(discord_attachments.Image, "open", side_effect=OSError("架空の読込失敗")) as opened:
            with self.assertRaises(OSError):
                discord_attachments._decode_image_bytes(b"fictional", 100)
        self.assertTrue(opened.call_args.args[0].closed)

    def test_cleanup_skips_noncallable_close_and_continues_after_failure(self):
        first = Mock(close=Mock(side_effect=OSError("架空の解放失敗")))
        last = Image.new("RGB", (2, 2))
        discord_attachments.close_image_attachments([first, object(), SimpleNamespace(close=None), last])
        first.close.assert_called_once()
        with self.assertRaises(ValueError):
            last.getpixel((0, 0))

    async def test_declared_oversize_is_not_downloaded(self):
        attachment = self.attachment(size=discord_attachments.MAX_IMAGE_SIZE_BYTES + 1)
        self.assertEqual(await discord_attachments.load_image_attachments([attachment]), [])
        attachment.read.assert_not_awaited()

    async def test_downloaded_oversize_is_not_decoded(self):
        attachment = self.attachment(b"12345", size=1)
        with patch.object(discord_attachments, "MAX_IMAGE_SIZE_BYTES", 4), patch.object(
            discord_attachments, "_decode_image_bytes",
        ) as decode:
            self.assertEqual(await discord_attachments.load_image_attachments([attachment]), [])
        attachment.read.assert_awaited_once()
        decode.assert_not_called()

    async def test_boundary_image_is_rgb_and_decoded_without_blocking_loop(self):
        with Image.new("RGBA", (2, 3), (10, 20, 30, 40)) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            data = buffer.getvalue()
        operation, threads = make_thread_probe(None)
        original = discord_attachments._decode_image_bytes

        def decode(image_bytes, remaining_rgb_bytes):
            operation()
            return original(image_bytes, remaining_rgb_bytes)

        with patch.object(discord_attachments, "MAX_IMAGE_SIZE_BYTES", len(data)), patch.object(
            discord_attachments, "_decode_image_bytes", side_effect=decode,
        ):
            images = await discord_attachments.load_image_attachments([self.attachment(data)])
        self.assertEqual(len(images), 1)
        self.addCleanup(images[0].close)
        self.assertEqual((images[0].mode, images[0].size), ("RGB", (2, 3)))
        self.assertEqual(images[0].getpixel((0, 0)), (10, 20, 30))
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_invalid_and_failed_downloads_do_not_hide_later_images(self):
        failed = self.attachment()
        failed.read.side_effect = OSError("架空の取得失敗")
        with Image.new("RGB", (1, 1)) as source, io.BytesIO() as buffer:
            source.save(buffer, format="PNG")
            valid = self.attachment(buffer.getvalue())
        images = await discord_attachments.load_image_attachments([failed, self.attachment(), valid])
        self.assertEqual(len(images), 1)
        images[0].close()

    async def test_non_image_is_not_downloaded(self):
        attachment = self.attachment()
        attachment.filename, attachment.content_type = "fictional.txt", "text/plain"
        self.assertEqual(await discord_attachments.load_image_attachments([attachment]), [])
        attachment.read.assert_not_awaited()

    async def test_repeated_cancellation_waits_for_decode_and_closes_all_images(self):
        loop = asyncio.get_running_loop()
        entered, release = asyncio.Event(), threading.Event()
        first, second = Mock(size=(2, 2)), Mock(size=(2, 2))
        decoded = []

        def decode(data, remaining_rgb_bytes):
            if decoded:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(3):
                    raise AssertionError("画像展開の再開を待てませんでした。")
                return second
            decoded.append(True)
            return first

        with patch.object(discord_attachments, "_decode_image_bytes", side_effect=decode):
            task = spawn(discord_attachments.load_image_attachments([
                self.attachment(), self.attachment(),
            ]), label="画像展開のキャンセル検証")
            try:
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        first.close.assert_called_once()
        second.close.assert_called_once()
