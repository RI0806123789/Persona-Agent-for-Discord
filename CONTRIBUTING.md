# 貢献ガイド

不具合の報告、機能提案、ドキュメントの改善、コードの修正を歓迎します。参加する際は [行動規範](CODE_OF_CONDUCT.md) に従ってください。

## Issueで相談する

最初に [既存のIssue](https://github.com/RI0806123789/Persona-Agent-for-Discord/issues) を検索し、同じ内容があればそのIssueへ情報を追加してください。新しい報告には、[バグ報告・機能提案フォーム](https://github.com/RI0806123789/Persona-Agent-for-Discord/issues/new/choose) を使います。

- バグ報告には、OS、Python・ライブラリのバージョン、再現手順、期待する結果と実際の結果を記載する。
- 機能提案には、困っていること、使う場面、希望する動作、代替案を記載する。
- Issue・PR・レビューは日本語を基本とし、確認した事実と推測を分けて説明する。
- 大きな変更や依存ライブラリの追加は、実装前にIssueで対応範囲と理由を相談する。

APIキー、Discordのトークン、個人情報、実際の会話ログを公開しないでください。再現例には架空のユーザー・内容を使い、ログに含まれる情報を確認してください。脆弱性は公開Issueへ投稿せず、[SECURITY.md](SECURITY.md) の窓口へ報告します。

## Forkと作業ブランチ

GitHubでこのリポジトリをForkし、次のコマンドで取得します。`YOUR_GITHUB_ACCOUNT` を自分のアカウント名に置き換えてください。

```bash
git clone https://github.com/YOUR_GITHUB_ACCOUNT/Persona-Agent-for-Discord.git
cd Persona-Agent-for-Discord
git remote add upstream https://github.com/RI0806123789/Persona-Agent-for-Discord.git
git switch -c feat/short-description
```

機能追加には `feat/...`、不具合修正には `fix/...` のブランチを使い、`main` へ直接コミットしません。既存の作業ツリーを使う場合は `git status --short` と `git diff` で変更を確認し、他の作業を上書きしないようにしてください。

## 開発環境

Python 3.11以上とGitが必要です。以下はリポジトリルートで実行します。

**Windows / PowerShell**

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

以降の `python` は、この仮想環境のPythonを指します。Windowsでは `.venv\Scripts\python.exe`、Linuxでは `.venv/bin/python` に置き換えて実行するか、仮想環境を有効化してください。

仮想環境を有効化する場合は、Windowsで `.venv\Scripts\Activate.ps1`、Linuxで `source .venv/bin/activate` を実行します。PowerShellで有効化が制限されている場合も、上記のPython実行ファイルを直接使えます。

依存関係の準備にはパッケージのダウンロードが必要です。準備後の次の確認には、ネットワーク接続やAPIキーは不要です。

## 完全ローカルで確認する

リポジトリルートから、まず依存関係の整合性を確認します。

```bash
python -m pip check
```

全Pythonソースの構文を、モジュールを起動せずに確認します。このコマンドは `.env`、実際のDB、Discord、Geminiを使わず、コンパイル結果もファイルへ保存しません。

```bash
python -B -c "from pathlib import Path; paths = sorted(Path('src').rglob('*.py')) + sorted(Path('deploy').glob('*.py')) + sorted(p for p in Path('tools/console/backend').rglob('*.py') if '.venv' not in p.parts) + [Path('tools/console/dev_launcher.py')]; [compile(p.read_text(encoding='utf-8-sig'), str(p), 'exec') for p in paths]; print('構文確認: OK')"
```

ローカル処理の例として、Discord向けのメッセージ分割を確認できます。

```bash
python -B -c "import sys; sys.path.insert(0, 'src'); from persona_bot.utils.discord_helpers import split_message; parts = split_message('あ' * 4001); assert [len(p) for p in parts] == [2000, 2000, 1]; print('メッセージ分割: OK')"
```

文書処理・記憶圧縮・会話リセット・自律行動・会話統計などの回帰テストは標準ライブラリの `unittest` で実行できます。

```bash
python -B -m unittest discover -s tests -v
```

テストは `tests/` 内で用途別に分けています。分類と個別実行の例は [テストの案内](tests/README.md#フォルダ構成と個別実行) を参照してください。

APIキー・`.env` は不要です。外部サービスの呼び出しはモックに置き換え、DBとJSONの確認には一時ディレクトリと架空のデータを使います。`bot.py` のimportは設定読込・DB初期化を行うため避け、メッセージ処理の確認には副作用のない `persona_bot.core.message_handler.MessageHandler` へサービスを注入します。既存の実行時データは使いません。

[回帰テストCI](.github/workflows/tests.yml) はpush・PRでWindows／LinuxとPython 3.11／3.13の組み合わせを実行し、`pip check`・構文確認・上記の回帰テストを検証します。リンター設定はありません。PRには、変更に応じた確認内容と結果を記載してください。

直接依存は `requirements.txt`、直接依存が引き込む推移依存は `constraints.txt` で固定しています（`requirements.txt` の `-c constraints.txt` から読むので、インストールの手順は変わりません）。直接依存を足したり版を変えたりするPRでは、[推移依存の制約ファイルを作り直す](.github/workflows/update-constraints.yml) ワークフローを手動で実行し、成果物の `constraints.txt` で置き換えてください（ジョブの概要に今の内容との差分が出ます）。`constraints.txt` に直接依存は書きません。推移依存は公開から14日以上たった版だけから選ぶため、依存の脆弱性検査が `constraints.txt` の版で脆弱性を検出し、修正版がまだ14日未満のときは、修正版を `.github/constraints/security-exceptions.txt` に脆弱性の ID・理由・外す条件のコメント付きで書いてから作り直します。

依存の定義ファイル（`requirements.txt`・`constraints.txt`・`tools/console/backend/requirements.txt`・`tools/console/frontend`と`Site`の`package.json`・`package-lock.json`）を変えたPRでは、[依存の脆弱性検査](.github/workflows/dependency-audit.yml) も実行されます。`pip-audit` と `npm audit --omit=dev` で既知の脆弱性を照合し、見つかった場合はジョブの概要に一覧と警告を表示します（現在はジョブを失敗させません）。検査には外部通信が必要なため、上の「完全ローカルで確認する」には含めていません。手元で確かめる場合は、作業用とは別の一時的な仮想環境へ `.github/audit/requirements.txt` を入れ、`python -m pip_audit -r requirements.txt` を実行します。影響が無いと確かめた脆弱性はワークフローで除外しているため（理由と外す条件はワークフローのコメントを参照）、CI と同じ結果にするには `--ignore-vuln GHSA-mrfv-m5wm-5w6w` を付けます。`pip-audit` はボットの依存ではないため、`requirements.txt` には追加しないでください。

## 管理コンソールをローカルで確認する

ボットとは別の仮想環境と既存の固定済み依存を使う。以下の`python`は`tools/console/backend/.venv`のものに置き換える（未作成なら同ディレクトリで`python -m venv .venv`）。`.env`・`profiles.json`や実サーバーは不要で、SSHはループバックの模擬サーバー、DBは一時ディレクトリを使う。

```bash
cd tools/console/backend
python -m pip install -r requirements.txt
python -m pip check
python -B -m unittest discover -s tests -v
cd ../frontend
npm ci
npm test
npm run build
```

WindowsでPowerShellの実行ポリシーにより`npm`が使えない場合は`npm.cmd`を使う。`npm run build`はTypeScript検証も含む。ルートの`tests`とバックエンドの`tests`はimport名が異なる前提のため、上記のように作業ディレクトリを分けて実行する。`tests.yml`でバックエンドはWindows/Linux・Python 3.11/3.13、画面はWindows/Linux・Node.js 24を検証する。

## 紹介サイトをローカルで確認する

紹介用ホームページ（`Site/`）は、ボット・管理コンソールとは別にビルドする。外部通信・シークレット・実データは不要。

```bash
cd Site
npm ci
npm test
npm run build
```

表示は`npm run preview`（`http://127.0.0.1:4174`）で確かめる。文言の置き場所・公開の手順は[Site/README.md](Site/README.md)を参照する。`site.yml`はLinux・Node.js 24でテストとビルドを検証する。

## Discord・Geminiを使う確認

接続や応答の実動作が必要な変更は、[READMEのセットアップ手順](README.md) に従い、自分の検証用DiscordサーバーとAPIキーを使います。Geminiの呼び出しは利用量として計上されます。

`.env.example` をリポジトリルートの `.env` へコピーし、キーを設定してから、`src/` 内で `python bot.py` を実行します。ボイス機能を確認する場合は、LinuxのOpusライブラリなどREADMEに記載された準備も行ってください。

## コーディング規約

- 変数・関数・クラス名は英語を使い、既存コードの命名とインデントに合わせる。
- コメント、ドキュメント、ユーザー向け文言、ログは日本語で書く。
- モデル名、タイムアウト、上限などの調整値は `src/persona_bot/config/constants.py` に集約する。
- Geminiの呼び出しはサービス層、SQLは `src/persona_bot/core/database.py`（スキーマの定義と適用は `db_migrations.py`、旧データの読み取りは `db_legacy_importers.py`）、プロンプトの組み立ては `src/persona_bot/services/prompting.py` に置く。
- サーバーごとの設定は `GuildSettings` で扱い、ユーザーごとの記憶を混ぜない。
- バックグラウンド処理の起動には `background.spawn` を使う。
- DBの変更は既存スキーマを直接書き換えず、マイグレーションと保存・復元処理を揃える。
- 依存ライブラリの追加はメンテナーと合意してから行う。

詳しい構成と注意点は [CLAUDE.md](CLAUDE.md) にあります。ボットの起動時は `src/` を作業ディレクトリとします。記憶・DBは `data/memory/`、NGワード・違反記録は `data/ng/`、ペルソナはルート直下の `prompts/` を参照します。旧配置のデータを持つ環境では、[READMEのデータ移行手順](README.md#data-directory-migration)に従ってボット停止中に移してください。

## Pull Requestを出す

1. 変更に応じたローカル確認を実行する。
2. `git diff --check` と差分を確認し、シークレット、個人情報、ローカルの絶対パス、無関係な変更が含まれていないことを確認する。
3. 対象ファイルを明示してコミットする。例：`git add -- CONTRIBUTING.md`。
4. コミットの件名は `feat:` / `fix:` に続けて日本語で要約し、本文に変更理由と「★ 壊れやすい点」を記載する。
5. Forkへ作業ブランチをpushし、元リポジトリの `main` 向けにPRを作成する。
6. PRテンプレートに、変更後の動作、関連Issue、確認結果、未確認の範囲を記載する。

```bash
git push -u origin feat/short-description
```

Issueを解決するPRでは本文末尾に `Closes #番号` を記載します。関連するだけのIssueには `Refs #番号` を使い、まだ完了していない作業はDraft PRで共有してください。レビューでは指摘した箇所と理由を具体的に伝え、修正後に確認結果を更新します。
