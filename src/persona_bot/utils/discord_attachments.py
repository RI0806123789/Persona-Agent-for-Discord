"""添付ファイルの判定と、画像の縮小デコード・解放（`discord_helpers.py` から分けたモジュール。#468）。

`discord_helpers.py` は、従来の import 先として全部を再公開する窓口として残している。
新しいコードは、必要なものだけを読み込めるこのモジュールから直接 import してよい。
"""



import io
import logging
from pathlib import Path
from typing import Any, Sequence

import discord
from PIL import Image

from persona_bot.config.constants import (
    IMAGE_EXTENSIONS,
    IMAGE_RGB_BYTES_PER_PIXEL,
    MAX_IMAGE_ATTACHMENTS,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_JPEG_PIXELS,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIZE_BYTES,
    MAX_IMAGE_SOURCE_DIMENSION,
    MAX_IMAGE_TOTAL_RGB_BYTES,
    SUPPORTED_DOCUMENT_EXTENSIONS,
)
from persona_bot.core.background import run_in_thread

# 運用ログの出元は分割前と同じ `persona_bot.utils.discord_helpers`。
logger = logging.getLogger("persona_bot.utils.discord_helpers")


def is_supported_document_attachment(attachment: discord.Attachment) -> bool:
    return Path(attachment.filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def attachment_is_image(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type or ""
    suffix = Path(attachment.filename).suffix.lower()
    return content_type.startswith("image/") or suffix in IMAGE_EXTENSIONS


def describe_supported_attachments(*, documents: bool = True) -> str:
    """対応している添付の拡張子を、利用者向けの案内文として返す。

    案内に拡張子を直書きすると、受け付ける形式を変えたときに文言だけが古いまま残るため、
    判定に使っている IMAGE_EXTENSIONS / SUPPORTED_DOCUMENT_EXTENSIONS から組み立てる
    （集合は順序を持たないので、表示順を固定するために並べ替える）。
    """
    def join(extensions: set[str]) -> str:
        return " / ".join(sorted(extension.lstrip(".") for extension in extensions))

    text = f"画像（{join(IMAGE_EXTENSIONS)}）"
    if documents:
        text += f"と文書（{join(SUPPORTED_DOCUMENT_EXTENSIONS)}）"
    return text


def _decode_image_bytes(image_bytes: bytes, remaining_rgb_bytes: int) -> Image.Image:
    """ヘッダーで絶対上限を確認し、縮小・RGB変換した画像の所有権を渡す。"""
    with io.BytesIO(image_bytes) as buffer, Image.open(buffer) as image:
        width, height = image.size
        pixels = width * height
        max_pixels = MAX_IMAGE_JPEG_PIXELS if image.format == "JPEG" else MAX_IMAGE_PIXELS
        if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_SOURCE_DIMENSION or pixels > max_pixels:
            raise ValueError("画像の寸法・画素数が上限を超えているため見送ります。")
        if image.format == "JPEG":
            # アスペクト比に沿った目標を渡し、縦長・横長でも縮小デコードを効かせる。
            scale = min(1.0, MAX_IMAGE_DIMENSION / max(width, height))
            image.draft("RGB", (max(1, int(width * scale)), max(1, int(height * scale))))
        image.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION))
        width, height = image.size
        if width * height * IMAGE_RGB_BYTES_PER_PIXEL > remaining_rgb_bytes:
            raise ValueError("画像の展開後の合計容量が上限を超えるため見送ります。")
        return image.convert("RGB")


async def load_image_attachments(attachments: Sequence[discord.Attachment]) -> list[Image.Image]:
    image_objects: list[Image.Image] = []
    attempted = 0
    total_rgb_bytes = 0
    try:
        for attachment in attachments:
            if not attachment_is_image(attachment):
                continue
            if attempted >= MAX_IMAGE_ATTACHMENTS or total_rgb_bytes >= MAX_IMAGE_TOTAL_RGB_BYTES:
                break
            # 失敗した画像も枚数に数え、取得・デコードの試行自体を制限する。
            attempted += 1
            if attachment.size > MAX_IMAGE_SIZE_BYTES:
                logger.warning('画像読み込み: サイズ上限を超えているため見送ります。')
                continue
            try:
                image_bytes = await attachment.read()
                if len(image_bytes) > MAX_IMAGE_SIZE_BYTES:
                    logger.warning('画像読み込み: 取得した画像がサイズ上限を超えているため見送ります。')
                    continue
                image = await run_in_thread(_decode_image_bytes, image_bytes, MAX_IMAGE_TOTAL_RGB_BYTES - total_rgb_bytes)
                image_objects.append(image)
                width, height = image.size
                total_rgb_bytes += width * height * IMAGE_RGB_BYTES_PER_PIXEL
            except Exception as error:
                logger.error(f'画像読み込みエラー: {type(error).__name__}', exc_info=error)
        return image_objects
    except BaseException:
        # 中断時に受取先がなくなる画像も回収する。処理中の画像はrun_in_threadが回収する。
        close_image_attachments(image_objects)
        raise


def close_image_attachments(images: Sequence[Any]) -> None:
    """呼出元が所有する画像をすべて回収し、元の例外やキャンセルを保つ。"""
    for image in images:
        try:
            close = getattr(image, "close", None)
            if callable(close):
                close()
        except Exception as error:
            logger.error(f'画像の解放に失敗しました: {type(error).__name__}', exc_info=error)
