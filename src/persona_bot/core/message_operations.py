"""メッセージ処理の各ステージ（受付・振り分け・応答）が呼ぶ外部操作の契約（#395）。

#377 で受付（`message_admission`）・振り分け（`message_routing`）・応答（`message_response`）を
分けた後も、各ステージは `message_handler` のモジュール全体を受け取り、関数・定数・logger・
discord を区別なく属性として参照していた。必要な属性が型に表れず、呼出元の import を整理すると
その経路を実行するまで不足に気付けなかった。

ここでは次のように責務を分ける。

- **外部操作**（このモジュールの `MessageOperations`）: Discord への送信・履歴取得、ボイスの読み上げ、
  添付画像の取得、ペルソナのファイル読込、背景タスクの起動。テストが差し替える境界で、
  `MessageHandler` が `message_handler` モジュールの同名の属性から呼出しのたびに組み立てる
  （`patch.object(message_handler, "send_long_reply", ...)` や `make_message_handler` の差し替えが
  そのまま効く）。足りない・呼び出せない操作は構築時に `TypeError` で知らせる。
- **定数・型・判定や組み立ての純粋な関数**: 各ステージが自分で import する。差し替え口にはしない。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import discord


# 受付・振り分け・応答のログは、分離前（#377）と同じく MessageHandler の名前で出す
# （運用ログの出所と、テストの assertLogs の対象を変えない）。
MESSAGE_LOGGER_NAME = "persona_bot.core.message_handler"


class FetchHistoryText(Protocol):
    """`discord_helpers.fetch_channel_history_text`: チャンネルの直近履歴を1つのテキストにする。"""

    def __call__(
        self, channel: Any, reset_message_id: int | None, bot_user: Any, limit: int, *,
        is_blocked: Callable[[int], bool] | None = None,
    ) -> Coroutine[Any, Any, str]: ...


class SendLongReply(Protocol):
    """`discord_helpers.send_long_reply`: 長文を分割して返信し、1通目を返す。"""

    def __call__(
        self, message: discord.Message, text: str, *, suppress_embeds: bool = False,
    ) -> Coroutine[Any, Any, discord.Message | None]: ...


class PlayTtsResponse(Protocol):
    """`voice.play_tts_response`: 接続中のボイスで返信を読み上げる（配信コメントへの応答だけが使う）。"""

    def __call__(
        self, message: discord.Message, response_text: str | None, prompt_file: str | None = None, *,
        is_valid: Callable[[], bool] | None = None,
    ) -> Coroutine[Any, Any, None]: ...


class LoadImageAttachments(Protocol):
    """`discord_helpers.load_image_attachments`: 添付画像を取得・縮小する。返した画像は呼出元が閉じる。"""

    def __call__(self, attachments: Sequence[discord.Attachment]) -> Coroutine[Any, Any, list[Any]]: ...


class LoadPersonaTemplate(Protocol):
    """`paths.load_persona_template`: ペルソナのファイルを別スレッドで読む。"""

    def __call__(self, prompt_file: str) -> Coroutine[Any, Any, str]: ...


class Spawn(Protocol):
    """`background.spawn`: 完了まで強参照を保つ背景タスクを起動する。"""

    def __call__(self, coro: Coroutine[Any, Any, Any], *, label: str) -> asyncio.Task[Any]: ...


@dataclass(frozen=True, slots=True)
class MessageOperations:
    """ステージが呼ぶ外部操作の一式。フィールド名は `message_handler` の差し替え口の名前と同じ。

    ★ 外部操作を足すときは、ここ・`message_handler` の import・ステージの呼出しをそろえる
    （`tests/core/test_message_operations.py` が、未使用のフィールド・未宣言の操作の参照・
    ステージの未定義の名前を検出する）。純粋な関数や定数をここへ足さないこと。
    """

    # Discord
    fetch_channel_history_text: FetchHistoryText
    send_long_reply: SendLongReply
    play_tts_response: PlayTtsResponse
    # 添付・ファイル
    load_image_attachments: LoadImageAttachments
    load_persona_template: LoadPersonaTemplate
    # 背景処理
    spawn: Spawn

    @classmethod
    def from_namespace(cls, namespace: object) -> MessageOperations:
        """モジュールなどの同名の属性から組み立てる。足りない・呼び出せない操作は名前を挙げて拒否する。"""
        names = operation_names()
        missing = [name for name in names if not callable(getattr(namespace, name, None))]
        if missing:
            raise TypeError(f"メッセージ処理の外部操作が足りません: {', '.join(missing)}")
        return cls(**{name: getattr(namespace, name) for name in names})


def operation_names() -> tuple[str, ...]:
    """契約に含まれる外部操作の名前（宣言順）。"""
    return tuple(field.name for field in fields(MessageOperations))
