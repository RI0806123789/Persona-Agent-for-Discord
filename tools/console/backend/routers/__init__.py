"""管理コンソール backend のルート（API のエンドポイント）を、ドメインごとの APIRouter に分けたもの（#482）。

- system.py: 接続先・接続・切断・コマンド履歴・状態・ボットの中の状態（insights）
- logs.py: ログの取得と、ライブ配信（WebSocket）
- operations.py: サービス操作・操作の記録・デプロイ
- database.py: bot.db の集計とテーブルの中身

アプリの組み立て（ミドルウェア・例外ハンドラ・共有状態・共有の補助）は `app.py` が持つ。共有状態は
`routers/context.py` の `state` を通して、呼び出しのたびに `app.py` のモジュールから読み直す。
"""
