# Repository Guidelines

作業前に[CLAUDE.md](CLAUDE.md)を読む。エージェント共通の方針はこのファイルに集約し、同じ規則を別ファイルへ複製しない。

## プロジェクト構成

- `src/`：Pythonのボット本体。`bot.py`が設定とサービスを配線し、`persona_bot/`を設定（`config/`）・中核処理（`core/`）・機能（`services/`）・画面（`views/`）・音声（`audio/`）・共通処理（`utils/`）に分ける。SQLは`core/database.py`（スキーマ定義は`db_migrations.py`、旧データの読み取りは`db_legacy_importers.py`）、プロンプトは`services/prompting.py`が担当する。
- `tests/`：設定・中核処理・機能・画面・共通処理・運用・結合テストを用途別に配置し、`helpers/`に共通補助を置く。
- `prompts/`：ペルソナ。Git管理は書式見本`00_prompt_format.txt`と`personas.json`の見本`00_prompt_format.json`だけで、キャラ別の`.txt`と`personas.json`は利用者がローカルで作る。`assets/`・`docs/`：素材と図。`deploy/`：運用設定例。`.github/`：Issue・PRテンプレート、CI（回帰テスト・依存の脆弱性検査）、Dependabotの設定。
- `Site/`：紹介用ホームページ（React + TypeScript + Vite。GitHub Pagesで公開）。ボットとは依存・ビルドを分け、文言は`Site/src/content.ts`に集約する。機能や既定値を変えたら、README・ヘルプと合わせて直す。
- `data/memory/`・`data/ng/`：実行時の記憶とモデレーション関連データ。実データを検証やコミットに含めない。`prompts/`のキャラ別ファイルも同様に扱う。

## 開発・起動コマンド

Python 3.11以上を使用する。ルートで仮想環境と依存関係を準備する。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

初回のみ `.env.example` をルートの `.env` へコピーし、Gemini・Discordのキーを設定する。既存の `.env` は保持する。

```powershell
cd src
..\.venv\Scripts\python.exe bot.py
```

Linuxではルートで `python3 -m venv .venv` を実行し、`src/` から `../.venv/bin/python bot.py` で起動する。ボイス機能の準備は[README](README.md)を参照する。

## コーディング規約

インデントは4スペース。変数・関数は `snake_case`、クラスは `PascalCase`、定数は `UPPER_SNAKE_CASE`。コメント・文書・利用者向け文言・ログは日本語にする。

調整値は `constants.py`、サーバー設定は `GuildSettings` に置く。ユーザー別の記憶を分離し、バックグラウンド処理は `background.spawn` を使う。依存追加は事前に合意する。設計上の注意は[CLAUDE.md](CLAUDE.md)に従う。

## 検証

標準の `unittest` による回帰テストとWindows／LinuxのCIがある。ビルド工程、lint設定、カバレッジ基準は未整備。検証コマンドは[CLAUDE.md](CLAUDE.md)と[貢献ガイド](CONTRIBUTING.md#完全ローカルで確認する)を参照する。

テストを追加する場合は `tests/<分類>/test_*.py` とし、標準の `unittest`・`IsolatedAsyncioTestCase` を優先する。分類フォルダには再帰的なテスト検出に必要な `__init__.py` を置く。外部通信はモック、DBは一時ディレクトリを使う。`bot.py` のimportには設定読込・DB初期化の副作用がある。

## コミット・PR

`feat/...`・`fix/...` ブランチで作業し、既存の未コミット変更を保持する。他の作業がある場合は専用worktreeを使い、同じ依頼を複数エージェントで並行実施しない。履歴には `update ver ...` もあるが、新規コミットは `feat:`・`fix:` と日本語の要約を使い、本文に理由と「★ 壊れやすい点」を記載する。CodexのAuthor・Committerは `Codex <codex@openai.com>` にする。

PRはテンプレートに沿って変更後の動作、確認結果、未確認範囲を説明し、必要な画像を添える。解決するIssueは末尾の `Closes #番号` で関連付ける。詳細は[CONTRIBUTING.md](CONTRIBUTING.md)を参照する。

## 作業完了とプライバシー

push・PR作成は利用者の「作業完了を宣言します」を待つ。宣言後は最新のmainとPRの状態を確認し、検証・コミット・push・PR作成まで実施する。文言の引用や手順の編集依頼は完了宣言として扱わない。無関係な差分は含めない。

コミット・公開前に、差分、PRタイトル・本文、コミットメッセージ、ログ・画像を確認する。個人情報、会話・記憶、絶対パス、非公開URL、Webhook・セッションURL、鍵・トークンは除去し、必要箇所は `YOUR_API_KEY`・`<USER_ID>`・`<PROJECT_ROOT>` 等へ置換する。公開窓口は根拠を確認し、不明点は実値を伏せて確認する。

修正後の手順と公開済みPRを再確認し、PR本文に「プライバシーチェック」節を設け、確認範囲と修正した種類・件数を記載する。PRのURL、検証・プライバシーチェックの結果を報告する。脆弱性は[SECURITY.md](SECURITY.md)の非公開窓口へ報告する。

## バージョン・リリース

更新履歴は版ごとに `docs/release-notes/ver0.x.x.md` へ書き、READMEには書かない。変更は見出しが `# ver0.x.x(BETA)` の既存ファイルへ追記する。なければ最新Releaseのパッチ番号を1つ上げたファイルを作り、一覧（`docs/release-notes/README.md`）の先頭とREADMEの「📋 更新履歴」の最新リンクも更新する。見出しは版を `#`、分類（新機能・不具合修正など）を `##` にする。BETA見出しには日付を付けず、READMEの未リリース機能の紹介にも `(BETA)` を付ける。機能ごとに番号を上げない。★ 管理コンソール（`tools/console/backend/remote_scripts/insights.py`）は、番号が最大のファイルの `#` 見出しを配備中の版として表示する。ファイル名と見出しの形を変えるなら、そちらとテストも直す。

マイナー番号の変更とRelease公開は利用者の指示で行う。リリース時は対象PRのmainへのマージとタグ重複を確認し、リリース用PRでBETA表示を解除して、ファイルの見出し・一覧・READMEの最新リンクに日付を付ける。そのファイルをリリースノートに使い（`gh release create ver0.x.x --notes-file docs/release-notes/ver0.x.x.md`）、タグ・タイトルは `ver0.x.x` に揃える。
