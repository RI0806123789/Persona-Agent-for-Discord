# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 概要

Google Gemini をバックエンドに持つ Discord ボット。ペルソナベースのテキストチャット、`/join` したボイスチャンネルでの Gemini Live による音声会話（参加者の声を受信して Live へ流し、返ってきた音声を再生する。全員の同意・話者の順番・割り込み・並行モデレーション。追加の依存なし）、自律行動の配信での TTS 読み上げ（FFmpeg 不要・edge-tts でペルソナごとに声が変わる）、画像/文書のマルチモーダル入力、長期記憶、ユーザーごとの親密度/感情トラッキング、コンテンツモデレーション、三審制ブラックリスト、チャンネル会話の議事録要約、ユーザー別会話統計ダッシュボード、無会話チャンネルへの自発的な話題提供、自律行動（ボットが自分からボイス/ステージチャンネルへ出て独り言を続け、コメントに配信者のように応答する。配信先は管理者が指定できる）、ボイス接続先の保護（手動で別のチャンネルへ移されたら元へ戻るか退出する）、記念日の自動お祝い、リマインダー、会話で明記された調べ物のサブエージェントへの委任、返信への 👍👎 リアクションによるフィードバック収集、オンボーディング導線（サーバー参加時の案内と使い方パネル）を備える。ボットのソースはすべて `src/` にある（開発者用ダッシュボードは別に `tools/console/`。後述）。ユーザー向け文字列・ログ出力・コードコメントはすべて日本語。新規に追加するものも日本語で統一すること。

## 作業の進め方

### 確認を取ってから行うこと

次の操作は、**何をなぜ実行するかを伝えて承認を得てから**行ってください。

- ファイル・ディレクトリの削除
- `git push` / 履歴を書き換える操作（`rebase` / `reset --hard` / `commit --amend`）/ ブランチの削除。
  ただし、利用者が「作業完了を宣言します」と伝えた**後**に、末尾の「作業完了時の手順」に沿って行う作業ブランチの
  push と PR 作成は、その宣言が依頼にあたるので承認は要らない（宣言の前に push・PR 作成をしてはならない）
- **実行時データ**（`data/memory/` 配下の `bot.db`・`projects.json`、`data/ng/blacklist.json`、`data/.migration-backups/`）を消したり書き換えたりすること。
  利用者が手元で作ったペルソナ（`prompts/` のテンプレート以外の `.txt` と `personas.json`）も同じ。Git の管理対象外で、
  消すと取り戻せない
  会話の記憶・会話統計・リマインダー・違反記録が入っており、消すと取り戻せない
- **依存ライブラリの追加。** このプロジェクトは**ライブラリを足さない**判断を随所でしています
  （読み上げは FFmpeg を使わず miniaudio でメモリ上のままデコードし、音色の EQ は numpy だけで実装して
  `scipy` / `librosa` / `pydub` は使わない）。足す前に必ず相談すること

利用者から明示的に頼まれた操作には応じてかまいません（その場合も、危険な操作は内容を示して確認する）。

### git

- 作業は**トピックブランチ**で行い、`main` へ直接コミットしない。
  命名は `feat/...` / `fix/...`（例: `feat/issue-1-community-standards`）
- コミットメッセージは `feat:` / `fix:` + **日本語**の要約。本文には
  **「なぜそうしたか」と「★ 壊れやすい点」**を書く（既存のログが手本）
- **Codex が作成するコミットの Author / Committer は `Codex <codex@openai.com>` にする。**
  `git -c user.name=Codex -c user.email=codex@openai.com commit ...` で、そのコミットだけ名義を指定する。
  手動コミットの名義を保つため、共通の `git config user.name` / `user.email` は変更しない。
- **PR は、利用者が「作業完了を宣言します」と伝えた後にだけ出す。** それまではトピックブランチへのコミットまでに
  とどめ、push もしない。宣言を受けたら、末尾の「作業完了時の手順」に従い、**プライバシーチェックをしてから**
  PR を出す。既存ブランチの再利用でかまわない
- PR を出す前に、`CONTRIBUTING.md` の「完全ローカルで確認する」のコマンド（`pip check`・全ソースの構文確認・
  メッセージ分割の確認）を回す。回帰テストも標準の unittest で実行する（下記参照）。
  Discord・Gemini の実動作に関わる変更は検証用サーバーで確かめ、確かめられなかった範囲は PR に「未確認」と書く
- ★ **対応する issue があれば、PR 本文の末尾に `Closes #N` を書く**（複数なら 1 行ずつ）。
  本文中に `#N` と書くだけでは**参照リンクになるだけで、マージしても閉じません**
- PR・コミットにセッション URL を書かない
- README の更新履歴とリリースは、末尾の「バージョンとリリースの運用」（`AGENTS.md`）に従う

### 同時に走らせるエージェント

作業の複雑さに応じて分けてください。**同時に 3 体まで。**

- **同じ作業を複数のエージェント（Claude Code と Codex など）で並行させないこと。** 実際に、同じ依頼から
  Issue が重複して起票されたり、追加の push より先に PR がマージされたりした。Issue の起票・push・PR の編集の直前には、
  `gh issue list` / `gh pr view` で最新の状態を取り直す
- 共有の作業コピーを別のエージェントが触っている可能性があるときは、自分用の worktree（ブランチ）で作業する

### 依存の更新提案と脆弱性検査（#285）

- **`.github/dependabot.yml`** が、pip（ルート・`tools/console/backend`・`.github/audit`・`.github/constraints`）、npm（`tools/console/frontend`）、
  github-actions の更新を毎週月曜 9:00（日本時間）に PR で提案する（ルートの推移依存＝`constraints.txt` の分は1つの PR にまとめる）。`cooldown` の `default-days: 14` で公開から2週間未満の版を
  提案させない（依存を公開から2週間以上たった版に固定する方針と同じ）。**cooldown を外したり14日未満にしたりしないこと。**
  cooldown はバージョン更新だけに効き、Dependabot security updates（リポジトリ設定。現在は無効）には効かない
- Dependabot の PR は**提案にすぎない**。依存の更新も事前の相談が要るので、エージェントが自分の判断でマージしないこと。
  ★ ルールセット「Protect Rule」が全ブランチで強制 push を禁じ PR を必須にしているため、Dependabot は作成済みの PR ブランチを
  rebase できない（初回の作成は通る）。main と衝突したら、閉じて次の提案を待つか利用者に相談する
- **`.github/workflows/dependency-audit.yml`** が、`pip-audit -r` でボットと管理コンソールのバックエンドの requirements を
  （Windows／Linux・Python 3.11／3.13 で推移依存まで解決して）、`npm audit --omit=dev` で画面の `package-lock.json` を照合する。
  依存の定義ファイルを変えた push・PR と、毎週月曜 9:00（日本時間）に走る。権限は `contents: read` だけでシークレットを使わない
  （フォークの PR でも動く。`pull_request_target` に変えないこと）。実行の条件の `paths` は依存の定義ファイルと揃えておく
  （ファイルを移したら `paths` と `tests/deploy/test_dependency_audit_config.py` も直す）
- 検出は**警告から始める**: `AUDIT_REQUIRED: 'false'` の間は、警告の注釈とジョブの概要に出すだけでジョブは失敗しない
  （検査自体の失敗＝通信障害なども同じ警告になる）。誤検知でないことを確かめてから `'true'` にし、テストの期待値も直す
- **除外は影響が無いと確かめた脆弱性だけ**: `pip-audit` の `ignored_vulns` に ID を足すときは、ワークフローのコメントに理由と
  外す条件を書き、`tests/deploy/test_dependency_audit_config.py` の `EXPECTED_IGNORED_VULNS` に除外の前提にしている
  `requirements.txt` の行も書く（その行が変わるとテストが失敗し、見直しを促す）。除外中の ID はジョブの概要に毎回出る。
  現在は `GHSA-mrfv-m5wm-5w6w`（CVE-2025-69277。PyNaCl 1.6.2 未満の libsodium の Ed25519 の点の検証）だけ。ボットが PyNaCl で
  使うのは XChaCha20-Poly1305 の AEAD だけで影響する関数を呼ばず、`discord.py[voice]==2.7.1` が `PyNaCl<1.6` に制約しているため
  上げられない。PyNaCl 1.6 系を許す discord.py の版が出たら（master は変更済み・未リリース）更新して除外を外す。
  ★ `[voice]` を外して PyNaCl を個別に固定する回避策は採らない（`pip check` は extra の依存を検査しないので、不整合を誰も検出できない）
- **`pip-audit` は CI 専用の道具で、ボットの依存ではない。** 版は `.github/audit/requirements.txt` に固定し（Dependabot が更新を提案）、
  ルートや管理コンソールの `requirements.txt` に足さないこと
- **監査ジョブの `PYTHONUTF8: '1'` を保持する。** 日本語コメント入りの requirements を Windows の既定コードページで読むと、監査前に `UnicodeDecodeError` になる。`PYTHONIOENCODING` は標準入出力だけを変えるため代用にならない。
- 管理コンソールの開発サーバー（`dev_launcher.py` が起動する vite）は devDependencies だが、`npm audit --omit=dev` の対象外。
  更新は Dependabot の提案で追う

### 推移依存の固定（`constraints.txt`。#285）

- **直接依存は `requirements.txt`、推移依存は `constraints.txt` に固定する。** `requirements.txt` の末尾の `-c constraints.txt` で読むので、
  `pip install -r requirements.txt`（README・CI・管理コンソールのデプロイ・pip-audit）はそのままで固定版が効く。**この行を消さないこと**
  （推移依存がインストールした時点の最新版＝公開当日の版になる）。直接依存を `constraints.txt` に書かないこと（2か所で食い違う）
- **作り直しは `.github/workflows/update-constraints.yml`（手動実行だけ）。** Windows／Linux・Python 3.11／3.12／3.13 で `-c` の行を除いて解決し、
  `deploy/merge_constraints.py` が合成して成果物にする（コミットはしない。差分をジョブの概要で見て PR で取り込む）。直接依存は先に
  `pip download --no-deps` して `--find-links` に置き、公開日の上限（`--uploaded-prior-to`、既定 `P14D`）を掛けない（人が選んだ版。
  セキュリティ修正で公開直後の版を固定することがある）。推移依存は索引から公開日の上限内の版だけを選ぶ。生成に使う pip は
  `.github/constraints/requirements.txt` に固定する（`--uploaded-prior-to` は pip 26.0.1 から）
- **推移依存のセキュリティ修正は `.github/constraints/security-exceptions.txt` で公開日の上限の例外にする。** 上限内の版に既知の脆弱性があり、
  修正版が公開から日が浅いとき（pip-audit が `constraints.txt` の版で検出したとき）に、修正版を ID・理由・外す条件のコメント付きで書いて
  作り直す（生成は直接依存と同じく `--find-links` に置き、`-c` で版を指定する）。外す条件を満たしたら行を消して作り直す。
  導入時に実際に、上限内の certifi 2026.7.22 が CRITICAL の GHSA-6q48-6c7w-9gw2（漏えいした emSign のルート証明書）に当たり、
  修正版 2026.10.19 を例外にした（固定しない main では最新版が入っていたので、例外が無いと固定によって脆弱な版へ戻る）。
  Dependabot はこのフォルダでは `allow` で pip だけを更新する（例外の版を書き換えさせない）。テストは各行のコメントと
  `constraints.txt` への反映を照合している
- **直接依存を足したり版を変えたりしたら作り直す。** Dependabot の直接依存の PR が推移依存の固定と衝突して CI で落ちたら、
  ルールセットにより Dependabot のブランチへは push できないので、新しいブランチで直接依存の更新と作り直した `constraints.txt` を
  まとめて PR にし、Dependabot の PR は閉じる
- **版が環境で分かれる依存は環境マーカーで書き分ける**（例: contourpy 1.4.0 は Python 3.12 以上が必須で、3.11 は 1.3.3）。合成スクリプトは
  Python の版の範囲 → OS → 両方 → 環境の列挙の順に書き方を試し、どの環境でもちょうど1行が当てはまることを自己点検する。
  ★ 書き分けた依存は `.github/dependabot.yml` の `ignore` に入れること（Dependabot は両方の行を同じ版にしかねない）。
  ★ Dependabot の `groups` の `exclude-patterns` は `requirements.txt` の直接依存と同じにそろえ、正規化した名前（`discord-py`・`pillow`）で書く
- ★ 手元の pip は環境マーカーを動かしている Python で評価する（`--python-version` を付けても変わらない）。手元で別の版向けに
  解決して確かめるときは、マーカーの評価が同じになる Python から行う。ハッシュの照合（`--require-hashes`）はしていない

## ボットの起動

ビルド工程・リンター設定は未整備。標準の unittest / IsolatedAsyncioTestCase による回帰テストと、Windows／Linux・Python 3.11／3.13で実行する `.github/workflows/tests.yml` がある。依存の脆弱性検査は別の `.github/workflows/dependency-audit.yml`（「依存の更新提案と脆弱性検査」の項）。

**ローカル確認**（リポジトリルートで実行。`.env`・実際の DB・Discord・Gemini を使わない。`CONTRIBUTING.md` と同じもの）:

```bash
python -m pip check
# 外部通信・シークレット・実データ不要の回帰テスト
python -B -m unittest discover -s tests -v
# 1モジュールだけ／テスト名の一部に一致するものだけを実行する
python -B -m unittest tests.core.test_bot_reply_settings -v
python -B -m unittest discover -s tests -k test_last_slot_accepts_only_one_concurrent_request
# src/ と deploy/ の全 .py の構文確認（モジュールは起動しない）
python -B -c "from pathlib import Path; paths = sorted(Path('src').rglob('*.py')) + sorted(Path('deploy').glob('*.py')) + sorted(p for p in Path('tools/console/backend').rglob('*.py') if '.venv' not in p.parts) + [Path('tools/console/dev_launcher.py')]; [compile(p.read_text(encoding='utf-8-sig'), str(p), 'exec') for p in paths]; print('構文確認: OK')"
# 個別モジュールの動作確認の例（src を import パスに足す）
python -B -c "import sys; sys.path.insert(0, 'src'); from persona_bot.utils.discord_helpers import split_message; assert [len(p) for p in split_message('あ' * 4001)] == [2000, 2000, 1]; print('メッセージ分割: OK')"
```

テストは `tests/` 内で用途別に配置する。構成と個別実行の手順は [テストの案内](tests/README.md#フォルダ構成と個別実行) を参照する。CI は上記に加えて `python -m compileall -q src deploy tests tools/console/backend tools/console/dev_launcher.py` で `tests/` の構文も確かめ、`PYTHONIOENCODING=utf-8`・`MPLBACKEND=Agg` を設定する。Linux のジョブだけ `REQUIRE_JAPANESE_GRAPH_FONT=1` で日本語フォントの未導入を失敗にする（未指定の環境では字形テストを省略する）。

確認用のスクリプトから **`bot.py` を import しないこと**（トップレベルで設定の読み込みと DB の初期化が走る）。`persona_bot.core.message_handler.MessageHandler` は設定読込やDB初期化を実行せずimportできる。メッセージ処理のテストはサービスと外部境界をモックに差し替えてこのクラスを直接呼ぶ（AST抽出は不要。`tests/helpers/message_handler_helpers.make_message_handler` が、渡さなかった依存を `Mock` で埋めて組み立てる。渡せる名前はサービスと外部操作（`MessageOperations`）だけで、定数・判定の関数など差し替えても効かない名前は拒否する）。Discord の架空の Interaction・Message・Channel・Guild は各テストで手書きせず `tests/helpers/discord_mocks.py` のファクトリで作る（`response` に `defer` だけを持たせるなど、持たせる属性を絞って厳密さも保てる）。チャンネルの履歴取得は `make_channel(history_messages=[...])`（新しい順に渡す）、`discord.Forbidden` などの第1引数の HTTP 応答は `make_http_response(status, reason)` で作る（#400）。それ以外のモジュールも `sys.path.insert(0, 'src')` で import できる。外部サービスはモックに置き換え、DB を使う確認は一時ディレクトリと架空のデータで行う。

```bash
# 1. 依存関係のインストール (Python 3.11+)
pip install -r requirements.txt

# 2. シークレットの用意: プロジェクトルート直下（src/ の中ではない）に .env を作る。
#    cp .env.example .env してから API_KEY_GEMINI= と TOKEN_DISCORD= の右辺を実キーに書き換える。
#    .env は gitignore 済み。未設定のまま起動すると "キー未設定" を出して例外で停止する。

# 3. Opus 共有ライブラリはボイス TTS（/join・自律行動）で必要（FFmpeg はもう使わない）
#    Windows: 不要（discord.py が opus の DLL を同梱している）
#    Linux:   sudo apt install -y libopus0
#
#    requirements.txt の discord.py は必ず [voice] 付きにすること。ボイス接続に必要な
#    davey（DAVE = Discord が 2026-03-02 に必須化した音声E2E暗号化）が入らず、
#    /join が RuntimeError('davey library needed in order to use voice') で失敗する。

# 4. src/ ディレクトリの中から実行する:
cd src
python bot.py     # 成功時に "ログイン成功: <bot>" と表示される
```

起動は従来どおり **`src/` の中から `python bot.py`** とする。`bot.py` が配線を担当し、各モジュールは `persona_bot/` パッケージ内で用途別に分ける。内部のimportは `from persona_bot.core.app_state import BotState` のようにパッケージ名から指定する。各 `__init__.py` は説明だけを置き、サービスの生成や設定読込を行わない。プロンプトの相対パスは引き続き `src/` を基準に解決する（「パス解決」の項を参照）。

| フォルダ（`src/persona_bot/` 内） | 担当 |
| --- | --- |
| `config/` | 定数、選択肢、APIキー読込、パス解決 |
| `core/` | 状態、Discordクライアント、コマンド、メッセージ処理、バックグラウンド処理、SQLite |
| `services/` | Gemini、プロンプト、記憶、親密度、モデレーション、自発会話、自律行動、文書、統計、リマインダー、評価 |
| `views/` | 機能パネルと共通UI部品 |
| `audio/` | TTSと音声の加工、音声会話の受信（`voice_receive.py`）と形式変換・再生音源（`live_audio.py`） |
| `utils/` | Discord送信、JSON入出力、時刻、グラフ描画 |

## サーバー運用 (`deploy/`)

本番は Oracle Cloud 上の Ubuntu（ユーザー `ubuntu`、`/home/ubuntu/Persona-Chat-in-Discord`）で **systemd のサービス名 `bot`** として常駐する。ユニットは `deploy/bot.service.example`（プレースホルダ入り・Git 管理対象）と `deploy/bot.service`（実値入り・gitignore 済み）の**2本立て**で、実パスやユーザー名を公開側に残さない。導入・運用コマンドは README の「7. 常時稼働の設定」にある。

- **`WorkingDirectory` は `src/`、`ExecStart` は `.venv/bin/python` の絶対パス**。systemd はシェルを介さないため `activate` は効かない。
- **`StartLimitIntervalSec=0`（再起動のレート制限を無効化）を戻さないこと**: 既定の「10秒で5回失敗したら諦める」だと一時的な回線障害でボットが二度と復帰せず、systemd を入れた目的（手動回復をなくす）が失われる。副作用として設定ミスでは永久に再起動ループになるため、`systemctl status bot` が `activating (auto-restart)` のままなら設定ミスを疑う運用が前提。なお **systemd 229 以降この指定は `[Unit]` に置く** — `[Service]` に書くと黙って無視される。
- **`ProtectSystem=strict` / `ProtectHome=yes` を足さないこと**: 実行中に `data/memory/`（SQLite DB・JSON）と `data/ng/` へ書き込むため、read-only エラーで記憶とモデレーションが黙って壊れる。入れるなら `ReadWritePaths=` で個別に開ける必要がある。
- **`WatchdogSec` は未導入**（依頼どおり、ハング型のフリーズが実際に観測されてから検討する）。`Restart=always` が拾えるのはプロセスが落ちるケースだけで、「生きているのに応答しない」状態は拾えない。導入にはアプリ側の `sd_notify(WATCHDOG=1)` 定期送信が必須で、送信が滞ると健全なプロセスまで殺す。
- **ユニットファイルは LF 改行必須**（`.gitattributes` で固定済み）。CRLF だと `ExecStart=` のパス末尾に `\r` が混入して起動に失敗し、原因が非常に分かりにくい。
- ログは journald（`journalctl -u bot -f`）。loggingの標準出力ハンドラは毎回flushする。ほかの標準出力も即時反映させる `PYTHONUNBUFFERED=1` と、日本語・絵文字の文字コードを揃える `PYTHONIOENCODING=utf-8` はユニット側に維持している。

## 開発者用ダッシュボード (`tools/console/`)

`backend/remote_scripts/insights.py` と `db_table.py` は、`collect(...)` が呼出しごとの状態を作って結果の辞書を返し、`main()` とmainガードだけでargv解釈・JSON出力を行う。import時に設定ファイル・DB・Discord・journalへ触れない境界を保つ。サーバーへ送るのは引き続き標準ライブラリだけの1ファイルで、ボット本体のimportや別ファイルの配備を必要としない。収集処理のテストは通常のimportと関数・メソッドの呼出しを使い、実行用コードをASTから再構築しない（設定ソースをASTで読む処理は維持する）。

データ画面の開いているテーブルと閉じるアニメーション中のテーブルは、選択時の`connectionKey`と組で保持する。現在の接続と一致する選択だけを描画し、接続変更時のeffectだけに解除を任せない（子の取得effectが先に動くため）。新しい接続では明示的な選択までテーブル内容を取得しない（#344）。

サーバー設定インサイトはスキーマ15の`autonomous_stage_channel_id` / `autonomous_voice_channel_id`も取得する。正の整数のIDは精度を保つ文字列、指定なしは`null`、列が無い旧版は項目なしとして区別する。サーバー一覧とDM設定の専用欄は描画時に`mask`し、`collectSecrets`にも登録する。設定名を取得するためのDiscord API呼出しは増やさない（#292）。

インサイトのモデル・応答モード・自発会話間隔は、保存値を各選択肢と照合し、ボット本体の`GuildSettings.apply_persisted`と同じ既定値へ解決する（間隔は整数のみで`bool`を除外）。`journal()`は成功時に一覧（0件を含む）、失敗時に`None`を返す。初回のログ取得失敗時は古い会話の検索を重ねず、`note()`は同じエラー文を初出順に1件だけ記録する（#259）。

SSH設定由来の`SshError`は対象ファイルと対処方法を維持して返す。ログのWebSocket切断後は理由と受信済みの行を残し、自動再接続せず手動再接続を待つ。`journalctl -g`の終了コード1・標準エラーなしだけは検索結果0件とし、実際の取得失敗は警告する。

```text
tools/console/
├── backend/             # API・SSH・デプロイ（ボットとは別の仮想環境）
│   ├── remote_scripts/  # insights・DB件数・テーブル読取を標準入力で遠隔実行
│   └── tests/           # 模擬SSH・一時DB・API境界の回帰
├── frontend/
│   ├── src/             # React画面・controllers.tsの接続/操作フック
│   └── tests/           # Node.js標準テスト
└── dev_launcher.py      # 手元の開発用起動
```

`.github/workflows/tests.yml` はボットに加え、管理コンソールのバックエンド（Windows/Linux・Python 3.11/3.13）とフロントエンド（Windows/Linux・Node.js 24）を別ジョブで検証する。手元の再現コマンドは `CONTRIBUTING.md` の管理コンソール節を参照。依存は既存の固定版を各環境へ入れ、本番の接続先・秘密情報・実DBは使わない。

デプロイ開始でも確認時の`profile_id`・`connected_at`を照合し、不一致は409。成功したサービス操作とデプロイ終了でインサイトキャッシュを失効させ、取得中の古い結果も再キャッシュしない。`db_summary.py` は件数のみを読み取り専用で取得し、日別評価は既存複合索引の期間検索と日本時間の日付条件を併用して旧日時形式も扱う。ログ転送の終了処理は再キャンセル時もSSHプロセス・転送タスクを回収する。不正なJSON・項目型もログの取得エラーとして停止し、受信済みの行を保持する。画面のサービス操作確認は接続変更で閉じ、古いコールバックも実行しない。伏せ字は接続先ID・デプロイ先と現在のGitブランチにも適用する。

サービス操作・接続変更・デプロイ開始はAPIの共通ロックで順序を揃え、デプロイ中のサービス操作・接続・切断を409で拒否する。サービス操作はロック取得後に接続先・接続時刻とデプロイ状態を再確認し、SSH応答を待つ間もロックを保つ。送信済みの操作を別タスクで完了まで待ち、呼出元の繰り返しキャンセルでも解放を先行させない。終了時は`DeployManager.wait`で処理・巻き戻しの終了を待ってから切断し、起動ログ取得の失敗は待機で隠さず即座に扱う。画面は`requestScope.ts`で接続変更開始・接続先変更と取得順序を照合し、古い応答や予約済みタイマーを捨てる。伏せ字にはホーム所有者とホーム部分・接続先ラベルも含め、通知の見出し・詳細とログのツールチップにも、表示のたびに適用する（通知は原文を保持する。詳細は「伏せ字モード」の項）。

SSH 経由でボットを見守り、サービス操作・デプロイを行う開発者用の画面（React + TypeScript の画面と、FastAPI + asyncssh のバックエンド）。ボットとは依存関係も起動も分けており、ボットの `requirements.txt` とは別に管理し、CIでは専用ジョブを使う。セットアップ・起動・設計は `tools/console/README.md` にある。

Discordの参加サーバー一覧は`limit=200`・`after`で最大50ページ取得し、整数のIDで重複排除・保存設定との照合を行う。後続失敗・429・取得上限・進捗停止・時間切れは`ok: false`・`partial: true`で取得済み行と未取得の保存設定を残し、画面に部分取得を明示する。Discord全体20秒・スクリプトの外部待機50秒の予算を使い、SSHの60秒は延長しない。ボイス状態の照会は100サーバーまでとし、429・残時間不足以降の追加照会を省略する。ID・トークン・APIの生エラーを新たに出力しない（#251）。

- **ボットのコード（`src/`）をダッシュボードのために変えないこと。** 値は SSH で読める既存の情報源（systemd・journald・`bot.db` の読み取り専用・サーバー上のトークンでの Discord API の読み取り）から集める。ボットの import もしない（手元の `database.py` の版も `ast` で読むだけ）。
- **実行できるコマンドは `backend/remote_commands.py` のホワイトリストだけ。** 画面から届いた文字列をコマンドに埋め込まない。ホスト鍵の検証を切らない（`known_hosts=None` にしない）。127.0.0.1 だけで待ち受け、専用ヘッダー・Host・Origin を確かめる。
- **デプロイは `backend/deploy.py` が管理する。** 接続先の `deploy_branch`（既定 `main`）を `origin` から取得して早送りし、`requirements.txt` か `constraints.txt`（`remote_commands.DEPENDENCY_FILES`）が変わったときだけ設定した `python` で `pip install`・`pip check` を実行して再起動する（`constraints.txt` を外すと、推移依存の固定版の更新が本番に入らない）。ブランチの不一致・Git 管理対象の未コミット変更・サーバーにだけあるコミットは事前確認で拒否する。更新後に失敗したらコードと更新した依存関係を戻し、再起動に進んでいた場合は前の版で再起動・起動確認を行う。**コードの巻き戻しは `git reset --hard` を使わない**（#381）— 事前確認の後にサーバーで手動の編集・ブランチの切り替え・コミットが行われうるため、`remote_commands.deploy_rollback_command` が戻す直前に1回の遠隔コマンドの中で「ブランチが `deploy_branch`」「HEAD がデプロイ前か取り込み先・取り込み後」「追跡ファイルに変更なし」を確かめ、外れていれば何も変えずに止まる（終了コード `ROLLBACK_EXIT_*`。`rollback_failed` で、変更を消さない手動の手順を案内する）。実際に戻すのは `git reset --keep`（確認の後の編集とも衝突すれば git が中止する）。★ 終了コードの番号と `tests/fake_ssh_server.py` の `respond_rollback`、本物の git で確かめる `RollbackCommandRealGitTests` をそろえて直すこと。`bot.db` は戻さない。同時実行は1件だけとし、最後の記録はメモリ上に保持する。
- **起動確認は「ログイン成功」の文言に依存する。** `src/bot.py` の `on_ready` が出す `ログイン成功: <ボット名>` を `backend/remote_commands.py` の `LOGIN_SUCCESS_TEXT` で探す（最大120秒）。再起動後の PID の変化・稼働状態・自動再起動の回数も確かめる。文言を変えたら判定側・`backend/tests/fake_ssh_server.py`・関連テストと説明をそろえる。ログイン後の各機能の実動作は別途確認する。
- **自動の確認で本番の接続先へつながないこと。** 模擬 SSH サーバーは `backend/tests/fake_ssh_server.py` の自動テスト専用とし、起動スクリプトや画面の接続先には出さない。SSH・デプロイのテストはループバックの空きポートと一時フォルダの鍵・known_hosts を使う。bot.db を読むスクリプトは、模擬サーバーが一時フォルダに作る架空の DB（架空の利用者・会話）に対して本当に実行する。実際の接続先は `backend/profiles.json`（Git 管理外）に書く。
- **見本（`backend/profiles.example.json`・`examples/ssh_config.example`・`examples/sudoers.example`）の書き換える値は `YOUR_` で始める**（`.env.example` と同じ流儀）。`profiles.py` の `_PLACEHOLDER` がこの形の値が残った接続先を見分け、バックエンドは接続を断る（仮のホスト名を DNS が偶然引いて、知らないサーバーへつなぎに行かないように）。見本の値の書き方を変えたら、`_PLACEHOLDER`・3つの見本・`tests/test_profiles.py` をそろえて直すこと。実際の値は見本ではなく `backend/profiles.json`（Git 管理外）に書く。
- **依存は公開から2週間以上たった版に固定する**（`frontend/package.json` は完全一致。`.npmrc` の `save-exact`）。依存の追加は、ボットと同じく事前に相談する。
- **管理コンソールの共通処理（#353）:** 接続・状態取得のポーリングは `frontend/src/controllers.ts`、キーボード操作は `useConsoleShortcuts.ts`、ログの分類・伏せ字は `logPresentation.ts` に置く。ログは保持中の全行を判定してから末尾抽出・検索を行う。バックエンドの中断耐性待機は `async_utils.await_shielded` を使い、呼出元は中断の記録を確認して必要な後片付け後にキャンセルを再送出する。SSH の JSON 読取では接続世代を確認する。
- **bot.db のテーブルの中身（データページ）:** 利用者が行数の一覧でテーブルを選んだときだけ、`backend/remote_scripts/db_table.py` が `mode=ro` で行を取り出す（会話・記憶・リマインダーの本文と ID を含む。集計の `DB_SUMMARY_SCRIPT` は件数だけのまま）。表示できるテーブルは `backend/remote_commands.py` の `DB_TABLES`・`db_table.py` の `TABLES`・`frontend/src/dbTables.ts` の `DB_TABLE_SPECS` の3か所で一致させる（テストで照合している）。ボットのスキーマに列やテーブルを足したら `DB_TABLE_SPECS` と `fake_ssh_server.py` の架空の DB も直し、ID・名前・本文の列は伏せ字の対象の種類（`isSecretKind`）にすること。会話統計の収集を止めた人（`stats_opt_out = 1`）の発言サンプルと解析はサーバーから持ち帰らない。ID は JavaScript の数値で丸まらないよう文字列で返す。表示名（`user_names`）は表示中の行に出てくる ID だけを `WHERE user_id IN (...)` で引く（`NAME_LOOKUP_CHUNK` 個ずつ。古い SQLite の変数上限 999 未満）。`reminders`・`user_stats` を全件読む形に戻さないこと（記録が増えるほど1ページの表示が遅くなる）。API の `offset`（0以上）・`limit`（1〜`DB_TABLE_PAGE_MAX`）は `Query` で検証し、範囲外は 422 にする（#399。`db_table_command` の丸めは直接呼ばれた場合の最後の砦）。上書きの順序（新しいリマインダーの名前 → 会話統計の名前を最優先）も保つ。
- **伏せ字モード（`frontend/src/privacy.ts`）:** 画面が知っているホスト名・ボット名・サーバー名などを集め、エラー文・ログ・コマンド履歴の中からも探して隠す。秘密になりうる値を新しく表示したら `collectSecrets` に足すこと。ログ本文は下記の受信時区分で保守的に隠し、本文内の書式や区切り線を公開可能な境界にしない。ボットの `usage_graph.log_snapshot` の書式を変えたら、会話見出しを識別する `logPrivacy.ts`・`backend/tests/fake_ssh_server.py`・`backend/remote_scripts/insights.py`（`User (` と `Total token count` を数える）も合わせて確認する。
  - **ログの伏せ字は保守的（`frontend/src/logPrivacy.ts`）:** 各行は受信時に `messagePrivacy`（`unknown`/`conversation`/`system`）を付け、バッファ・一時停止中の保留・保持上限・クリア後も保つ。取得の最初と会話見出し以降は区切り線・計数・運用ログ風の本文・別PIDの行でも公開へ戻らない。伏せ字中に本文を出すのは journald の `pid=1` かつ `source=systemd` の行だけ（既知の秘密のみ置換）。ボットの出力は構造化されておらず本文と通常運用を確実に区別できないため、**通常運用を含む systemd 以外の全ログ本文を意図して隠す**。概要は保持行すべてを判定してから末尾を切り出し、検索・表示も同じ判定を使う。デプロイは `deploy.Step.output_contains_logs` が真（再起動ログを含んだ時点で、抜粋・8000文字の切り詰め後も維持）か属性なしの旧形式なら出力全体（コンソールの診断文も）を隠し、偽と明示した通常のコマンド出力だけ既知の秘密を置換する。伏せ字OFFは原文。伏せ字のためにボットへ専用ログ出力を追加しない。
  - **画面の伏せ字は描画時（#216）:** 接続先ラベル・現在/配備先ブランチ（Overview・Sidebar・TopBar・ConnectionPage・`pages/shared.tsx`、title/aria 属性を含む）は専用欄として `mask`、自由文・デプロイの進捗/見出し/説明/確認ラベル・aria は `redact` を描画時に通す。確認ダイアログの本文とデプロイのタイトルは `Privacy` を受け取る描画関数で保持し、保存済みの ReactNode に原文を固めない（開いた後の伏せ字ON/OFFにも追従する）。表示を伏せても `profile_id`・`connected_at`・確認入力の照合・API 送信値・接続変更時の確認取消・古いコールバックの拒否は変えない。新しい表示欄は秘密の登録だけでなく描画時の適用まで確認すること。
  - **通知は原文を保持し、描画時に伏せる（#217）:** `ToastProvider` は通知の原文（と下記の `detailSafe`）だけを保存し、`ToastViewport` を `PrivacyContext` の内側に置いて `Snackbar` が title・detail・action.label を描画のたびに処理する。登録時の伏せ字処理や選択済みの文面は持たないので、表示済みの通知も伏せ字の切り替え・後から増えた秘密値・hover/focus 中の保持に追従する。**`ToastViewport` は今後も `PrivacyContext` の内側に1つだけ置くこと**（外に出すと原文が出る）。通知の登録時に `redact` した文字列を保存し直さないこと。
  - **SSH 設定エラーの安全な文面（#218）:** 欠損した秘密鍵・明示した `known_hosts`・既定の `known_hosts` のエラーは、通常表示の診断文（従来どおりローカルのパスを含む）とは別に `SshError.safe_message` を持ち、API の `detail_safe`・ログWebSocketの`message_safe`（フックの`messageSafe`に保持し、ログ画面の本文とツールチップにも適用）・接続状態の `last_error_safe`・操作履歴の `error_safe`、フロントの `ApiError.safeMessage`・通知の `detailSafe` へ通す。表示は `privacy.ts` の `redactError(text, safeText, privacy)` が伏せ字中だけ安全な文面へ切り替える。安全な文面はローカルの home・所有者・任意のパス・独自のファイル名を `<LOCAL_HOME>`・`<LOCAL_PATH>` などに置き換え、ファイルの種類・`.ssh` の相対位置・`profiles.json` の設定項目・ホスト鍵を登録する対処は残す（実際の場所は伏せ字OFFで確認する）。再接続・切断で現在の安全な文面は消し、履歴は保持する。ログ接続開始のエラーに安全な文面がなければ、WebSocketは固定の再試行案内を`message_safe`として返す。新しい SSH 設定エラーを足したら安全な文面も同時に作ること。外から接続が切れた場合（`_handle_connection_lost`）は、切断理由に接続先のホスト名・IP・パスなど画面が秘密として知らない値が入りうるため、理由を含まない定型文 `CONNECTION_LOST_SAFE_MESSAGE` を `last_error_safe` と履歴の `error_safe` に入れる（#399）。
  - **運用ログの名前・識別子（#219）:** `privacy.ts` は、ボットの運用ログ（`services/autonomous_service.py` の配信開始・接続失敗など、`voice_guard.py` のボイス移動検知・復帰）が出す過去のチャンネル名と `guild`・`channel`・`guild_id`・`channel_id`・`user_id`・移動前・移動後の識別子を、現在の接続・insights の名前一覧が無くても隠す（名前欄・識別子欄の対応表を共通の `redact` と `redactOperationalMessages` に適用。systemd の行・接続エラーとツールチップ・ログを含まないデプロイ出力・通知など `redact` を通る文が対象。systemd 以外のログ本文は上記の受信時区分で従来どおり全体を隠し、この処理で緩めない。伏せ字OFFは原文で、伏せ字のためにボットへ専用ログ出力を追加しない）。名前の中の括弧・装飾・改行を途中で切らないよう、対応する最後の接尾辞まで広く隠すため、同じ書式のレコードが同じ範囲に複数あると間の診断文も伏せることがある。名前とみなす範囲は Discord のチャンネル名の上限に余裕を持たせた長さ（`NAME_SPAN`）までに限り、離れた行の接尾辞まで一致させない（上限を外すと長いログ全体が隠れ、照合も重くなる）。原文の各開始位置で重なる名前の範囲も集め、範囲を統合してから伏せる。先に文字列を置換して後続の開始位置を失う形へ戻さないこと。**ボット側のログ書式を変えたら `privacy.ts` の対応表も合わせて直すこと**（★壊れやすい点）。
- **動き:** CSS の `--spring-*` は `frontend/src/theme/motion.ts` がばねの物理計算から作り、最初の描画より前に書き込む（CSS に静的な値は無い）。跳ねない Standard が既定（利用者の好み）。ページ・テーマ・伏せ字の切り替えは `viewTransition.ts` の `withViewTransition` を通す（`<html>` の `data-view-transition` で CSS が動きを選ぶ）。ダイアログをボタンの位置から広げる `useEmergeFrom` は Web Animations で動かすため、その間は `data-emerge` で CSS の transition を止める（transition の方が優先されるので、止めないと動きが打ち消される）。閉じる動きを見せる開閉は `components/Collapse.tsx`（`usePresence` で縮み終わってから外す）を使う。
- **確認:** `tools/console/backend` で `.venv\Scripts\python.exe -B -m unittest discover -s tests`、`tools/console/frontend` で `npm run build`（`tsc --noEmit && vite build`）。

コンソールの専用ランチャーはバックエンドだけに標準入力パイプを渡し、`PERSONA_CONSOLE_MANAGED=1`を設定する。画面終了時はパイプを閉じ、`backend/managed_server.py`がuvicornへ正常終了を要求する。デプロイ完了・巻き戻しとSSH切断を最大60分待ち、期限超過時だけ強制終了してログと終了結果へ失敗を残す。通常の`app.py`単独起動は標準入力を終了通知として扱わない。

遠隔の終了通知とチャネル回収を応答期限・キャンセル後も待つのは、`DeployRunner`が送るデプロイのコマンド（取り込み・依存更新・巻き戻し・pip check・その前後の確認）と、手動のサービス起動・停止・再起動とする。`SshSession.run(wait_for_exit=True)`で明示的に選び、開始から終了・チャネル回収までを別タスクで所有する。呼出元の繰り返しキャンセルでも、このタスクを打ち切らない。シェルへの終了要求だけでは子のgit・pipやsystemdジョブが残る可能性があるため、シグナルで打ち切らず自然な終了を待つ。この経路の応答期限は終了待ちを打ち切る期限ではなく、終了が来るまでは操作の排他を保ち、画面に状態不明・終了確認待ちを示す。読み取り用の`run`は従来どおり指定した期限で`SshError`を返し、通信エラーでも終了未確認フラグを立てず、再接続できる。

手動サービス操作はSSHプロセスの既知の終了後も、`systemctl show <service> --no-pager --all --property=Job`で当該unitのジョブがないことを確認する。`Job=`（systemctlの空表示）と`Job=0`だけを終了とし、空の応答・不正形式を終了扱いしない。ジョブが残れば最大60秒・0.5秒間隔で共通ロックを保持して照会する。SSHの応答期限超過後に終了した場合や非0終了でも確認し、通信断・終了通知なし・確認失敗・ジョブ待機の期限超過では`command_state_unknown`を保持して後続の更新を拒否する。待機中の一時表示は確認完了で外すが、終了不明の記録は再接続・接続先変更でも消さない（#250）。

デプロイの早送り送信後は、遠隔コマンドの終了とチャネル回収を確認してから実HEADを再取得し、元のコミットと一致した場合だけ未変更と扱う。依存更新も終了確認後に巻き戻す。キャンセル時も必要な巻き戻しの完了を待ってからキャンセルを伝える。デプロイのコマンドの通信断・終了通知なし・シグナル終了は遠隔の子プロセスまで終了した保証がないため、`unknown`（状態不明）を記録し、デプロイ内のHEAD確認・巻き戻し・後続コマンドを送らない。状態不明の間はデプロイ・巻き戻し・サービス操作だけを409で拒否し、概要・ログ・DB閲覧などの読み取りとSSHの再接続・切断は許可する。状態不明は再接続で消さず、更新操作の排他を保持する（実行中の終了待ち・巻き戻しが続く間は従来どおり接続変更も拒否する）。サーバーで残留処理の終了とコード・依存関係・サービスの状態を手動確認した後にバックエンドを起動し直す（記録はメモリ上のため、再起動自体は遠隔処理の終了を保証しない）。

デプロイタスクの実行中を示す`running`と、状態不明による更新操作の拒否を示す`state_unknown`は分け、`unknown`でタスクが終わった後の`/api/deploy`は`running: false`と`deploy.status: "unknown"`を返す。

接続状態API・接続・切断の応答も`state_unknown`を返す。これは`SshSession.command_state_unknown`・`DeployManager.state_unknown`と手動サービス操作の終了確認待ちを合わせたセッション全体の状態で、終了不明の記録は別の接続先へ移ってデプロイ記録が画面から消えても保持する。画面全体に残留処理とサーバーの状態を確認してからバックエンドを再起動する案内を残し、デプロイ・サービス操作のボタンと確認の開始を止める。表示は固定文だけとし、接続先などの識別情報は追加しない。

中断後の巻き戻し・復旧で想定外の例外が起きても、終了時刻と失敗（遠隔処理が終了未確認なら状態不明）を記録してから、元のキャンセルを伝える。終了通知とタスクの実行中判定も通常の終了と同じ経路を通す。

デプロイと巻き戻しの起動確認は、再起動直後に確認したPIDとjournaldの`_PID`を照合する。PID不明・旧PIDの成功行は採用しない。時刻境界の1秒の余裕は維持し、ログ待機中と成功確定直前にもPID・自動再起動回数を再確認する。

プロフィール統合の運用ログは件数だけを出し、ユーザーIDを含む辞書は出さない。中止時のステージ枠回収失敗もチャンネル名の伏せ字対応表と回帰テストに含める（#288）。

## アーキテクチャ

全体像の図は `docs/フロー図.drawio`（draw.io 形式。起動フロー・メッセージ受信〜応答・`on_message` パイプライン・自律行動の 4 ページ）にある。図はコードを書き写したものなので、構成を変えたら図も直すこと。

### 配線 (`bot.py`)

`bot.py` がコンポジションルート（合成の起点）。設定を読み込み、`genai_client.create_genai_client` で共有SDKクライアントを生成し、`Database`（`database.py`、後述）を最初に初期化してから（`PersonaClient` にも渡し、終了時に閉じさせるため Discord クライアントより先に作る）、各サービスのインスタンスを1つずつ生成して、DI フレームワークを使わず手作業で相互に注入する。共有される可変ランタイム状態はすべて単一の `BotState` データクラス（`app_state.py`）にまとめられ、必要なサービスに渡される。**ただしペルソナ・モデル・応答モードなど「サーバーごとに変わる設定」は `BotState` 直下ではなく `GuildSettings` に入っている**（「ギルド単位の設定」の項を参照）。`register_commands`（`commands.py`）がスラッシュコマンドを `CommandTree` に登録する。`on_ready` でコマンドを同期し、各種の見回りループ（`memory_maintenance_loop` / `auto_topic_loop` / `autonomous_loop` / `anniversary_loop` / `reminder_loop` / `feedback_prune_loop`。いずれも `background.make_guarded_loop` で作る）を開始し、文書ワーカーのコルーチンを起動する。ループの二重起動は `BotState` の `*_background_started` フラグで防ぐ（`on_ready` は再接続のたびに発火するため）。`on_voice_state_update` は `VoiceGuard.handle_voice_state_update`（ボイス接続先の保護）と `VoiceConversationService.handle_voice_state_update`（音声会話の参加者の出入り。#268）へ、それぞれ別の `try` で委譲する（後述）。

### 背景処理と終了 (`background.py`, `bot_client.py`)

`PersonaClient.background_tasks`（`TaskOwner`）がボット単位の所有者。`PersonaClient.start` とDiscordイベントからコンテキストを引き継ぎ、その配下の `spawn` を自動登録する。単独で起動する処理は `spawn(..., owner=client.background_tasks)` または `with client.background_tasks.scope()` で所有者を明示する。管理外の処理や別の所有者のタスクは終了対象に含めない。周期処理は `make_guarded_loop(..., owner=client.background_tasks)` で登録する。

`TaskOwner.run` は通常は呼出元のTaskを追跡する。Taskを伴わず手動で駆動された場合は本体を所有者付きの子Taskへ渡し、同じ受付停止・終了待機の対象にする。サービス終了通知も所有者を明示し、回収後にも完了済みか確認してから共有リソースを閉じる。

スラッシュコマンドはDiscordが本体タスクを直接生成するため、コンテキストの継承だけでは所有集合へ登録されない。`bot.py`から`register_commands(..., task_owner=client.background_tasks)`へ明示的に渡し、`/functions`・`/join`・`/leave`の共通入口で本体と失敗通知までを`TaskOwner.run`に登録する。本体の例外は所有期間内で`handle_command_error`へ渡し、通知は`wait_for_cleanup`で完了を待つ。停止開始後はブラックリスト判定を含む本体を開始せず、実行中の本体・失敗通知と保護付きの後片付けを回収してから共有接続を閉じる。所有者を省略した単独登録では専用の`TaskOwner`を使う。

終了は一つのタスクを共有する。受付停止・周期処理のキャンセル後、文書ワーカー・自律配信・音声会話の終了callbackを先に呼び、所有するイベントと背景タスクを中断して終了を待つ。`run_in_thread` の同期ワーカーと `wait_for_cleanup` の後片付けは直接キャンセルせず、親タスクも完了まで待つ。終了通知と回収の共通期限は `BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS`。超過時は `TimeoutError` を返し、使用中の共有リソースを閉じない。停止中の新規タスクもキャンセル完了まで追跡する。終了タスク自身は所有集合から分離し、後片付けを通常の `spawn` で起動しないこと（停止後は新規受付を拒否する）。回収後に Discord → Gemini → DBの順に閉じる。`close` の重複・呼出元の繰り返しキャンセルでも終了通知は一度だけ実行する。期限超過後の再終了では、同じ終了通知タスクを待ち直す。同期スレッドそのものの強制終了はできないため、期限超過は正常終了を意味しない。

### ギルド単位の設定 (`app_state.py`)

退出時は `forget_guild()` が旧 `GuildSettings` を無効にする。`PanelView.ensure_settings_current()` でパネル受付時と設定更新の非同期待機後に確認し、古いパネルには `/functions` での開き直しを案内する。`persist()` もロック取得後に無効な設定を拒否する。保存ロックは同じサーバーの退出前後で共有し、再参加時の復元は開始済みの旧保存が完了するまで待つ。ロックの索引は弱参照とし、過去のサーバーIDを溜めない（#342）。

`on_guild_remove` は `guild_lifecycle.remove_guild_state` へ委譲し、自律配信の停止完了後に、そのギルドの設定・ウェルカム投稿済み記録・ボイス保護をメモリから外す。停止の例外やキャンセルでもキャッシュを回収し、他のギルド・DMとDB上の保存済み設定は保持する。

退出待機中の再参加は `on_guild_join` の最初の `prepare_guild_join` で区切る。退出中だけ保持する `guild_removals` の所有印を外し、旧設定・旧ウェルカム記録・旧接続保護を同期的に回収してから、新しい状態を受け付ける。退出側は停止待機後に所有印と設定の同一性を照合し、新しい音声会話を停止したり、新しい設定・保護・案内記録を消したりしない（#349）。

保存済みの自発会話間隔は`AUTO_TOPIC_INTERVAL_CHOICES`にある整数だけを復元し、`bool`や選択肢外は既定値へ戻す。

設定列の読込変換は `Database._guild_settings_values` に集約し、起動時の全件復元と再参加時の対象サーバー復元で共有する。保存列を追加したら、この共通変換と `GuildSettings.apply_persisted`・保存処理を揃える。

同じボットを複数の Discord サーバーへ招待しても設定が混ざらないよう、可変設定は**サーバー（ギルド）ごとに分離**されている。

- **`GuildSettings`**: ペルソナ（`current_prompt_file`。空文字は「既定のペルソナに従う」で、実際に使う値は `snapshot_response()` が一覧から解決する。「ペルソナの一覧」の項を参照）・モデル（`current_model_name`）・応答モード（`current_respond_mode`）・グラウンディング（`grounding_enabled`）・会話リセット地点（`channel_reset_points`）・自発会話の設定と監視状態（`auto_topic_*`）・自律行動の設定と配信状態（`autonomous_*`。管理者が指定した配信先 `autonomous_stage_channel_id` / `autonomous_voice_channel_id` を含み、`None` は「指定なし」）・記念日の投稿記録（`anniversary_posted_dates`）を持つ。
- **`BotState.get_guild_async(guild_id)`**: 通常会話・機能ハブ・文書処理の非同期入口。未登録なら `Database.guild_state_load` を `run_in_thread` で待ち、対象サーバーの保存済み設定と記念日の投稿記録を復元してからキャッシュへ登録する。退出後の再参加でも同じ経路を通る。サーバーごとの読込ロックで同時要求に同じ設定を返し、DB待機でイベントループを塞がない。読込失敗・キャンセル時は既定値を公開せず、次のアクセスで再試行する。退出時は `forget_guild` で読込中の古い結果も無効にする。機能ハブは復元待機前に本人限定で応答を保留し、完了後に元の応答を編集する。同期の `get_guild` は既存キャッシュ・DM・DB未接続時の既定値生成だけに使い、DB接続済みの未登録サーバーは非同期入口を使わせるため例外にする。同期の活動観測は `get_guild_if_ready` を使い、DB接続時の未復元サーバーは見送る。DB未接続なら従来どおり既定値を生成でき、配信終了や外部移動の処理はキャッシュだけを参照する。起動時の一括復元は取得済みの値を使い、個別の追加読込を行わない。**`guild_id=None`（DM）は `BotState.dm_settings` を返す** — DM にはギルドが無いため、全 DM で1スロットを共有する。`iter_all_guild_settings()` は「全サーバー + DM」を横断する見回り処理用。
- **`BotState` に残すもの**: 最終活動時刻（`last_activity_timestamp`）とバックグラウンドタスクの起動フラグ類のみ（サーバーに依存しないプロセス全体の状態）。
- **永続化**: 設定値（ペルソナ・モデル・応答モード・グラウンディング・自発会話の ON/しきい値・自律行動の ON・配信先）と記念日の投稿記録は DB に保存され、起動時に `BotState.restore_from_database(db)`（`bot.py` の `Database` 初期化直後に1回）が復元する。**設定を変えたら `await guild_settings.persist()` を呼ぶこと** — UI の確定ボタンやトグルの直後がその場所。保存はフェイルオープン（失敗してもインメモリの変更は残る）。`persist()` は `_persist_lock` で直列化し、**保存する値はロックを取ってから読む**（保存は行の総入れ替えなので、並んだ2つの保存の書き込み順が入れ替わると古い値で上書きされる）。書き込みは `run_in_thread` で待つ（素の `asyncio.to_thread` に戻すと、キャンセルの瞬間にロックが外れて同じ上書きが起きる）。記念日の記録（`record_anniversary_post`）も `run_in_thread` で待つ。`get_guild_async()` が復元・生成する `GuildSettings` には `attach()` で DB が引き継がれるので、呼び出し側は何もしなくてよい。`restore_from_database` は、復元より前に `get_guild()` で作られていた設定にも `attach()` する（つながないと、その設定の `persist()` が DB へ書かれない）。
- **永続化しないもの**: `channel_reset_points` / `auto_topic_channels` / `auto_topic_posted_at` / `autonomous_session` / `autonomous_last_activity` / `autonomous_recent_activity` は実行時に自然に再構築される一時状態のため、意図的にインメモリのみ。ここを永続化したくなった場合はチャンネル単位の書き込みが毎メッセージ走る点に注意すること（`autonomous_*` の実行時状態はそもそもボイス接続が再起動をまたげないため、復元しても意味がない）。
- **規約**: サービス層は `BotState` から設定を勝手に読まないこと。メッセージ・コマンドの非同期入口が `await state.get_guild_async(guild_id)` で解決し、**`GuildSettings` かその中の値（例: `model_name`）を引数で渡す**。`GeminiService.generate()` / `summarize_document()` が `model_name` を必須引数に取るのはこのため（サービスが「どのギルドの設定か」を知らずに済む）。固定モデルを使う経路（`stats_service.py` のルーターなど）は `ROUTER_MODEL_NAME` を明示的に渡す。UI の各 View も `BotState` ではなく `GuildSettings` を受け取る。

### `on_message` パイプライン (`message_handler.py`)

受付前に配信が終わって通常会話へ移る発言は、`is_autonomous_comment` を `False` に戻して通常の委任判定を使う。本文なし添付の画像確認が空でも、現在は配信終了済みで通常会話の受付条件を満たせば、文書受付・画像失敗案内へ進める。読込済み画像は空の結果も再利用する。`watch_channel` は入力判定後の配信再確認と通常枠の確保を終えた発言だけで行い、判定中に配信が終わった場合も登録する（#256）。

`bot.py` のイベント関数は、配線時に生成した `MessageHandler.on_message` へ委譲する。`MessageHandler` は従来の生成引数・メソッド・外部境界の差し替え口を持つ窓口で、受付は `message_admission.py`、振り分けは `message_routing.py`、応答生成と後処理は `message_response.py` が担当する。各ステージへ渡すのはサービス（`MessageHandler` のフィールド）と、型付きの外部操作 `message_operations.MessageOperations`（履歴取得・長文返信・読み上げ・画像取得・ペルソナ読込・背景タスクの6つ）だけで、モジュール全体は渡さない（#395）。外部操作は `message_handler` の同名の属性から**呼出しのたびに**組み立てる（テストは構築後に `patch.object(message_handler, ...)` で差し替えるため、構築時に固定しないこと）。不足・呼び出せない操作は構築時に `TypeError` になる。定数・型・判定や組み立ての純粋な関数は各ステージが直接 import し、差し替え口にしない。ステージのログは分離前と同じ `persona_bot.core.message_handler` のロガー（`MESSAGE_LOGGER_NAME`）に出す。★ 外部操作を足すときは `MessageOperations`・`message_handler` の import・ステージの呼出しをそろえる（`tests/core/test_message_operations.py` が、契約と実装の引数の食い違い・未使用の操作・契約外の参照・ステージ内の未定義の名前を検出する）。起動処理とは独立して同じ実装をテストできる。ボット以外のメッセージはすべて、決まった順序の関門を通過する。編集時はこの順序を守ること:

1. **応答モードのゲート** — `"mention"` モード（デフォルト）では、DM 以外でボットへのメンションが無いメッセージは無視される。**例外は自律行動の配信中のチャンネル**（`autonomous.is_comment_channel`）で、そこへの書き込みはメンション無しでも「コメント」として拾う。ゲートより前（ただし 2 のブラックリスト判定の後）に `auto_topic.mark_activity` と `autonomous.mark_activity` を呼び、ボット宛てでない発言も「会話が行われている」観測として記録する。**ゲートで返信の対象外になった発言でも、ボットがいま接続しているボイス/ステージチャンネルの付属チャット（`discord_helpers.is_connected_voice_chat`。判定のたびに `guild.voice_client` を見るので、移動・切断に追従する）だけは、3 のローカル NG 判定と違反記録を行ってから終了する**（#172）。「返信する条件」と「NG 判定の対象」を分けたもので、AI 判定・返信・統計は行わない（宛先でない発言ごとに Gemini を呼ぶとコストが発散するため）。それ以外のチャンネルへ監視範囲を広げないこと。
2. **ブラックリスト**（`blacklist_service`）— ブロック中のユーザーは黙って破棄される。**この判定だけは 1 より前、サーバー設定の解決と活動の観測よりも先に行う**（後に置くと、無視しているはずの発言で自発会話の沈黙タイマーが戻り、自律行動のゲージや無応答での退出まで左右される）。
3. **NG ワードフィルタ**（`ng_word_service`）— 登録語とクリーニング済み本文を同じNFKC・casefold・ひらがな統一で正規化し、ゼロ幅文字・非空白の制御文字を除いて正規表現で照合する。一致した入力は `notify_violation` へ渡す。空白・区切り記号は飛ばさない（#282）。
   **通常会話の受付枠** — ローカルNG判定後、入力AI判定・委任用の履歴取得・画像読込より前に `ChatAdmission.try_acquire` で同期的に確保する。利用者ごとの直近 `CHAT_RATE_WINDOW_SECONDS` 秒の受付数（`CHAT_MAX_REQUESTS_PER_USER`）はサーバー・DMをまたいで共有し、同時実行はサーバーごと（全DMは1枠群）の `CHAT_MAX_CONCURRENT_PER_GUILD` 件、全体の `CHAT_MAX_CONCURRENT_GLOBAL` 件まで。満杯なら待機や追加のGemini呼出しを作らず、件数超過と混雑を区別した定型案内を `message.reply` で返す（メンション通知なし）。案内は利用者ごとに同じ期間内で1回までで、判定と記録の間に await を挟まない。案内記録も最大 `CHAT_RATE_MAX_USERS` 人とし、期間内の記録は追い出さず、満杯なら新規の案内を控える。送信失敗はログだけに残し、期間内の再送はしない。受付記録は最大 `CHAT_RATE_MAX_USERS` 人で、期間内の記録を追い出さず新規利用者を見送る。文書受付・リマインダー・委任・例外・キャンセルを含む全経路の `finally` で実行枠を返す。失敗・中断でも期間内の受付数は戻さない。状態は `MessageHandler` 内に持ち、永続化しない。
   **配信コメントの受付枠** — NG ワード判定の後、本文なしの画像添付も含め、画像読込と入力モデレーションの前に `autonomous.claim_comment_slot` で同期的に確保する。枠が無いコメントは画像ダウンロード・デコード・API呼出しをせず終了する。読込開始後に画像が使えないと判明してもクールダウンは消費する。受付前に配信が終わっていればコメント枠を取らず通常会話へ進む。活動観測はゲート前に済んでいる。
4. **ユーザー入力の AI モデレーション＋記憶ルーティング＋リマインダー抽出**（`content_moderator.check_and_route`）→ 不適切なら `notify_violation`。安全なら記憶カテゴリ（`profile`/`projects`/`none`）と、リマインダー依頼なら日時＋本文も同時に得る。**対象テキストが同一なので3つの判定を1回の Gemini 呼び出しに統合して API コストを抑えている**（個別に呼べば3回）。戻り値は `(is_safe, reason, category, reminder)` の4つ。**本文に「サブエージェント」の明記がある発言（`agent_plan.requests_subagent`）だけは、代わりに `check_route_and_plan` を呼び、同じ1回の判定に委任の判定を足す**（直近の会話 `AGENT_ROUTER_HISTORY_LIMIT` 件も渡す。配信コメントは対象外）。戻り値は5つ目に `AgentPlan | None` が付く。詳細は「サブエージェント」の節。
5. **配信コメントの振り分け** — モデレーション後に `is_comment_channel` を取り直し、配信中なら `autonomous.handle_comment` とセッションを照合した読み上げへ直接進む。未対応添付のエラー返信・文書キュー・通常統計・リマインダー登録・長期記憶操作は行わない。配信が終わっていて通常経路の条件も満たさなければ終了する。配信コメントは専用制限を維持し、通常枠を消費しない。配信終了後に通常経路へ移るときは、その時点で通常枠も確保し、満杯なら同じ案内制限を通して終了する。
6. **通常チャットの添付の振り分け** — 未対応の拡張子は `describe_supported_attachments` で対応形式を案内して終了。文書（`pdf/txt/csv/md`）は `MAX_DOCUMENT_SIZE_BYTES` を確認し、ワーカーへキュー登録して受付だけ返信する（未処理件数の上限で断られたら `DOCUMENT_QUEUE_FULL_NOTICE` を返し、受付完了とは言わない）。画像だけなら通常チャットへ進み、本文が空なら `IMAGE_ONLY_QUESTION` を使う。画像を読めなければ生成せず案内する。
7. **通常チャットの会話統計とリマインダー** — `stats.record` を背景実行し、抽出済みのリマインダー依頼を `create_from_request` へ渡す。確認文が返れば返信して終了し、`None` なら会話へ進む。文書処理はこれより前なので優先される。
8. **通常チャットの応答** — 委任すると決まった依頼（`AgentPlan.delegate`）は `_handle_agent_request` と `core/agent_request.py` で子に作業させてから、それ以外は直接 `handle_chat_message` で応答する。**テキストの返信は読み上げない**（#268。`/join` のボイスチャンネルでは Gemini Live が声で会話するため、読み上げると声が重なる。`play_tts_response` を使うのは 5 の配信コメントへの応答だけ）。**`handle_chat_message` は送信（`send_long_reply`）の失敗だけを呼出元へ伝える**。親密度は応答モデレーションを通過し、`send_long_reply`が完了した後だけ更新・保存する（#262）。送信失敗・送信待ちのキャンセルでは変更しない。送信後の親密度更新が失敗してもログに残し、評価・記憶を続ける。送信後の評価対象の登録（`_track_reply_for_feedback`）と記憶への追記（`_remember_reply`）は、それぞれ失敗してもログに残して送った本文を返す（#157）。追記に失敗したときは成功扱いにせず、記憶圧縮も起動しない。再生成・再送はしない（二重返信になる）。キャンセルは `except Exception` に掛からないので従来どおり伝わる。

`notify_violation` と音声会話は `violation_notice.record_violation_notice` で違反を1回記録し、3回目でユーザーをブラックリスト登録する（`BLACKLIST_MAX_VIOLATIONS`）。並行して届いた別の違反で既にブロック済みのとき（`record_violation` は回数を増やさず `just_blocked=False` を返す）は何も返信しない（「あと0回」の警告や二重の案内を出さない）。記録できた回数（`違反記録: N/3 回目`）とブラックリスト登録はログに出す（「NGワード検出」のログは記録より前に出るため、それだけでは数えられたか分からない。#173）。**保存に失敗した違反は回数に数えない** — `BlacklistService` は保存に成功してからメモリの記録を差し替えるため、失敗した違反はどこにも残らない。このとき例外を外へ出さず、ログに失敗を残し、利用者には「あと N 回」を付けない警告だけを返す（事実と食い違う通知をしない）。保存の再試行はしない。NG ワードとモデレーションは、送信前に**ボット自身の応答に対しても再度**適用される（応答中の NG ワードは ● でマスクされ、不適切な応答は定型の謝罪文に差し替えられる）。

違反通知の送信失敗は `DiscordException` を捕捉して型をログへ残し、保存済みの違反を取り消さない。`BlacklistService.unblock` はブロック中だった記録の解除だけを成功とし、未ブロックの警告回数は保持する。

### プロンプト組み立て (`MessageHandler.handle_chat_message` → `prompting.py`)

通常チャットは `GeminiService.generate_with_metadata` の `GenerationResult(text, usage, sources, model_name)` を受け取り、採用した生成モデルを評価へ渡す。検索用サブモデルを使っても選択設定を書き換えず、生成時のペルソナのスナップショットと組にする。メタデータが不要な呼出元では従来の `generate`（3要素の戻り値）を使う。

感情タグはモデレーション前に除去するが、親密度・感情の更新は `screen_bot_response_with_status` の通過後だけ行う。差し替えた謝罪文は会話記憶・記憶圧縮・評価対象へ含めず、出典も付けない。文書返信も同じ判定結果で会話記憶への追記を制御する（文書要約自体と最終応答の両方を通過した場合だけ要約を保存する）。`screen_bot_response` は本文だけが必要な既存の呼出元向けの入口として維持する。

文書要約の Gemini 応答に使用量があれば `DocumentService` が `UsageTracker.log_tokens` で記録する。要約JSONが不正な場合も応答を受け取った時点で記録するが、会話件数を表す `User (` 行や文書名・要約本文は出さない（#352）。

メッセージごとに、単一のフラットなプロンプト文字列を組み立てる。構成要素は、ペルソナのテンプレート + 親密度/感情ブロック + 選択された長期記憶 + 文書要約 + チャンネル履歴 + 現在の質問。履歴の添付は、画像なら `[画像添付あり]`、それ以外（文書など）は `[添付ファイルあり]` と区別して書く（`build_history_text`。すべて画像と書くと、PDF を添えた発言までモデルが画像付きと誤解する）。従来の `GeminiService.generate` は `(text, usage_metadata, grounding_sources)` を返す。

`fetch_channel_history_text` / `build_history_text` は呼出元から受け取る `is_blocked` で現在ブロック中の利用者を除き、他ボットも除外する。自ボットの返答は維持する。通常会話・委任の判定・文書返信・自発会話・記念日・自律行動の全8取得箇所へ同じ判定関数を渡し、サービスへは`bot.py`から注入する。履歴の取得・除外・整形が失敗した場合は空文字を返し、追加のAI呼び出しやブロック対象の履歴なしで会話を続ける。キャンセルは伝播する。文書の `exclude_message_id` とリセットポイントも引き続き適用する。会話要約の `build_summary_source_text` も同じ除外を使い、判定関数はハブから `swap_panel` を通じて引き継ぐ（#281）。

`prompting.sanitize_prompt_data` は、生の発言・質問・コメント・名前・記憶・文書要約・子の報告などに含まれる見出し行とロール行を全角化する。発言本文へ適用した後、履歴整形側が正規の`ユーザーの質問:` / `BOTの回答:`を付ける。履歴・要約元ログのビルダーは無害化済みの結果を `SanitizedHistory` として返し、プロンプト組立時に再走査しない。通常の文字列は `preserve_history_roles=True` で正規ロールを維持しつつ見出しを再確認する。外部入力を直接 `SanitizedHistory` に包む入口は作らない。処理はべき等で、通常文と改行・コードが生成したセクション・ペルソナ・JSON出力の形式契約を維持する。区切りの無害化だけでAIへの判定誘導を完全に防ぐものではない（#281）。

### データベース (`database.py`)

SQLiteのロック待機上限は `DATABASE_BUSY_TIMEOUT_SECONDS`（既定30秒）。`sqlite3.connect(timeout=...)` が同じ接続の `busy_timeout` をミリ秒で設定するため、別のPRAGMAで重複設定しない。期限までにロックが解放されなければ例外を返し、無制限の再試行は行わない。

guild_settings の列構成は初期化時に検証し、保存時は `schema_version` が変わった場合だけ `table_info` を取り直す。外部のスキーマ変更で未知の列が増えた場合は、設定の置換で消さずにエラーにする。

スキーマ初期化では `BEGIN IMMEDIATE` で明示的にトランザクションを開始し、初版テーブルの作成・全未適用DDL・`PRAGMA user_version` を同時に確定する。DDLは接続のコンテキストマネージャーだけでは自動でトランザクションを開始しないため、この開始処理を外さないこと。途中失敗時は開始前のスキーマと版へ戻り、再起動で再試行する。移行成功のログはコミット後に出す。

スキーマ14は記念日巡回の`profile_load_all_for_key`用に`idx_profile_key_user`（`key, user_id`）を追加する。既存の`(user_id, key)`主キーと初版DDLは変更しない。索引にはプロフィール本文を含めず、容量・書込負荷の増加とキー検索の全件走査回避を一時DBで確認する（#249）。

`Database` は窓口（ファサード）で、責務を2つのモジュールへ分けている（#400）。**`db_migrations.py`** は初版のテーブル作成（`CREATE_TABLE_STATEMENTS`）・バージョンごとの変更（`MIGRATIONS`）・未適用分を実行する `apply_migrations`。**`db_legacy_importers.py`** は旧 `affinity.json`・`user_stats.json`・`queue.json`・`pending_log.txt`・`profile.json` を読んで解釈する関数で、DB・ロック・ログに触れない（`tests/core/test_db_modules.py` が import を検査する）。DB への書き込み・ロック・取り込み後の改名・ログは `Database` が担い、ログの出元は分離前と同じ `persona_bot.core.database`。値の変換（`_affinity_row_params`・`_user_stats_row_params`・`_queue_row_params`）は通常の保存と共有するため `database.py` に置く。
- ★ **`SCHEMA_VERSION = <整数>` は `database.py` に置いたままにする。** 管理コンソールの `local_repo.py` がこの行を ast で読む。`_QUEUE_ACTIVE_STATUSES` と `DELETE FROM reminders WHERE status IN (...)` も、画面のテスト（`dbTables.test.mjs`）が `database.py` の本文から正規表現で読む。`MIGRATIONS` の最大のキーとの食い違いはテストが検出する。
- ★ **`Database._apply_migrations` は自分のモジュールの `SCHEMA_VERSION`・`_MIGRATIONS` を `apply_migrations` へ渡す。** テストがこれらを差し替えて、段階的な移行や途中失敗を再現するため（`_MIGRATIONS` は `db_migrations.MIGRATIONS` と同じ辞書オブジェクトの別名）。同じ理由で、`free_backup_path` の連番の上限（`LEGACY_BACKUP_MAX_SUFFIX`）も呼び出し側から渡す。

SQLite（`data/memory/bot.db`、WAL モード）が構造化データの永続化先。データの読み書きの SQL 文はすべて `database.py` に集約されている（スキーマの定義と適用は上の `db_migrations.py`）。テーブルは10個: `affinity`（親密度・感情）、`user_stats`（会話統計）、`queue_tasks`（文書処理キュー）、`pending_log`（記憶圧縮前の一時ログ。`user_id` 列でユーザー別）、`profile`（プロフィール記憶。`(user_id, key)` の複合主キーでユーザー別。値は JSON 文字列）、`guild_settings`（サーバーごとの設定。`guild_id` が主キー。DM は `DM_SETTINGS_GUILD_KEY`＝`0`。**保存は `INSERT OR REPLACE` による行の総入れ替え**なので、列を足す際は `guild_settings_fields.py` の保存列と実行時属性の定義、および復元時の値の検証を更新する。DB の列と共通定義が一致しない場合は保存を拒否する）、`anniversary_posts`（記念日のお祝い投稿記録。`(guild_id, user_id, label)` が主キー）、`reminders`（リマインダー。`status` は `pending`/`done`/`canceled`。見回り用に `(status, remind_at)`、本人の登録時の上限確認・一覧・件数用に `(user_id, status, remind_at)` の索引つき。`recurrence` は `none`/`daily`/`weekly`/`monthly` で、繰り返しは通知後も `pending` のまま `remind_at` だけが進む）、`feedback`（返信への 👍👎 評価。`(message_id, user_id)` が主キーで押し替えは上書き。集計用に `(guild_id, created_at)`、古い評価の削除用に `(created_at DESC, message_id DESC)` の索引つき）、`legacy_imports`（旧ファイル名と内容のSHA-256による取り込み済み記録）。`projects.json` は LLM が構造を自由に決めるため、テーブル化せず JSON ファイルのまま（ただし `{"_schema": "per_user_v1", "users": {user_id: {...}}}` の形でユーザー別に束ねる）。**`affinity`/`user_stats`/`profile`/`pending_log`/`projects.json` はすべて `user_id` で分離され、記憶は完全にユーザーごとに独立している**（他人の記憶が混ざらない＝プライバシー保護）。

- **スキーマ変更の手順**: `db_migrations.MIGRATIONS` にそのバージョンの SQL 文を追記し、`database.py` の `SCHEMA_VERSION` を1つ上げる（`MIGRATIONS` の最大のキーと同じ値）。適用済みバージョンは `PRAGMA user_version` に記録され、起動時に未適用分だけが実行される。`db_migrations.CREATE_TABLE_STATEMENTS`（初版の形）は変更禁止 — 変更は必ず `MIGRATIONS` 側で行う（実例: バージョン2 = `profile` テーブル追加、バージョン3 = `profile` の複合主キー化＋`pending_log.user_id` 追加、バージョン4 = `guild_settings` / `anniversary_posts` テーブル追加、バージョン5 = `reminders` テーブル追加、バージョン6 = `reminders` へ繰り返し列（`recurrence` / `recurrence_anchor`）追加、バージョン7 = `feedback` テーブル＋索引追加、バージョン8 = `user_stats` へ統計収集の停止列（`stats_opt_out`）追加、バージョン9 = `guild_settings` へ自律行動の有効/無効列（`autonomous_enabled`）追加、バージョン10 = `reminders` へユーザー別の索引（`idx_reminders_user_status`）追加、バージョン11 = `legacy_imports` 追加、バージョン12 = `feedback` へ日時・メッセージIDの降順索引（`idx_feedback_created_message`）追加、バージョン13 = `pending_log` へ `(user_id, id)` の索引（`idx_pending_log_user_id`）追加。記憶圧縮の巡回が利用者ごとに行う取得・範囲削除の全件走査を避ける。#207、バージョン14 = `profile` へ `(key, user_id)` の索引追加（#249）、バージョン15 = `guild_settings` へ自律行動の配信先列（`autonomous_stage_channel_id` / `autonomous_voice_channel_id`。0 = 指定なし）追加（#266））。**`SCHEMA_VERSION` は自分が追加した分ちょうどまでしか上げないこと** — 番号を飛ばすと、後から埋めた小さいバージョンは `user_version` が既に先へ進んでいるため永久に実行されない。
- **ユーザー別記憶への移行（バージョン3）**: 導入前は単一ユーザーのテスト運用のため記憶を「全体共有の1スペース」で持っていた。移行時、旧・全体共有データ（`profile` 行・`pending_log` の未割当行・`projects.json` の中身）は失わないよう `LEGACY_MEMORY_OWNER_ID`（`constants.py`。旧・唯一の実利用者の Discord ユーザーID）の記憶として引き継ぐ。SQL では担えない「旧行を owner へ割り当てる」後始末は `_migrate_legacy_profile_rows`（冪等。スキーマ移行本体とは別トランザクション）が行う。引き継ぎ元が不要になれば `LEGACY_MEMORY_OWNER_ID` を `""` にしてよい。`""` のときは旧データを破棄する — 旧 `profile` 行に加え、`pending_log` の未割当行（`user_id = ''`）も起動時に削除し、旧 `pending_log.txt` は取り込まない（ファイルは `.imported.bak` として残る）。残すと `user_id=''` の「誰でもない利用者」として記憶圧縮の対象に数えられ、誰も読まない記憶のために Gemini を呼び続ける（`pending_distinct_user_ids` も念のため空の `user_id` を返さない）。
- **旧 JSON からの取り込み**: 初回起動時、旧ファイル（`affinity.json`/`user_stats.json`/`queue.json`/`pending_log.txt`）が残っていれば DB へ取り込み、`<元の名前>.imported.bak` に改名する（バックアップ兼二重取り込み防止）。**同名の退避ファイルが既にあるときは上書きしない**（#379）— `_free_backup_path` が取り込みの前に `<元の名前>.imported.<連番>.bak`（連番は `LEGACY_BACKUP_MAX_SUFFIX` まで）の空き名を決め、空きが無ければ取り込まずに元のファイルを残す。★ 引き継ぎ先が無い `pending_log.txt` は DB へ入らず、退避ファイルが唯一のコピーになる。改名を `path.replace(固定名)` に戻さないこと。JSON として読めない・最上位が object でない（`queue.json` は `tasks` が配列でない）ファイルは `_load_legacy_json_object` が例外にするので、取り込まず改名もせずに残して警告し、次回起動時に再試行する。**`_json_loads_or` で `{}` 扱いに戻さないこと** — 戻すと、取り込めなかったファイルが「取り込み済み」として改名され、誰も気づけない。
旧`pending_log.txt`は元バイト列のSHA-256とファイル名を`legacy_imports`へ、会話と同じトランザクションで保存する。改名だけ失敗した場合は次回に取り込みを省いて改名を再試行する。内容が異なる元ファイルや、元ファイル内・通常会話の同文の発言は削除しない。取り込み自体が失敗すれば会話と記録をともにロールバックする。

- **profile.json は手書き追加用の受け皿**: 毎起動時、`profile.json` に中身があれば `profile` テーブルへマージ（同名キーはファイル側が勝つ）し、取り込めた場合だけ `write_json_file` で原子的に `{}` に戻す。ユーザー別記憶に対応するため形式は `{ユーザーID: {キー: 値, ...}, ...}`（全キーがASCII数字のユーザーID、全値がdictの場合だけユーザー別と判定。それ以外の旧・全体共有形式はownerへ取り込む後方互換あり）。JSON として読めない場合や、旧形式の owner が未設定・全ユーザーの内容が空などで1件も取り込めない場合は、ファイルを残して警告する。DB保存やリセットの失敗時も原本を保持し、次回起動時に再試行する（手書きミスやI/O障害でデータを失わない）。
- **並行処理**: 各サービスが `run_in_thread`（＝別スレッド）から呼ぶため、接続は `check_same_thread=False` + 内部の `threading.Lock` で直列化。書き込みはトランザクションで原子的（旧 JSON 方式と違い、書き込み途中のクラッシュでデータが壊れない）。`close()` も同じロックを取り、実行中の読み書きが終わってから閉じる（何度呼んでもよい）。終了時は `PersonaClient.close()` が文書ワーカー停止・遠隔ファイル回収を待ってから Discord → Gemini → DB の順に閉じる（DB を最後にするのは、終了処理中の保存が使うため）。

### JSONファイルの保存 (`storage.py`)

`write_json_file` はJSONへの変換成功後に親ディレクトリを作り、同じディレクトリの一時ファイルへUTF-8で書き、flush・fsync・close後に `os.replace` で差し替える。置換前の失敗では元のファイルを保ち、一時ファイルを回収する。既存ファイルの権限モードも引き継ぐ。`load_json_file` は `utf-8-sig` で読み、手で直した `projects.json`・`blacklist.json` が「UTF-8 (BOM 付き)」でも BOM 無しと同じ内容として扱う（`utf-8` に戻すと、正しい記憶が BOM だけで破損扱いになり退避されて空になる。#153）。未作成時のみそのまま既定値を返し、空ファイル（BOM だけのものを含む）・不正JSON・不正UTF-8・最上位がobject以外の場合は `<元名>.corrupt-<UTC日時>-<識別子>.bak` へ退避後、ログを出して既定値で再開する。退避失敗や読込のI/Oエラーは例外を伝播し、その後の既定値による上書きを防ぐ。破損前の内容を自動復元する機能はなく、復旧にはバックアップを確認する。実行時ファイルは従来どおり単一プロセスで扱う。

### 記憶システム (`memory_service.py`)

圧縮不能な pending_log の消去と失敗回数のリセットは `_clear_compact_pending` の同じ同期ワーカーで行う。DB消去成功後にキャンセルが伝わっても、新しいログへ古い失敗回数を持ち越さない。DB消去が失敗した場合は回数を残す（#349）。

圧縮結果は `io_lock` の下で `_save_compacted_memory` を1回の `run_in_thread` に渡し、繰り返しのキャンセルでも全保存の終了を待つ。`Database.save_compacted_memory` はプロフィール置換と対象ログ削除を同じトランザクションで行い、その確定前に同期コールバックで最新の文書要約を引き継いだJSONを保存する。コールバックからDBメソッドを呼ぶと再入不可のロックで停止するため、ファイル保存だけを行う。JSON保存失敗時はDBをロールバックし、DB確定失敗時は元のJSONの復元を試みる。失敗回数は全保存の成功後に戻す。プロセス強制終了や復元も失敗する障害に対する、DBとファイルをまたぐ完全な原子性は保証しない。

`build_contexts` は非同期の記憶取得入口。`io_lock` の下でワーカーへ処理を渡し、通常記憶と文書記憶を同じスナップショットから組み立てる。`projects.json` は1回の取得につき1回だけ読む。ファイルを手書きした場合も次回の取得で反映し、永続的なキャッシュは持たない。個別取得の `build_context` / `build_document_memory_context` もawaitする。文書要約保存・圧縮時のJSON読込と書込・プロンプトの主経路の読込もイベントループへ同期I/Oを持ち込まない。

カテゴリでルーティングされる長期記憶。**すべて `user_id` でユーザー別に分離される。** DB の `profile`/`pending_log` テーブルと、`projects.json`（`data/memory/` 配下の JSON。`users[user_id]` にユーザーごとのスライスを持つ）で構成される:
- **ルーティング**: 各質問を `profile` / `projects` / `none` に分類し、**そのユーザーの**スライスだけをプロンプトに読み込む。チャット主経路では `content_moderator.check_and_route` がモデレーションと同時に分類する（`route_category` は文書ワーカーなど主経路以外で使う）。`route_category` はあいさつ・相槌などの短い定型発言を正規表現（`_looks_memory_irrelevant`）で先に弾き、LLM を呼ばず `none` を即決して API を節約する。失敗時は `none` にフォールバック（安全側）。
- **書き込み経路**: 各返信の後、Q/A を発言者の `user_id` 付きで非同期に `pending_log` テーブルへ追記する。
- **コンパクション（圧縮）**: `flush_if_needed`（`MEMORY_MAINTENANCE_MINUTES` ごとのバックグラウンドループ。返信後にも便乗して発火する）は、`pending_log` を持つユーザーを列挙し、**ユーザーごとに個別に**圧縮する（他人の会話ログを同じ Gemini 呼び出しに混ぜない＝プライバシー保護）。各ユーザーは Q/A 件数のしきい値（`MEMORY_FLUSH_THRESHOLD_ENTRIES`）に達するか、そのユーザーがアイドルのときに、そのユーザーの `pending_log` を `profile` テーブル/`projects.json` の該当スライスへ圧縮統合する（他ユーザーのスライスには触れない）。1回のフラッシュで処理する人数は `MEMORY_FLUSH_MAX_USERS_PER_RUN` で上限を設け、一度に大量の API 呼び出しが走らないようにする（残りは次回の見回りで処理）。**巡回は前回の続きのユーザーから始める**（#380）— 一覧は昇順なので、毎回先頭から回すと、先頭のユーザーが例外（`MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS` 回の連続失敗で巡回を打ち切る）を出し続けたときに後続が永久に見送られる。`_flush_cursor`（メモリのみ。確認したユーザーを成功・見送り・失敗・上限による中断のどれでも `finally` で記録する）を `_order_from_cursor` が使って1周分を回転させるので、1回の試行数と連続失敗の上限は変わらない（昇順固定に戻さないこと）。フェイルセーフ設計で、パース失敗時は pending ログを**保持**（リトライ）するが、3回連続失敗した場合 — またはセーフティフィルタによるブロック時 — は無限ループを避けるためそのユーザー分をクリアする。連続失敗回数はユーザー別に数え、ログをクリアしたとき（ブロック時も）は 0 に戻す（消したログの失敗を新しいログへ持ち越すと、一時エラー1回で上限に達して消えてしまう）。`io_lock` + `flush_lock` で保護されている。
- **文書要約は圧縮の対象外**: `projects.json` の `document_summaries` は圧縮プロンプトへ**渡さない**（プロンプトが「不要な古い情報は削除」と指示するため、渡すと LLM の判断ひとつで要約が消えたり別のキーへ書き換えられたりして戻せない）。保存時（`_save_compacted_projects`）は、書き込み直前に読み直した最新の一覧をそのまま引き継ぎ、LLM の出力に同名のキーがあっても捨てる。圧縮を待つ間に `store_document_summary` が追記した分も、この読み直しで自然に残る。要約の件数管理（`DOCUMENT_SUMMARY_LIMIT`）と同一ファイルの重複除去は、単件入口からも共用する`_store_document_summaries`が担う。

読込・保存の例外はユーザー単位で分離し、原データと生成失敗回数を保持して後続へ進む。読込失敗も処理人数の上限へ数え、連続する通常例外は`MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS`で巡回を打ち切る。キャンセルは伝播する。`flush_lock`→`io_lock`の取得順序と、保存時のDB・JSONの整合性は維持する。

一時ログの保存は空・空白だけ・未設定のユーザーIDを拒否する。件数しきい値未満の圧縮は、各ユーザーの最後の`pending_log.created_at`でアイドル時間を判定し、別ユーザーの活動や再起動に左右されない。

### 文書パイプライン (`document_service.py`)

全添付の要約が成功した後、本文と補足項目をNGマスクし、一括でAI出力判定に通す。安全な要約は`build_contexts(pending_summaries=...)`で今回の依頼の文脈だけに渡し、最終応答の出力判定も通過してから投稿者の要約を`store_document_summaries`で一括保存する。拒否された要約や応答は長期記憶へ保存しない。ファイル名は要約プロンプトへ渡す前に1行へ整え、制御文字・区切り表記を無害化する。同名・同じSHA-256のファイルは既存要約を置き換え、再試行で重複させない。途中で失敗した添付群は保存しない。従来のハッシュが無い要約は保持する。

返信直前の`queue_tasks.status='sending'`の永続化から、送信・完了状態の保存・会話ログ追記までを一つのコルーチンにまとめ、全体を`wait_for_cleanup`で待つ。通常の単発・繰り返しキャンセルは記録開始後ならすべて完了してから伝播し、保護開始前なら未送信の`processing`を再開可能に残す。再起動時は`processing`だけを再開し、`sending`は送信結果不明の`failed`として自動再送しない。DiscordとSQLiteは同時に確定できないため、送信開始の記録直後にプロセスが強制終了すると未送信の可能性もある。履歴で実際の返信を確認してから必要な分を再依頼する。

起動時の中断タスク復旧は、成功するまで `DOCUMENT_WORKER_IDLE_SECONDS` の待機付きで再試行する。成功前は通常の取得・処理を始めず、成功後は復旧を繰り返さない（実行中の正常なタスクを `pending` へ戻さない）。停止・キャンセル時も復旧中の同期処理を回収する。

ダウンロード後も実バイト数の上限を検証する。旧キューで投稿者IDが未設定（`0`を含む）なら取得したメッセージの投稿者を使い、要約保存・記憶取得・会話ログ追記を同じユーザーへ揃える。回答後の記憶追記と完了状態保存は個別に保護し、保存失敗を`failed`への変更や二重のエラー返信へ変えない。回答前の失敗状態保存が失敗しても、利用者への失敗通知は試みる。

**失敗の返信に例外の本文を載せないこと**（#220。返信は公開チャンネルに出て、ペルソナ読込の `FileNotFoundError` などは内部のパスを含む）。返信は `build_failure_notice` が作り、定型文 `DOCUMENT_FAILURE_NOTICE` に添えてよいのは `DocumentUserError`（利用者が対処できる検証エラー: サイズ超過・暗号化PDF・UTF-8で読めないテキスト・対応する添付なし）の文面だけ。それ以外の例外は**一般の `ValueError` も含めて**定型文だけを返す（Gemini の失敗や pypdf 未導入のように利用者が直せない失敗も同じ）。例外の型と本文は `describe_error` でログと `queue_tasks.error` にだけ残す。`DocumentUserError` の文面は固定の案内と利用者自身の添付のファイル名だけで作り、内部の値を差し込まないこと。ファイル名を載せるため、失敗の返信も `REPLY_ALLOWED_MENTIONS` で返信先の本人以外へ通知させない。

質問は、受付時に NG 判定と入力モデレーションを通して `queue_tasks.question` に保存した値だけを使う（空なら `DOCUMENT_ONLY_QUESTION`。#203）。ワーカーが再取得した投稿の本文は、受付後に編集されていても誰も検査していないので生成へ渡さない（編集に気づいたらログだけ残す）。履歴からも元の投稿を除く（`fetch_channel_history_text` の `exclude_message_id`。除かないと編集後の本文が履歴経由でプロンプトへ入る）。再取得した本文を優先する形に戻さないこと。添付は編集で外すことしかできず差し替えられないため、再取得した一覧を使う。文書の取得後、最初の添付処理より前に `snapshot_response()` を取得し、全添付の要約・プロンプト読込・最終生成で共用する。`build_contexts` の文書件数は `max(DOCUMENT_MEMORY_CONTEXT_LIMIT, len(attachments))` とし、同時添付が既定件数を超えても先頭の要約を落とさない。PDFは `with temp_path.open("rb")` のストリームを `PdfReader` へ渡し、検証の例外時も閉じる。検証ワーカーは `run_in_thread` で終了まで待ってからキャンセルを伝播し、Windowsで削除前にハンドルを解放する。終了タスクの状態保存と古いタスクの掃除は別に扱い、掃除の例外はログだけに留める（完了済みタスクを失敗へ戻したり、回答後にエラーを二重返信しない）。

添付ファイルはチャットをブロックしないよう別系統で処理される。`enqueue_from_message` がタスクを DB の `queue_tasks` テーブルに書き込み（`author_id` を含む）（未処理＝`pending`/`processing`/`sending` の件数を利用者別 `DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER`・全体 `DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL` で制限する。件数の確認と追加は `Database.queue_try_enqueue` が同じロック・トランザクションで行うので、並行した受付でも上限を超えない。確認と追加を2回の呼び出しに分けないこと。満杯なら追加せず `None` を返す。#202）、単一のワーカーコルーチンが `pending` タスクを取得し、一時ファイルへダウンロードし、検証し、Gemini へアップロードし、構造化 JSON へ要約する。各添付のアップロード済みファイルと一時ファイルを回収してから、全要約をNGマスクとAI判定に通し、保存前の文脈から返信を生成する。最終応答の判定も通過した場合だけ、**投稿者（`author_id`）の** `projects.json` スライスへ一括保存する。起動時、`processing` のまま止まっているタスク（処理中にボットがクラッシュしたもの）は `pending` へ戻される。完了済みタスクは `QUEUE_MAX_FINISHED_TASKS` 件に切り詰められる。タスクの取得（claim）は SELECT と UPDATE を同一トランザクションで行うため二重取得されない。PDF の検証に使う `pypdf` は `validate_pdf_file` の中で import する（モジュールの先頭へ戻さないこと — 未導入の環境で起動と回帰テストの検出まで止まる）。未導入なら PDF だけが日本語の `ValueError` で失敗し（利用者には定型文だけを返す）、PDF の回帰テストは省略される。

### Gemini 呼び出し (`gemini_service.py`, `genai_client.py`)

`google-genai` の共有 `Client` を、生成・モデレーション・記憶ルーティング・記憶圧縮・文書処理へ注入する。接続先の種類はGemini Developer APIを明示し、APIキーはSDKがヘッダーに載せる。独自REST経路は使わない。Discord側は `bot_client.PersonaClient` を使い、終了時に `close_genai_client` でSDKの非同期・同期接続と、factoryが所有するHTTPX接続を閉じる（その後に、渡された `Database` も閉じる）。SDKは渡されたHTTPX接続を閉じないため、共通終了処理を使う。

JSON 応答の生成設定は `genai_client.generate_json_content`、ブロック・本文・必須キーの確認は `storage.read_json_response` に集約する。生成は `genai_client.generate_content` → `client.aio.models.generate_content` へ集約する。画像はPIL画像をSDKに渡し、検索は `types.Tool(google_search=types.GoogleSearch())` を指定する。検索非対応を示す400/404/422（検索ツールの名前か、渡すツールは検索だけなので「tool」への言及と、非対応の理由を含むもの。画像を添えた試行では「multimodal」への言及も対象。「media」「image」だけの失敗は画像自体の問題なので代替しない。#398）と、検索付きの試行の利用枠超過（429）の場合だけ、設定が有効なら `GROUNDING_MODEL_NAME` で検索を試し、最後にユーザーの選択モデルで検索なしへ進む（Gemini 3 以降の検索は無料枠に含まれず 429 になるため、無料枠ではサブモデルの Gemini 2.5 の検索枠で検索する）。タイムアウト・5xx・認証・検索なしの生成の利用枠超過・その他の通信エラーでは追加生成しない。サブモデルを使っても、最後の非検索生成には元の `model_name` を渡す。

`GeminiService.generate` は開始時にmonotonicな締切を1回だけ決め、各段に残り時間だけを渡す。共通入口は秒をSDKの `HttpOptions.timeout`（ミリ秒）へ変換し、`HttpRetryOptions(attempts=1)` で生成のSDK内部リトライを止め、`asyncio.wait_for` でも同じ時間を制限する。期限切れは `(None, None, [])`、利用者からのキャンセルは伝播する。timeout未指定の検索付き生成は `GROUNDING_REQUEST_TIMEOUT_SECONDS` を使う。記憶取得や生成後のモデレーションはこの生成期限の対象外。

アップロード・削除は `client.aio.files`、文書要約は共通の生成入口を使う。PDFは `application/pdf`、txt/csv/mdは `text/plain` を明示し、ファイル転送にも全体の期限を設ける。factoryは新旧SDKに共通する`vertexai=False`でDeveloper APIを選び、旧SDKにない`enterprise`引数は指定しない。公開HTTPXクライアントと`async_client_args.transport`へ同じトランスポートを指定し、aiohttp経路のSDK独自再送を避ける。転送transportはuploadコマンドの応答に必須の状態ヘッダーがない場合、応答を閉じて明示的に失敗させる。これによりSDK内の同期sleep・再送を防ぎ、イベントループと期限管理を保つ。SDKのprivate実装は上書きしない。

Geminiのテキストは `storage.safe_response_text` から取得する。空候補・プロンプトや候補の安全ブロックでは本文を返さず、思考パートはSDKの本文取得で除外される。`get_prompt_block_reason` は新SDKの文字列enumと旧形式の数値enumを区別し、未指定をブロックと誤認しない。モデルのJSONは `storage.extract_json_object` でコードブロックを優先し、ネスト・文字列内の括弧を標準パーサーで処理する。不正候補の後も走査し、モデレーションは `required_keys=("safe",)` で説明中の別形式のJSON例を除外する。

`GeminiService._generate` は採用前に本文を `storage.contains_internal_response_text` で検証する。SDKが構造化して返す思考・コード・関数呼び出しは本文取得で除かれるが、それらが目印を失って通常のtextとして返った応答（`tool_code`・`tool_outputs` だけの行、行頭の `print(google_search.`、先頭行だけの `thought`）は、思考と最終回答の境目を切り分けられないため削らずに破棄し、同じモデル・検索設定で `GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS` 回まで生成全体の期限内で再生成する。上限に達したら `(None, None, [])` を返す。Discord・読み上げ・会話記憶・利用ログへ流れる前の共通の入口なので、呼び出し側に個別の検証を足さないこと。診断ログ（`describe_response_parts`）にはモデル・検索の有無・終了理由・パートの種類と件数だけを出し、本文・プロンプト・検索語を載せない。文中の `thought` や通常のコード例まで検出対象を広げないこと（正常な回答まで再生成・失敗になる）。

### 親密度 / 感情 (`affinity_service.py`)

ユーザーごとの2次元感情（valence/arousal、30分の半減期で 50 へ減衰）に加えて、緩やかに変動する親密度。DB の `affinity` テーブルに永続化される。モデルには返信の末尾に `[V:X, A:Y]` タグを付けるよう指示しており、`handle_chat_message` がタグを抽出・除去する。本文が空でなくモデレーションを通過した場合だけ、親密度・感情を更新して対象ユーザー分の保存を開始する。保存は `asyncio.Lock` で直列化される。

旧JSON・通常保存の数値`None`は既定値を使い、親密度・機嫌・テンションは`AFFINITY_MIN`〜`AFFINITY_MAX`へ収める。統計・文書キューの数値`None`も0、空の文字列項目は空文字、キュー状態は`pending`を使う。

通常の保存は `AffinityService.save_background(user_id)` → `Database.affinity_upsert(user_id, entry)` で対象1行だけを更新する。`affinity_save_all` は明示的な全件置換用で、返信ごとの保存には使わない。旧 JSON の取り込みを含む3か所の保存パラメータ（列順・欠けた値の既定値）は `database._affinity_row_params` に集約してある。列を足すときはこの関数と3か所の INSERT 文をそろえて直すこと。ロック内で対象の現在値だけをコピーし、キャンセル時も `run_in_thread` の完了までロックを保持する。親密度・機嫌・テンション・`last_interaction` の4項目を保存・復元し、半減期・中立値・上下限・親密度の増減しきい値は `AFFINITY_*` から参照する。

**感情タグを扱うコードはこのモジュールに集約すること。** `[V:X, A:Y]` の正規表現は `_AFFINITY_TAG_PATTERN` の1か所だけに置く（空白と大文字・小文字のゆらぎを許容する。`[v:+5, a:-10]` も抽出・除去する）。`extract_affinity_changes(text)` は `(本文, (機嫌の変化量, テンションの変化量))` を返し（末尾に連続するタグだけを除去して最後の変化量を採用し、本文中の引用は保持する。末尾のタグが無ければ変化量は `None`、本文は無加工）、`strip_affinity_tag(text)` は除去だけを行う（自発的な会話・記念日のお祝い・会話要約のように、特定の相手への返信ではなく親密度を更新しない経路で使う）。読み上げ用の `strip_speech_tags(text)` はこれを再利用し、`_SPEECH_EMOTION_TAG_PATTERN` で感情名（happy / sad / angry / neutral / excited / calm / surprised / scared）と arousal / valence / affinity の数値タグも除去する（カンマ区切りの複合形式を含む）。一般の角括弧付き本文は保持する。以前は同じ正規表現が `bot.py` / `auto_topic_service.py` / `summary_view.py` に3重定義されており、空白の許容などに差異が生まれるとタグがそのままユーザーへ露出する不具合に直結していた。

### ボイス / TTS (`voice.py`, `voice_dsp.py`)

接続ごとの再生制御は `audio/playback.py` に集約する。TTSとLiveは同じロック・停止済みスレッドの待機を使い、音源の所有者を確認してから停止する。スレッドの終了は `run_in_thread` 経由で待ち、イベントループをブロックしない。

**読み上げ（TTS）を使うのは自律行動の配信だけ**（第一声・独り言・配信コメントへの応答。#268）。`/join` のボイスチャンネルは Gemini Live が声で会話し（次の「音声会話」の節）、テキストの返信は読み上げない。Live の音声と読み上げは同じ接続で同時に流れない（`/join` の接続がある間は自律配信を始めないため）。

閉じていないコードブロックも末尾まで読み上げから除く。旧キューのワーカーが終了しても、新しいキューの参照は削除しない。

edge-tts単独の待機は `TTS_EDGE_TIMEOUT_SECONDS`（既定10秒）で打ち切り、全体の `TTS_TIMEOUT_SECONDS`（既定30秒）の残りでgTTSへ退避する。外部キャンセルでは退避を始めず伝播する（#290）。

gTTSへの退避では、各HTTP要求へ `TTS_GTTS_CONNECT_TIMEOUT_SECONDS`・`TTS_GTTS_READ_TIMEOUT_SECONDS` を渡す。外側の合成期限や停止・キャンセルでも同期ワーカーを放置せず、処理完了とバッファの回収を待つ。複数のHTTP要求を含む退避処理では合成期限を超えて終了を待つ場合があるため、通信期限を外さない。

`_play_pcm` は接続単位の弱参照ロックで再生・停止・回収を直列化し、再生中・一時停止中の既存音声を停止する。再生待ちのキャンセル時にも`stop()`を試し、再生スレッドの終了を待ってから音声ソースの`cleanup()`と所有するPCMバッファの`close()`を行う。固定版discord.pyの`_player`参照は`_get_audio_player`へ隔離する。呼出元は依頼のVoiceClient同一性・投入時のチャンネル・任意の`is_valid`をまとめた判定を`is_valid`として渡し、`_play_pcm`はロック取得後と前の`AudioPlayer.join`の完了後に再照合する（終了済みスレッドでもexecutorの順番待ちの間に移動・差し替え・配信終了が起こりうるため）。最後の照合から`play()`までは`await`を挟まず、無効な依頼は現在の音声を止めずに破棄する。`stop()`はスレッドをjoinせず、`after`はスレッド内のcleanupより先に呼ばれるため、どちらも終了の代わりにはならない。繰り返しのキャンセルでもjoinを終えてから伝播する。切断時はdiscord.py内部の接続待機期限まで回収を待つことがある。`PCMAudio.cleanup()`だけでは入力ストリームは閉じないため、バッファの回収も維持する。

`play_tts_response(message, response_text, prompt_file)` は、**ボットがボイスチャンネルに接続している間だけ**（自律行動で自分から入っているとき）配信コメントへの返信を読み上げる。未接続・DM・整形後に本文が残らない場合は黙って何もしない（no-op）。チャット本体は読み上げの成否に影響されないフェイルオープン設計。`prompt_file` は `MessageHandler` が今回の応答に使った `ResponseSettings.prompt_file` を渡す（サービス層に `BotState` を読ませない規約どおり、値だけを渡す）。

**元メッセージを持たないテキストは `speak_text(guild, text, prompt_file)` を使う。** `play_tts_response` はこれを `message.guild` 付きで呼ぶだけの薄い層で、実体（接続確認・整形・キュー投入）はすべて `speak_text` 側にある。自律行動の独り言のように「どの発言への返信でもない」読み上げはこちらを直接呼ぶこと（キュー・ワーカーを二重に実装しないため）。

自律配信の第一声・独り言・コメント応答では、両入口の `is_valid` にセッションと接続先の照合関数を渡す。キューには本文・ボイスプロファイルと一緒にこの関数も保持し、投入前・合成前・再生直前に確認する。通常チャットも含め、全依頼へ投入時のVoiceClientとチャンネルIDを記録し、合成前・再生直前に照合する。切断・接続差し替え・同じVoiceClientの移動後は旧依頼を捨てる。同一接続・同一チャンネルの再利用時はキューを保持し、ワーカー終了時は所有するキューだけを回収する。

**音声エンジンは edge-tts（主）＋ gTTS（退避）の二段構え。** `_fetch_mp3` が主エンジンの **edge-tts**（Microsoft Edge の読み上げが使うニューラル音声。API キー不要）で合成し、例外または空応答なら **gTTS**（`lang=TTS_LANG`）へ退避する。edge-tts は公式に third-party 利用が認められた API ではなく過去に Microsoft 側の仕様変更で使えなくなった経緯があるため、**この退避経路を消さないこと**（消すと壊れた瞬間に読み上げ機能ごと止まる）。退避中は話者・高さ・速さを再現できずキャラ差が失われるが、音色（EQ）だけは掛かる。退避と復旧のログは `_edge_tts_failing` で**状態が変わったときだけ**出す（失敗が続く間、返信のたびにログが溢れないようにするため）。

**FFmpeg には依存しない。** どちらのエンジンも返す mp3（24kHz モノラル）を **miniaudio**（C ライブラリを同梱した pip の binary wheel。exe 不要）で**メモリ上のまま** `TTS_SAMPLE_RATE`（48kHz）/ `TTS_CHANNELS`（ステレオ）/ 16bit の生 PCM へデコードし、`discord.PCMAudio` に渡す。Discord のボイス送信はこの形式をそのまま受け取れるため、変換用の外部プロセスも一時ファイルも要らない（CPU 1コア・メモリ 1GB 級の VPS で動かすための構成）。**`discord.FFmpegPCMAudio` を使う実装に戻さないこと** — 返信のたびに別プロセスが起動し、小メモリ環境では OOM の原因になる。

- **非同期と別スレッドの使い分け**: `_synthesize_pcm` は **async 関数**。edge-tts は async ネイティブなのでイベントループ上でそのまま `await` する（通信待ちでループは塞がらない）。同期処理である gTTS の呼び出しと、デコード＋EQ の CPU 処理だけを `run_in_thread` へ逃がす。全体を `asyncio.wait_for(_synthesize_pcm(...), timeout=TTS_TIMEOUT_SECONDS)` で包むが、同期処理中のキャンセルはワーカー完了を待って伝える。**edge-tts を `asyncio.to_thread` の中で `asyncio.run()` して呼ばないこと** — イベントループが入れ子になり動作しない。
- **読み上げキュー**: 再生中に届いた返信を捨てないよう、ギルドIDごとに `collections.deque` を持つ（`asyncio.Queue`の非同期待機や未完了数の管理は使わない）。投入前に同じdeque内の旧接続・旧チャンネル宛ての待機分と任意の `is_valid` が偽になった待機分を除き、残った待機数が`TTS_QUEUE_MAX`以上なら新規依頼を見送る。ワーカーは`popleft()`で到着順に取り出し、ワーカーが参照するdeque自体を差し替えない（#261）。積むのは **`_SpeechRequest`（テキスト・ボイスプロファイル・投入時の接続とチャンネルID・任意の有効性判定）**で、プロファイルは `speak_text` の時点で `resolve_profile` により確定させる（待っている間にペルソナを切り替えられても、積まれた分は依頼時のキャラクターの声で読み上げる。新規に合成する分から新プロファイルが反映されれば十分）。ワーカーは最初の依頼で起動し、キューが空になったら `_speech_queues` から自分を消して終了する（参加していないサーバー分のタスクを常駐させないため）。**キューが空だと判定してから登録を消すまでの間に `await` を挟まないこと** — 挟むとその隙に積まれた依頼が誰にも処理されず取り残される。キューが溢れた分は捨てる（古い返信を延々と喋り続けるより会話の現在地に追従する方が自然なため）。
- **再生の完了待ち**: `voice_client.play()` の `after` コールバックは discord.py の**再生スレッド**から呼ばれる。イベントループ側のオブジェクト（`asyncio.Event`）へは必ず `loop.call_soon_threadsafe` 経由で触ること。
- **接続は毎回見直す**: 読み上げ中に `/leave` されうるため、ワーカーはループのたびに `guild.voice_client` を取り直して接続状態を確認する。
- **読み上げテキストの整形** (`build_speech_text`): `affinity_service.strip_speech_tags` で感情・親密度の内部タグを除去してから、URL・コードブロック・インラインコード・カスタム絵文字・メンション・Markdown 記号を除去し、`TTS_MAX_CHARS` を超える分は `TTS_TRUNCATION_NOTICE` を付けて打ち切る。特に **URL は音声エンジンが1文字ずつ読み上げて極端に長くなる**ため、ASCIIのURL部分を除去し、直後の日本語や全角句読点は残す（#290）。通常チャット以外の入口にも備えて音声側でタグを除去し、タグだけならキューへ積まない。
- 調整値は `constants.py` の `TTS_*` に集約する（インラインに書かない）。

**キャラクター別ボイスプロファイル**: 「声の高さ（`pitch`）」と「話す速さ（`rate`）」は **edge-tts のサーバー側パラメータ**が、「音色（`eq`）」だけを `voice_dsp.py` の numpy 製バイクアッド EQ が担う。**`scipy` / `librosa` / `pydub` を持ち込まないこと** — 前2つは 1GB 環境に対して重すぎ、`pydub` は内部で FFmpeg プロセスを起動するため上記の方針と矛盾する。

- **どのキャラをどう鳴らすかは、利用者がローカルで作る `prompts/personas.json` の `voice`**（値は `voice` / `pitch` / `rate` / `eq`。`personas.py` が読み、`voice_dsp.resolve_profile` が一覧から引く）。EQ の中身は `constants.py` の `VOICE_EQ_PROFILES`。書いていない項目・未登録のペルソナ・組み込みの標準は `VOICE_PROFILE_FALLBACK` へ落ちる。**特定のキャラの声を公開側のコード（`constants.py` など）に書かないこと**（キャラ別のファイルを公開しない方針のため。旧 `VOICE_PROFILES` は #189 で外した）。`voice_dsp.py` や `voice.py` にもハードコードしない。
- **`pitch` / `rate` は edge-tts のネイティブ形式の文字列**（`"+10Hz"` / `"+5%"`。正規表現 `^[+-]\d+Hz$` / `^[+-]\d+%$` に一致しないと `Communicate` が `ValueError` を投げる）。**半音単位 `st` は使えない。**
- **「半音 → Hz」を計算で求めないこと**: 効き方が話者ごとに違い、Nanami は理論式の約1.5倍効き、Keita は約0.55倍しか効かないうえ **+6半音付近で飽和し、下方向はほとんど効かない**。`constants.py` に実測の較正表をコメントで置いてあるので、それを目安に Hz を直書きする。
- **`voice_dsp.py` に WSOLA・リサンプリングを戻さないこと**: 設計初期に gTTS の単一音声から高さと速さを作るため自前で持っていたが、edge-tts はピッチを変えても長さが変わらない（＝リサンプリングではなくフォルマントを保つ）ため不要になった。戻すと音質を落とすだけで得るものがない。
- **バイクアッドはブロック処理**: IIR は1つ前の**出力**を使う再帰式のため numpy でベクトル化できず Python のループになる。`TTS_BIQUAD_BLOCK_SIZE` ごとに区切ってフィルタの状態を持ち越す（一気に `tolist()` すると 30 秒の音声で数十 MB の一時オブジェクトができる）。
- **音色を変えないペルソナは numpy を通さない**: `needs_equalizer` が偽なら miniaudio に 48kHz ステレオまで変換させる。この分岐を消さないこと（`flat` のペルソナの経路が無駄に重くなる）。EQ を掛ける場合はモノラル1ch分だけ処理し、`np.repeat(clipped, TTS_CHANNELS)` でのステレオ化は最後に回す（計算量が半分で済む）。
- **int16 のクリップは必須**: EQ のブーストでレンジを超えるため、`np.clip` してから `astype(np.int16)` する（超えたまま変換すると音が割れる）。
- **プロファイルは読み込み時に点検する**: `personas.py` の `_build_voice` が一覧の読み込み（起動時とキャラ設定パネルを開いたとき）に `pitch` / `rate` の形式を確認し、誤った項目は警告を出して既定値を使う。形式ミスは `Communicate` の `ValueError` 経由で**黙って gTTS 退避に落ち続ける**（音は出るのでキャラ差が消えたことに気づきにくい）ため、この点検を外さないこと。
- **フェイルセーフ**: EQ 名が未知・`eq` キー自体が無い場合は `DEFAULT_VOICE_EQ_NAME` の音色へフォールバックする（既定は加工なしの `flat`）。数値でない周波数・ゲイン・Qも `TTS_EQ_*` の既定値で処理する。NaN・無限大も同じく既定値へ落とす（`_as_float` が `math.isfinite` で弾く。通すとフィルタ係数が NaN になり、int16 変換で雑音になる）。`bit_crush` は `_bit_crush_bits` が1以上のときだけ有効とし、`needs_equalizer` の判定と `apply_equalizer` の適用で同じ関数を使う（判定だけ `bool()` にすると `"0"` や `-1` で numpy の経路へ無駄に回る）。

### 音声会話（Gemini Live。`voice_receive.py`, `live_audio.py`, `live_session.py`, `voice_conversation.py`, `voice_consent_view.py`。#268）

`/join` で入ったボイスチャンネルで、参加者と声で会話する。1VCにつき1つの Live セッション（`LIVE_MODEL_NAME`＝`gemini-3.8-live`）を張りっぱなしにし、参加者の声を 16kHz で流し込み、返ってくる 24kHz の音声を再生する。設計の決定事項は Issue #268 のコメントにまとめてある（実電話・DM 通話は対象外、新しいモード切替コマンドは作らない）。`LIVE_ENABLED = False` で `/join` は接続だけになる。

- **受信は依存を足さずに自前で組む**（`audio/voice_receive.py`。#268 で利用者が選択。上流が更新停止中の `discord-ext-voice-recv` は使わない）: `LiveVoiceClient`（`discord.VoiceClient` の派生）が `create_connection_state` で音声 WebSocket のフックを差し込み、SPEAKING（op 5）から「SSRC → user_id」を覚える。`VoiceReceiver` が `VoiceConnectionState.add_socket_listener` で UDP を購読し、ソケットの読み取りスレッドで RTP の分解 → 転送層の復号（PyNaCl の `Aead`。`aead_xchacha20_poly1305_rtpsize` だけ。拡張ヘッダーの4バイトは追加認証データ、拡張の本体は暗号文の先頭）→ DAVE の復号（`dave_session.decrypt(user_id, MediaType.audio, …)`。復号できなければパススルー中の相手以外は捨てる）→ `discord.opus.Decoder` → 16kHz モノラルへの変換までを行い、結果だけを `call_soon_threadsafe` でイベントループへ渡す。★ **discord.py の内部属性（`_connection`・`secret_key`・`mode`・`dave_session`）とフックに依存する**。discord.py の版を上げたら `tests/audio/test_voice_receive.py`（discord.py と同じ方式で暗号化したパケットの往復と、依存する入口の存在を確かめる）を必ず回すこと。受信の依存はこのモジュールに閉じ込め、置き換えるときもここだけを差し替える。`/join` は最初からこのクラスで接続する（受信に要る情報は接続の最初から流れてくるため、後から差し替えられない。素の `VoiceClient` で同じチャンネルに居たら切り直す）。
- **形式の変換は numpy だけ**（`audio/live_audio.py`）: `Downsampler`（48kHz ステレオ → 16kHz モノラル。窓関数法のローパス FIR の後に間引く。断片の境目の状態と間引きの位相を持ち越す）、`Upsampler`（24kHz → 48kHz ステレオ。線形補間で1サンプル遅れる）。`LivePlaybackSource` は届いた順に流す `discord.AudioSource` で、生成が追いつかない間は無音を返し、`finish()` 後は流し切って終わり、`abort()` で残りを即座に捨てる（割り込み・違反の検出用）。`LIVE_PLAYBACK_STALL_SECONDS` の間なにも届かなければ打ち切る。
- **Live の接続**（`services/live_session.py`）: 発話の区切りはボット側の VAD が `activity_start` / `activity_end` で伝える（サーバー側の自動検出は `automatic_activity_detection.disabled`。複数人の VC で、誰の発言をいつ渡すかをボット側で決めるため。割り込みは既定の `START_OF_ACTIVITY_INTERRUPTS`）。入出力の文字起こし・セッションの再開（ハンドルを覚え、go_away を受けたら即張り替え）・文脈の圧縮（`LIVE_COMPRESSION_*`。無料枠の TPM を1ターンで使い切らないよう既定より小さい）を使う。切断時は `LIVE_RECONNECT_MAX_ATTEMPTS` 回まで再接続し（再開に失敗したら新しいセッションで試す）、**利用枠の超過（429・quota）では再接続しない**（`on_closed("quota")`）。★ ただし **Google 検索ツール付きの接続だけが「quota を超えた」と拒否される**ことがある（Live の検索は無料枠外で、音声だけなら同じキーで接続できる。コードは 1011・文面は "You exceeded your current quota"）。ボイスの接続の最初と再接続では、検索ありで超過になったときに限り検索だけを外して1回だけ試し直す（`_drop_search_on_quota`。声は残す）。検索なしでも超過なら本物の超過として終える。最初の接続で設定を拒否されたら（1007/1008/400 など。ボイス名・検索の非対応）、ボイス名と検索を外して1回だけ試し直す。`close()` の後は再接続しない（`/leave` の後に勝手につなぎ直さない）。同意の撤回では `forget_context()` / `restart_fresh()` で再開ハンドルを捨て、撤回した人の発言が残った文脈を引き継がない。★ 文脈の破棄は世代（`_context_epoch`）で照合する（#311 のレビュー）: 再接続で接続を待っている間に撤回されたら、撤回前のハンドルで開いた接続は採用せずに閉じて新しいセッションでつなぎ直し、文脈を破棄した後に古い接続から届いた再開ハンドルも受け取らない（ハンドルの変数を消すだけでは、送信済みの接続設定で撤回前の会話が再開される）。SDK の接続口は差し替えられる（テストは模擬の接続）。★ 再開ハンドルの属性は `_resume_handle`。メソッド `_handle` と同じ名前にすると、属性がメソッドを隠して受信が止まる。
- **同意（`ConsentState`。接続中だけメモリ）**: ボイスチャンネルの人間の参加者（ボット・ブラックリスト登録者を除く）全員が音声処理に同意するまで入出力を止める（`_update_gate`。途中参加・別チャンネルへ移された間も止まる）。記憶・親密度への同意は別で初期オフ。撤回では記憶・親密度の同意も外し、同意していた人の撤回なら**ゲートの開閉を待たずに** `restart_fresh()` で文脈を捨てる（撤回した人が VC を出ていて、残った人だけでゲートが開いたままの場合があるため。#310 のレビュー）。同意パネル（`views/voice_consent_view.py`。`ui.View` 直継承・`timeout=None`）は VC の付属チャット（送れなければ `/join` を実行したチャンネル）に1通だけ出し、本文には人数だけを出す（誰が未同意かは出さない）。ボタンの結果は本人だけに返す。DB には保存しない（スキーマは変えていない）。
- **話者の管理（Active Speaker Manager）**: Live へは同時に1人分の音声だけを送る。話し始めは `LIVE_VAD_START_MS` 以上の有声（音量 `LIVE_VAD_ENERGY_THRESHOLD`）で、判定前の `LIVE_VAD_PREROLL_MS` 分も送る（語頭の欠け防止）。`LIVE_VAD_SILENCE_MS` の無音で話し終わり、`LIVE_MAX_UTTERANCE_SECONDS` で強制的に区切る。ほかの人が話している間の発言は「発話希望」として最大 `LIVE_SPEAKER_QUEUE_MAX` 人・`LIVE_SPEAKER_REQUEST_TTL_SECONDS` 秒だけ覚え（音声は溜めない）、話し終わった後に優先する。ボットの返答中（再生中・生成待ち）は `LIVE_BARGE_IN_MS` 以上で割り込みとし、再生を止めて新しい発言へ切り替える。生成中に割り込んだときは、サーバーの `interrupted` か `turn_complete` を受けるまで古い返答の残りを捨てる。★ 返答が来ないまま時間が過ぎたら返答待ち・割り込みの確認待ちを解く（`LIVE_PLAYBACK_STALL_SECONDS`）。解かないと、以後の話し始めがずっと割り込み扱いになり、応答の音声もすべて捨てられる。発話の区切りと音声の順序を保つため、`start` / `audio` / `end` は1本の待ち行列で送る。★ 一時停止・再接続で待ち行列を整理するときは**音声だけを捨て、区切りは残す**（送信済みの `start` に対応する `end` まで消すと、Live 側で発話が開いたままになる）。送信側は接続の世代ごとに `start` を送ったか（`_open_generation`）を覚え、再接続した新しい接続へ対応の取れない `end`・音声を送らない。遅延は発話の終わりから再生の開始までを測る（終了時に P50/P95 をログへ出す）。
- **モデレーション（並行検査＋短時間バッファ）**: 入力・出力の文字起こしをローカルの NG ワードで即座に、AI で生成と並行して判定する。入力の AI 判定は話し終わってから文字起こしが出そろうのを待って行い（`LIVE_INPUT_TRANSCRIPT_*`）、判定した本文（`judged_input`）を覚えておく。判定の途中・後に続きが届いたら増えた分も含めて判定し直し（#311 のレビュー）、判定していない本文を含む発言は記憶に保存しない。出力の AI 判定は1ターンに1回（`ContentModerator.judge_voice_turn`）で、**感情の変化量の推定を同じ呼び出しに相乗りさせる**（親密度のために Gemini を追加で呼ばない）。判定は `VERDICT_SAFE` / `VERDICT_UNSAFE` / `VERDICT_UNKNOWN` の3段階で、判定不能は再生を止めないが記憶・親密度の条件にはしない（条件付き fail-open）。既存の `check` / `check_and_route` は従来どおり判定不能を通過扱いにする（`_judge` が `_judge_verdict` を包む）。応答は `LIVE_OUTPUT_BUFFER_MS` 溜めてから再生し始め、違反を見つけたら再生を止めて残りを捨てる（検出前に再生された部分は取り消せないことは仕様上許容）。参加者の違反は同じ発言につき1回だけ `BlacklistService.record_violation` へ数え（文字起こしは少しずつ届くため）、ボット自身の出力の違反は参加者に数えない。★ **入力の文字起こしの帰属先（`_transcript_owner`）は発話権（`_current`）と別に持つ**（#310 のレビュー）。前の人の文字起こしが閉じる前に次の人が話し始めたら（割り込みなど）、閉じるまでに届いた断片はどちらにも帰属させずに捨て、両方の発言を `transcript_incomplete` にして記憶に使わない（前の人の NG で次の人の違反を数えない・前の人の内容を次の人の記憶に入れないため）。閉じたとみなすのは、サーバーの `finished`・その発言の返答の `turn_complete`・割り込みの `interrupted`・`LIVE_PLAYBACK_STALL_SECONDS` の経過・Live の張り替え。帰属先を `_current` に戻さないこと。違反の通知は対象の本人だけをメンション許可する（`AllowedMentions` の既定は `@everyone` も許可するため、`everyone=False, roles=False` を明示する）。
- **記憶・親密度**: 個人の長期記憶・親密度は Live へ渡さない（共有の場で漏らさない。自律配信のコメント応答と同じ方針）。記憶に同意した人の、入出力とも `VERDICT_SAFE` で違反・割り込み・破棄の無い発言だけを `append_pending_log` へ回し、親密度に同意した人は推定した変化量で `update_emotion` する。
- **ペルソナと声**: 接続した時点の `snapshot_response()` のペルソナを `build_live_system_instruction`（音声通話での話し方を上書きし、感情タグ・記号を話させない）で system_instruction にする。途中の設定変更は次の `/join` から反映する。声は `personas.json` の `voice.live_voice`（英字で始まる名前だけを通す。`personas._LIVE_VOICE_PATTERN`）。🔍 Google検索が ON のサーバーでは検索ツールを渡す。
- **配線と寿命**: `VoiceConversationService` を `bot.py` で生成し、`register_commands(voice_conversation=...)`・`on_voice_state_update`（参加者の出入り）・`remove_guild_state`・終了時の `stop_all` へ渡す。`/join` は接続前に `reserve()` で同時実行の枠（`LIVE_MAX_CONCURRENT_SESSIONS`）を確保し（判定と確保の間に `await` を挟まない）、接続後に `start()`、失敗したら案内して退出する。**同意パネルの投稿までを開始の成功の条件にする**（送れなければ `ConsentPanelError`。パネルが無いと誰も同意できず、Live の接続と枠だけが残るため。会話の登録はパネルの投稿の後）。パネルの投稿を待つ間に Live が再接続できずに終わった場合も、未登録の会話には `end()` が届かないため、`live_closed_reason` を見て開始を失敗にする（#311 のレビュー）。同じチャンネル・同じ接続で動いている会話は再利用し、止まっている・別のチャンネル用（移された先で `/join` された）なら同じ接続のまま始め直す。`/leave` は切断の前に `stop()`。会話側の事情（Live の終了・接続の喪失）は `end()` で、入れ替わった後の古い会話からの要求には触らない。`end()` は終了を待った後にも新しい会話・枠の確保が無いかを確かめ、待つ間に `/join` が同じ接続で始めた会話を切断しない（案内も出さない）。見回り（`tick`、`LIVE_TICK_SECONDS`）が話し終わりの判定と接続の喪失（voice_guard の退出・外部からの切断）を検知する。受信と再生は独立していて、`voice_guard` の `stop()` は再生だけを止める。
- **開始予約の所有権（#346）**: `reserve()` は予約票（上限なら `None`）を返し、接続・設定読込の前から `start(reservation=...)`・`release_reservation(reservation=...)` まで同じ票を渡す。停止後に取り直した予約を古い開始処理が採用・解放しない。置換では旧会話を登録から外してから停止を待ち、遅延した旧会話の `end()` に新しい予約や接続を触らせない。容量は登録と予約のサーバー集合で数える。
- **受信ストリームの世代（#347）**: `LiveVoiceClient` は SSRC の話者と割当世代を不変の組として受信側へ渡す。切断・再割当・再接続で世代を変え、UDP側で通番比較より前に古いデコーダを捨てる（WebSocket側からデコーダを直接変更しない）。Liveの `receive()` は正常なターン終了でも終わるため次ターンを受け直すが、空ストリーム・再開始要求・セッション差し替え後は古い受信を繰り返さない。
- **保存しないもの**: 生の音声・録音・文字起こしの全文・会話ログ・同意は永続化しない。ログには件数・状態・遅延（P50/P95。終了時）だけを出し、文字起こしの本文や話者の ID を出さない。
- **確認範囲**: 受信・変換・接続・会話の制御・同意パネル・`/join` 連携は回帰テスト（`tests/audio/test_live_audio.py`・`test_voice_receive.py`・`tests/services/test_live_session.py`・`test_voice_conversation.py`・`test_voice_moderation.py`・`tests/views/test_voice_consent_view.py`・`tests/core/test_join_live_voice.py`）のモックで再現している。実際の Discord（DAVE の受信）と Gemini Live での会話・遅延・利用量は検証用サーバーで確かめること。

### ボイス接続先の保護 (`voice_guard.py`)

音声会話の`VoiceConversationService.end()`は、会話停止の待機後に加え、終了案内の送信後にも新しい会話と開始枠の予約を確認する。保護解除・切断の直前まで確認し、案内待ち中に同じ接続で再開した会話を旧処理から切断しない（#345）。

外部移動後の接続確立が時間切れになった場合も、同じVoiceClientと保護情報を照合し、自律配信の `handle_external_move` を切断前に通す。締め・所有ステージ枠・セッションを回収し、配信側が引き受けなかった同じ接続だけを既存の退出処理で片付ける（#289）。

管理者は「メンバーを移動」権限でボットを別のボイスチャンネルへ移せ、`ADMINISTRATOR` はチャンネルの権限上書きを迂回するため、移動そのものを事前に拒否する手段は無い。`VoiceGuard` はボット自身の `on_voice_state_update` から**予期しないチャンネル間の移動を検知した後に**保護動作を行う（一瞬の移動も起きない「完全な禁止」ではない。この制約は README と ❓ 使い方 にも書いてある）。

- **アプリ自身の操作との見分け方**: アプリはチャンネル間の移動（`move_to`）を、このモジュールが元へ戻すとき以外に行わない。`/join` の切り替えは「切断→接続」、`/leave`・配信の終了は「切断」、配信の開始は「接続」、discord.py の接続復旧も同じチャンネルへのつなぎ直しなので、ボット自身のボイス状態は「なし→A」「A→なし」と変わるだけ。**「A→B」はすべて外部からの移動とみなす**（外部の利用者はボイスに居ないボットを通話へ引き込めないので、「なし→A」は必ずアプリ自身の接続）。★ **アプリ側に `move_to` でチャンネルを移す処理を足すときは、移す前に `adopt()` で行き先を登録すること** — 登録しないと手動の移動と誤認して元へ戻してしまう。
- **意図した接続先**: 「なし→A」で `adopt(A)`、「A→なし」で `release`（退出後は自動で入り直さない）。`handle_voice_state_update` は**同期関数のまま保つこと**（イベントの届いた順に登録を更新するため。`await` を挟むと `/join` の「切断→接続」の2件が入れ替わりうる）。接続を見届けていない移動では、移動前のチャンネルを意図した接続先とみなす。行き先が意図した接続先と一致する移動（自分が戻した結果）は無視する。
- **保護動作**: 検知した時点で再生中の読み上げを `stop()` する（移動先で古い音声を流さない。待ち行列は voice.py が投入時の接続・チャンネルと照合して捨てる）。その後 `background.spawn` で `_protect` を走らせる。`_protect` はサーバーごとのロックで直列化し、移動直後の張り直し（discord.py は移動後に音声の WebSocket を張り直すことがある）を `VOICE_MOVE_GUARD_SETTLE_SECONDS` × 最大 `VOICE_MOVE_GUARD_SETTLE_RETRIES` 回待ってから状態を取り直す。自律配信の接続（`autonomous.manages_connection`）は `autonomous.handle_external_move` に任せる（締めの一言・自分で開いた枠の終了・切断。追従しない理由は自律行動の節）。`/join` の接続は元のチャンネルへ `move_to` で戻り、ステージなら `enable_stage_speaker` で発言者へ戻す。
- **退出する条件**（無限に再試行しない）: 元のチャンネルが無い、`/join` と同じ基準の権限（チャンネルを見る・接続・発言、ステージはメンバーをミュート）が無い、`move_to` が例外を出した、ステージへ戻った後に `enable_stage_speaker` が失敗した（#258）、**`move_to` の後も元のチャンネルに居ない**（discord.py の `move_to` は時間切れでも例外を出さずに戻ることがあるため、成否は必ず戻った後の接続先で確かめる）、張り直しが終わらない、`VOICE_MOVE_GUARD_WINDOW_SECONDS` の間に `VOICE_MOVE_GUARD_MAX_RETURNS` 回戻しても移され続けた。退出は `release` してから `disconnect(force=True)`。同じチャンネルの `adopt` では戻した回数の記録を消さない（`/join` の打ち直しで歯止めを外せないようにするため）。
- **人の操作を優先する**: 待機・移動の前後で `guild.voice_client` が同じ接続か、意図した接続先が差し替わっていないかを確かめ、`/join`・`/leave` で切れた・差し替わった接続には触らない。`_leave` は必須の`expected_home`を受け取り、現在の`guild.voice_client`と`_homes`の保護情報オブジェクトが両方一致するときだけ`release`と切断を行う（旧移動の失敗中に同じ接続へ`adopt`された・別の接続へ差し替わった・同じチャンネルIDで保護情報が再登録された場合は新しい登録と接続を残す）。照合から解除・切断開始まで`await`を挟まず、戻り先の欠損・権限不足・回数上限、移動の例外、例外なしの移動失敗、ステージの発言者への復帰失敗、接続安定待ちの上限の全経路で対象の保護情報を渡す。昇格待機中に差し替わった接続や再登録された保護情報にも触らない。移された先で `/join` された場合は `adopt` で移動先が意図した接続先になり、元へ戻さない。
- **状態の保存範囲**: 意図した接続先と戻した時刻はサーバーごとにメモリだけに持つ（ボイス接続が再起動をまたげないため）。接続の唯一の情報源は引き続き `guild.voice_client`。`VOICE_MOVE_GUARD_ENABLED = False` で従来どおり移動先に留まる（自律配信は毎分の見回りで終了する）。
- **確認範囲**: 実際の Discord での移動操作は回帰テスト（`tests/services/test_voice_guard.py`）のモックで再現しており、実機での確認は検証用サーバーで行うこと。

### スラッシュコマンド (`commands.py`)

`/functions`・`/join`・`/leave` は副作用の前に `reject_blocked_interaction` でブラックリストを確認する。拒否は本人だけに通知する。パネルも `PanelView.interaction_check` で毎回確認し、開いた後のブロックを反映する。`FunctionsView` が渡す `blocked_user_check` を `swap_panel` で引き継ぐこと。サブパネルへ渡し忘れると開いたままの画面から操作できてしまう。

`/join`の接続エラーと通常会話の設定ファイル欠落は、利用者へ固定の案内を返し、生の例外本文・例外型・内部のファイル名を表示しない。詳細は各モジュールの`logger.warning` / `logger.error`へトレース付きで残す（#262）。接続時は`cls`のファクトリーで今回作ったVoiceClientを控え、失敗・中断時はその接続が現在も登録され未接続の場合だけ保護を解除して強制切断する。切断に失敗しても、同じ未接続の登録だけをSDKの`cleanup()`で解除する。正常接続や差し替えられた接続は触らず、後片付けは`wait_for_cleanup`で繰り返しのキャンセルから保護する。

`/join` はボットのMemberを取得できた場合、接続先の `permissions_for` で「チャンネルを見る」「接続」「発言」を確認する。不足している権限だけを案内し、defer・自律配信の停止・既存接続の切断より前に終了する。

参加・退出は`VoiceCommandCoordinator`でブラックリスト等の確認後、`defer`より前に同期的に有効な操作世代を受付順の一覧へ記録する。応答待ちの世代も別に記録し、応答保留の失敗やロック前のキャンセルで脱落した世代を外して最新の有効な操作へ戻す。応答保留後にサーバー別の共通ロックを取り、自律配信の停止・接続の再確認・保護解除・切断・接続・保護先の採用を直列化する。

ロック取得時に自分より新しい応答待ちの世代があれば、ロックを解放して状態変更のEventを待ち、見送りの判断をやり直す。新しい世代がすべてロック待ちへ進んだ場合にだけ見送り通知を送り、送信中もロックを保持しない。通知後も後続の実行開始までは元の世代を残し、後続の脱落で再び最新になったら元の操作を実行する。通知自体が失敗しても再判定を続け、見送りが確定した経路だけで通知の例外を共通の失敗処理へ伝える。後続の脱落で元の操作が復帰して成功した場合は通知の例外を返さない。実際に送信を試みた通知は再送せず、送信前の再確認で不要になった場合は通知済み扱いにしない（#289）。有効な操作の実行開始時にそれ以前の世代を一覧から外す（実行済みの後続操作より古い要求は復活させない）。既にこの方法で無効になった古い世代には、他の応答待ちを待たずに見送りを通知できる。状態変更は同期的にEventを起こして差し替え、確認と待機の間の通知を取りこぼさない。

参加は配信停止後・切断後に世代を照合し、接続前の見送りを判断する。接続後は常に接続先を採用して成功を案内し、後続の操作がロック取得後に実行されて必要なら切断・移動する。後続が応答保留の失敗やキャンセルで脱落しても、接続できた参加は保持する。異なるサーバーは独立して進める。待機中のコマンドは終了時に中断でき、接続・切断を始めたら`wait_for_cleanup`で完了を待ってからロックを解放する（配信停止は既存の保護付き回収を使う）。終了処理はこのロックを取得せず、所有タスクを回収する。応答待ちを含む全操作が成功・失敗・キャンセルで終わったらロックと世代の一覧を破棄する。

`register_commands` は `tree.on_error` に共通の `handle_command_error` を登録する。本体の例外は共通入口で捕捉し、通常例外だけをdiscord.pyと同じ`app_commands.CommandInvokeError`で包む（既存の`AppCommandError`は保持する）。`interaction.command_failed`を立てて所有期間内で通知を完了させ、例外を再送出しないことで二重通知と成功イベントを防ぐ。本体より前の実行条件の確認など、SDK側の失敗は従来どおり`tree.on_error`へ渡る。未応答なら初回応答、応答・defer済みならfollowupで、いずれも本人だけへ日本語の失敗通知を返す。権限不足・実行条件・クールダウンも案内し、例外の詳細は通知へ含めない。通知の失敗は例外の型だけをログへ残して終了し、再通知しない。本体のキャンセルはそのまま伝播し、通知待機中の繰り返しキャンセルは保護した通知の完了後に伝播する。

`/join` は接続できたとき（同じチャンネルの接続を使い続けるときも）`voice_guard.adopt` で人が選んだチャンネルを意図した接続先にし、既存接続を切り替える前と `/leave` の切断前に `voice_guard.release` で保護を解く（戻している最中の保護処理が、切る予定の接続を動かしたり退出後に入り直したりしないため）。`register_commands` の `voice_guard` は既定値 `None`（テストなどで省略できる）。

音声会話（#268）: `register_commands(voice_conversation=...)` を渡し `LIVE_ENABLED` が真なら、`/join` は受信に対応した `LiveVoiceClient` で接続し、接続の前に `reserve()` で同時実行の枠を確かめ（上限なら接続せずに `NOTICE_LIVE_CAPACITY`）、接続後に `start_live_voice` → `VoiceConversationService.start()`。Live に接続できない・同意パネルを送れない・ペルソナを読めないなど開始に失敗したら、保護を解いて切断し、原因（利用枠の超過・パネル・その他）で案内を分ける（声で会話できない接続をボイスチャンネルに残さない）。同じチャンネルで動いている会話は再利用し、`LiveVoiceClient` で接続中なのに会話が止まっている・会話の `channel_id` が要求先と違う場合は、同じ接続のまま始め直す。素の `VoiceClient` で同じチャンネルに居たら切り直す。接続先を切り替える前と `/leave` の切断前に `stop()` する。案内の送信は動的な文面なので `allowed_mentions` を必ず付ける（`tests/core/test_mention_policy.py` が検出する）。省略時（`None`）は従来どおり接続だけを行う。

すべてのコマンドは `register_commands` で登録され、`on_ready` で同期される。全一覧: `/functions`（機能ハブ。エフェメラル）、`/join` + `/leave`（ボイスチャンネル）。**`/join` と `/leave` はどちらも先に `autonomous.stop_session()` を呼ぶ** — 自律行動でボットが自分から通話に入っている可能性があり、素の `disconnect` だけで済ませるとボットが開いたステージ枠が「配信中」の看板を出したまま残る。`/leave` での強制退出は自律行動の緊急停止手段でもある（idea.md の要件）。`/join` は配信を止めた後も同じチャンネルへ接続中なら、切り直さずにその接続を使う（切り直すと読み上げ中の音声とキューが失われる）。接続の待ち時間は `VOICE_CONNECT_TIMEOUT_SECONDS`。`/functions` はハブパネルを開き、ボタンで各機能へ分岐する: **⚙️ キャラ設定**（`SettingsView` に変身。管理者は row 4 の 🎙️ 配信先 から `AutonomousChannelView` へ進める）、**📋 会話要約**（`SummaryView` に変身）、**📈 会話統計**（`StatsView` に変身。ユーザー別ダッシュボード。管理者はここから 👍 フィードバック集計も見られる）、**💬 自発会話**（`AutoTopicView` に変身。自発的な話題提供と 🎙️ 自律行動の ON/OFF・しきい値設定。「ボットが自分から話し出す」系の入口はここに統一している）、**⏰ リマインダー**（`ReminderView` に変身。本人の予定の一覧・取り消し）、**📊 グラフ**（利用トークングラフをその場で表示）、**🔄 NGワード再読込**、**🔍 Google検索**（グラウンディングのその場トグル。確定なしで即そのサーバーの `GuildSettings` に反映）、**❓ 使い方**（`HelpView` に変身。会話のしかたと全機能の説明。権限制限なし）、**🔒 統計プライバシー**（`StatsPrivacyView` に変身。本人の会話統計の収集停止・蓄積データ削除）、**🚫 ブロック解除**（**ボット所有者専用**。全サーバー共通のブロック中ユーザーを一覧・解除する。サーバー管理者にも一覧・登録人数は公開しない。`is_bot_owner`で入口を制限し、一覧・設定人数の取得と解除の直前には`is_current_bot_owner`で所有者を再取得する。取得失敗は拒否し、チーム所有ではチーム所有者本人だけを許可する。旧形式の全体ブロックは維持する）。旧コマンド（`/status`、`/summarize`、`/graph`、`/ng_reload`、`/unblock`、および `/ai`、`/set`、`/gemini`、`/setting`、`/reset`）は削除済み — 設定・要約・利用グラフ・NG再読込・ブロック解除は `/functions` ハブに、テキストチャットは mention モードの `on_message` パイプラインに統合された。

### キャラ設定 UI (`settings_view.py`, `commands.py`, `choices.py`)

ペルソナ・モデル・応答モードの確定は、サーバーでは管理者のみ許可し、DMでは利用者本人が操作できる（設定値は従来どおり全DM共有）。ハブの 🔍 Google検索 の切り替えと 💬 自発会話（自律行動を含む）の確定も同じ扱いで、`authorize_administrator_operation(..., allow_dm=True)` を通す（#398。自律行動ではボットがボイスへ自分から入るため、サーバー全体の設定を一般メンバーに変えさせない）。選択値のファイル確認などの待機後、設定を書き込む直前に最新の管理者権限を再取得し、失効・取得失敗時は保存しない。管理者限定パネルの確認手順は `view_parts` の共通ヘルパーへ集約する。

配信先パネルの「戻る」は`OpenAutonomousChannelButton`から渡す`make_settings`でキャラ設定を作り直す。共通`BackButton`は指定された`make_parent`を優先し、指定が無ければ従来どおり`make_hub`を使う。非同期の生成関数を待つ前に操作世代を確保し、待機中の時間切れや別の遷移で失効した結果を表示しない。キャラ設定からは次の「戻る」で機能ハブへ戻れる。ブラックリスト判定・メッセージ参照の引継ぎは共通の遷移処理に任せ、戻る操作では未確定の配信先を保存しない。管理者用ボタンは戻る操作時点の権限で表示し直し、全体の違反件数は応答保留後に最新の所有者を再確認してから表示する（#292）。

`/functions` のハブ（`functions_view.py` の `FunctionsView`、エフェメラル）から **⚙️ キャラ設定** を選ぶと、同じメッセージが `SettingsView` に変身する（ペルソナ/モデル/応答モードのセレクト + 確定/戻る/リセットボタン、120秒タイムアウト）。一時的な選択値は View 上に保持され、**確定 (Confirm)** ボタンを押したときにのみ `GuildSettings`（`/functions` 実行時に `interaction.guild_id` から解決したもの）に書き込まれる — それまでは共有状態を一切変更しない。Google検索（グラウンディング）のトグルはハブ側にあり、こちらは押した瞬間に `GuildSettings` へ反映される（ハブは「その場で実行」系のため）。**各 View は `BotState` ではなく `GuildSettings` を受け取る** ため、設定変更の影響はそのサーバー内（DM の場合は全 DM）に閉じる。`choices.py` はモデル/モード、および要約期間（`SUMMARY_PERIOD_CHOICES`）の選択肢リストと表示名マップの唯一の情報源。ペルソナの選択肢は固定の一覧を持たず、`personas.py` が `prompts/` から作る（次項）。

**🎙️ 配信先（`autonomous_channel_view.py` の `AutonomousChannelView`。管理者専用、#266）**: キャラ設定パネルは row 0〜2 をセレクトで使っているため、自律行動の配信先はボタン（row 4 の `OpenAutonomousChannelButton`）から同じメッセージを専用パネルへ `swap_panel` で差し替えて設定する。ボタンは `SettingsView(is_admin=...)`（`OpenSettingsButton` が押した時点の `is_administrator(interaction.user)` を渡す）が真のときだけ出し、押した時点でも `interaction.guild` と `is_administrator` を確かめ、`defer(ephemeral=True)`後に`is_current_administrator`で最新のMemberを取得する。権限失効・取得失敗時は配信先を表示せず、設定の変更も行わない。パネルはステージ・ボイスのセレクト（どちらも単一選択、先頭に「指定なし（自動）」）と確定/戻るで、確定まで `GuildSettings` を変えない。確定は `defer` の後に `is_current_administrator`（最新の Member）で権限を再確認してから `autonomous_stage_channel_id` / `autonomous_voice_channel_id` を書き、開始失敗の待ち時間を解除して `persist()` する。選択肢は「指定なし」を含めて `DISCORD_SELECT_MAX_OPTIONS` 件まで（チャンネルは先頭の24件。指定中のチャンネルは範囲外でも末尾に残し、超過は本文で注記）。使えないチャンネルは選択肢の説明と本文で知らせ、判定は `autonomous_stage.can_use_stage` / `can_use_voice` を再利用する（判定を画面側で書き直さないこと）。本文に「Discord の仕様上、管理者が手動でボットを別のチャンネルへ移すことは防げない。移された場合は配信を終了する」を必ず残す。

### ペルソナの一覧 (`personas.py`)

キャラ別のプロンプトは公開しない（#189）。Git が管理する `prompts/` のファイルは書式見本の `00_prompt_format.txt`（`PROMPT_TEMPLATE_FILE_NAME`）と、`personas.json` の書式見本の `00_prompt_format.json`（`PERSONA_SETTINGS_TEMPLATE_FILE_NAME`）だけで、キャラ別の `.txt` と任意の `personas.json`（表示名・声・既定）は利用者が各自のパソコン・サーバーで作る（JSON の見本を `personas.json` へコピーして書き換える）。`.gitignore` は `prompts/*` で直下全体を除外し、テンプレート2つだけを否定行で戻している。

- **選択肢は `prompts/` の `.txt` から作る**: テンプレート・`.` で始まる名前・フォルダは除く。`personas.json` の `personas` に書いた順が先、残りはファイル名の順。表示名は `name`、無ければ拡張子なしのファイル名。`default` が無い・見つからないときは先頭、1つも無ければ空（組み込みの標準）。
- **保存値の形は変えないこと**: `"../prompts/<ファイル名>"`（`PERSONA_VALUE_PREFIX`）。旧版の固定の選択肢と同じ形なので、ファイル名が同じなら DB の保存値をそのまま引き継げる。形を変えると全サーバーの選択が既定へ戻る。
- **空文字は「既定のペルソナに従う」**（`FOLLOW_DEFAULT_PERSONA`。`GuildSettings` の初期値・DB の未設定と同じ）。`snapshot_response()` が `PersonaCatalog.resolve` で一覧の既定へ解決し、空のままなら `load_persona_template` がファイルを読まずに `BUILTIN_PERSONA_PROMPT`（`constants.py`）を返す。テンプレートだけを取得した直後でも、存在しないファイルを参照しない。
- **見つからないファイルの保存値は消さない**: `apply_persisted` はペルソナの形の値なら、ファイルが無くても残してログを出し、応答は `resolve` が既定へ回す。Git の更新でキャラ別のファイルが一時的に消えた環境でも、戻せば元のキャラに戻るようにするため（旧版は既定へ書き換えていたが、その状態で別の設定を確定すると選択が失われた）。キャラ設定の確定も、ペルソナを変えたときだけファイルを確かめる（変えていなければ見つからない値も保つ）。
- **`persona_file_name` が prompts/ の外を読ませない関門**: 区切り文字・テンプレート・隠しファイル・`.txt` 以外を拒否する。保存値の復元（`apply_persisted`）と確定時の確認（`is_selectable_persona`）の両方で通す。確定時の確認を「ファイルがあるか」だけに戻さないこと（`../.env` を選ばせると API キーを Gemini へ送ってしまう）。
- **一覧はスナップショット**: `reload_persona_catalog()`（`bot.py` で設定の復元より前に1回）と `refresh_persona_catalog()`（⚙️ キャラ設定を開くとき。応答を保留してから `run_in_thread` で読む）だけがファイルを読み、読み終えた一覧を差し替える。応答・読み上げ・表示は `current_persona_catalog()` を参照するだけで、イベントループ上でファイルを読まない。フォルダを読めないときは前回の一覧を保つ。`SettingsView` は開いた時点の一覧を持ち、選択肢と表示をそろえる。
- **Discord の制限**: 選択肢は先頭の `DISCORD_SELECT_MAX_OPTIONS`（25）件まで、値が `DISCORD_SELECT_VALUE_MAX_LENGTH`（100文字）を超えるファイルは警告して選択肢から外す。1つも無いときはペルソナのセレクトを出さない（選択肢0件のセレクトは Discord に拒否され、パネルごと開けなくなる）。
- **`personas.json` の誤りでは止めない**: 手書きのファイルなので `utf-8-sig` で読み、JSON の誤り・型の誤り・`voice` の未知の項目・形式の誤った声は警告して、その部分だけ既定にする。ファイルは書き換えない（`load_json_file` の退避は使わない）。ログに `personas.json` の中身を出さない。
- **JSON の見本はそのまま読める形に保つ**: ボットが読むのは `personas.json` だけで、見本（`00_prompt_format.json`）は読まない。JSON にはコメントが書けないため、見本の説明は最上位の `_説明` に置いている。最上位は `default` と `personas` だけを使い、それ以外の項目は警告せずに無視する（コピーしたまま使っても警告を出さない）。最上位の項目に検証を足すときは `_` で始まる項目を警告しないこと。`tests/config/test_personas.py` が、見本を `personas.json` としてコピーすると警告なしで表示名・声・既定が反映されることを確かめている。`personas` の中に説明用の項目を置かないこと（ファイル名とみなされ「対応するファイルがありません」と警告される）。
- **テストで実際の `prompts/` を読まないこと**: 利用者のキャラ別ファイルが手元にあると通り、CI（テンプレートだけ）では落ちる。`tests/helpers/persona_helpers.py` の `install_persona_catalog` / `install_empty_persona_catalog` で架空の一覧に差し替える。`tests/config/test_personas.py` は Git の管理対象がテンプレート2つだけであることと、新しいファイル・`personas.json` が除外されることも確かめる。テンプレートの名前を変えるときは、`PROMPT_TEMPLATE_FILE_NAME` / `PERSONA_SETTINGS_TEMPLATE_FILE_NAME`・`.gitignore` の否定行・README をそろえる。
- **サブエージェントの進捗の定型文（`agent`）**: 項目は `AGENT_LINE_KEYS`（作業スレッド用の `name` / `handoff` / `child_*` を含む）、差し込みは `{role}` / `{step}` / `{current}` / `{total}` だけ。`avatar_url` だけは文面ではなく、`_build_agent_avatar_url` が `https://` で始まる `AGENT_AVATAR_URL_MAX_CHARS` 文字以内の1行かを点検する（誤りは URL を出さずに警告し、Webhook の既定のアイコン）。`_build_agent_lines` が読み込み時に `string.Formatter.parse` で差し込み名・空の書式指定・変換指定なしを点検し（検証時に`str.format`は実行しない）、誤った項目は警告して既定の文面にする（警告に文面そのものは出さない）。`PersonaCatalog.agent_lines` は書かなかった項目を `AGENT_DEFAULT_LINES` で補って返す。見本（`00_prompt_format.json`）の例を変えたら `tests/config/test_personas.py` の見本のテストが通ることを確かめる。
- **管理コンソールも同じ規則で一覧を作る**: `tools/console/backend/remote_scripts/insights.py` は、`personas.py` がある版ならボットを import せずに `prompts/` のファイルと `personas.json` から一覧・表示名・既定（無い・見つからないときは先頭、空なら組み込みの標準）を再現し、保存値が見つからない場合は実効のペルソナと保存値を分けて返す（#223。旧版のサーバー向けに `choices.py` の `PERSONA_CHOICES` も読む）。**一覧の作り方（有効なファイルの判定・並び順・既定の決め方）や保存値の扱いを変えたら `insights.py` も直す** — `tools/console/backend/tests/test_remote_scripts.py` の `test_estimate_matches_bot_catalog_rules` が、ボット本体の `load_persona_catalog` と結果を突き合わせて食い違いを検出する。

### 機能ハブ / 会話要約 (`functions_view.py`, `summary_view.py`, `summary_service.py`, `stats_view.py`)

`edit_panel_message` で `view=None` に閉じる場合は、HTTP編集前に新規操作の受付を止める。編集失敗時は旧Viewを復元し、成功時だけ停止する。統計・要約の実行条件は編集待機前に固定し、統計本文1通目の表示後に続きや画像の送信が失敗しても本文を上書きせず、本人限定のfollowupで案内する（#348）。配信先設定から戻る際はペルソナ一覧とNGワード件数を更新し、確定前の選択値を引き継ぐ。

統計の項目・対象セレクトは操作時の `interaction.user` で管理者権限を更新し、権限が無ければ対象を本人へ戻して管理者用の候補・ボタンを取り除く。`BackButton` は操作の `Interaction` を `make_hub(interaction)` へ渡し、`FunctionsView.clone(interaction)` は初回の `is_admin` を引き継がず操作時の権限でハブを生成する。`HubFactory` はこの引数を受け取る契約に揃える。ブロック解除は#279でボット所有者本人に限定する。`defer(ephemeral=True)` の後、`is_current_bot_owner` で最新のアプリ所有者を取得してから実行し、確認できなければ解除しない。

設定パネルの確定は`defer(ephemeral=True)`で応答する。リマインダー選択肢の空説明は`None`で省略し、会話要約の全体公開は`AllowedMentions.none()`を分割投稿にも引き継ぐ。

`PanelView`は同じメッセージを共有するパネルの表示先・操作世代・編集ロックを引き継ぐ。DB等の待機前に`begin_panel_operation`で世代を確保し、画面更新は`edit_panel_message`を使う（保留後の確定更新は`deferred=True`）。古い操作の画面更新を破棄しても、受付済みの保存は最後まで待つ。遷移では実際に表示していたViewと不採用の遷移先を停止し、旧タイマーが最新画面へ触れないようにする。長時間処理の結果も同じ操作世代で確認する。

DBを待つGoogle検索トグル・自発会話の確定・リマインダー一覧と取消・統計プライバシーの確定と削除は、先にコンポーネント応答を`defer(ephemeral=True)`してから保存・取得する。その後は`edit_original_response`で元のパネルを更新し、`swap_panel`も`response.is_done()`に応じて編集経路を選ぶ。保留後もViewの停止・Webhookの参照・ブラックリスト判定を引き継ぐ。選択肢の表示文は`view_parts.select_option_text`で1行にし、空の名前はID等で補う。統計の候補取得は選択中のユーザーを上限内に残すが、収集停止中のユーザーは含めず、`has_data`も収集停止分を数えない。

サブパネルへの遷移とハブ復帰では、古いViewを停止して `await interaction.original_response()` の `InteractionMessage` を新しいViewへ渡す。通常の `interaction.message` を保持すると、エフェメラルUIのタイムアウト編集がチャンネル経由になり404で失敗する。

`/functions` はエフェメラルな `FunctionsView`（ハブ）を開く。Discord の View は最大5行のため設定と要約を1枚に合体できず、ボタンで選んだ機能のパネルへ**同じメッセージを差し替える**方式で統合している。⚙️キャラ設定→`SettingsView`、📋会話要約→`SummaryView`、📈会話統計→`StatsView`、💬自発会話→`AutoTopicView`、⏰リマインダー→`ReminderView`、❓使い方→`HelpView`、🔒統計プライバシー→`StatsPrivacyView`、🚫ブロック解除→`UnblockView`（`unblock_view.py`。ボット所有者のみボタン表示）はメッセージを差し替え、📊グラフ・🔄NG再読込は別のエフェメラルメッセージで結果を返す。**ハブの5行（row 0〜4）は使い切っている**ので、以降の機能追加はハブではなくサブパネル側へ置くこと（row 3 = ❓使い方 / row 4 = 🔒統計プライバシー）。差し替えは `view_parts.swap_panel()` を使う（ハブの `stop()` 忘れを防ぐため。新規パネルは `ui.View` 直継承ではなく `PanelView` を継承する）。🔍Google検索はハブ上のトグルで、押した瞬間にそのサーバーの `GuildSettings.grounding_enabled` を反転して保存する（サーバーでは管理者のみ。保留後に最新の Member で確かめてから書き込む）。各サブパネルは共通の **↩️ 戻る** ボタン（`view_parts.py` の `BackButton`）を持ち、`make_hub`（`FunctionsView.clone` を渡す）でハブを再生成して同じメッセージを戻す — `view_parts.py` が `functions_view.py` を import すると循環参照になるため、生成関数を渡す形にしている。**会話要約**は期間（1時間〜1か月）と表示範囲（自分だけ/全員）を選んで実行する。`summary_service.generate_channel_summary` が対象期間のメッセージを `fetch_messages_since` で取得（`SUMMARY_MAX_MESSAGES` 件超過で自動停止）、`build_summary_source` で**発言者名つきの履歴と採用メッセージ件数**を一緒に作り（本文の改行は件数に含めず、表示と利用履歴で同じ件数を使う。本文だけを返す `build_summary_source_text` は互換用に残す）、`build_summary_prompt`（`prompting.py`）＋ **そのサーバーの `GuildSettings`** の選択モデル/ペルソナで議事録を生成する。生成結果は感情タグ除去・Markdown見出し除去・`screen_bot_response` を経て、表示範囲に応じてチャンネル投稿またはエフェメラル表示される。`RunSummaryButton` は生成から送信までを `try` で囲み、パネルを「要約中...」のまま残さない。全員表示の投稿の失敗は、権限不足（`Forbidden`。権限設定の確認か「自分だけ」での表示を案内）と途中での失敗（`HTTPException`。一部が投稿されている可能性を案内）を個別に伝え、それ以外の想定外の例外も定型の案内へ差し替える（例外の文面は内部の情報を含みうるので見せない）。「自分だけ」で1通目を表示済みなら、それを上書きせず followup で伝える。Gemini の通信エラー・タイムアウトは `GeminiService.generate` が `None` に変えるため、`generate_channel_summary` が案内文を返す。

### 自発的な会話 (`auto_topic_service.py`, `auto_topic_view.py`)

話題提供は各チャンネルの生成前と送信前に、現在の有効状態・監視対象・最終活動時刻・間隔・自律配信状態を再確認する。記念日は対象Memberを先に解決し、本人が閲覧できる監視チャンネルを選ぶ。生成前と送信前にも有効状態・監視対象・本人の閲覧権限を確認する。設定・活動・対象者の閲覧条件の変更による中止は処理済み時刻・記念日記録へ確定せず、後続チャンネルにも現在の条件を適用する。ペルソナ・モデルは従来どおり `snapshot_response()` で開始時の値を維持する。

自動投稿は開始時・ペルソナと履歴の待機後の生成前・生成とモデレーション後の送信前に、`_can_send` でボット自身の閲覧・送信権限を確認する（#257）。スレッドでは `send_messages_in_threads` を使い、DM・ボットのMember未取得時も生成を見送る。生成前の権限不足は記録せず、復旧後の見回りや記念日の別チャンネル選択で再試行する。生成後にボット自身の権限不足を検知した場合は送信せず `True` を返し、送信時の `Forbidden` と同じく話題提供の処理済み時刻・記念日のお祝い済み記録を残す。同じ沈黙期間・同日中のAPI再実行を抑え、生成文を別チャンネルへ流用しない。話題提供は `AllowedMentions.none()`、記念日は対象Memberだけを許可し、全体・ロール・他ユーザーへの通知を抑止する。

一定時間会話が無いテキストチャンネルへ、過去の話題をもとにボットから話題提供する機能。デフォルトは**無効**で、`/functions` → **💬 自発会話** の `AutoTopicView`（間隔セレクト + ON/OFF トグル2つ + 確定/戻る。確定まで `GuildSettings` を変更しない）から有効化する。**設定・監視状態はサーバーごとに独立している。**しきい値は `AUTO_TOPIC_INTERVAL_CHOICES`（1/3/6/12/24時間、`choices.py`）から選ぶ。

**`AutoTopicView` は自律行動（次項）の ON/OFF も兼ねる。** 「ボットが自分から話し出す」系の入口をここ1枚に統一しているため、トグルは 💬 自発的な会話 と 🎙️ 自律行動 の2つある（入口を分けると、どちらを切ったのか分からなくなる。ハブの row 0〜4 が埋まっていて新しいボタンを足せないという事情もある）。しきい値も両者で共用する（自発会話では「何時間黙っていたら話題を投げるか」、自律行動では「何時間で話したいゲージが 100% になるか」）。

- **監視対象**: ボットが会話に参加した（`on_message` パイプラインを通過した）ギルドチャンネルが `watch_channel` で自動登録される。**DM は対象外**。最終活動時刻は、メンション判定より**前**に呼ばれる `mark_activity` により「ボット宛てでない人間の発言」でも更新される（会話が活発なチャンネルに割り込まないため）。
- **発火条件**: `auto_topic_loop`（`AUTO_TOPIC_CHECK_MINUTES` ごとの見回り）が「無会話時間 ≥ しきい値」かつ「前回の自動投稿より後に人間の発言がある」チャンネルにのみ投稿する。つまり一度話題提供したら、誰かが発言するまで同じチャンネルには再投稿しない（沈黙中の連投防止）。生成失敗時は記録を残さず、次回の見回りで自然にリトライされる。
- **話題生成**: 現在のペルソナ + チャンネル履歴（`AUTO_TOPIC_HISTORY_LIMIT` 件）から `build_auto_topic_prompt`（`prompting.py`）で組み立て、グラウンディング無効で生成する。**長期記憶（`profile`）は使わない** — 記憶はユーザー別に分離されており、特定ユーザーの個人記憶を共有チャンネルへの話題提供に使うとその人の私的情報が他の参加者へ漏れるため（プライバシー保護）。材料は全員が既に見ているチャンネル履歴だけにする。感情タグは除去のみ（相手が特定できないため親密度は更新しない）。NG ワードマスクは適用するが、モデレーションで不適切と判定された場合は謝罪文に差し替えず**投稿自体を見送る**（唐突な謝罪の投稿を防ぐ）。
- **状態の保存範囲**: 有効/無効としきい値は DB（`guild_settings` テーブル）に永続化され再起動後も維持される。監視チャンネル（`auto_topic_channels`）と投稿記録（`auto_topic_posted_at`）はインメモリのみで、再起動後はそのチャンネルでボットが再び会話に参加した時点から監視が始まる。
- **自律行動との競合抑止**: `_run_pending_checks_for_guild` は、そのサーバーで自律行動の配信が進行中（`GuildSettings.autonomous_session is not None`）なら**何もせずに戻る**。両方が有効なサーバーでは、しきい値を共用する都合で「無会話が続いた瞬間」に両方が同時に発火しうる。ボイスで独り言を始めながらテキストにも話しかけると、どちらに応えればよいのか分からずうるさいだけになるため、既に自分から喋っている間は話題提供の側を黙らせる。この判定を外さないこと。

### 自律行動（ボイス/ステージでの「配信」） (`autonomous_service.py`, `autonomous_metrics.py`, `autonomous_stage.py`, `auto_topic_view.py`)

第一声・独り言の送信失敗時は、その配信の所有枠と接続を片付ける。待機中に交代した配信は停止しない。`stop_session`のキャンセルは回収完了後に伝播し、`stop_all_sessions`は残りのサーバーも回収してから伝播する。ゲージ・活動窓・深夜判定の時差情報がない日時はUTCとして扱い、時差付きの日時もUTCへ揃えて比較する。

`AutonomousService` は見回り・コメント処理・発話・停止の統括を担当する。ゲージ・活動窓・深夜判定・開始失敗の待ち時間はDiscordに依存しない `autonomous_metrics.py`、チャンネル選択・発言権・所有ステージ枠の終了・接続の切断は `autonomous_stage.py` に分離している。`compute_talk_desire` / `describe_quiet_hours` は既存のパネルとの互換性のためサービスからも再公開する。終了処理のタスク生成とキャンセル伝播はサービス、枠IDと接続の参照照合はステージヘルパーの責務とする。

停止はサービス内の `_stopping` にサーバーごとの `_StopAttempt`（終了案内の送信者と、`_protected=True` の `spawn` で起動した共通の回収タスク）として保持する。`stop_session` はセッション記録を従来どおり先に解除して発話を止め、所有枠の削除と切断は共通タスク（`_finish_session_stop`）が終了案内の完了後に行う。二重停止・`/join`・`/leave` は `wait_for_task` でこの回収の完了を共有して待ち、繰り返しのキャンセルにも耐える。終了案内は呼出元と一緒にキャンセルでき、終了案内や回収タスク自身からの再入は自己待機を避けて即座に返る。回収タスクの `finally` が成功・例外・中断のいずれでも停止記録を解放する。`autonomous_stage.py` と `core/commands.py` は変更せず、既存の待機経路で足りる。

開始待機はサービス内の `_starting` にサーバーごとの `_StartAttempt` として保持する。接続やステージ準備を待つ間の `stop_session` はこの要求も中止にし、セッションが未登録でも `True` を返す。別タスクからの停止は、開始試行の完了イベントを後片付けまで待つ。`/join`・`/leave`はこの待機後に接続を確認し、開始処理が後から切断する接続を再利用しない。待機側の繰り返しキャンセルも後片付け完了後に伝播する。`/leave` は接続の有無にかかわらず停止処理を呼ぶ。開始処理は接続後と準備後に、停止要求・有効設定・静音時間・元のVoiceClientとの一致・接続先を再確認し、成立しなければ所有枠と元の接続を片付ける。待機が完了するまでは同じサーバーの二重開始を抑止し、`finally` で開始要求を破棄する。この要求は永続化しない。

第一声・独り言・コメント応答の履歴取得には、通常チャットと同じ `guild_settings.channel_reset_points.get(channel.id)` を渡す。自律配信だけリセット前の文脈を読み続けないよう、3経路を揃える。

**ボットが自分の意志でボイス/ステージチャンネルへ入り、独り言を続けながら、聞きに来た人のコメントへ配信者のように応答する機能。** 自発的な会話が「テキストへ話題を1つ投稿する」だけなのに対し、こちらは通話に居座って会話の場そのものを作る。デフォルトは**無効**で、`/functions` → **💬 自発会話** の 🎙️ 自律行動 トグルから有効化する（前項参照）。ver0.1.7(BETA) の機能。この節で「idea.md」と書いているのは、この機能の要件をまとめた利用者のメモのこと。**リポジトリには含まれていない**ので探さなくてよい（参照している要件は、この節にすべて書き写してある）。

- **話したいゲージ**（`compute_talk_desire`。モジュール関数なのでパネル表示からも呼べる）: 0〜100。基礎値は無会話時間に比例し、`auto_topic_interval_hours` で 100 に届く。そこへ「直近 `AUTONOMOUS_ACTIVITY_WINDOW_MINUTES` の人間の発言数 × `AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE`」を `AUTONOMOUS_ACTIVITY_BONUS_MAX` を上限に加点する（人が集まっているサーバーほど早く話したくなる）。**人間の発言をまだ一度も観測していないサーバーでは 0 を返す** — 招待されただけのサーバーや再起動直後に、いきなりボイスへ入り込まないための歯止め。観測は `on_message` から呼ばれる `mark_activity` が行う。
- **【重要】開始に失敗したら必ず `_record_start_failure` を通すこと**: ゲージは開始に失敗しても下がらない（時間が経つほど増えるだけ）ため、記録しないと次の見回り＝1分後に同じ判定が通り、**接続→失敗→切断を永久に繰り返す**（権限設定が中途半端なサーバーが1つあるだけで1日1440回）。待ち時間は `AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES × 連続失敗回数`（`AUTONOMOUS_START_RETRY_MAX_MULTIPLIER` 倍で頭打ち。既定 30分〜最長4時間）。**ゲージ自体はリセットしない** — リセットすると権限を直したあともしきい値時間ぶん待たされる。代わりに `/functions` のパネルで確定するとこの待ち時間が即座に解除される。開始に成功したら `autonomous_start_failures` / `autonomous_start_retry_at` を消すこと。行き先が無い（`autonomous_stage.pick_channel` が None）場合だけは API を1回も叩いていないので待ち時間を入れない。
- **発火と行き先**: `autonomous_loop`（`AUTONOMOUS_CHECK_MINUTES` = 1分ごと）がゲージ ≥ `AUTONOMOUS_GAUGE_THRESHOLD` のサーバーで `_start_session` を呼ぶ。管理者が配信先を指定していれば（`_connect_session` が `GuildSettings` の `autonomous_stage_channel_id` / `autonomous_voice_channel_id` を `pick_channel` へ引数で渡す。サービスに `BotState` を読ませない）、ステージの指定 → ボイスの指定の順に、`can_use_stage` / `can_use_voice` を満たすものを使う。指定先は種別ごとの一覧（`guild.stage_channels` / `guild.voice_channels`）から `find_channel` で探し、削除済み・権限不足なら次の指定、最後に従来の自動選択へ戻す（#266。使えない指定のために開始を止めたり、ログを毎分出したりしない）。自動選択の行き先は**ステージ優先**で、発言者になれるステージが無ければボイスチャンネルから選ぶ（候補が複数あれば `random.choice`）。権限は必ず `permissions_for(guild.me)` で**チャンネル単位に**確認する（`/join` と同じ作法）。ステージは「参加しただけでは観客（suppress=True）で音声を流せない」ため `mute_members` が必須、ステージ枠が無ければ自分で開くので `manage_channels` / `move_members` も要る。条件を満たさないチャンネルは候補から外す（入っても喋れないため）。
- **ステージ枠**: `create_instance` のトピックは `AUTONOMOUS_STAGE_TOPIC`（「会話相手募集中…」）。チャンネル一覧に出るので、これが人を誘う看板になる。**`send_start_notification=True` にしないこと** — サーバー全体へ @everyone 相当の通知が飛び、ボットが配信を始めるたびに通知爆撃になる。**自分で開いた枠だけを終了時に閉じる**（`stage_started_by_bot` と作成応答から得た `stage_instance_id`）。枠のIDが一致しなければ削除しない。他人が開いた枠に相乗りした場合は閉じない。
- **発話**: 第一声（`build_autonomous_opening_prompt`）→ `AUTONOMOUS_TALK_INTERVAL_MINUTES` ごとの独り言（`build_autonomous_monologue_prompt`）。どちらもチャンネル付属のチャットへ投稿し、同じ本文を `voice.speak_text` で読み上げる。第一声・独り言・コメント応答は開始時の `snapshot_response()` をプロンプト読込・生成・TTSで共用し、途中の設定変更は次の応答から反映する。投稿直前にセッションを照合し、投稿後の音声投入直前には `can_speak_session` で同じセッション・接続中・同じチャンネルを確認する。キューと合成の待機後も音声側で照合する。コメント応答では `MessageHandler` がスナップショットを渡し、返された本文を同じ条件で読み上げる。生成が失敗しても `last_spoke_at` は `finally` で必ず進める（進めないと、生成が壊れている間ずっと毎分 API を叩き続ける）。第一声は生成に失敗しても `AUTONOMOUS_OPENING_NOTICE`（募集の看板）だけは必ず投稿する。
- **開始準備のキャンセル**: `prepare_stage` のボイス状態待機・発言者への変更がキャンセルされた場合も、自分で開いた枠だけを削除し、`_start_session` が開始時のVoiceClient参照と照合して切断する。後片付けは `background.wait_for_cleanup` の専用タスクで完了まで待ち、繰り返しのキャンセルを受けても終了後に伝播する。枠削除の例外で切断を省略せず、借りた枠・差し替わった枠や接続には触らない。
- **【重要】自発発言に AI モデレーションは掛けない**: 独り言・第一声は `_screen_speech()` で感情タグ除去と NG ワードのマスクだけを通す。これは idea.md の明示的な方針（発言のたびに Gemini をもう1回呼ぶコストを避ける）で、`auto_topic_service._screen_auto_post` とは扱いが違う。**一方、コメントへの応答は「ユーザーからのインプットへの応答」なので従来どおり `screen_bot_response` で NG マスクと AI モデレーションを通す。** この非対称を崩さないこと。
- **コメントの受け付け範囲**: `is_comment_channel` が真になるのは**配信中のそのチャンネル**だけ。`MessageHandler.on_message` はこのときメンション判定を飛ばして `handle_comment` へ回す（傍観者は配信のチャットに気軽に書き込むもので、メンションを求めるのは配信の体裁に合わない）。他のテキストチャンネルからの発言はコメントとして認めない。ブラックリスト・NG ワード・入力モデレーションの関門は従来どおり通る。配信中のチャンネルは `auto_topic.watch_channel` に**登録しない**（登録すると配信終了後もそこがテキスト話題提供の対象として残り続ける）。
- **【重要】コメントには入力モデレーション前の流量制限（`claim_comment_slot`）が要る**: メンション不要で拾う以上、何もしないと「そのチャンネルの全メッセージ ×（モデレーション＋応答生成）」で Gemini 呼び出しが発散する（通常のメンション必須モードでは、メンションが自然な流量制限になっていた）。基準は返信完了ではなく直前の受付時刻で、配信単位のため同じユーザーの連投も制限する。クールダウン未満のコメントは入力AI・生成・出力AIを省略し、見送った入力のAI違反は記録しない。AIで拒否した受付も枠を保持し、受付前のローカルNGだけで拒否した入力は枠を消費しない。活動観測とローカルNG判定は維持する。**判定と時刻の記録を1メソッドにまとめてあるのは間に `await` を挟ませないため**（挟むと同時に届いた2件が両方通る）。基準に `last_spoke_at` を流用しないこと — 独り言でも更新されるため、**いちばん拾いたい「独り言に反応した最初のコメント」を捨ててしまう**。専用の `session.last_comment_reply_at` を使う。`MessageHandler` は入力モデレーション前に確保したセッションを `handle_comment(..., claimed_session=...)` へ渡し、同一セッションなら二重に枠を取らない。配信が入れ替わった場合、その予約は使わない。予約なしの直接呼び出しだけは内部の `_claim_comment_slot` で判定する。
- **コメント応答が通常チャットと違う点**: 長期記憶（`profile`/`projects`）を渡さず、親密度を更新せず、👍👎 の評価対象にも登録しない。記憶を渡さないのは、共有の場である配信で特定ユーザーの私的な記憶を読み上げると本人以外へ漏れるため（自発的な会話・記念日と同じ方針）。親密度と評価を外すのは、リスナーが「傍観者」であって1対1の会話相手ではないという idea.md の位置づけによる。**画像は通常チャットと同じく扱う** — 本文が空で画像だけのコメントは `AUTONOMOUS_IMAGE_ONLY_COMMENT` を本文の代わりに置き、`load_image_attachments` の結果を `generate` へ渡す（置き換えないと「空のコメントへの返事」を生成し、配信中だけマルチモーダルが死ぬ）。
- **配信中かどうかは、モデレーションの後にもう一度確かめる**: `on_message` の入口の判定と `handle_comment` を呼ぶ地点の間には Gemini 呼び出し（モデレーション）が挟まるため、その間に配信が終わりうる。`MessageHandler` は routing で `is_comment_channel` を取り直し、配信が終わっていて**かつ通常経路の条件（DM / 全発言モード / メンションあり）を満たすなら `handle_chat_message` へ流す**。古い判定のまま `handle_comment` を呼ぶと、セッションが無いので None が返り、メンション付きの発言まで黙って捨てられる。`handle_comment` 側も送信の直前に `_is_current_session` で確かめる（終わった配信へ後から応答を投稿しないため）。
- **`mark_activity` / `is_comment_channel` の引数順は `(channel_id, guild_id)`**（`AutoTopicService.mark_activity` / `watch_channel` と同じ）。同じ `on_message` から並べて呼ぶメソッドで順番が違うと、取り違えても型が同じ（int）ため静かに誤動作する。順番を変えないこと。
- **停止条件**（見回りが毎分確認する）: ①コメントが `AUTONOMOUS_IDLE_STOP_MINUTES`（30分）途絶えた ②深夜の時間帯制限に入った ③`AUTONOMOUS_SESSION_MAX_MINUTES` を超えた ④パネルで無効化された ⑤**別のチャンネルへ移動させられた** ⑥ボイス接続が外部から切れた ⑦閲覧・送信・ボイスでの発言権限、またはステージの発言者状態を失った。⑥・⑤の後、①〜④と独り言の生成前に⑦を確認し、不足した権限・状態をログへ出す。接続喪失・権限不足時も `stop_session` へ進め、所有ステージ枠・ボイス接続・記録をまとめて片付ける。サーバーを参照できない場合だけは記録を片付ける。①〜⑤は締めの一言を投稿してから退出する（④の文言も省かないこと — パネルの確認文はエフェメラルで操作した本人にしか見えず、聞いていた人からはボットが黙って消えたようにしか見えない）。⑦は閲覧・送信が可能なときだけ `AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS` を残し、投稿できないと分かっている場合は送信しない。`/leave` と `/join` からも `stop_session` を呼ぶ（`/leave` での強制退出は idea.md の要件）。**`stop_session` は例外を外へ出さない** — 締めの投稿やステージ枠の後始末が失敗しても、切断とセッション記録の破棄までは必ず進める（片付けが途中で止まると接続だけが残って誰も止められなくなる）。そのため後片付けは `discord.HTTPException` だけでなく**想定外の例外もログにして飲み込む**。終了案内や後片付けの途中でキャンセルされても、専用async taskで所有枠と接続の終了を完了してからキャンセルを伝播する。
- **継続時の権限**: `autonomous_stage.missing_session_permissions` が `channel.permissions_for(guild.me)` で判定する。接続済みなので `connect` は再要求せず、ボイスは実効の `speak` を確認する（discord.py は接続拒否による暗黙の発言拒否も反映する）。ステージは `speak` ではなく、同じチャンネルのボイス状態の `suppress` で発言者かを確認し、開始時の `manage_channels` / `move_members` / `mute_members` は要求しない。`guild.me`・チャンネル・ステージのボイス状態が未取得なら、その情報だけで権限不足とは判断せず、既存の停止条件は引き続き確認する。停止時はゲージも空に戻り、権限不足のチャンネルは開始用の `pick_channel` から外れるため、開始失敗の待ち時間を追加せずとも即座に同じ場所への接続を繰り返さない。
- **停止中の接続差し替え**: 停止開始時のVoiceClient参照だけを切断対象とし、待機中に `/join` や新配信へ差し替わった接続は切らない。同じVoiceClientが自動再接続した場合はforce切断する。枠キャッシュが無いときは取得し、所有IDを照合して閉じる。
- **チャンネル移動は「追従」ではなく「終了」**: 管理者にドラッグで別のチャンネルへ移されると接続は生きたままなので、接続の有無だけでは気づけない。`_stop_if_moved` が `guild.voice_client.channel.id` と `session.channel_id` を突き合わせ、食い違ったら締めの一言（`AUTONOMOUS_CLOSING_NOTICE_MOVED`）を残して配信を畳む。通常は `voice_guard` が移動の直後に `handle_external_move` から呼び、毎分の `_tend_session` はイベントを取りこぼした場合の受け皿。開始待機中（`_starting`）は `handle_external_move` が何もしない — 開始処理自身の照合（`_can_finish_start`）が接続ごと片付けて再試行の待ち時間も記録するため（停止要求で中断させると「利用者の停止」扱いで待ち時間が入らず、毎分の再挑戦と移動を繰り返しうる）。追従しないのは、セッションが開いたステージ枠を `channel_id` で覚えているため、行き先を差し替えると元の枠を閉じられなくなるから。
- **セッションの取り残しを防ぐ**: `_check_guild` は `guild` が引けないとき（サーバーから追い出された・キャッシュ未取得）も、セッション記録が残っていれば片付ける。残すと `is_comment_channel` が真を返し続け、さらに自発会話の競合抑止も効いたままになって**そのサーバーの話題提供が二度と動かなくなる**。
- **時間帯制限**: `AUTONOMOUS_QUIET_HOURS_START`〜`END`（既定 00:00〜05:59、`AUTONOMOUS_TIMEZONE` 基準）は開始せず、配信中でも退出する。タイムゾーンは**モジュール読み込み時に1回だけ**解決し（`autonomous_metrics.QUIET_HOURS_TZ`）、サービスもパネル表示も同じものを使う（`resolve_timezone` は失敗時に警告を出すため、パネルを開くたびに呼ぶとログが溢れる）。**画面へ出す時間帯は `describe_quiet_hours()` で作ること** — 定数から直接組み立てると、tzdata の無い環境で UTC へフォールバックしたときに「実際は UTC の 00:00〜05:59（＝日本時間の 09:00〜14:59）なのに Asia/Tokyo と名乗る」食い違いが起きる。ゲージは無会話時間で増えるため、誰も居ない深夜ほど早く満タンになる — 寝ている間にボイスチャンネルで喋り続けないための歯止め（idea.md の制限）。開始 > 終了（例: 22〜6）の日またぎ指定にも対応している。
- **接続の唯一の情報源は `guild.voice_client`**: `AutonomousSession` に VoiceClient を持たせないこと。持たせると、管理者にキックされた・回線が切れたときに実態と食い違った参照が残り、切断済みのクライアントへ喋り続ける。**`/join` で人が呼んだ接続があるサーバーでは配信を始めない**（読み上げ用に呼ばれている最中かもしれないため）。
- **多重発話の防止**: 生成と読み上げは数十秒かかるため見回りループの中で待たず `background.spawn` で裏へ逃がす。二重起動は `session.busy` で防ぐ（`_tend_session` が spawn の**前**に立て、`_speak_monologue` が `finally` で下ろす）。`handle_comment` は生成に入る前にも `last_spoke_at` を進め、応答を作っている間に独り言がぶつからないようにする。生成を待っている間に配信が終わっていることがあるため、送信の直前に `_is_current_session` で確かめる。
- **状態の保存範囲**: 永続化するのは有効/無効（`guild_settings.autonomous_enabled`、スキーマ バージョン9）と配信先の指定（`autonomous_stage_channel_id` / `autonomous_voice_channel_id`、スキーマ バージョン15。DB では 0、メモリでは `None` が「指定なし」）だけ。セッション・ゲージ・活動記録・開始失敗の待ち時間はインメモリのみ（ボイス接続自体が再起動をまたげないため、復元しても意味がない）。
- 調整値は `constants.py` の `AUTONOMOUS_*` に集約する。

### 記念日・誕生日の自動お祝い (`auto_topic_service.py`, `memory_service.py`)

自律配信中はお祝いを見送り、投稿済み記録も残さない。生成・モデレーション等の待機中に配信が始まった場合も送信前に再確認し、終了後の見回りで再試行できる状態を保つ（#349）。

会話の中で出てきた誕生日・記念日を長期記憶に覚えておき、当日にボットからお祝いを投稿する機能。専用の UI は無く、記憶と自発会話の枠組みに相乗りしている。

- **記憶側**: 記憶圧縮（`memory_service._compact_memory`）のプロンプトが、月日が確定できる記念日を `profile` の `ANNIVERSARY_PROFILE_KEY`（= `"anniversaries"`）キーへ `[{"label": "誕生日", "month": 5, "day": 1}]` の構造化リストで出力する。保存前に `io_lock` 内の同期ワーカーで最新のDBプロフィールを読み、純粋関数 `merge_anniversary_profile` が `label` 単位で統合する。生成前のスナップショットとも比較し、待機中に追加・訂正・削除されたラベルやキーは最新値を優先する。未変更ラベルの日付訂正は有効なLLM出力を優先し、既存だけのラベルは保持する。キー欠落・リスト以外の出力では最新値をそのまま残し、空リストでも既存を削除しない（#291）。新しい要素の形式はお祝い側と共通の `utils.anniversaries.parse_anniversary` で検証する（空でない文字列ラベル、boolを除く整数の月日、2月29日を含む暦上の妥当性）。既存の不正な要素は保持して読取側で読み飛ばす。**LLMの判断だけでは記念日を削除できない**が、省略による消失は戻せず、消したい記念日が残るより被害が大きいため保持を優先する。単発の予定はプロンプトで保存対象外とし、ユーザーが `data/memory/profile.json` に手書きしても登録できる。
- **お祝い側**: `run_anniversary_checks()` が `ANNIVERSARY_CHECK_MINUTES`（既定60分）ごとの `anniversary_loop`（`bot.py`）から呼ばれる。`Database.profile_load_all_for_key()` で全ユーザーの記念日を一括取得し、`ANNIVERSARY_TIMEZONE`（`Asia/Tokyo`）基準の「今日」と月日が一致するユーザーを、そのユーザーが所属する各サーバーの**自発会話の監視チャンネル**でお祝いする。投稿先は最終活動が新しい順に試し、送信できる最初のチャンネルを使う（`_pick_anniversary_channel`。削除済みのチャンネルは監視から外す。この選別では Gemini を呼ばない）。送信できるチャンネルが無ければ記録せず、権限が戻れば同日中の見回りで投稿する。生成後に送信を拒否された（`discord.Forbidden`）場合は、自発的な会話と同じくその日を処理済みとして記録し、毎時の再生成を止める（生成文はそのチャンネルの履歴から作ったため、別のチャンネルへ流用しない）。プロンプトは `build_anniversary_prompt`（`prompting.py`）。
- **二重投稿の防止**: `GuildSettings.anniversary_posted_dates`（`"{user_id}:{label}"` → `"YYYY-MM-DD"`）で同一サーバー・同一記念日の同日再投稿を防ぐ。記録は `record_anniversary_post()` がメモリと `anniversary_posts` テーブルの両方へ書き、起動時に復元されるため**当日に再起動しても再投稿しない**。主キーが `(guild_id, user_id, label)` の upsert なので毎年お祝いしても行は増えない。
- **フェイルセーフ**: `anniversaries` は LLM が書き込む自由記述寄りの記憶のため、型や形式が期待どおりでないエントリは静かにスキップする（例外で見回りを止めない）。
- **プライバシーと安全性**: 自発的な会話と同じ方針。材料はチャンネル履歴のみで**個人の `profile` は渡さない**（「今日が記念日」以上の私的情報を共有チャンネルへ出さない）。DM は対象外。NG ワードはマスクし、モデレーションで不適切と判定されたら謝罪文に差し替えず**投稿自体を見送る**。

### リマインダー (`reminder_service.py`, `reminder_view.py`)

アーカイブ済みスレッドの再開前にも系列ロック下でDBのpending状態を確認し、取消済みなら再開しない。再開HTTPの待機中は取消を受け付け、送信直前の再確認も維持する（#349）。

一覧取得・取消のDB障害は `ReminderStorageError` で呼出元へ伝え、正常な空一覧（`[]`）や対象なし（`None`）と区別する（#341）。UIは固定の失敗文を表示し、操作中のパネルを残して再試行できるようにする。待機中にパネルが切り替わった／時間切れになった場合は上書きせず、本人限定の追加応答で失敗と開き直し方を案内する。

日時の解析だけでなく、時差のない日時へのタイムゾーン付与とUTC変換までを例外捕捉の範囲に含める。`ValueError`・`OverflowError` は `None` とし、極端な年の入力でも登録せず通常会話へ進む（#291）。

通知先がアーカイブ済みスレッドなら、元のスレッドを再開してから通知する。ロック済みなら`manage_threads`を必要とし、再開失敗や送信権限不足では投稿しない。親チャンネルへの代替投稿とロック解除は行わない。

会話から登録し、指定時刻に**依頼されたチャンネル**でメンション付きに通知する。専用コマンドは無い。

- **登録経路**: `content_moderator.check_and_route` がモデレーションと同じ1回の呼び出しで日時＋本文＋**繰り返し種別**（`repeat`）を抽出する（**リマインダーのために Gemini を追加で呼ばないこと** — 判定を足すならこのプロンプトに相乗りさせる）。相対表現を解釈させるため、プロンプトには現在日時（曜日つき、`REMINDER_TIMEZONE` 基準）を埋め込む。`_build_combined_system_prompt` の JSON 出力例は3つ（安全時・リマインダー時・不適切時）あるので、形式を変えるときは**全部そろえて**直すこと（1つだけ直すとモデルが形式を揺らす）。
- **検証は `ReminderService` 側**: 日時の解釈（複数フォーマットを順に試す）、過去・`REMINDER_MAX_DAYS_AHEAD` より先の拒否、`REMINDER_MAX_PENDING_PER_USER` の上限、本文の切り詰め。繰り返しでも判定するのは**初回の日時**。**登録を見送るときは `None` を返す**（呼び出し側は通常の会話へ流す）。上限超過だけは意図が明確なので案内文を返す。
- **通知は生成しない**: 正確さが最優先なので Gemini を呼ばず、`⏰ @ユーザー リマインダーです: 本文` のテンプレートで組み立てる（繰り返しは `🔁 次回は …` を添える）。NG ワードのマスクだけ適用する（登録時にモデレーション済みのため再判定しない）。繰り返しは同じ本文が定期的に公開チャンネルへ出るため、マスクは**毎回**通すこと。通知を飛ばしてよいのは依頼者本人だけなので、`allowed_mentions` で本人のユーザーだけを許可する（本文の `@everyone`・`@here`・ロール・他人へのメンションは AI モデレーションを通過するため、送信側で止める。記念日のお祝いと同じ制御）。
- **二重通知の防止**: `Database.reminder_claim_due()` が SELECT と UPDATE を同一トランザクションで行い、期限到来分をクレームしてから返す（`queue_tasks` と同じクレーム方式）。送信自体が失敗しても `pending` に戻さない — 重複通知より「時間外れの再通知」の方が困るため。
- **繰り返し（`none`/`daily`/`weekly`/`monthly`）**: 新しい行を作らず、**同じ行の `remind_at` を次回時刻へ進めて `pending` のまま残す**（`reminder_id` が変わらないのでパネルからの取り消しが直感どおりに効き、`done` 行で保持枠を食い潰さない）。単発は従来どおり `done`。次回時刻の計算は `reminder_service.next_occurrence()`（暦だけを扱う純粋関数）が担い、`reminder_claim_due` へ**同期関数として渡してクレームと同一トランザクションで実行する** — 2ステップに分けると間でクラッシュしたときに繰り返しが失われる。
  - **渡す計算関数から `Database` のメソッドを呼ばないこと**（`self._lock` は再入不可の `threading.Lock`。同じスレッドから取り直すとデッドロックする）。`async def` にするのも不可（`await` されずコルーチンが返る）。
  - **停止明けは「未来になるまで反復」して追い付く**。1回分だけ進めると追い付くまで見回り（60秒）のたびに通知が飛ぶ。歯止めは `REMINDER_RECURRENCE_MAX_CATCHUP_STEPS`（到達したら `canceled` にして止める）。次回時刻を作れなかった繰り返しは、`None` が返った場合も計算が例外で落ちた場合も `canceled` にする（例外だけ `done` にすると正常に完了したように見え、途絶えた原因を追えない）。単発は常に `done`。過ぎた回はまとめて通知しない（通知は最大1回、時刻は未来へ）。
  - `_is_stale` が見るのは**進める前の（本来の）予定時刻**。そのためクレームが返すレコードの `remind_at` は進める前の値のままにし、進めた後の時刻は `next_remind_at` に入れて返す。stale で今回を見送っても次回時刻は計算済みなので**系列は途切れない**（長期停止明けは翌回の定刻から再開する）。
  - **月次は `recurrence_anchor`（初回時刻）を固定**したまま `calendar.monthrange` で末日へ丸める（`timedelta(days=30)` は使わない）。アンカーを毎回更新すると 1/31→2/28→**3/28** とずれる。正しくは 1/31→2/28→3/31。
  - 日時の加算は `REMINDER_TIMEZONE` のローカル時刻で行ってから UTC へ戻す（DST のあるタイムゾーンへ変えても壊れないため）。許容値・表示名・追い付き上限は `constants.py` の `REMINDER_RECURRENCE_*` に集約する。
  - **現在との比較は UTC へ戻してから行う**（#222）。壁時計の時刻どうしで比べると、夏時間の終了で同じ時刻が二度現れる時間帯に過去の時刻を `pending` で保存し、毎分取り直してしまう。曖昧な時刻は1回目、存在しない時刻は進める前のオフセットで解釈して後ろへずらす（02:30 → 03:30。`_local_to_utc` が `fold=0` で固定）。登録時の日時の解釈（`_parse_at`）も同じ `_local_to_utc` を通す（#398）。日次・週次も月次と同じく**アンカーの壁時計の時刻**で次回を作る（前回に1日足すと、ずらした 03:30 のまま戻らない）。`reminder_claim_due` も、計算した次回時刻が現在以前なら系列を止める。
- **時刻の扱い**: 保存は UTC の ISO 8601 で**秒精度に統一**する（期限判定を文字列比較で行うため、桁数が混ざると比較が壊れる）。表示だけ `REMINDER_TIMEZONE` に変換する（`_format_local`）。日時の書式に `strftime` の `%-m` / `%#m` は使わないこと（プラットフォーム依存）。
- **パネルの文字数**: 一覧はヘッダー・注記・改行を含め `REMINDER_PANEL_MAX_LENGTH` 以内に収め、入りきらない行は「…ほか N 件」と表示する。省略は本文だけに適用し、通常の上限20件すべてを取り消し用セレクトに残す。セレクトの `label` / `description` は `view_parts.select_option_text` で改行・連続空白を半角空白1つにまとめてから切り詰める（本文は会話から登録されるため改行を含みうる。改行入りの選択肢は Discord に拒否され、パネルごと開けなくなる）。
- **権限**: `/functions` → **⏰ リマインダー** で一覧・取り消し。表示は本人の分だけで、`reminder_cancel` は SQL の条件にも `user_id` を含めるため他人のものは操作できない。
- **取消と未送信通知の順序（#252）**: チャンネル取得・スレッド再開は取消のロック外で待つ。繰り返しの送信直前に系列ごとの非同期ロックを取り、`reminder_is_pending` で現在の状態を再確認し、送信終了まで同じロックを保つ。取消もこのロックを取り、取消成功が先なら本文と次回予告を送らない。送信開始が先なら今回の通知（分割分を含む）の終了を待って次回以降を取り消す。単発はクレーム時に完了済みで、それ以降の取消は成功しない。別の系列は独立し、DBの共有同期ロックをDiscord通信中に保持しない。系列ロックは弱参照で管理し、実行・待機が終われば回収する。状態確認に失敗した通知は送信せず、既存の通知失敗と同じく今回分を再送しない。

### ユーザー別会話統計 (`stats_service.py`, `stats_view.py`, `stats_privacy_view.py`)

収集停止中の本人が性格推定を開き、解析済みキャッシュがない場合は、解析失敗ではなく収集停止中のため新規解析をしていない旨を表示する。既存キャッシュの表示と他人への非公開境界は維持する（#349）。

収集設定の確定は `set_opt_out` の戻り値を確認し、`False` なら停止・再開の成功文を出さず「設定は変更されていません」と再試行の手順を案内する（#340）。保存失敗を「再起動で戻る可能性」とだけ伝えない。

解析キャッシュの日時は`parse_iso_datetime`で読み、末尾`Z`もPython 3.10で扱う。

`RunStatsButton` の説明文は `split_message` で分割し、最初を元パネル、続きを本人だけのfollowupへ送る。画像のFile・バッファは引き続き `finally` で回収する。`FeedbackButton` は権限・サーバー・サービスの確認後、集計より前に本人限定の応答をdeferし、結果を分割してfollowupへ送る。集計後も管理者権限を再確認し、失敗は本人だけへ案内する。

`RunStatsButton` は生成とFile作成も `try` の対象にし、生成失敗時は「集計中」の元パネルをエラー表示へ更新する。成功・失敗・キャンセルのいずれでも `discord.File.close()` の後にバッファを閉じ、Fileの作成途中で失敗した場合も作成済みの分を回収する。他人の統計を表示する場合は生成前と送信直前に管理者権限を再確認し、送信直前（描画・集計の待機のあと）は `interaction.user`（押した時点の写し）ではなく `discord_helpers.is_current_administrator` で`guild.fetch_member` から取り直した Member で判定する。ランキング・フィードバックも同じで、取得失敗・脱退・DMは拒否し、拒否時は作った画像を閉じる（#208）。両ボタンは結果を別の followup で送るためパネルの操作世代では連打を止められず、`StatsView.try_begin_aggregation` / `end_aggregation` で同じ集計の実行中の再押下を断る（判定と登録の間に await を挟まない。`finally` で外す。#398）。対象ユーザー選択のSelectも操作時に管理者権限を再確認する。ブロック解除は別の権限境界として、ボット所有者本人であることを操作時に再確認する（#279）。本人の統計は引き続き管理者権限なしで表示できる。

`/functions` の **📈 会話統計** から開く、ユーザー別の会話ダッシュボード。**すべてエフェメラル（非公開）で表示される。** 集計対象は「ボットへの発言のみ」で、`on_message` パイプラインの添付の振り分けの後（リマインダー判定の前。配信コメントは対象外）に `stats.record(...)` を fire-and-forget で呼んで蓄積する（失敗してもチャット本体は止めないフェイルオープン）。データは DB の `user_stats` テーブルに永続化され、`asyncio.Lock` で保存を直列化する（単一プロセス前提）。

- **集計データ**: ユーザーごとに累計発言回数・時間帯別ヒストグラム（`hourly[24]`、`STATS_TIMEZONE`＝`Asia/Tokyo` の「時」に変換）・直近発言サンプル（`STATS_RECENT_SAMPLES_MAX` 件のリングバッファ）を記録する。
- **話題・性格の LLM 解析** (`ensure_analysis`): 直近サンプルを Gemini に渡し、「よく話す話題」と「ビッグファイブ性格特性」を JSON で推定する。結果は `STATS_ANALYSIS_TTL_HOURS` の間キャッシュし、TTL 内なら再計算しない。LLM 呼び出し・パース失敗時は既存キャッシュにフォールバックする。LLM の出力は保存前に型と有限値へ正規化する（`normalize_topics` / `normalize_big_five`）。描画関数も同じ関数と `normalize_hourly` を通すこと — 以前の版が型を確かめずに保存したキャッシュが DB に残っていても、描画が例外で落ち続けないようにするため。性格推定は `STATS_PERSONALITY_MIN_SAMPLES` 件未満のとき「データ不足」として描画をスキップし、性格推定だけを見るときは解析自体を呼ばない。件数不足で解析した結果には性格推定を保存せず（`sample_count` を記録する）、件数が揃ったら TTL 内でも1回だけ解析し直す（`_lacks_personality`）。
同じユーザー・同じサンプルと収集設定の世代の実行中解析は共有する。`force=True`も実行中の同じ解析に合流し、完了後の次のforce要求で再計算する。サンプルや収集設定が変わった要求だけを新たに開始し、最後に開始した要求だけが保存できる。1人の待機者の中断は他の待機者へ伝播させず、全員が中断した場合は解析と保存処理を回収してから終了する。失敗・中断・完了時は実行中記録を外す。

- **項目別グラフ** (matplotlib、`usage_graph.py` と同じ日本語フォント作法): 時間帯＝棒グラフ / 話題＝重み付き横棒 / 性格＝ビッグファイブのレーダーチャート。項目セレクトで「全部 / 時間帯 / 話題 / 性格推定」を切り替えられる。
- **権限**: 一般ユーザーは**自分の統計のみ**閲覧できる（対象は `viewer_id` に固定）。**管理者のみ**、ユーザー選択メニューで他メンバーの統計を見られ、発言回数ランキング（全体比較）ボタンも使える。権限判定は `interaction.user.guild_permissions.administrator` によるサーバー側判定で、`RankingButton` 側でも二重にチェックする。
- **再起動でリセットされない**: DB の `user_stats` テーブルに永続化される。調整値（TTL・上限・タイムアウト・タイムゾーン）は `constants.py` の `STATS_*` に集約。
- **収集の停止（opt-out）** (`stats_privacy_view.py`): 本人が `/functions` → **🔒 統計プライバシー** から統計収集そのものを止められる。設定はユーザー単位で**全サーバー横断**（統計データ自体が `user_id` 単位でサーバーをまたぐため）、`user_stats.stats_opt_out` 列（スキーマ バージョン8）に永続化される。既定値は `STATS_OPT_OUT_DEFAULT`（`False` ＝従来どおり収集する。`True` にすると「原則収集しない」オプトイン運用になる）。
  - **判定は `StatsService` の内部で行う**（`record()` の冒頭＝エントリ生成より前、と `ensure_analysis()` の入口）。呼び出し側（`MessageHandler`）ではチェックしない — `record` の呼び出し箇所が増えたときのチェック漏れを防ぎ、fire-and-forget の主経路に `await` を足さないため。判定は `is_opted_out()` の**キャッシュ参照のみ**で DB へは行かない。
  - **通常更新は `Database.stats_save_user` による対象ユーザー1行の UPSERT**。発言・解析・収集設定・削除は `_save_lock` で直列化し、入れ子もコピーした候補を保存してから、成功した値だけキャッシュへ反映する（#253）。保存失敗時はキャッシュと解析の世代を変更しない。中断時も同期ワーカーの終了を待ち、同じロックの下で変更前の1行へ補償保存してからキャンセルを伝える。新規行だけは `stats_delete_user` で補償し、通常の蓄積データ削除にはこのメソッドを使わない。補償は `wait_for_cleanup` で繰り返しのキャンセルから保護する。補償自体もI/O障害で失敗した場合は運用ログへ残し、完全な復元は保証できない。`stats_save_all` は明示的な全件置換専用で、通常更新には使わない。`user_stats` の9列の保存変換は `_user_stats_row_params` へ集約し、旧JSON取込・通常保存・全件置換で共有する（#260）。`opt_out`を優先し、無ければ旧形式の`stats_opt_out`、両方無ければ`STATS_OPT_OUT_DEFAULT`を使う。`user_stats` に列を足すときは `stats_load_all`・共通変換・旧JSON取込とUPSERTのINSERT列を揃え、`stats_opt_out` を含む「保存→読み込み→保存」の往復を実測すること。
  - **停止と削除は別操作**: 停止（opt-out）は以降の収集を止めるだけで既存データは残る。🗑️ 蓄積データを削除（2回押しの確認つき）が集計値・発言サンプル・解析キャッシュを消す。**削除で行ごと消さないこと**（`del cache[user_id]` は禁止 — opt-out の設定まで消える）。
  - **他人からは見えない**: opt-out 中のユーザーは管理者のユーザー選択（`get_user_options`）と発言回数ランキング（`build_ranking_graph`）から除外される。他人が opt-out 中のユーザーを指定した場合は、既存の「◯◯ さんの会話データはまだありません。」と**同一の文言**を返す（専用の文言を出すと停止していること自体が露見する）。`StatsButton` は全体の `has_data`（停止者を除外）で入口を閉じず、押した本人を初期対象に `StatsView` を開き、データの有無は表示時の `build_dashboard` が判断する（空なら上記の既存文言）。そのため唯一のデータ所有者が停止中でも、全員が停止中でも、本人は収集を再開せず保存済みの統計と本人限定の注記を見られる。`has_data` を停止者込みへ戻さないこと。明示的な注記 `🔕 現在、統計収集を停止しています。` は**本人が自分のダッシュボードを見たときだけ**。`build_dashboard(user_id, item, viewer_id=None)` の `viewer_id` は「誰が見ているか」で、**省略時は本人扱い**。呼び出し元の `stats_view.RunStatsButton` は `viewer_id=view.viewer_id` を渡している（省略すると、管理者が他人の統計を開いたときに相手の opt-out の注記まで見えてしまう）。
  - **対象は統計だけ**: 長期記憶（`profile` / `projects` / `pending_log`）は opt-out の対象外。パネルの説明文でその旨を明示している（誤解させると透明性の目的を損なうため、文言を削らないこと）。
  - **フェイルオープンの例外**: 設定の保存・データ削除に失敗したときは、UI に「保存できませんでした」「削除できませんでした」と伝える（`save_background(user_id)`・`set_opt_out`・`clear_user_data` が成否を `bool` で返す）。プライバシー操作の失敗は黙って成功に見せない。解析の LLM 待機中はロックを離すが、削除・収集設定変更の世代を確認して古い結果を書き戻さない。解析失敗時も現在のキャッシュを参照し、待機中に消した解析を返さない。

### Discordの共通処理

`PersonaClient`の既定は`REPLY_ALLOWED_MENTIONS`。直接のリマインダー登録確認にも同じ設定を指定し、本文のNGマスクを適用する。返信先本人の通知は維持し、本文中の全体・ロール・他ユーザーは通知しない。チャンネル名を含む公開ボイス案内には`AllowedMentions.none()`を明示する。記念日・リマインダー配信は個別に対象者本人だけを許可する（#278）。

自発会話・記念日・リマインダー・公開要約の長文投稿は `discord_helpers.send_long_message` に送信オプションごと渡す。`send_long_reply` の「元発言へ最初の返信を返す」契約は別に維持する。`send_long_reply` は既定で `REPLY_ALLOWED_MENTIONS`（返信先本人への通知だけを許可し、`@everyone`・`@here`・ロール・本文中のユーザーは通知しない）を1通目と2通目以降の全チャンクへ渡す（#201）。通常会話・文書・配信コメントの返信がすべてここを通るので、呼び出し側で `allowed_mentions` を省略しても安全側になる。本文は AI が生成・引用したもので、モデレーションは通知先の認可を保証しないため、この既定を外さないこと。管理者の判定は `discord_helpers.is_administrator` を使う。自発会話・記念日・リマインダーが投稿前に行うチャンネル単位の送信権限の判定（閲覧と、スレッドなら「スレッドで送信」・それ以外は「メッセージを送信」）は `discord_helpers.can_bot_send_messages` に集約している。各サービスで `permissions_for` を書き直さないこと。

利用トークンとランキングの画像送信は `view_parts.send_graph_ephemeral` に集約する。defer後は `followup=True` を渡し、本人限定で送る。画像バッファの所有権も渡し、成功・失敗・キャンセルのいずれでも `discord.File.close()` の後にバッファを閉じる。Bot応答・自発会話・自律配信・リマインダーの出力マスクは `NgWordFilter.mask_with_log` を使い、文脈別のログと後続のモデレーションを維持する。

### グラフ描画と同期I/Oの待機

ペルソナの非同期読込は `config.paths.load_persona_template` に集約し、パス解決も読込もワーカーへ渡す。文面組立専用の `prompting.py` は引き続きサービスをimportしない。PNG変換は `graph_rendering.save_figure_to_buffer` がFigureを必ず閉じ、成功時のバッファだけ呼出元へ渡す。

`load_image_attachments` が正常に返した画像は呼出元が所有し、通常チャットと自律コメントの両方で直後からの`try/finally`と`close_image_attachments`により回収する。配信終了後の通常経路では読込済みの `image_objects` を `handle_chat_message` へ借用として渡し、再取得・再展開せず、所有者の `on_message` だけが閉じる（同じ依頼で二重に画像予算を使わない）。画像取得の途中で中断した場合は取得側が回収する。画像の`close`属性が呼出可能でない場合は飛ばし、解放の失敗が他の画像の回収やキャンセル伝播を妨げないようにする。

自発会話・記念日・自律配信・会話要約のペルソナ読込も `run_in_thread` で待つ。設定確定の存在確認とNGワード再読込はDiscordへ応答をdeferしてからワーカーで行う。NGワードは単語と正規表現を準備してからスナップショットを差し替え、読込途中の状態を公開しない。画像は `MAX_IMAGE_SIZE_BYTES` を取得前と取得後に確認し、デコードとRGB変換をワーカーへ渡す。画像の試行は先頭の `MAX_IMAGE_ATTACHMENTS` 枚まで（読込失敗・上限超過も数える。画像以外は数えない）。`Image.open` の直後、デコード前に元画像の各辺を `MAX_IMAGE_SOURCE_DIMENSION`（16384px）、総画素数をJPEGなら `MAX_IMAGE_JPEG_PIXELS`（5000万）、それ以外なら `MAX_IMAGE_PIXELS`（1200万）と比較し、絶対上限を超えた画像は展開せず見送る。JPEGは縦横比に沿った目標寸法を `Image.draft` へ渡して縮小デコードし、全形式を `Image.thumbnail` で長辺 `MAX_IMAGE_DIMENSION`（2048px）以下にしてからRGBへ変換する。これにより4032×3024・8064×6048のJPEG写真や1080×8000のPNGも受け付ける。RGB変換前に縮小後の実寸で幅×高さ× `IMAGE_RGB_BYTES_PER_PIXEL` を見積もり、既に保持する縮小済み画像を差し引いた `MAX_IMAGE_TOTAL_RGB_BYTES` の残り予算を超えたら変換せず見送る。予算を使い切れば後続の取得も止める。この予算はRGB画素データの合計で、圧縮データ・変換中の元画像やPillow内部の領域を含むプロセス全体の使用量ではない。入力画像と圧縮バッファは成功・拒否・変換失敗でも閉じ、中断時は同期ワーカーの終了を待って結果と取得済み画像も回収する。

モデルのJSON抽出には用途ごとの `required_keys` を指定する（モデレーションは `safe`、文書要約は `summary`、記憶ルーティングは `category`、圧縮は `profile` / `projects`、統計は `topics` / `big_five` / `personality`）。記憶カテゴリと繰り返し種別の正規化は `utils/normalization.py` に集約する。`reminder_service.normalize_recurrence` は同じ関数を再公開している。

フォント候補は `constants.GRAPH_FONT_FAMILY` を唯一の情報源とし、`graph_rendering.graph_font_context()` が導入済み候補と汎用フォントだけを順序どおり返す。利用量・時間帯・話題・性格・ランキングの全描画でこの設定を `plt.rc_context` に渡し、グローバルな設定は変更しない。Linuxでは `fonts-noto-cjk` 等の日本語フォントをOSへ導入する必要がある。導入・キャッシュ再生成の手順はREADME、字形と5種類のPNG確認は `tests/utils/test_graph_fonts.py` を参照する。LinuxのCIはフォントを導入し、未解決ならテストを失敗させる。

統計の `build_dashboard` / `build_ranking_graph` と利用量の `build_graph_buffer` は、データをイベントループでコピーしてから描画をワーカーへ渡す。matplotlibはスレッド間で共有状態を持つため、全描画関数を `graph_rendering.serialized_plot` の共通ロックで直列化する（Aggバックエンドを維持）。UIは描画前に応答をdeferし、完了後に本人だけのfollowupで返す。描画中の収集設定変更・削除は更新世代でも照合し、古い画像を破棄する。中断時の画像バッファ・Figureも回収する。

`background.run_in_thread` は `asyncio.to_thread` の完了を待つ入口。キャンセルしてもスレッド自体は止まらないため、worker完了まで呼出元のロックを保持してからキャンセルを伝播する。記憶・違反記録・会話統計・サーバー設定・記念日の記録の保存はこの入口を使い、後着した保存が削除や新しい更新を上書きしないようにする（ロックと組み合わせて初めて順序が守られる。ロックの無い呼び出しを置き換えるだけでは並行した保存の順序は守れない）。起動時の記憶ディレクトリ準備も `MemoryService.io_lock` の下で実行する。

キャンセル時に受取先の無くなった結果は、`close` が呼び出せる場合だけ回収する。属性取得・回収の例外は型名だけをログに出し、キャンセルを優先して伝播する。正常に返す結果の所有権は呼出元に渡し、この入口では閉じない。

### リアクションによるフィードバック収集 (`feedback_service.py`, `stats_view.py`)

ボットのチャット返信に 👍👎 を自動で付け、押されたリアクションを「どのモデル・どのペルソナの返信が良かったか」の記録として `feedback` テーブルへ残す。目的は応答品質のチューニングとモデル選定の判断材料で、**ユーザーへの返信は一切しない**（会話の邪魔をしない）。機能全体は `FEEDBACK_ENABLED` で止められる。

- **対象は `handle_chat_message` のチャット返信だけ**。文書処理の返信・自発的な会話・記念日・リマインダー通知には付けない（評価したいのは「会話としての応答品質」であり、定型通知を混ぜると集計が濁るため）。分割送信された長文は **1通目（`message.reply` したもの）にだけ**付ける。そのために `send_long_reply` は1通目の `discord.Message` を返す（**戻り値を「最後のチャンク」に変えないこと** — 2通目以降は元の質問への紐付けが無い `channel.send`）。
- **評価対象はインメモリのレジストリで覚える**: `FeedbackService.track_reply()` が `OrderedDict`（`FEEDBACK_TRACKED_MESSAGES_MAX` 件のリングバッファ）へ「メッセージID → サーバー/チャンネル/宛先/モデル/ペルソナ」を記録する。**このメソッドは同期**で、登録だけを即座に済ませ、通信を伴うリアクション付与は `background.spawn` で裏へ逃がす（登録まで後回しにすると、付与が終わる前に押されたリアクションを取りこぼす）。永続化しないのは、返信のたびに DB へ書くとチャットの主経路が重くなるため（再起動で消えるが、評価は返信直後に押すものなので実用上困らない）。
- **👍👎 の付与はクールダウンで間引くが、レジストリへの登録は全返信に対して行う**: 付与は返信ごとに Discord への API 呼び出しを2回増やし、この経路はチャンネル単位のレート制限が厳しいため、詰まるとチャット全体の応答が遅くなる（実測で確認済み）。`_claim_reaction_slot()` が「同じチャンネルへ直近 `FEEDBACK_REACTION_COOLDOWN_SECONDS` 以内に付与していたら見送る」判定を行う。**一律に「N 回に1回」で間引かないこと** — レート制限が痛むのは連投しているときだけなので、固定比率だと痛くない場面のデータまで等しく捨てる。**登録の側を間引かないこと**も同様に重要で、登録さえ残っていれば付与されなかった返信にユーザーが自分で 👍👎 を押した分も記録できる（間引きがそのままデータの損失にならない）。集計は「モデル×ペルソナ」で最大36通りに分割される（ペルソナ12 × モデル3）ため、票の総数を削る変更は内訳集計を成立させなくする点に注意。判定と時刻の記録を `_claim_reaction_slot()` の1メソッドにまとめてあるのは**間に `await` を挟ませない**ため（挟むと同時に2件通ってクールダウンが効かない）。
- **`feedback_prune` を評価が押されるたびに呼ばないこと**: `feedback_prune` は `(created_at DESC, message_id DESC)` の索引で新しい評価を一時ソートなしに選ぶが、削除処理は引き続き `database.py` の**プロセス唯一のロック**を握る。その間はチャットの主経路が使う記憶・親密度・統計の読み書きまで待たされ、返信そのものが遅くなる。`FeedbackService.prune_stored()` を `bot.py` の `feedback_prune_loop`（`FEEDBACK_PRUNE_MINUTES` ごと。`tasks.loop` は `start()` 直後に1回目が走るので起動時の間引きも兼ねる）から呼ぶ形にしてある。保持件数は「多少超過してもよい」性質の上限なので、主経路の速さを優先する。
- **数えないリアクション**: ボット自身が付けた初期リアクション（`payload.user_id == client.user.id` で最初に弾く。これを忘れると起動直後から自己投票でデータが汚れる）、他のボット（`payload.member.bot`。**DM では `member` が `None`** なので無条件に参照しないこと）、👍👎 以外の絵文字（`str(payload.emoji)` で比較する。`PartialEmoji` なのでカスタム絵文字と取り違えないため）、レジストリに無いメッセージ（**ログも出さずに黙って無視** — 他人のメッセージへのリアクションでも毎回イベントが飛ぶため）。
- **削除イベントのボット判定**: `member` がない場合は `client.get_user` のキャッシュを使い、既知のボットを除外する。キャッシュもなければ、追加を受け付けた評価状態がある場合だけ取り消す。利用者を特定するための外部通信はしない。
- **リアクションの一括削除**: `on_raw_reaction_clear_emoji` は指定された絵文字だけを全評価者から外し、残る側の評価へ戻す。`on_raw_reaction_clear` は全評価を取り消す。個別の削除イベントは別に届かないため、両方の raw イベントを登録する。対象の評価状態は最初の `await` より前にまとめて更新し、評価者ごとの既存ロックを使って最新状態を保存する。未追跡の返信には触れず、再起動前の評価を一括で消すためのDB検索も追加しない（#391）。
- **第三者の評価は数える**: 応答品質はチャンネルの参加者全員が判断できるため。ただし「評価者（`user_id`）」と「返信の宛先（`target_user_id`）」を別々に保存し、後から当事者だけで絞り込める余地を残す。`target_user_id` は集計表示には使わない（表示に使うなら別途プライバシー検討が必要）。
- **押し替え・両方押し・取り消し**: DB は主キー `(message_id, user_id)` の1行（＝1票）だけを持ち、👍→👎 は `INSERT OR REPLACE` で上書きされる。Discord は 👍👎 の同時付与を禁じていないため、**どちらを1票にするかは DB の行ではなく `FeedbackService._votes`（評価者ごとに「いま付いている絵文字と付けた順」）で決める** — 最後に付けた方を採用し、片方を外したら残っている側を採用し直して書き直し、両方外したら `feedback_delete` で行を消す（#155）。外した絵文字と保存済みの `rating` を照合して消す方式に戻さないこと（採用中の側を外すと、残っている側の票まで消える）。追加を受け付けていない絵文字の取り消し（他のボットの分など）は何も書かない。**保存は評価者ごとの `asyncio.Lock` の下で「その時点の最新の状態」を書く**（`_save_vote`）。個別の追加・削除は、状態の更新直後に `_save_vote` へ進んで保存要求の版を採番する。一括削除は全員の状態を先に同期更新し、保護した回収タスクから順に保存するため採番が遅れるが、同じロックの下で最新状態を読み直す。版は保存要求の識別に使い、受け付け時点の値を後から書き戻さない。イベントごとに受け付けた時点の値を独立して `run_in_thread` で書くと、別スレッドでの確定順が入れ替わり、遅れた追加が後の取り消し・押し替えを上書きする（#154）。保存済みの版が自分の版以上なら書かずに戻り、失敗・キャンセルで未反映の分は次のイベントが最新を書き直す。別の評価者・別の返信は並行してよい。`_votes` は `_tracked` から外れた返信の分を一緒に捨てる（ロックの同一性を保つため、評価が空になっても評価者の項目は作り直さない）。イベント（`on_raw_reaction_add` / `on_raw_reaction_remove` / `on_raw_reaction_clear_emoji` / `on_raw_reaction_clear`）は `on_reaction_add` ではなく **raw 版**を使う — 前者はメッセージがキャッシュに載っている間しか発火せず、少し前の返信への評価を取りこぼす。必要な `reactions` インテントは `Intents.default()` に含まれるため **`bot.py` の intents は触らない**。
- **フェイルオープン**: リアクション付与の失敗（`discord.Forbidden` → `discord.HTTPException` の順で捕まえる）も記録の失敗もログだけでチャットは成功扱い。イベントハンドラの本体は全体を `try/except Exception` で包む（漏らすと discord.py の既定 `on_error` がログを吐くだけで原因が埋もれる）。
- **閲覧は管理者専用・エフェメラル**: `/functions` → 📈 会話統計 → **👍 フィードバック**（`stats_view.py` の `FeedbackButton`、row 2）。権限は `discord_helpers.is_administrator` で判定し、**コールバック内でも再判定する**（`RankingButton` と同じ二重チェック）。集計は `interaction.guild_id` のサーバー分だけで、**DM 分（`guild_id=0`）は誰にも見せない**。表示は全体の 👍/👎/満足度とモデル別・ペルソナ別の内訳（最大 `FEEDBACK_SUMMARY_MAX_ROWS` 行）で、**「誰が👎したか」は出さない**（個人が特定されると評価しづらくなるため）。
- **`FeedbackService` の受け渡し**: `StatsView` は機能ハブ（`functions_view.py`）が生成するため、依存を引数で通すとハブのコンストラクタ・`clone()`・`register_commands` まで芋づるで変わる。付随機能でその連鎖を起こさないよう、`bot.py` が起動時に `feedback_service.set_default_service()` で登録した既定インスタンスを `StatsView` が引く（コンストラクタの `feedback` 引数で明示的に渡すこともでき、渡された場合はそちらが優先される）。
- 記録は `FEEDBACK_MAX_STORED_ROWS` 件で古いものから捨てる（`feedback_prune`。呼ぶのは定期ループだけ）。調整値は `constants.py` の `FEEDBACK_*` に集約する。
- **案内の文言も合わせて直すこと**: 付与を間引く以上、「返信に必ず 👍👎 が付く」という案内は嘘になる。`help_view.py` の `_FEEDBACK_LINE`（❓使い方とウェルカム投稿の共通の情報源）に「一部の返信にしか付かないが、自分で押しても記録される」旨を書いてある。付与の方針を変えたらここも必ず対で直す。

### サブエージェント（委任型） (`agent_plan.py`, `agent_service.py`, `agent_view.py`, `agent_thread.py`, `message_handler.py`)

作業スレッドの子の投稿でWebhookが `NotFound` なら、該当キャッシュを破棄して同じチャンクを1回だけ再取得・再送する。送信済みの先行チャンクは繰り返さず、再取得失敗・再送失敗なら打ち切る（#348）。

読込済みの`image_objects`は空の結果も含め、`_process_accepted_message`→`_handle_agent_request`→`handle_chat_message`へ借用として引き継ぐ。親の応答で再取得・再展開せず、引継ぎ先で画像を閉じない（#292）。

外部中断時は`AgentProgressBase.close()`で中止Viewを止め、`wait_for_cleanup`で既知のメッセージのボタン除去を待ってから実行枠を返す。スレッド開始途中・返信1通の進捗開始途中・報告のAI判定待ちも対象とし、通常の`finish`完了後は二重編集しない。除去に失敗しても元のキャンセルを維持し、ログは例外の型名だけにする。`AgentCancelView`は`PanelView`と異なるViewのまま、生成時の`TaskOwner`を保持し、本人確認・中止要求・応答を`owner.run`で管理する。応答の`HTTPException`を捕捉し、応答失敗でも受け付けた中止要求は維持する（#292）。

統合判定は固定指示とNGワード見本を`system_instruction`、本文と委任時だけの履歴をJSON化した`<moderation_data>`区画に分ける。山括弧とJSONの引用符・改行をエスケープし、本文・履歴の指示は判定用データとして扱う。問い合わせ別のJSONスキーマを渡し、`safe is True` の場合だけ記憶分類・リマインダー・委任を返す。`False`や文字列の`"false"`など、既存の正規化で不適切と判定される値は従来どおり拒否する。解釈できない値・例外・空応答・不正JSONでは会話を通過させるが、記憶分類・リマインダー・委任は返さない。抽出した通知本文・委任各項目は既存上限内の1行へ整え、`@`を全角化する。入力の分離と構造化出力は判定誘導の完全な防止を保証しない（#280）。

会話の中で「サブエージェントを使って」と**明記された依頼だけ**を、人格役（ペルソナ）から子のAIへ委任する（#170）。スラッシュコマンドは無い。返答するのは常に人格役で、子は裏方として Markdown の報告を返すだけ。

- **起動条件は2段階**: ① 本文が `AGENT_TRIGGER_PATTERN` に一致する（`agent_plan.requests_subagent`。Gemini を呼ばない）。② そのときだけ `ContentModerator.check_route_and_plan` が、モデレーション・記憶カテゴリ・リマインダーと**同じ1回の呼び出し**で「委任するか（`delegate`）」と子への指示（`role` / `task` / `expected_output` / `steps`）を返す。明記があっても依頼でなければ `None`、人格役だけで答えられるなら `delegate=False`（人格役が `build_agent_declined_section` で一言添えて直接答える）。**明記の無い発言のルーターのプロンプトは変えないこと**（全チャットのトークンと既存判定の精度に響く）。委任の判定を足すときは出力例の3つすべてに `"agent"` を足す（`_build_combined_system_prompt` の `agent_key`）。
- **ルーターが「さっきの件」を具体化する**: 子は会話履歴を見られないため、ルーターへ直近の会話を渡し、指示語を具体的な依頼内容へ書き直させる。会話履歴は委任内容の整理用で、安全性の判定対象は今回のテキストだけ。不適切な入力・JSONを読めない判定からは委任を作らない（`_route`）。
- **子は独立した文脈**（`AgentService.run`）: 渡すのは `build_agent_worker_prompt` の依頼内容だけ。ペルソナ・会話履歴・長期記憶は渡さない。1手順につき検索付きの生成を1回（`AGENT_MAX_STEPS` が子のツール実行の上限）。前の手順のメモを次へ引き継ぎ、最後の手順が期待する出力形式の最終報告を書く。どの手順でも生成に失敗したら `None`（残りの手順は呼ばない）。トークンは `UsageTracker.log_tokens` で記録する（`User (` 行を出さないので管理コンソールの会話件数に数えない）。検索はサーバーの 🔍 Google検索 の設定に関わらず使う（Issue の決定事項）。
- **人格役の返信は `handle_chat_message` を再利用する**: 報告は `delegation_section`（`build_agent_report_section`）として差し込み、`delegated_sources` を渡したときは人格役は検索せず子の出典を添える。親密度・出力のモデレーション・評価・記憶・読み上げは通常の会話と同じ経路を通る（委任用に後処理を複製しないこと）。
- **進捗は案A（LLM を呼ばない定型文）**: 表示の形は2つで、`agent_thread.start_agent_progress` が作業スレッドを優先し、使えなければ返信1通の書き換えへ切り替える。どちらも `agent_view.AgentProgressBase`（中止ボタン・期限・回収の `run`）を継承し、`update` / `finish(key, report)` だけが違う。文面は `personas.json` の `agent`（`personas._build_agent_lines` が読み込み時に差し込みを点検し、誤った項目は既定へ）→ `AGENT_DEFAULT_LINES` の順。特定のキャラの口調を `constants.py` に書かないこと。ロール・依頼内容・出力形式・手順名はLLMの出力なので、`MessageHandler` が表示前に NG ワードを伏せる（子にも伏せた依頼を渡す）。進捗の送信・編集の失敗は作業を止めない。
- **作業スレッド（`AgentThreadProgress`。#267）**: 依頼メッセージに `message.create_thread`（`AGENT_THREAD_AUTO_ARCHIVE_MINUTES`）でスレッドを作り、親（ボット自身の名義）が `handoff` + 依頼内容・期待する出力・手順を投稿してそこに中止ボタンを付ける。子は Webhook（`username` = `name` の定型文、`avatar_url` = `personas.json` の `agent.avatar_url`）で `child_start`・手順ごとの `child_progress`（書き換えずに1通ずつ残すので間引かない）・`child_report` + 報告 + 参考リンク（分割・埋め込み抑止）を投稿し、最後に親が `done` / `failed` / `canceled` をスレッドへ残す。失敗・中止は人格役の返信が来ないので元のチャンネルにも返信する。`start` / `progress` は使わない。
  - **子の発言の安全性**: 報告本文と出典のタイトル・URLを`MessageHandler`でNGマスクし、組み合わせた全文を`screen_bot_response_with_status`で独立してAI判定する。拒否時は`finish("failed")`で定型案内だけを残し、報告の公開も親の生成も行わない。通過した報告だけを`finish`と人格役へ渡す。親の最終応答も従来どおり別に判定する。子の報告は親とは別の公開面となるため、公開前のAI出力判定を省略しない（#286）。投稿はすべて `AllowedMentions.none()`。子は今までどおり独立した文脈で、スレッドの会話や長期記憶を読ませないこと。
  - **使えない場面は返信1通へ（フェイルオープン）**: `thread_unavailable_reason` が `AGENT_THREAD_ENABLED`・テキストチャンネル（DM・スレッドの中・ボイス/ステージのチャットは不可）・`guild.me`・チャンネル単位の `view_channel` / `create_public_threads` / `send_messages_in_threads` / `manage_webhooks` を確かめる。その後の Webhook の用意・スレッド作成・委託内容の投稿のどれかが失敗しても `AgentProgress` へ切り替える。**Webhook はスレッドより先に用意すること**（逆にすると投稿できない空のスレッドが残る）。委託内容の投稿に失敗したスレッドは消さずに残す。`open` の途中で止められたら中止ボタンの View を止める。
  - **Webhook は使い回す（`AgentWebhooks`、`MessageHandler._agent_webhooks`）**: チャンネルごとに、ボット自身が作った `AGENT_WEBHOOK_NAME` の incoming Webhook を探し、無ければ1つ作ってインメモリにキャッシュする（再起動後も同じものを見つける）。依頼のたびに作らないこと（チャンネルあたりの Webhook の上限に達する）。作成は全チャンネル共通のロックで直列化する。投稿で `NotFound` なら `forget` してその依頼では子の投稿をやめ、次の依頼で作り直す。
  - **子の表示名は `webhook_username` で整える**: Discord は Webhook 名に `discord` / `clyde` を含められず、`@ # : ```` も使えない。ロールは LLM が決めるので、禁止語を取り除き記号を全角にし、80 文字で切る。空・`everyone` / `here`・取り除いてもまだ禁止語が残る場合は `AGENT_CHILD_FALLBACK_NAME`。外すと「Discord ボットの調査担当」のようなロールで子の投稿がすべて 400 になる。
- **中止と期限**: `AgentProgress.run` が子を別タスクで動かし、中止ボタン（依頼した本人だけ）・全体の期限（`AGENT_TOTAL_TIMEOUT_SECONDS`）・呼び出し側のキャンセルのどれでも子のタスクをキャンセルする。`finally` で未完了の子と中止待機のタスクへキャンセルを要求し、`gather(return_exceptions=True)` で両方を回収する（全体を `wait_for_cleanup` で保護し、親の繰り返しのキャンセルは回収後に伝播、片方の回収例外で他方の回収を止めない）。完了済みか・中止が押されたかは回収前に控え、中止後に子が報告を返しても成功へ変えず、期限切れの回収中に中止が押されても期限切れの扱いを保つ。`MessageHandler` の `run` 〜 `finally` の `release` は変更不要で、回収が終わるまで `AgentService` の実行枠も保持される。失敗・期限切れ・中止は定型文で伝え、人格役の生成はしない（読み上げもしない）。
- **同時実行**: `AgentService.try_acquire` が同じ利用者の依頼を1件ずつ、全体で `AGENT_MAX_CONCURRENT_TASKS` 件までに制限する（判定と登録の間に `await` を挟まない）。超えた依頼は `busy` の定型文を返し、通常の会話にも流さない。実行枠は `finally` で必ず返す。
- **状態**: 実行中の依頼者（`AgentService._running_users`）と作業スレッド用の Webhook のキャッシュ（`AgentWebhooks._cache`）はインメモリのみ。設定は `AGENT_*`（`constants.py`）に集約し、`AGENT_ENABLED = False` で明記があっても通常の会話として答える。`AGENT_THREAD_ENABLED = False` で作業スレッドだけを止め、常に返信1通の進捗表示にする。

### オンボーディング導線 (`help_view.py`, `discord_helpers.py`, `bot.py`)

「メンションするだけで会話できる」ことも「機能が `/functions` に集約されている」ことも、招待しただけでは誰も気づけない。そこで最初の一歩を案内する入口を2つ用意している — `/functions` → **❓ 使い方**（`HelpView`）と、サーバー参加時のウェルカム投稿（`bot.py` の `on_guild_join`）。

- **ヘルプとウェルカムの文言を2か所に書かないこと。** 両者は説明する内容が大きく重なるため、共通部分（メンションでの会話・添付・リマインダー・サブエージェント・読み上げ・自律行動・評価・記憶・統計）は `help_view.py` のモジュール内定数を唯一の情報源にし、`build_help_text()` / `build_welcome_text()` の両方がそれを組み立てて使う。片方だけ直すと「ウェルカムには書いてあるのにヘルプには無い」という食い違いが生まれ、案内としての信頼を失う。**機能を足したり既定値を変えたりしたら、ここの文言も必ず合わせて直すこと**（本文が実装と食い違わないことが最優先）。長い日本語テキストのため `constants.py` には置かない（`constants.py` は調整値の置き場という既存規約を守る）。
- **ボット名はハードコードせず `resolve_bot_name(client.user)` で解決する**（別名のボットで同じコードを動かすとメンション例だけが嘘になるため）。名前から `@` を落とすのは、`@everyone` という名前のボットが作られた場合への保険。
- **本文は Discord の1メッセージ上限に収める**: `_fit_to_discord_limit` が超過分を切り詰めて警告ログを出す。エフェメラルの差し替え（`edit_message`）は1通しか置き換えられないため `split_message` は使えない（View を持てるメッセージは1通だけ）。切り詰めは安全弁であり、発動したら文言を減らすこと。
- **使い方パネル**: `HelpView` は `PanelView` を継承し、部品は ↩️ 戻る（row 0）だけ。表示のみで副作用が無いため権限制限は設けない（DM でも使える）。差し替えは `view_parts.swap_panel()`。
- **ウェルカム投稿の配線は薄く保つ**: `on_guild_join` は「本文を組み立てて `discord_helpers.post_welcome_message` へ渡す」だけで、判断ロジックは各モジュール側にある。イベントハンドラの本体は `try/except Exception` で包む（案内の失敗でイベント処理を落とさない）。必要な `guilds` インテントは `Intents.default()` に含まれるため **`bot.py` の intents は触らない**。
- **投稿先の決め方**（`pick_announcement_channel`）: ① `guild.system_channel` → ② `guild.text_channels` を順に走査して最初に送信できるチャンネル → ③ 見つからなければ投稿しない（`print` のログのみ）。権限は必ず `channel.permissions_for(guild.me)` で**チャンネル単位に**、閲覧（`view_channel`）と送信（`send_messages`）の両方を確認する（サーバー全体のロールで許可されていてもチャンネル個別で拒否されうる。`/join` の接続権限チェックや自発会話と同じ作法。discord.py は閲覧できないチャンネルの送信権限も落とすが、その内部の挙動には頼らない）。「送ってみて失敗したら次」の例外頼みにしないこと（無駄に API を叩き、権限エラーとネットワーク障害を区別できない）。`guild.channels` は使わない（カテゴリやボイスが混ざる）。`guild.me` が `None`（キャッシュ未取得）なら権限を判定できないので諦める。
- **連投防止**: `BotState.welcomed_guild_ids`（**インメモリのみ**。`auto_topic_channels` と同じ「実行時に自然に再構築される一時状態」）。`on_guild_join` は実際に招待されたときにしか発火せず再起動・再接続では発火しないため、プロセスをまたぐ重複投稿は原理的に起きない（この集合は同一プロセス内でイベントが重複した場合の保険）。**投稿済みフラグは送信を待つ前に立てること** — 送信後に立てると、その待ち時間に入ってきた2件目が二重投稿になる。DB へは持たせない（招待時にしか発火せず、再起動をまたぐ重複投稿は起きないため）。
- **ウェルカム本文に `@everyone` / `@here` / ロールメンションを絶対に書かないこと**（公開チャンネルへの投稿なので通知爆撃になる。招待→キック→再招待を繰り返されると毎回投稿される）。送信側でも `discord.AllowedMentions.none()` を付けて二重に防いでいる。個人情報・記憶・統計も載せない（参加時点でそもそもデータが無い）。
- **フェイルオープン**: `post_welcome_message` は例外を外へ出さず、投稿できたかを `bool` で返す（`discord.Forbidden` → `discord.HTTPException` の順で捕まえる）。案内が出せなくてもボット自体は普通に使える。`WELCOME_MESSAGE_ENABLED`（`constants.py`）で投稿だけを止められ、止めても ❓ 使い方 からいつでも読める。既に招待済みのサーバーへは投稿されない（仕様として許容）。

## 規約と落とし穴

- **運用ログ（#283、全4段階）**: 通常起動では `bot.py` だけが `utils.logging_setup.configure_logging()` を呼び、core・services・views・audio・config/utilsは `logging.getLogger(__name__)` を使う。INFOは通常進捗、WARNINGは代替・見送り、ERRORは処理失敗とし、処理を続けられる後片付け失敗などの診断はDEBUGへ残す。捕捉した例外は `exc_info=True` または例外オブジェクトを渡して発生箇所を保持するが、例外本文をメッセージへ展開しない。共通Formatterは識別子を伏せ、例外型とファイル名・関数名・行番号だけを出す（不正な `exc_info` でも整形を失敗させず、型でない値は `str()` せず型名だけを示す。#399）。生の利用者・チャンネル・サーバーID、非公開語、会話、任意のファイル名は既定ログへ出さず件数・状態にする。識別子は `user_id=<ID>` のように**呼び出し側で伏せた文字列**を渡す（Formatter の伏せ字は最後の砦で、`assertLogs` が受け取るレコードや Formatter を設定しない単独実行には効かない。テストもレコードの本文で `<ID>` を確かめている）。例外は、`load_json_file` / `write_json_file` が出す内部の固定ファイル（`projects.json`・`blacklist.json`・`profile.json`）の名前（`path.name`）だけ。絶対パスと退避先の名前は出さず、利用者が名付けるファイルをこの関数へ渡さないこと（#378）。ログイン成功のボット名は起動確認とコンソールのボット名表示に必要なためINFOへ残す。標準出力のUTF-8設定をハンドラ生成前に行い、`client.run(..., log_handler=None)`でdiscord.pyの別ハンドラを増やさない。servicesは判定理由・NG語・会話断片・入力日時も既定ログへ出さず、記憶圧縮の失敗登録へ事前整形文字列を渡さない。viewsも固定文へ統一し、PanelView・AgentCancelViewのon_errorではSDK既定のView/Itemのreprを出さない。usage_graphは質問・応答・添付名・文脈名を記録せず、集計用の `User (会話)：` と `Total token count：` を維持する。設定不足のキー名と対処案内は、共通設定を呼ばない単独の設定読込でも表示できるレベルにする。
- **管理コンソールとのログ互換**: `User (...)：`・`Total token count：` と「ログイン成功」の文言は計数・起動確認の契約。旧printと新loggingの接頭辞付き行の両方を扱う。時刻・レベル・モジュール名の接頭辞は公開してよいログの証明にならないため、非systemdエントリーを保守的に伏せる既存の境界を緩めない。
- **パス解決**（`persona_bot/config/constants.py`, `persona_bot/config/paths.py`）: `BASE_DIR = src/`（`constants.py` の `Path(__file__).resolve().parents[2]`）、`PROJECT_ROOT` はその親。ペルソナファイルは `"../prompts/<ファイル名>.txt"` のような値（`src/` 基準。`PERSONA_VALUE_PREFIX` + ファイル名）として保存・選択され、`resolve_prompt_path` で解決される。`data/memory/`・`data/ng/` はプロジェクトルートの `data/` 配下、`prompts/` はルート直下にある。ファイルの配置を変えたら `tests/integration/test_package_layout.py`（ペルソナの値の解決先・公開テンプレートの実在と `.env`・ペルソナの場所と、DB・記憶・NG の `data/` 配下への保存先を確かめる）を回す。
- **旧データの配置移行**: 旧ルート直下の `memory/`・`ng/` が残っている場合は、ボット停止中に `python deploy/migrate_data_directory.py --check` で確認し、`python deploy/migrate_data_directory.py` で移す。スクリプトは `data/.migration-backups/` に旧配置をバックアップして内容を照合し、同名で内容が異なるファイルがあれば移動前に止める。移行前の旧データを無視して新規DBを作らないよう、ボット起動時も旧配置の残留を確認する。
- **設定（APIキー）**: `settings_loader.load_settings()` はプロジェクトルート直下の `.env`（パスは `constants.ENV_FILE_PATH`。**絶対パスで解決すること** — ボットは `src/` から起動するため、カレントディレクトリ基準の相対探索では見つけられない）を `python-dotenv` で読み込み、`.API_KEY_GEMINI` / `.TOKEN_DISCORD` を持つ `Settings` データクラスを返す。旧 `config.py` / `config_private.py` は ver0.1.6 で削除済み。
  - **`load_dotenv(override=False)` を変えないこと**: OS の環境変数が `.env` より優先される。systemd の `EnvironmentFile=` や Docker の `--env-file` へ移行できるようにするための挙動で、逆にするとサーバーに残った古い `.env` が黙って優先され「直したはずのキーが反映されない」事故になる。
  - **未設定なら起動を止める**: ファイル無し・変数無し・空文字・`.env.example` のプレースホルダのまま、のいずれでも `キー未設定` を出力して `SettingsError` を送出する。`load_settings()` は `bot.py` のトップレベルで呼ばれるためボットは起動しない。キーが無いまま起動すると「Discord へは接続できるが Gemini の呼び出しだけ全滅する」という切り分けの難しい状態になるため、あえて失敗させている。
  - **`python-dotenv` が未インストールでも例外にしないこと**: 警告を出して `os.environ` だけで続行する（環境変数でキーを渡す運用ならこのパッケージは不要なため）。
  - `.env.example` のプレースホルダ文字列を変えたら `settings_loader._PLACEHOLDER_VALUES` と `deploy/migrate_config_to_env.py` の `PLACEHOLDER_VALUES` も**必ず対で直す**こと（片方だけ直すと書き換え忘れの検出が効かなくなる）。
  - **移行用**: `deploy/migrate_config_to_env.py` が旧 `config_private.py` / `config.py` から `.env` を生成する（ver0.1.5 以前から更新した環境向け。ボット本体からは呼ばれない一度きりのスクリプト）。保存は `_write_secret_file` が、同じフォルダに作成時から 600 の一時ファイル（`tempfile.mkstemp`）を作り、権限・書き込み・fsync に成功してから `os.replace` で置き換える。どの段階で失敗しても秘密を書かず、既存の `.env` を残して終了コード 1 で止め、一時ファイルを回収する（権限の設定の失敗を警告だけで続行する、`O_TRUNC` で元の内容を先に切り詰める形に戻さないこと。#209）。旧ファイルは `ast` で構文解析するだけで **import しない** — import は中身を実行するため、値を読むだけの用途で実行する必要が無い。
- **調整値は `constants.py` に置く** — モデル名、タイムアウト、しきい値、上限値。新規もインラインではなくここに追加すること。
- **fire-and-forget は `background.spawn(coro, label=...)` を使う（`asyncio.create_task` を直接呼ばない）**: asyncio のイベントループは実行中タスクを**弱参照**でしか保持しないため、`create_task` の戻り値を捨てるとタスクが実行途中でガベージコレクトされて消えることがある（Python 公式ドキュメントが明記している既知の落とし穴）。`spawn` は完了までタスクへの強参照を保持し、完了時に自動で取り除く。加えてタスク内の例外を `label` 付きでログに出す — `create_task` に投げっぱなしだと例外は回収時まで報告されず、そのまま失われることもある。特に `documents.run_worker()` のような常駐コルーチンでは、消えると添付ファイルの処理が二度と走らなくなる。
- **タイムゾーン名を表示・プロンプトに載せるときは `timeutil.timezone_label(self._tz)` を使う（`REMINDER_TIMEZONE` などの定数をそのまま書かない）**: `resolve_timezone` は tzdata の無い環境で UTC へフォールバックするため、定数を表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」食い違いが起きる。リマインダーの統合判定プロンプトでは、この食い違いがそのまま9時間ずれた日時での登録に直結する。
- **モデレーションはフェイルオープン**: タイムアウト/例外・空候補・本文の欠落・不正JSONでは `check` / `check_and_route` が *safe* を返し、違反を数えない。統合判定のカテゴリ・リマインダーは `none` / `None` にする。Gemini の明示的なプロンプトブロックと有効なJSONの不適切判定は *unsafe* として扱う。**`check` / `check_and_route` の本体で例外を外へ漏らさないこと**: これらは `on_message` の主経路から呼ばれるため、抜けた例外はそのメッセージの処理ごと落とす。準備処理（タイムゾーンの解決など）を `try` の外に置くと、環境依存の失敗が全メッセージの無応答に直結する（実際に `ZoneInfo` の組み立てで起きた）。
- **保存する UTC 時刻は `timeutil.utc_now()` / `timeutil.utc_now_iso()` で作る**（`datetime.now(timezone.utc).isoformat()` を各サービスに直書きしない）。既定は秒精度で、リマインダーの期限判定のように ISO 文字列の比較で扱う値の桁数を揃える。例外は文書キューの `created_at` で、取り出し順が `ORDER BY created_at, task_id`（`task_id` の先頭はチャンネルID）のため、同じ1秒に届いた別チャンネルの文書を到着順に処理できるようマイクロ秒を明示して残す。リマインダーの `_utc_now` はテストが時計を固定する差し替え口なので名前を残す。記憶圧縮前ログの `"%Y-%m-%d %H:%M:%S UTC"` は圧縮プロンプト向けの表記なので対象外。本文が無い添付だけの依頼に使う質問文は `constants.py` の `IMAGE_ONLY_QUESTION`・`DOCUMENT_ONLY_QUESTION`。
- **タイムゾーンは `timeutil.resolve_timezone(name, purpose=...)` で解決する**: `ZoneInfo` を直接呼ばないこと。`requirements.txt`の`tzdata`により、OSにタイムゾーンDBのないWindowsやスリムなLinuxでも`Asia/Tokyo`を使う。未導入・データ欠落・無効なタイムゾーン名では、この関数がUTCへのフォールバックと警告ログをまとめて引き受ける。導入後はボットを再起動し、サービスの保持するタイムゾーンを読み直す。タイムゾーン名そのもの（`STATS_TIMEZONE` / `ANNIVERSARY_TIMEZONE` / `REMINDER_TIMEZONE`）は用途ごとに `constants.py` で個別に調整できる形を保つ。
- **再起動でリセットされる状態**（インメモリのみ）: 利用/トークン履歴（`usage_graph`、500件のリングバッファ）、ペルソナ一覧のスナップショット（`personas._catalog`。起動時とキャラ設定パネルを開いたときに `prompts/` から読み直す）、`channel_reset_points`（`/functions` → ⚙️キャラ設定パネルの会話リセットのアンカー）、自発的な会話の**監視状態**（`auto_topic_channels` / `auto_topic_posted_at`）、自律行動の**配信状態と話したいゲージ**（`autonomous_session` / `autonomous_last_activity` / `autonomous_recent_activity`。ボイス接続自体が再起動をまたげないため復元しない）、フィードバックの**評価対象レジストリ**・**評価者ごとのリアクション状態**・**付与クールダウンの記録**（`FeedbackService._tracked` / `_votes` / `_last_reaction_at`。記録済みの評価そのものは DB に残る）、ウェルカム投稿済みのサーバーID（`welcomed_guild_ids`。`on_guild_join` は招待時にしか発火しないため、消えても再投稿は起きない）、ボイス接続先の保護の**意図した接続先と戻した時刻**（`VoiceGuard._homes`。ボイス接続自体が再起動をまたげないため）、サブエージェントの**実行中の依頼者**（`AgentService._running_users`。進行中の作業は再起動で失われる）と作業スレッド用の **Webhook のキャッシュ**（`AgentWebhooks._cache`。Webhook 自体は Discord に残り、次の依頼で見つけ直す）、音声会話（#268）の**セッション・同意・再開ハンドル・SSRC と話者の対応**（`VoiceConversationService._sessions`・`ConsentState`・`LiveSession._resume_handle`・`LiveVoiceClient._ssrc_users`。ボイス接続自体が再起動をまたげないため）。一方、`GuildSettings` の**設定値**（ペルソナ/モデル/応答モード/グラウンディング/自発会話の ON・しきい値/自律行動の ON・配信先）・記念日の投稿記録・リマインダー・フィードバックの記録は DB に永続化され、再起動しても失われない。永続化される状態は `data/memory/bot.db`（SQLite: affinity / user_stats / queue_tasks / pending_log / profile / guild_settings / anniversary_posts / reminders / feedback / legacy_imports）、`data/memory/projects.json`、`data/ng/blacklist.json`。`data/memory/profile.json` は手書き追加用の受け皿で、起動時に DB へ吸収され `{}` に戻る。
- **単一プロセス前提**: DB は `database.py` 内部の `threading.Lock` + トランザクションで、`projects.json` は `MemoryService.io_lock`（`asyncio.Lock`）で保護される。`blacklist.json` の違反登録・解除は非同期ロックで直列化し、別スレッドで原子的保存した後にキャッシュのスナップショットを差し替える（`profile.json` は起動時に DB へ吸収されるだけ）。保存前に `data/ng/` を作る（初回の違反記録で保存できるように）。手で直すこともあるファイルなので、ユーザーごとの値が dict でない・回数が数値でない記録は「記録なし」として扱い、`is_blocked` から例外を出さない（`on_message` の最初に呼ばれるため）。プロセス間ロックは存在しない。DB は WAL モードのため、稼働中でも `sqlite3` CLI 等からの**読み取り**は安全（外部からの書き込みはしないこと）。
- **ログ出力とエンコーディング**: 起動時の `configure_logging` が標準出力・標準エラーをUTF-8へ再設定し、loggingハンドラも再設定後の標準出力を使う。単独スクリプトでINFOの運用ログも必要なら、入口で `persona_bot.utils.logging_setup.configure_logging()` を呼ぶ。ライブラリのimport時に共通ログ設定を行わない。設定を呼ばない単独実行ではPython既定のWARNING以上が対象になるため、起動に必要な対処案内をINFOへ下げない。
- **Discord の制限**: 2000文字を超える返信は `send_long_reply`/`split_message` を通す。`send_long_reply` は**1通目（元メッセージへの `reply`）**を返し、フィードバックのリアクション付与がそれを使う（戻り値を最後のチャンクに変えない。戻り値を無視する呼び出しはそのままでよい）。グラウンディング返信はリンクの埋め込みプレビューを抑制する。
- **`data/ng/NG_WORD_private.txt`** は gitignore されており、読み込み時に `NG_WORD.txt` の上にマージされる。`/functions` の 🔄 NGワード再読込 で再起動なしに再読み込みできる。
- **NGワードの照合とマスク**（#282）: 登録語と本文は共通の正規化を使い、ゼロ幅文字・非空白の制御文字を除いてからNFKC・casefold・ひらがな統一を行う。タブ・改行などの空白と区切り記号は保持する。正規化後の各文字から元本文の区間への対応を持ち、一致区間を元へ戻して`●`へ置換するため、結合・展開・非表示文字があっても元の文字数と周囲の文面を変えない。登録語は正規化後に重複を除いて最初の元表記を残し、最長一致優先で正規表現を1回だけコンパイルする。再読込は語とパターンの準備完了後にスナップショットを一括差し替えし、失敗時は従来の判定を保持する。`find_ng_words`と検出ログは一致した原文表記を最初の出現順で返し、正規化後に同じ語でも原文表記が異なれば別の要素になる。`mask_with_log`はログとマスクを同じスナップショット・一致範囲から作る。
- **手書きのテキストは `utf-8-sig` で読む**: NGワード（`ng_word_service._load_words_from_file`）とペルソナ（`paths.load_prompt_template`）、`storage.load_json_file` が読む `projects.json`・`blacklist.json`、旧設定の移行元（`deploy/migrate_config_to_env.py` の `_extract_keys` が読む旧 `config_private.py`・`config.py`）は、メモ帳の「UTF-8 (BOM 付き)」保存でも先頭に U+FEFF を残さない。`utf-8` に戻さないこと — BOM は `strip()` で消えないため、1行目のコメントがNGワードとして登録され、1行目のNGワードは検知できなくなる（JSON は `json.loads` が BOM を拒否し、正しい中身ごと破損扱いで退避される。旧設定は文字列の `ast.parse` が構文エラーにし、キー未設定で移行が止まる。#221）。`find_ng_words` は `dict.fromkeys` で重複を除き、文中に最初に現れた順を保つ（`set` だと順序が実行ごとに変わる）。

## 作業完了時の手順（PR 作成とプライバシーチェック）

このリポジトリは **Public** で、PR・コミット・Issue に書いた内容はそのまま公開される（後から消しても、キャッシュやフォークに残りうる）。

**PR は、利用者が「作業完了を宣言します」と伝えた後にだけ作る。** エージェントが自分の判断で作業を終えたときは、トピックブランチへのコミットまでにとどめ、**push も PR 作成もしない**。エージェント自身は「作業完了を宣言します」という言い回しを使わないこと（利用者の合図と紛れるため）。ファイルを変更しないタスク（質問への回答・調査だけ）では PR は作らない。

宣言を受けた後の push と PR 作成は、その宣言が依頼にあたるので都度の承認は要らない（`main` へ直接 push しないこと）。

### 宣言を受ける前（作業中）

1. トピックブランチで作業し、コミットする。コミットの前にも下の項目でプライバシーチェックを行い、個人情報を最初から履歴に入れない。
2. 作業が終わったら、ブランチ名・コミット・確認した内容を報告し、「PR を出してよければ『作業完了を宣言します』と伝えてください」と添えて、**利用者の宣言を待つ**。

### 宣言を受けた後（この順番を守る）

1. **最新の状態を取り直す**（`git fetch`、`gh pr list` / `gh pr view`）。別のエージェントや利用者が、すでに PR をマージしていたり `main` を進めていたりすることがある。対象のブランチの PR がマージ済みなら、`main` から切り直したブランチへコミットを移す。
2. **プライバシーチェック**を行う（公開前の最終確認）。
   - 対象: このブランチの差分すべて（`git diff origin/main...HEAD`）、コミットメッセージ（`git log origin/main..HEAD`）、これから書く PR のタイトルと本文、PR に添付する画像。
   - 見つけた個人情報は**削除するか、プレースホルダーに置き換える**（下の表）。
3. 必要な修正をコミットする。まだ push していないコミットに個人情報が入っていた場合は、後から消すコミットを足すのではなく、push する前にそのコミット自体を書き直して（`git commit --amend` など）履歴に残さない。履歴の書き換えに承認が要る運用なら、書き直す前に利用者へ確認する。
4. トピックブランチを push し、`gh pr create` で PR を作る（既存の PR があれば、そのブランチへ push して本文を更新する）。
5. PR 本文に `## プライバシーチェック` 節を設け、確認した範囲と結果を書く（置き換えた項目の**種類と件数**だけ。**置き換える前の値そのものは書かない**）。
6. PR の URL・確認結果・プライバシーチェックの結果を報告する。

### 確認する項目と置き換え先

| 種類 | 紛れ込みやすい場所 | 置き換え先の例 |
|---|---|---|
| 氏名・住所・電話番号・メールアドレス・生年月日 | 動作確認用の文、コメント、PR 本文の例文 | `<NAME>` / `<ADDRESS>` / `<PHONE>` / `<EMAIL>` |
| 学籍番号・所属・車のナンバーなど個人を特定できる情報 | 同上 | `<STUDENT_ID>` などの種類名 |
| Discord のユーザー・サーバー・チャンネル・メッセージの ID（17〜20 桁の数字） | ログの貼り付け、テストデータ、`LEGACY_MEMORY_OWNER_ID` | `<DISCORD_USER_ID>` / `<GUILD_ID>` / `<CHANNEL_ID>` |
| API キー・トークン | `.env` の値、ログ、デバッグ出力 | `.env.example` と同じ `YOUR_GEMINI_API_KEY` / `YOUR_DISCORD_BOT_TOKEN` |
| ローカルの絶対パス（OS のユーザー名を含む） | `C:\Users\<名前>\...` / `/home/<名前>/...` | `<PROJECT_ROOT>` やリポジトリ基準の相対パス |
| サーバーの実パス・ユーザー名・IP アドレス・ホスト名 | `deploy/bot.service` の値 | `deploy/bot.service.example` と同じプレースホルダ |
| 利用者の会話・記憶・違反記録・キャラ別のプロンプト | `data/memory/`・`data/.migration-backups/`・`chatlog/`・`data/ng/blacklist.json`・`data/ng/NG_WORD_private.txt`・`prompts/`（テンプレート以外）由来の文 | 削除（例が必要なら架空の文に差し替える） |

機械的な確認の例（ヒットした行は目で確かめ、誤検出なら残してよい）:

```bash
P='[0-9]{17,20}|0[0-9]{1,4}-[0-9]{1,4}-[0-9]{4}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|[A-Za-z]:[\\/]Users[\\/]|/home/|AIza[0-9A-Za-z_-]{20,}'
git diff origin/main...HEAD | grep -nE "$P"            # 差分
git log origin/main..HEAD --format=%B | grep -nE "$P"  # コミットメッセージ
git diff --name-only origin/main...HEAD                 # .env / deploy/bot.service / data/memory/ などが混ざっていないか
```

★ Windows のパス区切りは `[\\/]` のように**角括弧の中で**書くこと。`C:\\Users\\` のようにバックスラッシュを重ねて書くと、Windows の Git Bash では `\\` が `\` に潰れて grep に渡り、`Trailing backslash` でパターン全体が壊れる（エラーに気づかないと、**何も検出されないまま「問題なし」に見える**）。パターンを変えたら、架空の電話番号・メールアドレス・ID・パス・キーを並べたテキストで全部ヒットすることを確かめる。

### 注意

- **gitignore 済みのファイル**（`.env`・`deploy/bot.service`・`data/memory/`・`data/.migration-backups/`・`chatlog/`・`data/ng/blacklist.json`・`data/ng/NG_WORD_private.txt`・`prompts/` のキャラ別ファイルと `personas.json` など）を `git add -f` で追加しないこと。
- 公開を意図して置かれている既存の情報（`SECURITY.md` の報告先など）は変更しない。公開してよいか判断できない値を見つけたら、PR を作る前に利用者へ確認する。
- `.env.example` のプレースホルダ文字列は変えないこと（変えるなら `settings_loader._PLACEHOLDER_VALUES` と `deploy/migrate_config_to_env.py` も対で直す。「規約と落とし穴」の設定の項を参照）。
- **既に push 済みのコミットに個人情報が見つかった場合**は、force-push で履歴を書き換えず、作業を止めて利用者に報告する（履歴の書き換えやキーの再発行など、利用者の判断が要るため）。
- Issue・PR へのコメントなど、リポジトリへ公開で書き込む文章にも同じチェックを適用する。

## バージョンとリリースの運用

機能追加のブランチ運用、README への `ver0.x.x(BETA)` の記録、Releases の登録と BETA の解除の方針は、Codex など他のエージェントと共有するため `AGENTS.md` に書いてある（次の行で読み込まれる）。方針を変えるときは `AGENTS.md` だけを直し、ここへ同じ内容を書き写さないこと。

@AGENTS.md
