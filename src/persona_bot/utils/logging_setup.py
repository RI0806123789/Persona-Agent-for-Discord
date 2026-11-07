"""運用ログの形式と、例外・識別子を出力する境界をまとめる。"""

from copy import copy
import logging
from pathlib import Path
import re
import sys
import traceback
from types import TracebackType

from persona_bot.config.constants import LOG_DATE_FORMAT, LOG_FORMAT

_IDENTIFIER = re.compile(
    r"\b(guild(?:_id)?|channel(?:_id)?|user_id)(\s*=\s*)(?:[0-9]+|<[^>\r\n]+>)"
)
_DISCORD_ID = re.compile(r"(?<![0-9])[0-9]{17,20}(?![0-9])")


class OperationalFormatter(logging.Formatter):
    """識別子と例外本文を既定ログへ載せず、型と発生位置を残す。"""

    def format(self, record: logging.LogRecord) -> str:
        # 別のハンドラや assertLogs が受け取るレコードを変更しない。
        safe = copy(record)
        if record.name == "__main__" or record.name.startswith("persona_bot."):
            safe.msg = record.getMessage()
        else:
            # SDKは例外やスタックを本文・引数へ埋め込む場合がある。
            # getMessage()や引数のstr()を呼ぶ前に自由文を捨て、発生位置だけ残す。
            safe.msg = f"外部ライブラリのログを受信しました（{Path(record.pathname).name}:{record.lineno}）。"
        safe.msg = _IDENTIFIER.sub(r"\1\2<ID>", safe.msg)
        safe.msg = _DISCORD_ID.sub("<ID>", safe.msg)
        safe.args = ()
        # 先に別のハンドラが整形した例外本文も再利用しない。
        safe.exc_text = None
        safe.stack_info = None
        return super().format(safe)

    def formatException(self, exc_info) -> str:
        # SDK等の例外本文はキー・URL・会話を含み得るため出さない。
        # 障害調査に必要な例外型・モジュールのファイル名・行番号だけを残す。
        # 呼出元が作った不正な exc_info（3要素でない・型でない・トレースバックでない）でも、
        # 整形自体を失敗させてログ行ごと失わない（#399）。型でない値は、中身を出さないよう
        # str() せず型名だけを示す。
        try:
            kind, _, tb = exc_info
        except (TypeError, ValueError):
            return "例外情報を読み取れませんでした。"
        if kind is None:
            return "例外情報はありません。"
        if isinstance(kind, type):
            name = getattr(kind, "__name__", "不明")
        else:
            name = f"不明な形式（{type(kind).__name__}）"
        frames = traceback.extract_tb(tb) if isinstance(tb, TracebackType) else ()
        positions = [f"  {Path(frame.filename).name}:{frame.lineno} ({frame.name})" for frame in frames]
        return "\n".join([f"例外: {name}", *positions])


class _UnicodeSafeStream:
    """再設定できない出力先でも、文字コードだけでログを失敗させない。"""

    def __init__(self, stream) -> None:
        self.stream = stream

    def write(self, text: str):
        try:
            return self.stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.stream, "encoding", None) or "utf-8"
            return self.stream.write(text.encode(encoding, errors="replace").decode(encoding))

    def flush(self) -> None:
        self.stream.flush()


def configure_logging() -> None:
    """bot.py から起動時に1回呼び、全モジュールのログを標準出力へ送る。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                # 既に読み始めた出力先等は変更できない。ハンドラ側で文字コードを保護する。
                pass
    handler = logging.StreamHandler(_UnicodeSafeStream(sys.stdout))
    handler.setFormatter(OperationalFormatter(LOG_FORMAT, LOG_DATE_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # 外部ライブラリの通常接続ログにはセッション情報が入る。既定では警告以上に絞る。
    for name in ("discord", "google_genai", "google.genai", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
