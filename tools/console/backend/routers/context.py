"""ルートから、`app.py` のモジュール直下の状態を読むための窓口（#482）。

`app.py` は、接続（`session`）・デプロイ（`deploys`）・各ロック・インサイトのキャッシュ・操作の記録などの
**共有状態を、モジュール直下のグローバルとして持つ**（#461 の判断。コンテナへまとめない）。テストは
`patch.object(app, "session", fake)` のようにこのグローバルを差し替えるので、ルートは、状態を
**呼び出しのたびに `app.py` のモジュールから引き直す**必要がある（`from app import session` で名前を
取り込むと、差し替えが届かない）。

その窓口が `state`。`state.session` は、`getattr(app モジュール, "session")` と同じ。

★ ルートから `import app` しないこと。バックエンドは `python app.py` で起動するため、そのモジュールの名前は
  `__main__` で、`import app` は**別のコピー**を読み込んで状態が二重になる。`app.py` が自分自身
  （`sys.modules[__name__]`）を `bind` で渡すので、`__main__` でも `app` でも同じモジュールを指す。
★ ここに状態を持たせない（持たせると、`app.py` のグローバルと食い違う）。窓口だけ。
"""

from __future__ import annotations

from types import ModuleType


class AppState:
    """`app.py` のモジュールの属性へ、呼び出しのたびに転送する。"""

    def __init__(self) -> None:
        self._module: ModuleType | None = None

    def bind(self, module: ModuleType) -> None:
        self._module = module

    def __getattr__(self, name: str):
        # `_module` は __init__ で設定済みなので、ここへ来るのは app.py の属性を読むときだけ。
        module = self.__dict__.get("_module")
        if module is None:
            raise RuntimeError("app.py が状態を結びつける前に、ルートが呼ばれました。")
        return getattr(module, name)


state = AppState()
