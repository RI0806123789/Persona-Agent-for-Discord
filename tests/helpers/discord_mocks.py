"""Discord の Interaction・Message・Channel・Guild を模した、テスト用の架空オブジェクトを作る。

各テストが SimpleNamespace を手書きして重複していた Discord 周りの模擬を集約する（#378）。
チャンネルの履歴取得（`make_history`・`make_channel(history_messages=...)`）と、Discord の例外へ渡す
HTTP 応答（`make_http_response`）もここにある（#400）。
実通信は行わず、送信・応答のメソッドは AsyncMock なので、呼ばれた引数をそのまま検証できる。
ファクトリは呼ぶたびに新しいオブジェクトと新しい AsyncMock を返すので、テスト間で呼び出し履歴が混ざらない。

★ 厳密さを保つ工夫: 既存のテストには、`response` に `defer` だけを持たせるなど、
  「余計なメソッドを呼んだら AttributeError で落ちる」ことを期待したものがある。
  既定は実物に近い全部入りだが、`make_response("defer")` や `without=ONLY_RESPONSE` で
  持たせるメソッド・属性を絞れる。属性は `**attributes` で足したり置き換えたりできる。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

# make_response が既定で持つメソッド（discord.InteractionResponse のうち、パネル操作で使うもの）
RESPONSE_METHODS = ("defer", "send_message", "edit_message", "is_done")

# make_interaction の `without` に渡すと、user と response（と指定した属性）だけの最小の Interaction になる。
ONLY_RESPONSE = ("followup", "edit_original_response", "original_response")

# user を指定しなかったときは、属性そのものを持たせない（`interaction.user` が AttributeError になる厳密さを保つ）。
_UNSET: Any = object()


def _response_default(name: str) -> Any:
    if name == "is_done":
        return Mock(return_value=False)
    return AsyncMock()


def make_response(*methods: str, **overrides: Any) -> SimpleNamespace:
    """`interaction.response` の模擬を作る。

    methods を省略すると RESPONSE_METHODS を全部持つ。指定するとその名前だけを持つ
    （持たないメソッドを呼ぶと AttributeError になる）。overrides は同名の既定値を置き換え、
    未知の名前なら追加する（例: `make_response("defer", defer=AsyncMock(side_effect=error))`）。
    """
    unknown = [name for name in methods if name not in RESPONSE_METHODS]
    if unknown:
        raise ValueError(f"未対応の response メソッドです: {unknown}")
    attributes = {name: _response_default(name) for name in (methods or RESPONSE_METHODS)}
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


def make_followup(**overrides: Any) -> SimpleNamespace:
    """`interaction.followup` の模擬。`send` は AsyncMock。"""
    return SimpleNamespace(**{"send": AsyncMock(), **overrides})


def make_http_response(status: int, reason: str = "架空") -> SimpleNamespace:
    """`discord.HTTPException`・`Forbidden`・`NotFound` の第1引数（HTTP 応答）の模擬。

    例: `discord.Forbidden(make_http_response(403, "Forbidden"), "架空の権限エラー")`。
    実物の `aiohttp.ClientResponse` は使わず、例外が参照する `status` と `reason` だけを持つ。
    """
    return SimpleNamespace(status=status, reason=reason)


def make_interaction(
    *,
    user: Any = _UNSET,
    user_id: int | None = None,
    response: SimpleNamespace | dict[str, Any] | None = None,
    followup: SimpleNamespace | None = None,
    without: tuple[str, ...] = (),
    **attributes: Any,
) -> SimpleNamespace:
    """`discord.Interaction` の模擬を作る。

    - user: そのまま使う（None も指定できる）。省略して user_id だけ渡すと `SimpleNamespace(id=user_id)`。
      どちらも省略すると `user` 属性を持たない。
    - response: SimpleNamespace ならそのまま、dict なら make_response の既定への上書き、省略なら全部入り。
    - followup: 省略すると `send` が AsyncMock の既定。
    - without: 既定で持たせる属性（followup / edit_original_response / original_response）を外す名前。
      ONLY_RESPONSE を渡すと user と response だけになる。
    - attributes: guild・channel・client・message・extras など、追加または上書きする属性。
    """
    if user is _UNSET and user_id is not None:
        user = SimpleNamespace(id=user_id)
    if isinstance(response, dict):
        response = make_response(**response)
    values: dict[str, Any] = {
        "response": response if response is not None else make_response(),
        "followup": followup if followup is not None else make_followup(),
        "edit_original_response": AsyncMock(),
        "original_response": AsyncMock(),
    }
    if user is not _UNSET:
        values["user"] = user
    for name in without:
        if name not in ONLY_RESPONSE:
            raise ValueError(f"外せる属性ではありません: {name}（外せるのは {ONLY_RESPONSE}）")
        del values[name]
    values.update(attributes)
    return SimpleNamespace(**values)


def make_history(messages: Sequence[Any] = ()) -> Mock:
    """`channel.history(...)` の模擬を作る。呼ぶたびに新しい async generator を返す。

    messages は渡した順にそのまま yield する（Discord のように after・limit・oldest_first で
    絞ったり並べ替えたりはしない）。`fetch_channel_history_text` は新しい順に取得する想定なので、
    新しい順に並べて渡すこと。引数はどんな形でも受け取り、`Mock` なので
    `channel.history.call_args.kwargs["after"]` のように呼び出しの引数も検証できる。
    渡したリストは参照のまま使うので、呼ぶ前に追加・並べ替えした内容が反映される。
    after・limit に応じて絞るなど、より実物に近い挙動が要るテストは、自前の async generator を
    `make_channel(history=Mock(side_effect=...))` で渡す。
    """
    async def generate(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        for item in messages:
            yield item

    return Mock(side_effect=generate)


def make_channel(
    *,
    channel_id: int = 10,
    name: str | None = None,
    history_messages: Sequence[Any] | None = None,
    **attributes: Any,
) -> SimpleNamespace:
    """テキストチャンネルの模擬。`send` は AsyncMock。権限などは attributes で足す
    （例: `permissions_for=lambda member: SimpleNamespace(view_channel=True, send_messages=True)`）。

    history_messages を渡すと、`history` を `make_history(history_messages)` にする
    （渡さなければ `history` 属性を持たない）。`history` を別に attributes で渡すこととは併用できない。
    """
    if history_messages is not None and "history" in attributes:
        raise ValueError("history_messages と history は同時に指定できません")
    values: dict[str, Any] = {"id": channel_id, "send": AsyncMock()}
    if name is not None:
        values["name"] = name
    if history_messages is not None:
        values["history"] = make_history(history_messages)
    values.update(attributes)
    return SimpleNamespace(**values)


def make_guild(*, guild_id: int = 1, name: str | None = None, **attributes: Any) -> SimpleNamespace:
    """サーバー（ギルド）の模擬。ボット自身（me）・システムチャンネル・テキストチャンネルは空の既定。"""
    values: dict[str, Any] = {
        "id": guild_id,
        "me": SimpleNamespace(id=900),
        "system_channel": None,
        "text_channels": [],
        "voice_client": None,
        "fetch_member": AsyncMock(),
    }
    if name is not None:
        values["name"] = name
    values.update(attributes)
    return SimpleNamespace(**values)


def make_message(
    *,
    message_id: int = 1,
    author: Any = None,
    author_id: int = 11,
    channel: Any = None,
    guild: Any = None,
    content: str = "",
    **attributes: Any,
) -> SimpleNamespace:
    """メッセージの模擬。`reply`・`edit`・`delete`・`add_reaction` は AsyncMock。
    author を省略すると `SimpleNamespace(id=author_id, bot=False)`、channel を省略すると make_channel()。"""
    values: dict[str, Any] = {
        "id": message_id,
        "author": author if author is not None else SimpleNamespace(id=author_id, bot=False),
        "channel": channel if channel is not None else make_channel(),
        "guild": guild,
        "content": content,
        "attachments": [],
        "reply": AsyncMock(),
        "edit": AsyncMock(),
        "delete": AsyncMock(),
        "add_reaction": AsyncMock(),
    }
    values.update(attributes)
    return SimpleNamespace(**values)
