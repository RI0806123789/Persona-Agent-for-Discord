# 回帰テスト

## 遠隔集計の読込境界・終了処理の防御（#396・#397）

`tools/console/backend/tests/test_remote_collection.py` は、遠隔スクリプトをimportしてもargvの解釈・設定読込・DB・外部通信・JSON出力を行わないこと、連続した収集で行・エラー・設定・期限が混ざらないこと、従来の標準入力CLIの引数とJSON出力を保つことを確認する。ページング・設定解決・表示名取得の既存テストも、ASTで関数を作り直さず通常のimportと呼出しで実行する。

`core/test_app_lifecycle.py` は所有者のコンテキストを継承しない直接の終了処理と、回収後の終了通知の完了確認を検証する。`core/test_background.py` はTaskを伴わない手動駆動、`core/test_chat_admission.py` は未確保枠の返却、`core/test_database.py` は接続の待機設定・別接続のロック解放後の書込・期限超過時のロールバックを検証する。外部サービスと本番DBは使用しない。

## テスト用Discordモックの共通化（#378）

`helpers/discord_mocks.py` が、`Interaction`・`Message`・`Channel`・`Guild` の架空オブジェクトを作る（`make_interaction`・`make_response`・`make_followup`・`make_message`・`make_channel`・`make_guild`）。送信・応答は呼ぶたびに新しい`AsyncMock`なので、テスト間で呼び出し履歴が混ざらない。

既存のテストには、`response` に `defer` しか持たせず「余計なメソッドを呼ぶと`AttributeError`で落ちる」ことを期待したものがあるため、既定の全部入りのほかに、`make_response("defer")` で持たせるメソッドを絞り、`without=ONLY_RESPONSE` で`followup`などを外せる。`user`は省略すると属性ごと持たない（`user_id`だけ渡せば`SimpleNamespace(id=...)`）。追加・上書きしたい属性は`**attributes`で渡す。`utils/test_discord_mocks.py` が既定値・厳密さ・独立性・上書きを確かめる。新しいテストはまずここを使い、移行は既存の挙動を確かめながら1ファイルずつ進める（`tests/views/` の各テストと `test_shared_helpers`・`test_welcome_message` などが使用。#400 で `views/` の残りも移した）。

## テスト用モックの横断適用と履歴ヘルパー・DB層の分離（#400）

`helpers/discord_mocks.py` に、チャンネルの履歴取得を模す `make_history(messages)` と、HTTP 応答を模す `make_http_response(status, reason)` を足した。`make_channel(history_messages=[...])` は渡した順にそのまま yield する `history` を持つ（呼ぶたびに新しい async generator で、`Mock` なので `channel.history.call_args.kwargs["after"]` のように呼び出しの引数も検証できる。`fetch_channel_history_text` は新しい順に取得するため、新しい順に並べて渡す）。Discord のように `after`・`limit`・`oldest_first` で絞る挙動が要るテストは、自前の generator を `make_channel(history=Mock(side_effect=...))` で渡す。`history_messages` と `history` は同時に指定できない。`utils/test_discord_mocks.py` が順序・再走査・空の履歴・併用の拒否を確かめる。

`views/` の各テストは、手書きの Interaction・response・followup と `discord.Forbidden(SimpleNamespace(status=...))` を、`make_interaction`・`make_response`・`make_http_response` へ移した。元の厳密さ（持たせるメソッドを `defer` だけに絞る、`followup` や `original_response` を持たせない）は `make_response("defer")`・`without=` で保つ。サービスの代役（`stats`・`blacklist` など）と、本文や `author=None` といった独自の形の message は、共通化しても得るものがないため手書きのまま残している。

`core/test_db_modules.py` は、`database.py` から分けた `db_migrations.py`（スキーマの定義と `apply_migrations`）と `db_legacy_importers.py`（旧 JSON・テキストの読み取りと解釈）を単体で確かめる。`SCHEMA_VERSION` が `MIGRATIONS` の最大のキーと一致し欠番がないこと、`database._MIGRATIONS` が定義側と同じ辞書であること、管理コンソールが ast と正規表現で読む `SCHEMA_VERSION = <整数>`・`_QUEUE_ACTIVE_STATUSES`・`DELETE FROM reminders WHERE status IN (...)` が `database.py` に残っていること、パーサーが sqlite3・threading・logging を import しないことを検査する。`Database` を通した移行の途中失敗・旧ファイルの取り込みと改名は、従来どおり `core/test_database.py` などが確かめる。

## メッセージ処理ステージの外部操作の契約（#395）

`core/test_message_operations.py` は、受付・振り分け・応答の各ステージがモジュール全体ではなく `MessageOperations`（6つの外部操作）だけを受け取ることを確認する。`message_handler` の差し替え口が実装へつながっていること、契約の Protocol と実装の引数（名前・種類・既定値の有無・必須の引数）が食い違わないこと、足りない・呼び出せない操作が構築時に名前付きの `TypeError` になること、構築後のモジュールの差し替えが次の呼出しに効くことを検証する。ステージのソースは静的に調べ、操作を直接 import していないこと、`ops.` の参照がすべて契約にあり契約の操作がすべて使われていること、関数内で使う名前がモジュールに定義されていること（`symtable`。import を整理したときの不足を、その経路を実行する前に見つける）を確かめる。`helpers/message_handler_helpers.make_message_handler` はサービスと外部操作だけを受け付け、定数・判定の関数など差し替えても効かない名前を拒否する。

## サーバー設定の権限・集計ボタン・検索の代替（#398）

`views/test_admin_panel_permissions.py` の `ServerSettingPermissionTests` は、🔍 Google検索の切り替えと 💬 自発会話の確定が、一般メンバーには保留前に断られ、押した後にロールを外された・最新の Member を取れない場合は保留後に断られ、どちらも設定を書き換えず保存もしないことを確かめる。現在の管理者は変更・保存でき、DM の設定は本人が操作できる。`AggregationRepeatPressTests` は、全体比較・フィードバックの集計の実行中にもう一度押すと案内だけを返して集計を重ねず、終了・キャンセルの後はまた押せることを確かめる。`services/test_gemini_service.py` は、画像と検索ツールの併用を断られた場合に検索なしで画像ごと答え、画像そのものの失敗では追加の生成をしないことを、`services/test_reminder_service.py` は、登録時の日時の解釈が夏時間の境目でも繰り返しの次回時刻と同じ規則になることを確かめる。

## 接続切替・音声会話終了の待機中の再開（#344・#345）

`tools/console/frontend/tests/connection.test.mjs` は実際の`DataPage`と`DbTableDetail`を読み込み、ReactフックとAPIを差し替え、接続切替時に子のeffectを親より先に実行する。新接続で選ぶ前の内容取得がないこと、閉じるアニメーション用の中身も引き継がないこと、選択後のページング・行展開・伏せ字を確認する。

`services/test_voice_conversation.py` は終了案内の送信をEventで待たせ、実際の`reserve()`・`start()`による新会話の登録と、枠だけの予約の両方で接続・保護登録を維持することを確認する。後続会話がない通常終了では案内と切断を維持する。実Discord・Gemini・SSHへの接続は行わない。

## 統計設定・リマインダーのDB障害表示（#340・#341）

`views/test_storage_failure_notices.py` は一時SQLite DB・実サービス・実Viewコールバックを使い、DBメソッドだけを失敗させる。統計の停止／再開それぞれについて、保存成功・失敗の案内と次の発言の集計件数が一致することを確認する。リマインダーは一覧・取消の障害時にパネルを残し、同じボタン／選択肢で再試行できること、正常な空一覧・対象なし・本人だけの取消を維持することを確認する。時間切れ時は古いパネルを再表示せず、本人限定の追加応答で失敗を伝える。実Discord・Geminiへの接続は行わない。

## Issue #283の段階的ログ移行

第1段階は起動処理・core・共通ログ設定と、管理コンソールの旧print／新logging両対応を対象にする。`utils/test_logging_setup.py` は時刻・レベル・モジュール、ハンドラの重複防止、識別子の伏字、例外本文・絶対パス・ソース行の非漏洩、元レコードの非変更、不正な `exc_info`（3要素でない・型でない・トレースバックでない）でも整形が失敗せず中身を出さないこと（#399）を検証する。`PYTHONIOENCODING` を外した別プロセスでcp932を再現し、絵文字をUTF-8で出力できることも確認する。既存のcore回帰はINFOを明示捕捉し、診断用の元例外保持と実際の出力の情報境界を別々に確認する。

第2段階の `services/test_service_logging.py` は、NG語・モデレーション理由・記憶の例外本文・日時入力・配信先情報の非漏洩と元例外の保持を確認する。第3段階の `views/test_view_logging.py` はSDKの通常/TaskOwner配下のコールバック、repr非参照・元例外保持、グラフ文脈やファイル名の非漏洩、キャンセル伝播を確認する。

第4段階はaudio・設定読込・共通処理を対象とする。`utils/test_runtime_logging_privacy.py` は会話・添付名・応答本文・文脈名を出さずに会話件数とトークン数を集計できること、設定不足時のキー名と対処案内が単独実行の既定レベルでも残ること、不正なタイムゾーンを原文なしでUTCへ代替することを確認する。既存の音声・設定・保存・履歴取得等のテストも、元例外の保持・情報の非漏洩と代替・再試行・キャンセルの動作を確認する。実Discord・journald・本番SSH・ブラウザ操作はこの自動確認に含めない。

仮想環境のPythonを使い、リポジトリルートで実行する。

```bash
python -B -m unittest discover -s tests -v
```

## フォルダ構成と個別実行

```text
tests/
├── config/       # 調整定数
├── core/         # DB・メッセージ処理
├── audio/        # 音声合成・PCM・読み上げキュー・音色・音声会話の受信と形式変換
├── services/     # 会話・記憶・自律行動などの機能
├── views/        # Discordのパネル
├── utils/        # 保存・JSON解析・グラフなどの共通処理
├── deploy/       # 運用・移行スクリプト
├── integration/  # 起動配線・SDK移行・非同期I/O
└── helpers/      # テスト間で共有する補助処理（Discordの架空のInteraction・Message・Channel・Guildは `discord_mocks.py`）
```

表内のテストパスは `tests/` からの相対パスで表す。一括実行のコマンドは従来どおり。各フォルダの `__init__.py` により、Python 3.10／3.13の `unittest discover` がサブフォルダも検出する。分類だけ、モジュールだけ、ファイルの直接実行も選べる。

```bash
python -B -m unittest discover -s tests/services -v
python -B -m unittest tests.core.test_bot_reply_settings -v
python -B tests/core/test_bot_reply_settings.py -v
```

外部サービスはモック、DB・JSONは一時ディレクトリを使う。実際の設定・会話・記憶は読み込まない。メッセージ処理は `MessageHandler` を直接呼び、`bot.py` の起動処理やAST抽出は使わない。時刻集計の日本時間は標準ライブラリの固定オフセットで再現し、タイムゾーンDB欠落時のUTC代用も別途検証する。

`utils/test_timeutil.py`は、別プロセスでOSのタイムゾーンDBを無効にし、依存の`tzdata`で`Asia/Tokyo`を解決できることを確認する。UTCから日本時間への9時間の変換と日付境界を検証し、WindowsだけでなくLinuxのCIでもOSのDBに依存した見逃しを防ぐ。

同じファイルは `utc_now`・`utc_now_iso`・`parse_iso_datetime`・`as_utc`・`format_weekday`・`resolve_timezone` の単体テストも持つ（#378）。現在時刻を固定して秒への切り捨てと保存用文字列の桁数を確かめ、末尾`Z`・オフセット・うるう日・年またぎ・曜日の日付境界、タイムゾーン未設定の旧値のUTC扱い、不正な文字列と未知のタイムゾーン名のフォールバック（名前をログへ出さない）を検証する。

Windows・Python 3.13.3・tzdata 2026.5で全706件が成功。全158 Pythonファイルの構文・Python 3.10文法・compileall、XML・メッセージ分割・`pip check`も成功した。Linux／Python 3.10での実行と実サービス接続は未確認。

以下はフォルダ整理時の検証記録。Windows・Python 3.13で、整理前後に検出したテストの一覧が全288件一致し、従来の一括実行で全件が省略なしで成功した。リポジトリルートからの検出と `-t .` 指定でも同じ一覧になることを確認した。上の個別実行例は、分類別169件・モジュール指定9件・ファイル直接実行9件がそれぞれ成功した。移動した28ファイルは、プロジェクトの探索パスと共通補助のimportを除いてASTが一致し、全88ソースの構文・Python 3.10文法、依存関係の整合性も確認した。今回の整理ではLinux・Python 3.10での実行は未確認。

## Issue #285の確認（依存の更新提案と脆弱性検査）

`deploy/test_dependency_audit_config.py` は、`.github/dependabot.yml` と `.github/workflows/dependency-audit.yml` が守る約束を確かめる。PyYAML はボットの依存に無いため、YAML は解析せず文字列として照合する。

| 確認内容 | 内容 |
| --- | --- |
| 更新提案の対象 | pip（ルート・管理コンソールのバックエンド・検査ツール）、npm（画面）、github-actions がそろい、各ディレクトリに依存の定義ファイルがあること |
| 公開直後の版を提案しない | すべての更新対象に `cooldown` の `default-days` が14日以上あり、github-actions に未対応の `semver-*-days` を書かないこと。毎週の実行 |
| フォークのPRでも安全に動く | `permissions: contents: read` だけで、`write`・`secrets.`・`pull_request_target` を使わず、checkout は認証情報を残さないこと |
| 検査の対象と実行条件 | ボットと管理コンソールの requirements の `pip-audit`、画面の `npm audit --omit=dev`、回帰テストと同じ OS・Python の組み合わせ、依存の定義ファイルの `paths` と週1回の定期実行 |
| 警告から始める | `AUDIT_REQUIRED: 'false'` の間は警告の注釈だけで、切り替えたときだけ失敗させること（必須にするときはテストの期待値も直す） |
| 除外の管理 | `pip-audit` の除外（`ignored_vulns`）が `EXPECTED_IGNORED_VULNS` と一致し、それぞれに理由と外す条件のコメントがあり、ジョブの概要に出ること。除外の前提の `requirements.txt` の行（`discord.py[voice]==2.7.1`）が変わったら失敗して見直しを促すこと |
| 検査ツールの固定 | `pip-audit` を `.github/audit/requirements.txt` に `==` で固定し、ボット・管理コンソールの requirements に入れないこと |
| actions の固定 | すべてのワークフローの `uses:` がコミットSHAで固定されていること |

推移依存の固定（`constraints.txt`）について、同じファイルの `ConstraintsTests`・`ConstraintsWorkflowTests` は、`requirements.txt` が `-c constraints.txt` を読むこと、`constraints.txt` に直接依存と pip・setuptools・wheel を書かず、複数行の依存には環境マーカーがあること、書き分けた依存が Dependabot の `ignore` にあること、推移依存をまとめる `groups` の除外が `requirements.txt` の直接依存（正規化した名前）と一致すること、作り直しのワークフローが手動実行だけ・読み取りだけで、入力を環境変数経由で渡し、Python 3.12 を含み、直接依存を `--find-links` に置くこと、生成に使う pip が 26.0.1 以上に固定されていること、公開日の上限の例外（`security-exceptions.txt`）が直接依存でなく、脆弱性の ID・理由・外す条件のコメントを持ち、`constraints.txt` がその版になっていること、作り直しが例外を取得して `-c` で渡すこと、Dependabot がそのフォルダで pip だけを更新することを確かめる（壊した写しの7通りでも確かめた）。`deploy/test_merge_constraints.py` は合成スクリプトを確かめる: 版が1つなら条件なし、Python の版の範囲（中間・飛び飛びを含む）・OS・両方・環境の列挙による書き分け、生成していない新しい Python には最も新しい行が当てはまること、直接依存（`discord.py[voice]` → `discord-py` の正規化を含む）と道具の除外、「名前==版」以外の行・不正なファイル名・同じ環境の重複を誤りにすること、LF の出力。環境マーカーは `packaging.markers`（matplotlib が引き込む）で評価し、どの環境でもちょうど1行が当てはまり pip freeze の版と一致することを確かめる。管理コンソールの `backend/tests/test_deploy.py` は、`constraints.txt` だけが変わったデプロイでも依存関係を入れ直すことを確かめる。

設定を壊した写し（cooldown の削除・7日への短縮・対象の削除・書き込み権限・タグでの固定・必須化・dev を含む検査・`paths` の欠落・`pip-audit` の本体への追加・版の未固定）の10通りで、それぞれ対応するテストが失敗することを確かめた。除外の管理は、未登録の ID の追加・一覧を空にする・理由／外す条件の説明の削除・`pip-audit` への渡し忘れ・概要への表示の削除・`discord.py` の版の更新の7通りで、テストが失敗することを確かめた。取り出した検査ステップを Windows・Python 3.13 の Git Bash で実際の requirements に対して実行し、PyNaCl の2件（GHSA と PYSEC の別名）が除外されて検出なしで終わることも確かめた。`constraints.txt` についても14通り（`-c` の削除・直接依存や pip の混入・マーカーなしの重複・`ignore` の欠落・`discord.py` と書いた除外・除外の欠落・push での実行・入力の直接埋め込み・3.12 の削除・直接依存への日付の上限・古い pip・書き込み権限・検査の `paths` からの欠落）で同様に確かめた。最初の `constraints.txt` は手元の Windows で作った（Python 3.11・3.13 は実際の解決、3.12 は 3.11 から `--python-version 3.12` で解決。この依存の組み合わせでは 3.11 と 3.13 の違いが audioop-lts（3.13 から）と contourpy の版だけで、3.12 を境にする環境マーカーが無いことを確かめたうえで使った）。`requirements.txt` と合わせた導入を、Windows の 3.11・3.13（実際の環境）と、Windows 3.12・Linux 3.11／3.12／3.13（`--platform` を付けた解決）で dry-run し、全件が wheel で固定版のまま入ることを確かめた。取り出した検査ステップを Windows・Python 3.13 の Git Bash で実行し、検出時の警告の注釈・ジョブの概要への出力・警告だけの終了と、検査ツールが無い場合の失敗も警告になることを確かめた。GitHub 上での Dependabot の提案と Actions での実行は未確認。

## Issue #268の確認（Gemini Live による音声会話）

| テスト | 確認内容 |
| --- | --- |
| `audio/test_voice_receive.py` | discord.py と同じ `aead_xchacha20_poly1305_rtpsize` で暗号化したパケット（RTP 拡張・CSRC あり／なし）を復号し、改ざん・RTCP・短いパケット・未対応の方式を捨てる。SPEAKING による話者の対応、DAVE の復号とパススルー、古い通番・無音フレームの破棄、停止後の非配送を確かめる。受信が依存する discord.py の入口が残っていることも検出する（★ discord.py の更新時に必ず回す） |
| `audio/test_live_audio.py` | 48kHz→16kHz の音量保持・8kHz 超の抑制・断片の境目と位相の一致、24kHz→48kHz の長さと端数の持ち越し、再生音源の無音待ち・打ち切り・流し切り・破棄を確かめる |
| `services/test_live_session.py` | 手動の発話区切り・文字起こし・再開・圧縮・ボイス名・検索の設定、再開ハンドルでの張り替え、待ち時間つきの再接続、利用枠超過で再接続しないこと、再接続の回数制限、再開失敗時の新しいセッション、設定拒否時の最小設定での再試行、`close()` 後の再接続なし、同意撤回時の文脈破棄を模擬の接続で確かめる |
| `services/test_voice_conversation.py`（#310 のレビュー対応） | 割り込みの直後に遅れて届いた文字起こしを次の話者へ付けず誰の違反にも数えないこと、`finished`・`interrupted`・返答の終わり・時間切れで帰属を次の話者へ渡すこと、3人目が話し始めたときの間の発言の扱い、VC を出た人の撤回でもゲートを開いたまま文脈を捨てること、同意パネルを送れない・送り先が無いときに開始を失敗にして接続と枠を残さないこと、終了待ちの間に始まった新しい会話を切断しないこと |
| `services/test_live_session.py`・`services/test_voice_conversation.py`（#311 のレビュー対応） | 再接続の途中で同意が撤回されたら、撤回前の再開ハンドルで開いた接続を採用せずに新しくつなぐこと、文脈の破棄後に古い接続の再開ハンドルを受け取らないこと、入力の判定の後に届いた続きを判定し直し（不適切なら違反を1回だけ記録）、判定していない本文を記憶しないこと、同意パネルの投稿を待つ間に Live が終わったら開始を失敗にして会話と枠を残さないこと |
| `core/test_join_live_voice.py`（#310 のレビュー対応） | 移された先で `/join` したとき、元のチャンネル用の会話を使い回さず始め直すこと、同意パネルの失敗で専用の案内を出して切断すること |
| `services/test_voice_conversation.py` | 全員の同意・ブラックリスト登録者の除外・途中参加・移動中の一時停止、話し始めのしきい値とプリロール・話し終わり・長さの上限、1人ずつの送信と発話希望の優先・期限切れ、割り込みと古い返答の破棄、返答が来ない場合の待ちの解除、NG ワード・AI 判定での停止と違反の1回だけの記録（本人だけへのメンション）、出力の違反を参加者に数えないこと、判定不能では止めず記憶もしないこと、同意者だけの記憶・親密度、Live の終了・接続の喪失、同時実行の枠を確かめる |
| `services/test_voice_moderation.py` | 安全・不適切・判定不能の3段階、空の文字起こしを送らないこと、プロンプトのブロック、感情の推定の相乗りと上限、出力例とスキーマの一致、既存の `check` の fail-open の維持、Live 用のシステム指示を確かめる |
| `views/test_voice_consent_view.py` | 同意パネルの本文（人数だけを出す）、押した本人だけの同意の切り替えと本人だけへの返答、ブラックリスト登録者の拒否、終了後の拒否、応答失敗時の同意の保持を確かめる |

## Issue #409の確認（ペルソナの発話中の割り込みとエコー除去）

| テスト | 確認内容 |
| --- | --- |
| `audio/test_echo_canceller.py` | 合成した声（遅延・40ms の反響・雑音つきの回り込み）で、参照信号の時間軸（連続の並べ方・長い途切れでの張り直し・古い音の消去）、GCC-PHAT の遅延推定（既知の遅延を見つけ、無関係な音では推定しない）、回り込みの除去（ERLE 10dB 超・回り込みを発話と誤るフレーム 5% 未満）、回り込みに重ねて話した声の検出、参照信号が無いときの素通し、パケットの欠落と話し始めの時間軸、20ms でないフレームの素通し、処理時間の目安を確かめる。実際の Discord（相手側のスピーカー・音声処理）での効き方は検証用サーバーで確かめる |
| `services/test_voice_barge_in.py` | 文字起こしの正規化（カタカナ・半角・伸ばし棒の重なり）、言いよどみ・相槌（繰り返し・組み合わせを含む）を語に数えないこと、語の頭の言いよどみの除去と語の一部の「あの」の保持、日本語・英語の語数の見積もり、ボット自身の発話の回り込みの照合（短い発言は判定しない）、古い発話の忘却を確かめる |
| `services/test_voice_conversation.py`（#409） | ペルソナの声が出ている間の短い相槌を送らず返答も止めないこと、2秒話し続けたら話し始めごと送り、確定までは前の返答と打ち切り通知までの残りを流し続けること、言いよどみでは確定せず語がそろったら強めにフェードアウトすること、未確定のまま来た返答で前の返答を差し替えること、回り込みへの返答（声が止まった直後の発言を含む）を再生しないこと、エコー除去が回り込みと判定したフレームで話し始めないこと、一時停止でフェード中の声も即座に止めること、生成待ちの間は従来どおりすぐ割り込むこと、終了時に件数と推定遅延だけをログへ出すことを確かめる |
| `audio/test_live_audio.py`・`audio/test_voice_receive.py`・`services/test_voice_moderation.py`（#409） | フェードアウトで音量が下がり続けて止まり残りを捨てること・2回目のフェードを無視すること、送り出したフレーム（生成待ちの無音を含む）を参照信号へ書き、書けなくても再生を止めないこと、受信がエコー除去の結果と発話の強さを渡し、除去に失敗しても受信を止めないこと、誤った割り込みのとき確認させるシステム指示を確かめる |
| `core/test_join_live_voice.py` | `/join` が受信に対応した接続で始めること、同時実行の上限で接続しないこと、開始失敗時の切断と案内、同じチャンネルでの再利用・再開始、素の接続の切り直し、`/leave` が切断の前に会話を止めることを確かめる |
| `core/test_bot_reply_settings.py`・`core/test_agent_delegation.py` | テキストの返信を読み上げないこと（配信コメントへの応答は従来どおり読み上げる） |

Windows・Python 3.13.3・固定依存の環境で全1392件が成功（スキップ1）。`pip check`・全ソースの構文確認・`compileall`・メッセージ分割も成功した。SDK が組み立てる Live の初期化メッセージと送信メッセージの形はオフラインで確認した。Discord・Gemini はモックで、実際の Discord での DAVE 受信、Gemini Live との会話・遅延（P50/P95）・利用量、Linux での実行は未確認。

## Issue #292の確認

| テスト | 確認内容 |
| --- | --- |
| `core/test_agent_delegation.py` | 読込済み画像を委任後の親の生成にも借用し、再取得・二重回収しない。子の実行中・報告のAI判定中の外部キャンセルでは、中止ボタンの除去完了後に実行枠を返す。繰り返しキャンセルと回収エラーでも元のキャンセルを維持する |
| `views/test_agent_view.py`・`views/test_agent_thread.py` | 開始途中と実行中のView停止・ボタン除去、中止応答のHTTPエラー、所有タスクとしての終了待機と終了開始後の受付拒否を確認する |
| `views/test_autonomous_channel_view.py` | 配信先→キャラ設定→機能ハブの順に戻り、ブラックリスト判定・メッセージ参照・戻る時点の権限を引き継ぐ。未確定の配信先を保存せず、所有者変更後の違反件数の再表示と確認待ち中の時間切れ後の再表示を防ぐ |
| コンソール `backend/tests/test_remote_scripts.py` | v15の配信先IDを精度を保つ文字列で取得し、指定なしと列の無い旧版を区別する。不正な保存値を指定なしへ解決し、DBを変更しない |
| コンソール `backend/tests/test_dev_launcher.py` | 最新mainで既に両プロセスを監視するランチャーを使い、画面配信の先行終了でバックエンドの標準入力を閉じ、正常終了待ちとブラウザ回収へ進むことを確認する |
| コンソール `frontend/tests/screen-privacy.test.mjs` | ステージ・ボイスの配信先表示、自動選択と旧版未対応の区別、表示と自由文のID伏せ字、伏せ字切替への追従を確認する |

Linux・Python 3.12.14でボット全1236件が成功（スキップなし、日本語フォント必須）。コンソールバックエンドは255件中254件が成功し、Windows専用1件は対象外。フロントエンド全146件とTypeScript検証・Viteビルドも成功した。両Python環境の`pip check`、205 Pythonファイルの構文・Python 3.11文法、メッセージ分割、差分の空白とプライバシーチェックも成功した。

追加したv15のDBテストでは、Windows CIで一時DBの削除時に`WinError 32`が発生した。`sqlite3.Connection`のトランザクション用コンテキストだけでは接続を閉じないため、外側を`contextlib.closing`で囲み、読取前のコミットとハンドル解放を両立する。GCを待たず実接続を保持した確認でも、テスト終了時に作成した2接続が閉じていることを検証する。

Discord・Geminiはモック、SSHはループバックの模擬サーバー、DBは一時ディレクトリと架空データを使う。実際のDiscordでのパネル遷移・中止操作・Webhook編集、本番SSH接続とWindowsを含むブラウザでの手動操作は未確認。

## Issue #289・#290・#291の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #289 | `core/test_voice_commands.py`・`services/test_voice_guard.py` | 通知失敗後に復活した操作は成功、追越し確定時だけ通知例外を伝える。実際に送らなかった通知は後で送れる。接続確立の時間切れでも実自律サービスで締め・所有ステージ枠・配信記録・切断を回収し、接続差し替えや保護情報再登録を優先する |
| #290 | `audio/test_voice.py`・`audio/test_voice_session.py` | URL直後の日本語を保持し、edge-ttsのハング時に個別期限からgTTSへ退避してPCM合成を完了する。外部キャンセルでは退避せず、失効した配信の待機分で満杯でも新しい配信と通常返信を受け付ける |
| #291 | `services/test_reminder_service.py`・`services/test_affinity_service.py`・`services/test_memory_service.py` | 時差あり・なしの極端日時の登録見送り、本文引用の保持と末尾タグの採用、生成待機中の記念日追加・訂正・削除の保持を確認する。未変更ラベルへのモデルの訂正、他ユーザーの記憶、既存の保存ロールバックとキャンセル回収も維持する |

## Issue #414の確認

動作を変えない構造整理のため、既存のテストを新しい配置へ追従させたうえで、分けた境界ごとの契約を追加した。

| 領域 | テスト | 確認内容 |
| --- | --- | --- |
| `/join`・`/leave` の分離 | `core/test_join_command.py`・`core/test_join_live_voice.py`・`core/test_mention_policy.py`・`services/test_autonomous_start_races.py`・`core/test_voice_commands.py` | ボイス接続のログの出元・差し替え先（`LiveVoiceClient`・`enable_stage_speaker`・`announce_voice_join`）は `persona_bot.core.voice_commands`。権限案内（`join_permission_notice`）は権限が足りる・ボットのMemberが取れない場合は案内せず、不足は一覧し、ステージでは「メンバーをミュート」を添える。コマンド登録側の失敗通知・所有者の終了待機・ブラックリスト判定は `core/test_command_*.py` が従来どおり確認する |
| 旧データ取り込みの分離 | `core/test_db_modules.py`・`core/test_database.py`・`core/test_profile_absorption.py`・`services/test_affinity_service.py` | `LegacyDataImporter` が `Database` の接続とロックを共有し、ロガー・退避先の連番の上限・オーナーIDを呼出しのたびに読む。モジュールが sqlite3・threading・logging・persona_bot を import しない契約は維持する |
| 定期ループの起動 | `core/test_app_lifecycle.py`・`integration/test_package_layout.py` | `BotState.start_background_once` は 1 回だけ起動し、失敗ではフラグを立てず、知らないフラグ名は `AttributeError`。実際の `bot.py` の起動（3 コマンドの登録）も従来どおり通る |
| ペルソナ設定の検証 | `config/test_personas.py` | 4 種類の「オブジェクトでない」警告が結果の説明を含み値を出さない。未対応の項目は複数あっても警告 1 回で読み飛ばし、項目名を出さない。パネルを開くたびの再読込は件数を記録せず、起動時の再読込は記録する |

実際の Discord・Gemini・本番サーバーでの動作は未確認。

Windows・Python 3.13.3と`requirements.txt`の固定依存12件が一致する環境で、全1208件が成功（失敗・エラー・スキップなし）。`pip check`、201 Pythonファイルの構文とPython 3.11文法、メッセージ分割、`git diff --check`も成功した。独立レビューと変更17ファイルのプライバシーチェックを実施した。

外部通信はモック、DBとJSONは一時ディレクトリと架空データを使う。Linux・Python 3.11での実行、実Discordでの参加・退出・外部移動とステージ枠、実TTSの通信・音声再生は未確認。

## Issue #281・#282の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #281 | `utils/test_history_safety.py` | 現在ブロック中の利用者・他ボットを除外し、自ボットの返答を保持する。本文と表示名の偽装、リセットポイント・文書の元メッセージ除外との併用、取得中のブロック変更、取得・除外失敗時の空履歴とキャンセル伝播を確認 |
| #281 | `services/test_prompt_boundaries.py` | 生の発言・質問・名前・記憶・文書要約・統計サンプル・子の報告などの見出し・ロール行を無害化する。全履歴ビルダーでコード側の正規ロールとセクションを維持し、通常文・改行・繰り返し適用の結果を保持する |
| #281 | `core/test_agent_delegation.py`・`services/test_auto_topic_service.py`・`services/test_autonomous_speech.py`・`services/test_document_service.py`・`views/test_summary_view.py`・`integration/test_package_layout.py` | 通常会話と委任判定、自発会話・記念日、自律行動、文書返信、要約パネルの各経路へ同じブロック判定を渡す。実際の起動配線でもサービスへ同じブラックリストの判定関数を注入する |
| #282 | `services/test_ng_word_service.py` | 登録語と本文のNFKC・casefold・かな統一、結合濁点・文字展開・ゼロ幅文字・非空白制御文字の検知と元位置へのマスクを検証する。空白・記号・数字置換を飛ばさず、登録語の正規化重複・元表記の出現順・最長一致・コンパイル回数・再読込時のスナップショットを維持する |

Windows・Python 3.13.3と`requirements.txt`の直接依存12件に一致する検証環境で、新規23件を含む全1174件が成功（失敗・エラー・スキップなし）。`pip check`、201 Pythonファイルの構文・Python 3.11文法・compileall、構成図XMLの4ページ、メッセージ分割、`git diff --check`も成功した。独立した相互レビューと差分22ファイルのプライバシーチェックを実施した。

外部通信はモック、NGワードは一時ファイルの架空の語だけを使い、実際の`ng/`・非公開NGワード・会話・記憶は読み込まない。プロンプトの構造は文字列で検証した。Linux・Python 3.11での実行、実Discordでの履歴取得・通知、実際のAIに対する攻撃文の判定耐性は未確認。

## Issue #278・#279・#280の確認

- `core/test_mention_policy.py` は、直接返信・チャンネル名を含む公開案内・クライアントの既定で本文中のメンションを制限し、返信先本人への通知と個別の通知先指定を維持することを検証する。`core/test_message_gates.py` はリマインダー登録確認のNGマスクと本人への返信通知、`core/test_join_command.py` はチャンネル名を含む権限不足案内の通知制限を確認する。
- `utils/test_bot_owner.py` と `views/test_blacklist_privacy.py` は、個人・チームの所有者、所有者の変更、取得失敗・キャンセルを検証する。2つのサーバーの管理者には全体の一覧・登録人数を公開せず、旧記録のブロックを維持する。ボット所有者はサーバー管理者でなくても一覧・解除を利用できる。既存の解除テストもサーバー管理者ではなく所有者の再確認に合わせる。
- `services/test_content_moderator.py` は統合判定の入力・履歴の区画分離、JSONスキーマと全出力例の整合、抽出した通知本文の文字数・改行・メンションの正規化、不正な日時形式の破棄を検証する。`services/test_agent_plan.py` は委任各項目の同じ正規化と、通常の判定に委任指示を足さないことを確認する。`integration/test_sdk_migration.py` は固定版SDKが送るHTTP本文でシステム指示・入力・スキーマを確認する。`safe is True` 以外ではリマインダーや委任を返さず、明示的な不適切判定は拒否、例外・空応答・不正JSON・解釈不能な安全フラグでは会話を通す契約も維持する。

Windows・Python 3.13.3と`requirements.txt`の直接依存12件に一致する専用環境で、新規20件を含む全1151件が成功（失敗・エラー・スキップなし）。`pip check`、199 Pythonファイルの構文・Python 3.11文法・compileall、構成図XMLの4ページ、メッセージ分割、`git diff --check`も成功した。独立レビューと差分28ファイルのプライバシーチェックを実施した。

Discord・Geminiはモック、違反記録は一時JSONと架空の利用者を使う。Linux・Python 3.11での実行、実際の通知・所有者情報取得・AIによる攻撃文の判定は未確認。

## Issue #267の確認

`views/test_agent_thread.py` は、サブエージェントの作業スレッドを検証する。子の表示名が Discord の Webhook 名の制約（`discord` / `clyde` の除去、`@ # : `` ` `` の全角化、80文字、`everyone` / `here`）に合うこと、スレッド名・委託内容の組み立てと文字数の上限を確認する。DM・スレッドの中・ボイスのチャット・メンバー情報なし・各権限の不足・`AGENT_THREAD_ENABLED = False` ではスレッドを使わない。ボット自身が作った同名の Webhook だけを使い回し、無ければ同時の依頼でも1つだけ作り、失敗は次回に再試行する。親が委託内容を中止ボタン付きで投稿し、子が名前・アイコンを変えて開始・手順ごと（間引かない）・報告（分割・参考リンク・埋め込み抑止）を通知なしで投稿し、最後に親が終了を伝える。失敗・中止は元のチャンネルにも返信し、委託内容の中止ボタンで子の作業が止まる。Webhook が消えたら忘れて作業を続け、投稿の失敗でも止めない。Webhook・スレッド作成・委託内容の投稿の失敗と権限不足では返信1通の表示へ切り替え（Webhook を用意できなければスレッドを作らない）、開始中のキャンセルでボタンを止める。

`core/test_agent_delegation.py` は、作業スレッドを使えるときに依頼内容と報告へ NG マスクを通してスレッドへ出し、人格役には元の報告を渡して元のチャンネルへ返信すること、子の失敗はスレッドとチャンネルの両方へ伝えて報告を出さないことを確認する。`config/test_personas.py` は `agent.avatar_url` を文面と分けて読み、`https://` の1行以外は URL を出さずに警告して既定のアイコンにすることを確認する。既存のテスト（メンバー情報の無い偽のサーバー）は返信1通の表示のまま通る。

## Issue #408の確認

`views/test_agent_thread.py` は、作業スレッドを自動アーカイブの時間を指定せずに作ること、`AgentWorkThreads` がボットの閉じたスレッドへの最初の書き込みだけを再開として1回返し、上限を超えたら古いものから忘れることを確認する。`archive_work_thread` はロックせずにアーカイブし、Forbidden / NotFound / それ以外の失敗では例外の本文を載せない定型の理由を依頼したチャンネル（依頼がスレッドの中なら親チャンネル）へ通知なしで返信し、送れなくても例外にしない。

`core/test_agent_delegation.py` は、人格役が要約を元のチャンネルへ返信した後にだけ作業スレッドを閉じること、失敗・子の報告の拒否・人格役の生成失敗・返信の送信失敗では閉じないこと、閉じられない理由を元のチャンネルへ伝えることを確認する。閉じたスレッドへのメンション無しの書き込みには Gemini を呼ばずに追加の依頼を1回だけ尋ね、「サブエージェント」と明記したボット宛ての依頼は尋ねずに委任して完了後に閉じ直し、閉じていないスレッドや NG ワードを含む書き込みには尋ねないことを確認する。

追加28件を含む全1131件が Windows・Python 3.13.3 で成功（スキップなし）。Discord・Gemini はモックに置き換えており、実際のスレッド作成・Webhook の投稿（名前・アイコンの表示、スレッド内の中止ボタンの操作）は検証用サーバーで未確認。

## Issue #262の確認

`core/test_join_command.py`は実際のDiscord SDKのVoiceClient登録・解除を使い、通信だけを差し替える。接続時の権限拒否・タイムアウト・状態不整合・一般例外と切断失敗でも登録を回収して再参加でき、生成前の競合、接続の差し替え、正常接続、切断待機中の差し替えを保持する。繰り返しキャンセルでも後片付けの終了を待つ。エラー文は固定案内で、詳細は運用ログに残す。

`core/test_bot_reply_settings.py`は設定ファイル欠落時の内部名の非表示、モデレーション・送信待ち・Forbidden・通信失敗・キャンセル時に親密度を変えないことを確認する。送信成功後だけ更新・保存し、更新自体に失敗しても評価・記憶・読み上げを続ける。

追加10件を含む全1097件がLinux・Python 3.12.14で成功（スキップなし、日本語フォント必須）。実Discord・Gemini・ボイス接続での操作は未確認。

PR #274を取り込んだ競合解消後も、ボット全1103件とコンソールバックエンド250件が成功（Windows専用1件はLinuxで対象外）。両方の更新履歴・テスト記録を保持し、194ファイルの構文・Python 3.11文法、compileall、依存整合性、構成図4ページとメッセージ分割も再確認した。

## Issue #259・#260・#261の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #259 | コンソール `backend/tests/test_remote_scripts.py` | 旧・現行配置でモデル・応答モード・間隔の選択肢と型を検証し、無効値・boolを既定値へ解決する。DBは変更しない。初回ログ取得の権限・実行・タイムアウト失敗は1回で止め、成功時だけ古い会話を検索する。同じボイスエラーを1件へまとめ、異なるエラーと各サーバーの状態は残す |
| #260 | `core/test_database_json_values.py` | 旧JSONのopt_outとstats_opt_out、両方指定時の優先順、全9列の日本語を含む値、通常保存・全件置換・再起動・往復保存の一致を確認。既定値の切替、既存行の保持、失敗時の全行ロールバックと元ファイル保持も検証 |
| #261 | `audio/test_voice.py`・`audio/test_voice_session.py`・`audio/test_voice_playback_lifecycle.py` | 再生中の連続投入でも待機順と上限超過時の新規依頼の見送りを維持し、空になった後にワーカーを再起動する。旧接続・旧チャンネルの破棄、同一接続の保持、音声プロファイルとキャンセル・再生スレッドの回収を既存テストで確認 |

Linux・Python 3.12.14でボット全1093件とコンソールバックエンド全250件が成功（後者のWindows専用1件は対象外）。関連93件・遠隔スクリプト46件、両環境の`pip check`、194ファイルの構文・Python 3.11文法・compileall、構成図4ページとメッセージ分割も成功。外部通信はモックまたはループバックの模擬SSHに限定し、実Discord・TTS・遠隔サーバー・ブラウザでの手動操作は未確認。

## Issue #258の確認

`services/test_voice_guard.py` は、閲覧権限を失ったボイス／ステージへ戻ろうとせず退出すること、ステージの発言者復帰に権限・API・ボイス状態取得のエラーで失敗したら保護を解いて退出することを確認する。昇格待機中の接続差し替え・別チャンネルの採用・保護解除・同じチャンネルでの保護再登録は優先し、古い保護処理で切断しない。昇格成功・キャンセル伝播・退出失敗のログも検証する。

Linux・Python 3.12.14で追加5件を含む関連42件と全1063件が、失敗・エラー・スキップなしで成功した。Discordの接続とイベントはモックに置き換え、実際の管理者によるドラッグ移動やステージ復帰は未確認。

## Issue #256・#257の確認

- `services/test_autonomous_comments.py`: 受付前・入力判定中・画像確認中に配信が終わった場合の通常会話への移行を検証する。委任・通常の応答・監視登録、本文なしのPDF/TXT受付・画像失敗と未対応添付の案内、読込結果の再利用・画像回収、通常枠の混雑、メンションなしの拒否と配信中の副作用抑止を確認する。
- `services/test_auto_topic_service.py`: ペルソナ・履歴・生成・モデレーションの待機中にボット自身の閲覧・送信権限を失う場合を検証する。スレッド固有の送信権限と対象者の閲覧権限も分離し、生成前はAPI・処理済み記録を残さず再試行、生成後は送信せず再生成を抑えることを確認する。記念日は次回の別チャンネル選択も検証する。

Linux・Python 3.12.14で追加10件を含む関連79件と全1058件が、失敗・エラー・スキップなしで成功した。外部通信はモック、データは架空のものを使い、実Discord・Gemini・ボイス接続での動作は未確認。

## Issue #255の確認

`views/test_admin_panel_permissions.py` は、統計の項目・対象切替で権限を失った利用者を本人の表示へ戻し、他人の名前・発言回数の候補と管理者ボタンを再構築しないことを確認する。ハブへ戻る操作での権限の付与・剥奪とDMも検証する。ブロック解除は#279で所有者本人に限定し、応答待機中の所有者変更・情報取得失敗・キャンセルと正常な解除を検証する。

PR #269で修正済みの#253・#254は、`services/test_stats_service.py` と `core/test_guild_rejoin.py` の既存52件を再実行して成功した。#255の追加9件を含む全1048件もLinux・Python 3.12.14で失敗・エラー・スキップなしで成功。実Discord・Gemini・ボイス接続での動作は未確認。

## Issue #252・#253・#254の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #252 | `services/test_reminder_cancellation.py` | チャンネル取得・スレッド再開・別通知の待機中に本人が取り消した系列は未送信通知と次回予告を送らない。送信開始後の取消は今回の終了を待ち次回以降を止め、別系列とDB読込は待たせない。日次・週次・月次・単発、本人以外の取消拒否、二重クレーム防止、メンション制限、状態読込失敗とロック回収を確認 |
| #253 | `services/test_stats_service.py` | 発言・収集停止/再開・削除・解析の保存失敗と繰り返しキャンセルで、元の集計・サンプル・解析・設定・世代を保持する。後続の発言による履歴消失を防ぎ、保存中の候補は非公開とする。一時DBの既存行・新規行の中断補償と、復元完了までのロック保持、復元失敗ログを確認 |
| #254 | `core/test_guild_rejoin.py` | 実際の退出処理から再生成・1項目保存までで全保存設定を保持し、復元した記念日記録で同日再投稿を防ぐ。DM・他サーバー・新規・記念日記録だけのサーバーと実行時状態の分離、各テーブルの読込失敗後の再試行、起動時一括復元の追加読込省略を確認。初回復元中のイベントループ進行と同時取得の共有、繰り返しキャンセル後の再試行、退出による古い読込と待機者の無効化、機能ハブの先行deferと元応答編集、DB未接続時の同期観測も確認 |

すべて一時DBと架空の予定・統計・設定を使い、Discord・Geminiはモックに置き換える。統計の中断補償そのもののI/O障害やプロセス強制終了に対する完全な復元は保証せず、補償失敗は運用ログへ残す。実Discordでの通知・再招待とLinuxでの実行は未確認。

Windows・Python 3.13.3と`requirements.txt`に一致する固定依存で、新規27件を含む全1039件が失敗・エラー・スキップなしで成功した。`pip check`、190 Pythonファイルの構文・Python 3.11文法・`compileall`、構成図XML、メッセージ分割、`git diff --check`も成功。独立レビューで見つかった初回復元時の同期DB待機は非同期化し、イベントループ進行・退出と再参加の競合を回帰テストで確認した。Python 3.11での実行、Linux・実Discord・Geminiでの確認は含まない。

## Issue #249・#250・#251の確認

- `core/test_profile_key_index.py`: 新規DBとスキーマ13から14への移行・再初期化、10万件の通常プロフィールと10件の記念日での索引検索、記念日なし・複数利用者・不正JSON、ユーザー別主キーの検索計画、保存・全置換・圧縮と失敗時のロールバックを確認する。既存の`services/test_memory_service.py`と`services/test_auto_topic_service.py`でも記憶圧縮・記念日巡回の回帰を確認する。
- コンソールの`backend/tests/test_service_completion.py`ほか: 応答期限後の遠隔処理、SSH終了後のsystemdジョブ待ち、確認不能・通信断・終了通知なし・繰り返しキャンセルでも後続のサービス操作・デプロイを競合させないこと、状態不明と読み取り・再接続の契約を確認する。
- コンソールの`backend/tests/test_insights_pagination.py`と画面テスト: 0・201・400・401件の一覧と保存設定、後続ページ失敗・429・上限・進捗停止・重複ID・数値精度、全体の取得予算・ボイス照会上限と部分取得の表示を確認する。外部接続先・トークン・本番DBは使わない。

#249の参考測定はWindows・Python 3.13.3の一時DBへ架空100010行を投入し、索引あり／なし各5回の中央値で比較した。キー検索は18.539msから0.055ms、一括投入は603.339msから669.295ms、DBのページ容量は7749632バイトから10555392バイトだった。索引は容量と書込コストを増やすため、速度倍率や本番での所要時間は保証しない。検索計画が`SCAN profile`から`SEARCH profile USING INDEX idx_profile_key_user (key=?)`へ変わり、返却内容は一致することを回帰テストで確認する。実systemd・多数サーバーに参加した実ボットとLinuxでの動作確認は含まない。

Windows・Python 3.13.3と指定の固定依存で、ボット1012件・コンソールバックエンド246件・フロントエンド138件（合計1396件）が失敗・エラー・スキップなしで成功した。フロントエンドのビルド、両Python環境の`pip check`、188 Pythonファイルの構文・Python 3.11文法・`compileall`、構成図XML、メッセージ分割、`git diff --check`も成功。独立レビューで見つかった想定外例外時の状態不明保持漏れは修正し、例外注入と回帰で再確認した。実SSH・systemd・Discord・ブラウザ操作とLinuxでの実行は未確認。

## Issue #213・#214の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #213 | `views/test_agent_view.py`・`core/test_agent_delegation.py` | 中止ボタン・全体の期限・親のキャンセルで、子の`finally`を止めている間は`run`と実行枠が返らず、回収後に枠を返す。追加のキャンセル、正常報告・`None`・子の例外、中止待機タスクの回収と回収中のキャンセル・例外、中止後に報告を返した子の扱い、期限切れ回収中の中止を確認。中止・失敗時は人格役の生成と読み上げへ進まない |
| #214 | `views/test_stats_entry.py`・`services/test_stats_service.py`・`views/test_panel_concurrency.py`・`views/test_database_interactions.py` | 実`StatsService`・実`StatsButton`・実`StatsView`で、唯一のデータ所有者や全員が収集停止中でも、本人は再開せず保存統計と停止注記を見られ、データが空なら既存案内を出す。管理者の候補・ランキング・他人向け表示は停止者と停止状態を隠す。遷移時のblocked interaction・管理者判定・エフェメラル・パネル世代も維持（旧「入口で拒否する」期待は除去） |

実Gemini・実Discordでの表示は未確認。

## Issue #210〜#212の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #210 | `audio/test_voice.py`・`audio/test_voice_session.py`・`audio/test_voice_playback_lifecycle.py` | 終了済みの実`AudioPlayer`と混雑した実executor、実ワーカーで、前スレッドの終了待ち中の移動・接続の差し替え・配信終了・切断では再生せず、変化がなければ再生する。無効な依頼は現在の音声を止めず、繰り返しのキャンセルでもスレッドとPCMを回収する |
| #211 | `services/test_autonomous_session_cleanup.py`・`services/test_autonomous_permissions.py` | 実サービスと登録済みコマンドで、配信枠の削除・切断それぞれの待機中の二重停止・同じチャンネルへの`/join`・`/leave`・繰り返しのキャンセル・別の接続への差し替えを確認。終了案内中の共有待機、自己待機の回避、回収の例外・中断後の停止記録の解放も確認（権限テストは回収タスクだけを実行するようモックを調整） |
| #212 | `services/test_voice_guard.py` | 旧移動の失敗中に同じ接続への`adopt`・別の接続への差し替え・同じチャンネルIDでの保護情報の再登録が起きても、新しい接続と保護情報を残す。例外なしの時間切れも同様。変化がなければ従来どおり退出する |

実Discordでの移動・ステージ操作と実音声での確認は未実施。

## Issue #170の確認

| テスト | 確認内容 |
| --- | --- |
| `services/test_agent_plan.py` | 「サブエージェント」の明記（表記ゆれ・英語を含む）がある発言だけが委任の判定の対象になり、`AGENT_ENABLED = False` では対象にならない。ルーターの委任内容を検証し、読めない値・空の依頼は判定なし、`delegate=false` は指示なし、長さ・手順の数を上限で切る。委任の判定が `check_and_route` と同じ1回の呼び出しに入り、直近の会話と `"agent"` 付きの出力例がプロンプトに含まれること、明記の無い発言のプロンプトは変わらないこと、不適切・読めない判定・通信障害からは委任を作らないこと |
| `services/test_agent_service.py` | 子が手順の数だけ検索付き・選択モデル・期限つきで動き、前の手順のメモを引き継いで最後の手順が報告になる。感情タグの除去、出典の重複除去と上限、失敗した手順で残りを呼ばないこと、トークンの記録。同じ利用者の依頼は1件ずつ・全体の上限・返却後の再取得。定型文の差し込みと、壊れた文面の既定への置き換え |
| `views/test_agent_view.py` | 進捗メッセージが定型文で通知を飛ばさず中止ボタン付きで出ること、短い間隔の書き換えを見送り、終了時はボタンを外すこと。進捗を出せなくても失敗を伝えること。報告・失敗・例外・中止ボタン・全体の期限・呼び出し側のキャンセルで子のタスクを止めること。中止ボタンは依頼した本人だけが押せること |
| `core/test_agent_delegation.py` | 明記のある発言は委任の判定つきのルーターだけを呼び、子の後に人格役がペルソナで報告を言い直して返信し、検索せずに子の出典を添えて読み上げること。明記の無い発言・不要と判断した依頼・判定なしは普段どおり答え、不要と判断したときだけその旨を添えること。子の失敗では定型文で伝えて人格役を呼ばず、実行中の利用者には定型文だけを返すこと。不適切な依頼と配信のコメントは委任しないこと |
| `config/test_personas.py` | `personas.json` の `agent` で書いた文面だけが反映され、残りと一覧に無いペルソナは既定の文面になること。差し込みの誤り・型の誤り・未知の項目は警告（文面は出さない）して既定に戻すこと。見本（`00_prompt_format.json`）の `agent` が警告なしで反映されること |

## Issue #189の確認

| テスト | 確認内容 |
| --- | --- |
| `config/test_personas.py` | `prompts/` の`.txt`から選択肢を作り、テンプレート・隠しファイル・フォルダ・長すぎる名前を除く。`personas.json`の並び順・表示名・声・既定、形式の誤った声の既定値への置き換え、壊れたJSON・BOM付き・型の誤りでの続行（ファイルは書き換えず中身をログに出さない）。テンプレートだけ・フォルダ無しで組み込みの標準になること、確定時に`prompts/`の外やテンプレートを拒否すること。キャラ設定はペルソナ0件でセレクトを出さず作り方（両方の見本）を案内し、25件を超える分は出さない。開くときに応答保留の後で一覧を読み直し、見つからない保存値は残したまま他の設定を確定できる。JSONの見本（`00_prompt_format.json`）を`personas.json`としてコピーすると警告なしで表示名・声・既定が反映され、見本そのものは設定として読まないこと。Gitの管理対象がテンプレート2つだけで、新しいファイルと`personas.json`が除外されること |
| `core/test_guild_settings_persist.py` | 見つからないファイルの保存値を残して既定で応答し、ファイルが戻れば元のペルソナで応答する。`prompts/`の外・テンプレート・区切り文字入りの保存値は既定へ戻す。未選択は一覧の既定に従う |
| `audio/test_voice_dsp.py`・`views/test_panel_confirmations.py`・`views/test_remaining_io.py`・`core/test_database.py` | 声を一覧から引き、一覧に無い値・組み込みは既定の声。確定時のファイル確認をワーカーで行い、設定の保存と往復を架空のペルソナで維持 |
| `integration/test_package_layout.py` | ペルソナの保存値（`../prompts/<ファイル名>`）と`prompts/`・`personas.json`の位置が変わらず、公開テンプレート（`.txt`・`.json`）が実在する。`prompts/`の無い環境でも起動できる |
| コンソール`tests/test_remote_scripts.py` | `personas.json`の表示名と既定を旧版の`PERSONA_CHOICES`より優先し、読めない場合はファイル名で表示して案内を出す |

応答設定の取り違えを確かめる既存テストは、実際の`prompts/`を読まずに`helpers/persona_helpers.py`の架空の一覧へ差し替える。利用者のキャラ別ファイルが手元にあると通り、テンプレートだけのCIでは落ちる、という差を作らないため。

Windows・Python 3.13.3（`.venv`、google-genai 2.31.0）でボット738件・コンソールバックエンド175件が成功。Git管理対象と未追跡ファイルだけを別フォルダへ写した新規取得相当の環境（`prompts/`はテンプレートだけ、`.env`・DBなし）でもボット738件が成功し、外部通信を止めた起動で作り方の案内が出て組み込みの標準プロンプトで応答することを確認した。`pip check`・全ソースの構文・`compileall`・構成図XML・メッセージ分割・`git diff --check`も成功。手元のキャラ別ファイル13件（テンプレートを含む）は追跡を外した後もSHA-256が一致した。Linux／Python 3.10での実行、実際のDiscord・Gemini・音声での確認、サーバーへのSCP転送と移行手順の実施は未確認。

## Issue #160・#161・#162の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #160 | `core/test_database.py` | 途中まで進んだ複数段のDB移行が失敗しても、DDLと`user_version`を元の版へまとめて戻し、再起動時に再移行できる。空・空白・未設定のユーザーIDを一時ログへ保存しない |
| #160 | `services/test_memory_service.py` | 各ユーザーの最後の保存済みログでアイドル時間を判定し、他人の発言や再起動で待ちを延ばさない。ユーザー固有の読込・保存失敗でも後続へ進む既存の保護と、全体障害時の上限を再確認 |
| #160 | `services/test_document_lifecycle.py`・`services/test_document_service.py`・`services/test_document_shutdown.py` | 返信待機中に繰り返しキャンセルしても送信と完了記録を待つ。送信開始の記録をDBへ残し、再起動で自動再送しない。完了記録・会話ログ保存の失敗でも二重返信しない。後続添付の失敗時に先行要約だけを残さず、ファイルとアップロードを回収 |
| #160 | `services/test_memory_service.py` | 添付群の一括保存・同名同内容の再試行・保存失敗を検証し、他ユーザーの要約と従来の要約を保持 |
| #161 | `services/test_autonomous_speech.py`・`services/test_autonomous_session_cleanup.py` | 第一声・独り言の送信拒否で元の配信を停止し、交代した配信は維持。キャンセルは所有枠・接続の回収後に伝え、一括終了は他サーバーも回収 |
| #161 | `services/test_autonomous_metrics.py`・`audio/test_voice.py` | UTC・時差付き・時差なし日時で同じゲージと活動窓・深夜判定。未完了コードブロックを読み上げず、旧キューワーカーのキャンセルでも新しいキューは保持 |
| #161 | `services/test_auto_topic_service.py`・`services/test_reminder_service.py` | 記念日は本人が閲覧できるチャンネルを選び、生成中の権限剥奪にも対応。アーカイブ済みスレッドは同じ場所を再開して通知し、ロックと権限不足・再開拒否では親へ転送しない |
| #162 | 画面`tests/log-stream.test.mjs` | 自動再接続をしない既存動作を再確認。JSON・ログ項目・終了理由の型不正でエラーにし、既読ログを残す。手動再接続と旧ソケットのイベント除外も維持 |
| #162 | 画面`tests/connection.test.mjs`・`tests/serviceActions.test.mjs` | 接続先変更・同一接続先への再接続・切断で確認を閉じ、古い確認から要求を送らない。ブランチと操作先IDの伏せ字を表示・自由文・通知で検証し、伏せ字OFFも維持 |
| #162 | コンソール`tests/test_remote_scripts.py` | 日別評価は複合索引の期間検索を使い、旧空白形式・ISO形式の日本時間境界、未来日の除外、旧スキーマを維持 |
| #162 | `utils/test_shared_helpers.py` | 実際の`discord.File`が借用するバッファを使い、送信側がFileを閉じても、成功・失敗・キャンセルとも実バッファのcloseは1回だけ |
| #215 | 画面`tests/log-privacy.test.mjs`・`tests/log-stream.test.mjs`・コンソール`backend/tests/test_deploy_log_privacy.py` | 受信時の公開区分を、バッファ・一時停止・保持上限・クリア後も保つ。取得開始と会話見出し以降は、区切り線・計数・運用ログ風の本文・別PIDでも隠し、`pid=1`かつ`source=systemd`だけ既知の秘密を置換して表示。概要は全保持行を判定してから切り出す。デプロイのログ混在出力・属性なしの旧形式は全体を隠し、偽と明示した出力だけ置換、伏せ字OFFは原文 |
| #216 | 画面`tests/screen-privacy.test.mjs` | 接続先ラベル・現在/配備先ブランチ・デプロイの見出し/説明/確認ラベルを、表示とtitle/ariaの両方で描画時に隠し、OFFで原文へ戻る。確認ダイアログは開いた後の伏せ字の切り替えに追従し、確認入力・操作対象・接続変更時の取消・古いコールバックの拒否を維持 |
| #217 | 画面`tests/toast-privacy.test.mjs`・`tests/connection.test.mjs` | 通知を原文で保持し、実描画で title・detail・action ラベルを伏せる。表示済み通知が伏せ字のOFF/ONと後から増えた秘密値、hover・focus 中にも追従し、消去・上限・再試行・タイマーは維持 |
| #218 | コンソール`backend/tests/test_ssh_error_privacy.py`・画面`tests/ssh-error-privacy.test.mjs` | 欠損した鍵・明示/既定の known_hosts のエラーに通常の診断文と別の安全な文面を持たせ、API・接続状態・履歴・通知へ通す。伏せ字中はローカルの home・所有者・任意パスを隠して対処方法を残し、通常表示は従来のまま。再接続・切断で現在の安全文を消し履歴は保持 |
| #219 | 画面`tests/operational-log-privacy.test.mjs` | 配信開始・接続失敗・ボイス移動検知・復帰の運用ログから、現在の接続・insights が無くても過去のチャンネル名と guild/channel・移動前後の識別子を隠す。ログ本文・検索・概要・デプロイ出力・ツールチップ、複数行・複数対象・重なる照合範囲・補助面文字・伏せ字OFFの原文を確認し、現行のボットのログ書式の読み取りで対応表のずれも検出。#215 の systemd 以外の本文秘匿を緩めないことも確認 |

DB移行の版をトランザクション外へ移す案は、現行の原子的なロールバックを壊すため採用しない。旧音声ワーカーの参照照合、ログの自動再接続停止、Fileとバッファの順序は既に実装済みのため維持する。配信停止の`CancelledError`も握りつぶさず、後片付け後に呼出元へ伝える契約を維持する。

文書のDiscord送信とSQLite更新は同時に確定できない。通常のキャンセルは完了まで待つが、強制終了で`sending`が残った場合は結果不明として`failed`にし、自動再送しない。送信前に強制終了した場合は未返信の可能性があるため、履歴で確認してから再依頼する。

Windows・Python 3.13.3の固定済み依存でボット780件・管理コンソールバックエンド176件、Node.js 24.16.0で画面57件とTypeScript・Viteビルドが成功。両仮想環境の`pip check`、全166 Pythonファイルの構文・Python 3.10文法・`compileall`、構成図XML・メッセージ分割・`git diff --check`も成功した。実際のDiscord・Gemini・音声・SSH接続、ブラウザ操作、Linux／Python 3.10での実行は未確認。

## Issue #156・#158・#159の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #156 | `core/test_bot_reply_settings.py`・`services/test_gemini_service.py` | 選択モデル・検索サブモデル・検索なしへの退避を実際の生成経路へ通し、生成中に設定を変えても生成モデルと今回のペルソナで評価をDB集計する。同時生成のモデル情報を分離し、失敗時に採用モデルを返さない。既存の3要素の生成結果を使う全呼出元も回帰確認 |
| #158 | `core/test_app_lifecycle.py`・`services/test_autonomous_session_cleanup.py` | 既存の終了処理を再確認。背景タスク・同期ワーカー・所有するステージ枠と接続を回収してからDiscord・Gemini・DBを閉じ、終了要求の重複・繰り返しキャンセルでも順序を保つ |
| #158 | `core/test_command_errors.py`・`core/test_join_command.py`・`core/test_guild_lifecycle.py` | 共通のコマンドエラーハンドラを登録し、応答前・defer後とも本人だけへ通知。閲覧権限がないボイス・ステージでは既存接続に触れない。退出時は停止完了後に当該ギルドの一時状態だけを解放し、停止失敗・キャンセル時も回収する |
| #159 | `services/test_content_moderator.py`・`core/test_violation_recording.py` | APIの空応答・不正JSON・通信失敗で違反を数えず、明示的な不適切判定とプロンプトブロックは維持。違反通知の送信失敗（削除済み・権限不足・HTTPエラー）でも保存済み違反とログを保つ |
| #159 | `core/test_bot_reply_settings.py`・`services/test_document_service.py`・`services/test_feedback_votes.py`・`services/test_blacklist_service.py` | 判定待ち・拒否・タグだけの応答では親密度を更新しない。通常・文書の謝罪文を会話記憶へ入れず、通常の返信保存・読み上げは維持。他のボットの削除イベントを除外し、人間のraw取り消しを保つ。未ブロック利用者の違反記録を保持し、解除成功はブロック中だった場合だけ返す |

Windows・Python 3.13.3、固定済み依存の仮想環境で全760件が成功。`pip check`、全166 Pythonファイルの構文・Python 3.10文法、構成図XML、メッセージ分割、`git diff --check`も成功。

Linux／Python 3.10での実行、Discord・Gemini・音声の実接続、実サーバーでの退出・停止とコマンド操作は未確認。外部通信はモック、DB・JSONは一時ディレクトリと架空のデータを使用する。

## Issue #153〜#155の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #153 | `utils/test_storage.JsonStorageTests`・`services/test_memory_service.DocumentSummaryCompactionTests` | BOM付きUTF-8の正しいJSONをBOM無しと同じ内容で読み、改名・退避・ログ出力をしない。`build_contexts` でもプロジェクト記憶が空にならない。BOMだけ・BOMの後ろが壊れたJSON・object以外・不正UTF-8は従来どおり原本を退避する |
| #154 | `services/test_feedback_votes.py` | 先行する追加の保存を同期処理の手前で止めても、後続の取り消しで評価が復活せず、後続の👎を古い👍で上書きしない。待機中のイベントは最新の状態だけを書く。保存の失敗・キャンセル（実行中・待機中）でロックを残さず、未反映の分は次のイベントが書き直す |
| #155 | `services/test_feedback_votes.py` | 👍→👎→👎取消で👍、👎→👍→👍取消で👎が1票残る。採用していない側だけを外しても採用中の評価を保ち、DBへ書かない。両方付けても1票、両方外すと0票。モデル／ペルソナ別の集計、自ボット・他のボット・対象外の絵文字・追跡外の返信の除外、追跡を外れた返信の状態の破棄を維持する |

## Issue #148〜#150の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #148 | `views/test_panel_confirmations.py`・`views/test_reminder_view.py`・`views/test_summary_view.py`・`utils/test_shared_helpers.py` | 設定確定は非公開で応答保留し、空の選択肢説明は送信内容から省略。会話要約の公開先と非公開の追加応答を分離したまま、公開投稿の全チャンクでメンション通知を無効化 |
| #148 | `audio/test_voice.py`・`audio/test_voice_playback_lifecycle.py` | 実際の`VoiceClient.play/stop`・`AudioPlayer.run/after/cleanup`を使い、通信・codecだけを代用。期限超過・繰り返しキャンセル・完了通知後のcleanup・既存の再生と一時停止・同時再生で、スレッド終了までストリームを開いたまま保持し、最後に回収 |
| #149 | `core/test_profile_absorption.py`・`core/test_database_json_values.py`・`core/test_guild_settings_persist.py`・`services/test_affinity_service.py`・`services/test_stats_service.py` | 辞書だけの旧プロフィールをownerあり／なしで保持・移行し、ASCII数字以外・混在形式・BOM・保存失敗を検証。移行と通常保存の`None`、感情値の上下限、最後のタグ採用と複合タグ除去、`bool`や選択肢外の間隔、Python 3.10の末尾`Z`制約を再現 |
| #150 | コンソール`tests/test_ssh_integration.py`・`tests/test_remote_scripts.py`・画面`tests/log-stream.test.mjs` | SSHの鍵・known_hosts不在時の具体的な案内、検索該当なしと実際の権限・コマンド失敗の区別、WebSocketのエラー／終了理由の維持と自動再接続の停止、手動再接続・接続先切替・古いイベントの無視・一時停止中の行保持 |

Git管理外のローカル`deploy/bot.service`は別の一時フォルダへ退避し、`WorkingDirectory`の末尾`index`だけを`src`へ変更した。他の内容とLF改行を保持し、実設定はコミットに含めない。Windowsから本番systemdへの配置・再起動は実行していない。

Chromeでは実際の`LogsPage`を架空の接続エラー状態で描画し、切断理由・手動再接続ボタン・受信済みの行の表示を確認した。操作と通信イベントはフックの自動テストで検証し、実サーバーとのブラウザ通信は未確認。

Windows・Python 3.13.3でボット706件・管理コンソールバックエンド174件、Node.js 24.16.0で画面45件とTypeScript・Viteビルドが成功。両仮想環境の`pip check`、全158 Pythonファイルの構文・Python 3.10文法・compileall、構成図XMLと参照整合性・メッセージ分割・`git diff --check`も成功。

## Issue #151・#152・#157の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #151 | 既存の `core/test_database`・`services/test_reminder_service`・`services/test_stats_service`・`services/test_document_*` など | 親密度の保存パラメータ・時刻文字列の生成・リマインダーの送信権限・文書の既定質問文を共通化しても、保存・通知・処理の結果が変わらない（振る舞いを変えない整理のため、既存テストで回帰を確認） |
| #152 | コンソール `tests/test_remote_scripts.DbTableScriptTests` | 表示名は表示中の ID だけを `IN (...)` で問い合わせ、表示しない利用者の行を読まない。新しいリマインダーの名前で上書きし会話統計の名前を最優先にする順序と、ID が多いときの区切り（古い SQLite の変数上限未満）を確認 |
| #152 | `views/test_panel_confirmations.py` | キャラ設定と自発会話は確定を押すまで設定を変えず、確定で反映・保存する。ペルソナのファイルが無ければ変更しない。自発会話の確定で配信の再挑戦待ちを解除する。ブロック解除の成功・解除済みの表示と、選択肢の上限・違反回数 |
| #157 | `core/test_bot_reply_settings.py` | 送信後の記憶保存・評価登録が失敗しても、送った本文を同じ設定で読み上げへ渡す。保存失敗では記憶圧縮を起動せず、再生成・再送もしない。送信の失敗では読み上げ・記憶・評価を行わず、保存中のキャンセルは伝わる |

## Issue #171〜#173の確認

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #171 | `services/test_voice_guard.py`・`core/test_join_command.py`・`services/test_autonomous_session_cleanup.py` | 手動の移動から元のチャンネルへ戻り、再生中の読み上げを止める。自分の接続・切断・ミュートは移動とみなさない。元のチャンネルの削除・権限不足・移動の失敗・時間切れ・連続移動では戻らずに退出する。配信は即座に畳み、`/join`・`/leave` との競合では人の操作を優先する |
| #172 | `core/test_voice_chat_ng_gate.py` | メンション必須モードでも、接続中のボイス／ステージのチャットのNG入力を記録し3回でブロックする。AI判定・返信は増やさず、他のチャンネル・未接続・移動前のチャットは見ない |
| #173 | `core/test_violation_recording.py` | ローカルNG・AI判定・同時入力の3回目でブロック・保存・一覧・再読込が一致する。保存失敗は回数に数えず残り回数を出さない。ボット応答のNGは利用者の違反にしない。解除後は1回目から数え直す |

## Issue #112・#113・#116〜#121の確認

| Issue | 変更と回帰の範囲 |
| --- | --- |
| #112 | PNGバッファ・Figureの回収、ペルソナのパス解決/読込のワーカー実行、送信権限共有、文書の時刻統一と記憶I/O整理 |
| #113 | 音声テストの統合/移動（既存16件の検証本体が一致）、コミット取得の終了コード確認、WebSocket切断・再キャンセル時の回収 |
| #116 | 空/重複を除いて有効な参考URLを上限まで取得、LLMのnull/非文字列の棄却、BOM付き.env読込 |
| #117 | projects既存users保持、要約の二重展開防止、強制flush待機、NG再読込失敗時保持、ブラックリスト正規化、文書終了と遠隔ファイル回収 |
| #118 | 自律開始中断・接続の所有権、ステージのチャンネル一致、OFF時のお祝い抑止、不完全な統計解析の再試行と表示名削除、記号TTS見送り・空行/字下げ保持・未対応チャンネルの早期終了 |
| #119 | デプロイの接続先/時刻照合、操作後キャッシュ失効、サーバー別ボイスエラー、トークン見本/不正行、接続メタデータ世代、画面の停止状態と遅延応答 |
| #120 | 永続設定の許可値/実在検証、ステージ参加時の発言者昇格と案内、選択肢整形・DM定数・チャンネル名の統一 |
| #121 | DB集計の直接importと読み取り専用性、期間索引のEXPLAIN確認・旧日時形式、操作フック/世代/タイマー破棄、管理コンソールCI統合 |

Windows・Python 3.13でボット552件、バックエンド133件、Node.js 24で画面32件がすべて成功。画面ビルド、両仮想環境の`pip check`、143ファイルの構文/Python 3.10文法、構成図XML・メッセージ分割・`git diff --check`も成功した。実データは使用せず、外部通信はモックまたはループバックの模擬SSHへ限定した。追加したCIジョブの実行、Linux/Python 3.10での実行、実Discord・Gemini・遠隔デプロイとブラウザでの手動操作は未確認。

## Issue #167の確認

| 範囲 | テスト | 確認内容 |
| --- | --- | --- |
| 判定 | `utils/test_response_text.py` | 検索の呼び出し・思考・最終回答の連続、内部形式だけ、コードフェンス付き、前置き後の呼び出し・検索結果、CRLFを検出。日本語、英文の`thought`、文中の単独行・派生語、Pythonのコード例、インラインコードは対象外。構造化された思考・コード・関数呼び出しは本文に含まれず、診断の要約にも本文・検索語を含めない |
| 生成 | `services/test_gemini_service.GeminiInternalTextTests` | 選択モデル・429後の検索サブモデル・検索なしの各経路で、同じモデル・検索設定のまま上限まで再生成し、生成全体の期限を共有。破棄した応答の参考リンク・利用量を返さず、上限・期限切れでは追加の要求を出さない。感情タグと通常の応答は維持 |
| 返信 | `core/test_bot_reply_settings.py` | 実際の`GeminiService`を通し、Discord送信・NGワード処理・会話記憶・利用ログ・読み上げ入口へ最終回答だけが渡る。再生成でも解消しなければ定型の失敗案内だけを返し、評価にも登録しない |

修正前のソースへ追加テストを適用し、subTestを含め10失敗・3エラー（判定関数が無いことによる読込エラーを含む）を確認した。期限切れと通常応答の2件は修正前から成功し、既存動作を保護する。修正後はWindows・Python 3.13.3・google-genai 2.31.0で全606件が成功し、`pip check`、125ファイルのPython 3.10文法、`compileall`、構成図XML・メッセージ分割・`git diff --check`も成功した。SDKの生成要求・Discord・記憶・読み上げはモックで、架空の応答だけを使った。実際のGemini APIが内部形式を返す条件の再現、Linux・Python 3.10での実行、実Discordでの確認は未確認。

## Issue #134・#135の確認

| Issue | 回帰の範囲 |
| --- | --- |
| #134 | 旧PID・PID不明のログは成功にせず、新PIDのログが届くまで待機。待機中・成功確定直前のPID変更と自動再起動を検出し、通常デプロイ・巻き戻し後の起動確認も検証 |
| #135 | 起動・停止・再起動とデプロイ開始、サービス操作同士、接続変更を直列化。待機後に接続先・接続時刻・デプロイ状態を再確認し、待機中のキャンセル・送信後の再キャンセル・操作例外後のロック解放と再操作を確認 |

Windows・Python 3.13.3でボット578件・コンソールバックエンド157件、画面32件が成功し、画面ビルドと両仮想環境の`pip check`も成功した。146ファイルの構文・Python 3.10文法、構成図XML・メッセージ分割・`git diff --check`も成功。SSHはループバックの模擬サーバーまたはモックだけを使用した。Linux・実サーバーへのデプロイ・ブラウザの手動操作は未確認。

## Issue #132・#133・#144の確認

| Issue | 回帰の範囲 |
| --- | --- |
| #132 | 実uvicorn子プロセスで、終了要求後に模擬デプロイの巻き戻し・SSH切断・lifespanの終了を待つ。期限超過時の強制終了と失敗結果、画面終了・再読込・起動失敗時の回収を確認 |
| #133 | 早送り成功後のSSH応答消失・非ゼロ終了で実HEADを再確認。更新済みなら巻き戻し、未更新なら変更を避け、確認不能・巻き戻し失敗を明示 |
| #144 | 一人分のpending読込・スナップショット読込・JSON保存の失敗後も後続ユーザーを処理。原データと生成失敗回数を保持し、障害解消後の再処理、処理人数・連続失敗の上限、キャンセル伝播を確認 |

Windows・Python 3.13.3でボット578件・コンソールバックエンド141件、Node.jsの画面32件が成功し、画面ビルドも成功した。SSHはループバックの模擬サーバーだけを使用し、DB・JSON・子プロセスの通知先は一時ディレクトリへ限定した。実サーバーへのデプロイ・実Discord/Gemini・ブラウザの手動操作は未確認。

## Issue #138〜#140の確認

| Issue | 回帰の範囲 |
| --- | --- |
| #138 | `/join`の既存接続切断の失敗案内、新規接続の抑止、キャンセル伝播。接続再利用・ステージ昇格・権限確認も継続して検証 |
| #139 | 一時DBへの保存・圧縮の後片付け・周期処理の終了待機、受付停止、別の所有者の処理の維持、重複close・再キャンセル・callback失敗、文書遠隔ファイルと所有するステージ枠の回収 |
| #140 | 通常返信の複数件待機中の接続差し替え・同じ接続での移動、旧キューが満杯の状態での新規返信、同一チャンネルの接続再利用。自律配信の判定・上限・ワーカー回収も継続して検証 |

Discord・Gemini・音声通信はモックを使い、保存先は一時ディレクトリに限定した。

## Issue #145〜#147の確認

| Issue | 回帰の範囲 |
| --- | --- |
| #145 | 取り込み成功後の改名失敗と複数回の再起動、圧縮済みログの再取り込み防止、取り込み記録の保存失敗時の全件ロールバックと再試行。同じ本文の会話・異なる元ファイル・BOM・owner未設定も確認 |
| #146 | 同じユーザー・サンプルの解析共有、force要求と保存待機中の合流、完了順が逆でも新しい結果を保持。別ユーザーの保存・解析、削除・収集停止と再開、共有者の一部/全員の中断、開始前・保存中のキャンセル、失敗後の再試行、TTL・必要件数到達を確認 |
| #147 | ステージ準備中の同一チャンネルへの`/join`、開始待機の停止と再キャンセル時の回収。配信コメントの文書・リマインダー・通常統計の省略、未対応・破損・容量超過の添付で受付枠を消費しないこと、画像の再利用と回収、配信終了時の通常メンションへの復帰を確認 |

DB・JSON・画像は一時データを使い、Discord・Gemini・音声はモックで確認する。管理コンソールではスキーマv11の模擬DBに取り込み記録を加え、本体と全10テーブルの列が一致することも確認した。

Windows・Python 3.13.3でボット672件・コンソールバックエンド158件、Node.js 24.16.0で画面36件とビルドが成功した。両仮想環境の`pip check`、154ファイルの構文・Python 3.10文法、構成図XML・メッセージ分割・`git diff --check`も成功。既存のデプロイ試験1件は一括実行で一時的に失敗し、単独と最終の一括実行で成功した。Linux／Python 3.10の実行、実サーバー・Discord・Gemini・音声通信とブラウザの手動操作は未確認。

## 現在の全テストファイル

音声の数値検証は`audio/test_voice_dsp.py`へ統合し、セッション検証は`audio/test_voice_session.py`へ移動した。`services/`に同名の音声テストは残していない。次の一覧は現在の`test_*.py`全ファイルに対応する。

| 分類 | ファイル |
| --- | --- |
| config | [test_personas.py](config/test_personas.py) |
| config | [test_prompt_loading.py](config/test_prompt_loading.py) |
| config | [test_settings_loader.py](config/test_settings_loader.py) |
| config | [test_tuning_constants.py](config/test_tuning_constants.py) |
| core | [test_app_lifecycle.py](core/test_app_lifecycle.py) |
| core | [test_background.py](core/test_background.py) |
| core | [test_bot_reply_settings.py](core/test_bot_reply_settings.py) |
| core | [test_command_blacklist.py](core/test_command_blacklist.py) |
| core | [test_database.py](core/test_database.py) |
| core | [test_database_json_values.py](core/test_database_json_values.py) |
| core | [test_db_modules.py](core/test_db_modules.py) |
| core | [test_guild_settings_persist.py](core/test_guild_settings_persist.py) |
| core | [test_join_command.py](core/test_join_command.py) |
| core | [test_message_gates.py](core/test_message_gates.py) |
| core | [test_message_handler_import.py](core/test_message_handler_import.py) |
| core | [test_message_operations.py](core/test_message_operations.py) |
| core | [test_profile_absorption.py](core/test_profile_absorption.py) |
| core | [test_violation_recording.py](core/test_violation_recording.py) |
| core | [test_voice_chat_ng_gate.py](core/test_voice_chat_ng_gate.py) |
| audio | [test_voice.py](audio/test_voice.py) |
| audio | [test_voice_dsp.py](audio/test_voice_dsp.py) |
| audio | [test_voice_playback_lifecycle.py](audio/test_voice_playback_lifecycle.py) |
| audio | [test_voice_session.py](audio/test_voice_session.py) |
| services | [test_affinity_service.py](services/test_affinity_service.py) |
| services | [test_auto_topic_service.py](services/test_auto_topic_service.py) |
| services | [test_autonomous_comments.py](services/test_autonomous_comments.py) |
| services | [test_autonomous_metrics.py](services/test_autonomous_metrics.py) |
| services | [test_autonomous_session_cleanup.py](services/test_autonomous_session_cleanup.py) |
| services | [test_autonomous_speech.py](services/test_autonomous_speech.py) |
| services | [test_autonomous_stage.py](services/test_autonomous_stage.py) |
| services | [test_autonomous_start_cancellation.py](services/test_autonomous_start_cancellation.py) |
| services | [test_autonomous_start_races.py](services/test_autonomous_start_races.py) |
| services | [test_blacklist_service.py](services/test_blacklist_service.py) |
| services | [test_document_lifecycle.py](services/test_document_lifecycle.py) |
| services | [test_document_recovery.py](services/test_document_recovery.py) |
| services | [test_document_service.py](services/test_document_service.py) |
| services | [test_document_shutdown.py](services/test_document_shutdown.py) |
| services | [test_feedback_votes.py](services/test_feedback_votes.py) |
| services | [test_gemini_service.py](services/test_gemini_service.py) |
| services | [test_genai_client.py](services/test_genai_client.py) |
| services | [test_llm_value_validation.py](services/test_llm_value_validation.py) |
| services | [test_memory_service.py](services/test_memory_service.py) |
| services | [test_ng_word_service.py](services/test_ng_word_service.py) |
| services | [test_reminder_service.py](services/test_reminder_service.py) |
| services | [test_service_thread_cancellation.py](services/test_service_thread_cancellation.py) |
| services | [test_stats_service.py](services/test_stats_service.py) |
| services | [test_voice_guard.py](services/test_voice_guard.py) |
| views | [test_database_interactions.py](views/test_database_interactions.py) |
| views | [test_ng_reload_failure.py](views/test_ng_reload_failure.py) |
| views | [test_panel_concurrency.py](views/test_panel_concurrency.py) |
| views | [test_panel_confirmations.py](views/test_panel_confirmations.py) |
| views | [test_remaining_io.py](views/test_remaining_io.py) |
| views | [test_reminder_view.py](views/test_reminder_view.py) |
| views | [test_settings_view.py](views/test_settings_view.py) |
| views | [test_stats_delivery.py](views/test_stats_delivery.py) |
| views | [test_stats_entry.py](views/test_stats_entry.py) |
| views | [test_stats_pagination.py](views/test_stats_pagination.py) |
| views | [test_summary_view.py](views/test_summary_view.py) |
| views | [test_view_parts.py](views/test_view_parts.py) |
| utils | [test_logging_setup.py](utils/test_logging_setup.py) |
| utils | [test_discord_mocks.py](utils/test_discord_mocks.py) |
| utils | [test_graph_fonts.py](utils/test_graph_fonts.py) |
| utils | [test_image_attachments.py](utils/test_image_attachments.py) |
| utils | [test_json_extraction.py](utils/test_json_extraction.py) |
| utils | [test_lifecycle_helpers.py](utils/test_lifecycle_helpers.py) |
| utils | [test_response_text.py](utils/test_response_text.py) |
| utils | [test_runtime_logging_privacy.py](utils/test_runtime_logging_privacy.py) |
| utils | [test_shared_helpers.py](utils/test_shared_helpers.py) |
| utils | [test_storage.py](utils/test_storage.py) |
| utils | [test_timeutil.py](utils/test_timeutil.py) |
| utils | [test_welcome_message.py](utils/test_welcome_message.py) |
| deploy | [test_migrate_config_to_env.py](deploy/test_migrate_config_to_env.py) |
| deploy | [test_migrate_data_directory.py](deploy/test_migrate_data_directory.py) |
| config | [test_data_paths.py](config/test_data_paths.py) |
| integration | [test_event_loop_io.py](integration/test_event_loop_io.py) |
| integration | [test_package_layout.py](integration/test_package_layout.py) |
| integration | [test_sdk_migration.py](integration/test_sdk_migration.py) |

管理コンソールのテストは独立した依存環境で実行するため、この一覧には含めない。[貢献ガイド](../CONTRIBUTING.md#管理コンソールをローカルで確認する)を参照。

## サービスと音声の同期処理の確認（#90・#91）

`services/test_service_thread_cancellation.py` は、リマインダー5か所・文書キュー5か所・記念日1か所・フィードバック4か所・音声2か所の実スレッドを待機させ、繰り返しキャンセルしても同期処理の終了まで呼出元が待つことを確認する。一時 DB の登録を中断した場合も、トランザクション完了後にキャンセルを伝え、後続の読み取りと DB の終了ができることを確認する。

#91 の自発会話・記念日・自律配信のプロンプト読込は既存実装でワーカーへ移されている。`services/test_auto_topic_service.py` の2経路と `services/test_autonomous_speech.py` の第一声・独り言・コメントの3経路で、実スレッドでの読み込み中もイベントループが進むことを確認する。

## ボイスの権限・内部タグとキャンセル時の回収の確認（#87〜#89）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #87 | `core/test_join_command.py` | 接続・発言の片方または両方が不足した場合に、不足権限だけを案内。defer・自律配信の停止・既存接続の切断・新規接続を行わない。権限が揃った場合の接続再利用・待ち時間指定も維持する |
| #88 | `core/test_background.py` | 実スレッドを待機させ、繰り返しキャンセル後も完了まで待つ。バッファ回収、closeの無い結果・呼び出せない属性、属性取得・回収・ワーカーの例外が起きた場合のキャンセル伝播を確認。通常の結果は閉じず、通常のワーカー例外はそのまま伝播する |
| #89 | `audio/test_voice.py` | 先頭・文中・末尾の感情・親密度タグと大小文字・空白のゆらぎを除去。一般の角括弧付き本文を保持し、タグ除去後に文字数を制限する。両方の音声入口から合成へ渡る本文、タグだけの場合のキュー作成見送りを確認する |

#89は、角括弧内の英数字を一律に除去する案では `[Python]` などの本文も消えるため、`affinity_service.strip_speech_tags` に認識する内部タグを集約した。既存の感情変化の抽出や表示用のタグ除去は維持する。#87に合わせ、`services/test_autonomous_start_races.py` の架空の権限にも発言許可を追加し、手動接続と自律配信の開始競合の検証を維持している。

修正前のソースに追加14件を含む関連31件を実行し、subTestを含め30失敗・7エラーを確認した。修正後は親密度保存・自律配信の開始競合も含む関連54件が成功した。外部通信はモック、同期処理の中断は実スレッドとイベントで再現し、実行時データは使っていない。

2026-10-17、Windows・Python 3.13.3で全476テストが省略なしで成功した。`pip check`、全115ソースの構文・Python 3.10文法、`compileall`、構成図XML、4001文字のメッセージ分割、`git diff --check`も成功。Linux・Python 3.10での実行、Discord・Gemini・音声の実接続は未確認。

## 音声テスト・操作の利用制限・統計表示の確認（#80・#85・#86）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #80 | `audio/test_voice.py` | URL・コード・メンション・カスタム絵文字の除去、文字数上限、PCMのチャンネル数とクリップ、edge-ttsの音声チャンク連結、例外・空応答時のgTTS退避と復旧、キャンセル、デコードのワーカー実行、未接続時の見送り、キューの上限・順序・ペルソナ保持、合成失敗後の継続、別スレッドからの再生完了と完了通知の欠落 |
| #80 | `audio/test_voice_dsp.py` | ペルソナの既定値への退避、空音声、ローパス・ノッチの周波数特性、ゼロゲイン・未知フィルタの無加工、周波数の境界、量子化とビット数の上下限、入力配列の保持 |
| #85 | `core/test_command_blacklist.py` | 3コマンドをDM・サーバー・管理者の有無にかかわらず副作用の前に拒否。解除後の実行、パネル作成後のブロック、サブパネルへの遷移・ハブへの復帰後にも判定を維持する |
| #86 | `views/test_stats_pagination.py` | 集計前のdefer、4001文字を2000・2000・1文字に分割、全followupの本人限定、画像の維持と送信失敗時の回収、管理者以外・DM・サービス無効時の拒否、集計中の権限喪失、集計失敗時の案内 |

#80の「音声テストが無い」という前提と異なり、`audio/test_voice_dsp.py`・`audio/test_voice_session.py` と調整定数のテストは既に存在した。重複する検証は保ち、未確認だった分岐を新しい `audio/` で補った。音声本体と依存ライブラリは変更していない。

2026-10-17、追加30件を修正前のソースへ適用し、subTestを含め14失敗・5エラーを確認した。音声の21件は修正前から成功し、既存動作を保護する。ブロック拒否・パネルへの判定の引き継ぎ・統計の分割・応答保留・集計後の権限確認を修正した。既存のコマンドテストは、ブラックリストの代替オブジェクトと架空ユーザーIDを補い、従来の接続・終了の検証を維持している。

最新main（#69・#70、#76〜#79）の取り込み後、Windows・Python 3.13.3で全462テストが省略なしで成功した。`pip check`、全114ソースの構文・Python 3.10文法、`compileall`、構成図XML、メッセージ分割、`git diff --check`も成功。外部通信はモック、すべて架空のデータを使い、実行時の記憶・NGデータを使っていない。今回の追加分はLinux・Python 3.10での実行、Discord・Gemini・音声の実接続が未確認。

## 同期I/O・画像の容量制限・JSON抽出の確認（#69・#70）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #69 | `services/test_auto_topic_service`・`services/test_autonomous_speech`・`views/test_remaining_io` | 自発会話・記念日・第一声・独り言・コメント応答・要約のペルソナ読込中もイベントループが進む。設定のパス確認とNG再読込はdefer後のワーカーで実行し、結果を本人のパネルへ返す。DMを監視せず、公開要約の権限・通信エラーを通知する |
| #69 | `utils/test_image_attachments`・`services/test_ng_word_service.NgWordReloadTests` | 画像の取得前・取得後の容量確認、上限ちょうどの画像のRGB変換、壊れた画像の後の継続、中断時の全画像の回収。NGワードは正規表現の準備後に単語とまとめて反映する |
| #69 | 既存の `services/test_document_service.DocumentTemporaryFileTests` | 共通のワーカー待機へ置き換えた後も、書込エラー・単発／繰り返しキャンセルで一時ファイルが残らず、回収後に再作成されない |
| #70 | `utils/test_json_extraction.ServiceJsonTests`・`services/test_stats_service.StatsAnalysisTests` | 文書要約・記憶ルーティング・圧縮・統計解析で前置きの別形式JSONを読み飛ばす。必要なキーが無い場合の失敗・キャッシュ保持と、カテゴリ・繰り返し種別の空白／大小文字の正規化を確認する |
| #70 | `config/test_tuning_constants.ModerationAndChoiceTuningTests` | 起動前に変えたモデレーションのモデル・タイムアウトと、既定の応答モードが利用側へ反映される（既定ペルソナは #189 で固定の選択肢を外したため対象外） |

2026-10-17、Windows・Python 3.13.3で追加22件を修正前のソースに適用し、subTestを含め19失敗・8エラーを確認した。画像の4件は、新しい定数・デコード補助関数が修正前に存在しないことによるエラー。読込失敗後の継続・非画像の除外・統合判定の正規化・必要なキーが無い場合の既存の失敗処理の4件は修正前から成功した。修正後は全412テストが省略なしで成功した。

`pip check`、全106ソースの構文とPython 3.10文法、構成図XML、4001文字のメッセージ分割、`git diff --check`も成功。外部通信はモック、DB・画像・JSONは一時ディレクトリと架空のデータを使った。Linux・Python 3.10での実行と、Discord・Gemini・音声の実接続は未確認。#71・#72はマージ済みPRで、両方のマージコミットが作業開始時のmainに含まれることを確認した。

公開前に最新main（#73〜#75）を取り込み、全419テストが省略なしで成功した。全107ソースの構文・Python 3.10文法と `compileall`、依存関係、構成図XML、メッセージ分割、差分も再確認した。記憶圧縮による文書要約の保護と、必要なキーを指定したJSON抽出の両方を保持している。

## リマインダーの停止記録・会話要約の失敗表示・案内の投稿先・設定の保存順の確認（#76〜#79）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #76 | `core/test_database.ReminderClaimStatusTests` | 次回時刻の計算が例外でも `None` でも、繰り返し（毎日・毎月）は `canceled`、単発は `done` になり、返す記録と DB の両方が一致する。計算できた繰り返しは `pending` のまま次回時刻へ進み、返す記録の `remind_at` は進める前の予定時刻のまま |
| #77 | `views/test_summary_view` | 要約の想定外の例外で「要約中...」を定型の案内へ差し替え、例外の文面は見せない。公開投稿の権限エラーでは「自分だけ」での表示を案内する。「自分だけ」で1通目を表示済みなら上書きせず followup で伝え、パネルを書き換えられなければ followup に切り替え、それも失敗したら例外を外へ出さない。成功時と案内文を返す場合の表示は変わらない |
| #78 | `utils/test_welcome_message.AnnouncementChannelTests` | 送信だけ許可され閲覧できないチャンネル（システムチャンネルを含む）は選ばず、閲覧・送信の両方ができる最初のチャンネルを選ぶ。どれも無ければ選ばない。使えるシステムチャンネルは優先する |
| #79 | `core/test_guild_settings_persist` | 保存の書き込み中に次の保存を呼ぶと、先の書き込みが終わるまで待ち、最後に最新の値を書く。書き込み中にキャンセルしても書き込みの完了まで戻らず、次の保存はその後に最新の値を書く。記念日の記録もキャンセル時に書き込みの完了を待つ |

#76 の修正は Issue の案（`recurrence == "none" and not calculation_failed`）と異なり、単発を常に `done` にした。単発は計算に失敗しても通知されるため、`canceled` と記録すると実態と食い違う。#78 は discord.py 2.7.1 の `permissions_for` が閲覧できないチャンネルの `send_messages` も `False` にするため、Issue の Forbidden は実際には起きない。テストはその処理を通さない架空の権限で、ボット側の判定だけを確かめている。#79 は Issue の案の `run_in_thread` への置き換えに加え、ロックで保存を直列化した（置き換えだけでは並行した保存の順序は守れない）。

2026-10-17、Windows・Python 3.13で修正前の `src/` に対して追加15件を実行し、10件が失敗またはエラーになった。計算できた繰り返しの扱い・要約の成功時の表示・使えるシステムチャンネルの優先と、#74 のウェルカム設定の2件は修正前から成功し、変わらないことを確かめた。修正後は全件成功し、全410テストが省略なしで成功した。依存関係・全ソースの構文・Python 3.10文法・メッセージ分割・`compileall` も確認した。Linux・Python 3.10での実行と、実際のDiscord・Geminiでの再現は未確認。

公開前に最新main（#69・#70の修正）を取り込んだ。両方が会話要約の全員表示の投稿失敗を直していたため、#69・#70 の原因別の案内（権限不足・途中での失敗）を送信だけを囲む内側で残し、#77 の想定外の例外の受け止めを外側に置く形にまとめた。権限不足の案内は、権限設定の確認と「自分だけ」での表示の両方を含める。取り込み後も `views/test_summary_view`・`views/test_remaining_io` を含む全432テストが省略なしで成功した。

## 記憶圧縮の文書要約・ウェルカム設定・リマインダーの選択肢の確認（#73〜#75）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #73 | `services/test_memory_service.DocumentSummaryCompactionTests` | 圧縮プロンプトへ文書要約を渡さず、他のプロジェクト記憶は渡す。LLM が要約を出力しなくても、作り話の要約を出力しても、圧縮直前の一覧（既存＋圧縮中の追加。上限を超えた分は古い方から押し出し済み）がそのまま残る。要約の無いユーザーにはキーを足さず、型が崩れた要約も書き換えずに残す。他ユーザーの要約と、圧縮中に追記された会話ログは変わらない |
| #74 | `utils/test_welcome_message` | `WELCOME_MESSAGE_ENABLED = False` なら投稿先を探さず、送信せず、投稿済みにも数えない。`True` なら1回だけメンションを無効にして投稿し、同じサーバーへの2回目は投稿しない |
| #75 | `views/test_reminder_view.ReminderViewTests` | 改行・CRLF・タブ・全角空白を含む本文でも、Discord へ送る選択肢の label・description に改行・連続空白・前後の空白が残らず、100文字以内に収まる。空白だけの本文は「（本文なし）」と表示し、取り消し対象の ID は全件残る |

#74 は Issue の前提（設定が参照されていない）と異なり、`post_welcome_message` の先頭で以前から判定していたため、本体は変えずに回帰テストだけを追加した。`on_guild_join` 側へ同じ判定を重ねると判断が2か所に分かれるため、置かない。

2026-10-17、Windows・Python 3.13で修正前の `src/` に対して実行し、#73・#75 で追加・変更した11件はすべて失敗またはエラーになった。#74 の2件は本体を変えていないため修正前から成功する。修正後は全件成功し、全397テストが省略なしで成功した。依存関係・全ソースの構文・Python 3.10文法・メッセージ分割・`compileall` も確認した。Linux・Python 3.10での実行と、実際のDiscordでの改行入り選択肢の拒否（400）の再現は未確認。

## 読み上げの音色・/join・旧データ移行・終了処理の確認（#67・#68）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #67 | `audio/test_voice_dsp` | `nan`・`inf`・数値でない値を既定値へ落とす。`bit_crush` の「加工が必要か」の判定と実際の加工が、`"0"`・`-1`・`0.5`・`nan`・`1`・`"6"` などで一致する。周波数・ゲイン・Q が非有限でも出力が NaN にならない。同梱の `flat` と `robotic` の判定は変わらない |
| #67 | `core/test_join_command` | 同じチャンネルへ接続中なら切り直さずに案内する。別のチャンネル・切れた接続は切断してから `VOICE_CONNECT_TIMEOUT_SECONDS` 付きで接続する。初回の接続も同じ待ち時間を渡す |
| #68 | `deploy/test_migrate_config_to_env` | 移行後に案内する確認コマンドの import が、現在の `src/` から読み込める（`load_settings` は呼ばず、実際の `.env` を読まない） |
| #68 | `core/test_database.LegacyDataEdgeCaseTests` | 引き継ぎ先が空なら未割当の `pending_log` を削除し、`pending_distinct_user_ids` も空の ID を返さない。引き継ぎ先があれば従来どおり振り直す。旧 `pending_log.txt` は引き継ぎ先が無ければ取り込まず、どちらでもバックアップに残る。壊れた・最上位が object でない・`tasks` が配列でない旧 JSON は元の名前のまま残り、正常な旧 JSON は従来どおり取り込んで改名する。`close` は実行中の操作を待ってから閉じ、2回呼んでもよい |
| #68 | `core/test_app_lifecycle` | 復元より前に作られた設定も保存され、再起動相当の復元で戻る。終了時は Discord → Gemini → DB の順に閉じ、途中が失敗しても DB を閉じる。DB を渡さない生成も従来どおり終了できる |
| #68 | `services/test_blacklist_service` | `ng/` が無くても最初の違反を保存できる。dict でない記録・数値でない回数・数値でない ID があっても判定と一覧が動き、新たな違反で正しい記録に置き換わる |

既存の `services/test_autonomous_start_races.py` は、`/join` が接続の待ち時間を渡すようになったため、`connect` の代わりに使う補助関数がキーワード引数を受け取れるように直した（確かめている内容は変えていない）。

2026-10-16、Windows・Python 3.13で修正前の `src/`・`deploy/` に対して追加19件を実行し、15件が失敗またはエラーになった。同梱の音色の判定・DB を渡さない終了・引き継ぎ先がある場合の振り直し・正常な旧 JSON の取り込みの4件は修正前から成功し、変わらないことを確かめた。修正後は全件成功し、全390テストが省略なしで成功した。依存関係・全ソースの構文・Python 3.10文法・メッセージ分割・`compileall`・構成図のXMLも確認した。合成中の切断で再生しないこと（#67 の1点目）は #51 の修正で対応済みで、既存の `audio/test_voice_session.py` が確かめている。Linux・Python 3.10での実行と、実際のDiscord・ボイス接続・音声合成での再現は未確認。

## リマインダーの通知範囲・受信時の関門・会話統計の確認（#64〜#66）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #64 | `utils/test_shared_helpers.LongMessageTests` | リマインダー通知の分割後の全チャンクで、許可するメンションが依頼者本人だけ。本文の `@everyone`・`@here`・ロール・他人のメンションでは通知しない |
| #65 | `core/test_message_gates` | ブロック中のユーザーは活動の観測・ゲート判定・モデレーションの前に破棄し、ブロックされていなければメンションなしの発言も観測する。画像だけの発言で画像を読めなければ Gemini を呼ばずに断り、読めれば共通の質問文と画像を渡す。本文があれば画像が読めなくても答える。違反警告は記録の結果と一致し、並行した違反で既にブロック済みなら返信しない。未対応の添付には画像と文書の対応形式を案内する |
| #65 | `utils/test_shared_helpers.AttachmentLabelTests` | 履歴の添付を画像（Content-Type・拡張子）とそれ以外で書き分け、両方あれば両方を書く。対応形式の案内は定数から組み立てる |
| #66 | `services/test_stats_service.StatsAnalysisTests` | 件数不足で性格推定だけを開くと解析しない。件数不足の解析は性格推定を保存せず、件数が揃うと TTL 内でも1回だけ解析し直す。文字列だけの話題・数値でない重み・範囲外・NaN・無限大・真偽値・未知の軸を正規化して保存・描画し、以前の版の壊れたキャッシュと時間帯の値でも3種類のグラフを描ける |
| #66 | `services/test_memory_service.CompactionFailureCountTests` | プロンプトブロックでログを消したら連続失敗の回数を0に戻し、新しいログは上限未満の一時エラーでは消えず、上限に達したときだけ消える |

2026-10-16、Windows・Python 3.13で修正前の `src/` に対して追加・変更した21件を実行し、14件が失敗またはエラーになった。うち「読める画像だけの発言で共通の質問文を使う」1件は、修正前に定数 `IMAGE_ONLY_QUESTION` が無かったことによるエラーで、挙動（同じ質問文を使う）は修正前から同じ。メンションなしの発言の観測・本文がある場合の応答・違反回数に応じた警告・既存の長文分割の7件は修正前から成功し、変わらないことを確かめた。修正後は全件成功し、全347テストが省略なしで成功した。依存関係・全ソースの構文・Python 3.10文法・メッセージ分割・`compileall`・構成図のXMLも確認した。Linux・Python 3.10での実行と、実際のDiscord・Geminiでの再現は未確認。

公開前に最新main（#61〜#63の修正）を取り込んだ後も、全371テストが省略なしで成功した。構成図は、#61〜#63が変えたノードと今回変えた関門のノードが別々のため、両方の変更を保っている。

## 統計パネル・文書処理・配信開始の確認（#61〜#63）

| Issue | テストと確認内容 |
| --- | --- |
| #61 | `views/test_stats_delivery.py`：送信成功・失敗・キャンセル・Fileの作成途中の失敗で全バッファを解放。生成失敗を元パネルへ通知。統計表示・対象選択・ブロック解除と、統計の生成待機中の管理者権限変更を検証 |
| #62 | `services/test_document_lifecycle.py`・`services/test_document_service.py`：掃除エラー後も完了状態と返信1回を維持。4添付の要約と開始時の設定を保持。PDFの正常・暗号化・検証例外時と、検証中の繰り返しキャンセルでストリームと一時ファイルを回収 |
| #63 | `services/test_autonomous_start_races.py`・`services/test_autonomous_speech.py`：3つの発話経路でリセット地点を引き継ぐ。接続・準備待機中の停止・無効化・静音時間・切断・移動・接続差し替え、同時開始、`/join`・`/leave` の実コマンド経路を検証 |

修正前の実装に追加テストを適用し、バッファ未解放、生成例外の未処理、権限喪失後の操作、掃除エラーによる完了状態の喪失・二重返信、先頭添付の要約欠落、設定混在、リセット無視、停止と開始の競合を再現した。外部通信はモック、DB・JSON・PDFは一時ディレクトリ内の架空データを使い、実際の設定・会話・記憶は使用しない。Discord・Gemini・ボイス／ステージの実接続と、Linux・Python 3.10での実行は未確認。

Windows・Python 3.13.3で既存321件と追加24件の全345テストが省略なしで成功した。依存関係、全97ソースの構文とPython 3.10文法、メッセージ分割、構成図のXML、差分を確認した。開始途中の接続異常には再試行待ち時間を設け、利用者の停止・無効化・静音時間による中止は失敗回数に加えないことも確認した。

公開前に最新mainのPDF依存の遅延読込などを取り込み、全355テストが省略なしで成功した。`pypdf` を塞いだ状態でも全355件を検出し、実PDFを使う2件だけを省略して成功した。ストリーム解放・検証中のキャンセルは依存をモック化して省略せずに確認した。

## 記念日の投稿先・PDF依存・リマインダー索引の確認（#56〜#58）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #56 | `services/test_auto_topic_service.AnniversaryChannelTests` | 最新の監視チャンネルが削除済みなら監視から外して次へ、送信不可なら生成せずに次へ進む。送信先が無い間は生成も記録もせず、権限の復旧後に1回だけ投稿する。生成後の `Forbidden` はその日を記録して再生成せず、別のチャンネルへ流用しない。一時的な生成失敗は次回に再試行する |
| #57 | `services/test_document_service.OptionalPdfDependencyTests` / `integration/test_sdk_migration.PdfMigrationTests` | `pypdf` を読み込めない状態でも文書処理モジュールを読み込める。PDFの検証だけが `pypdf` を名指しした `ValueError` になり、テキストの検証は動く。PDFの回帰テストは `pypdf` が無ければ省略する |
| #58 | `core/test_database.ReminderUserIndexTests` | 登録時の上限確認・一覧・件数が実際に発行するSQLを `EXPLAIN QUERY PLAN` にかけ、`idx_reminders_user_status` を使い一時的な並べ替えが無いことを確認。版9のDBが予定を保ったまま版10へ移行することも確認 |

2026-10-16、Windows・Python 3.13で修正前の `src/` に対して追加10件と既存のPDF検証1件を実行した。記念日4件と索引2件（3操作のsubTestと移行）が失敗し、`pypdf` を塞いだ読み込みの3件がエラーになった（一時的な生成失敗の再試行と、既存のPDF検証は修正前から成功）。修正前は、版9の一覧SQLが `idx_reminders_due (status=?)` で全員の未通知の予定を走査していた。修正後は全件成功し、全309テストが省略なしで成功した。

`pypdf` の欠落は `sys.modules` を使って再現した。テスト全体の検出・実行は、修正前は2モジュールが読み込みで失敗して274件しか実行されず3件がエラーになった。修正後は全309件を検出し、PDFの1件だけを省略して成功した。依存関係・全ソースの構文・Python 3.10文法・メッセージ分割・`compileall`・構成図のXMLも確認した。Linux・Python 3.10での実行と、実際のDiscord・Geminiでの再現は未確認。

公開前に最新main（#51〜#53の修正）を取り込んだ後も、全331テストが省略なしで成功した。`pypdf` を塞いだ状態でも331件を検出し、PDFの1件だけを省略して成功した。

## 自律配信の音声・開始キャンセルと手書きプロフィール（#51〜#53）

| Issue | 回帰テストと確認内容 |
| --- | --- |
| #51 | `services/test_autonomous_speech.py`・`audio/test_voice_session.py`：投稿待機中の終了・配信差し替え・移動・切断後は読み上げない。キュー・合成の待機後も古い音声を破棄し、通常返信は処理する。第一声・独り言・コメントで開始時のペルソナ・モデル・検索設定を維持 |
| #52 | `services/test_autonomous_start_cancellation.py`：ボイス状態待機・発言者への変更中にキャンセルしても、所有枠と開始時の接続を解放。後片付け中の繰り返しキャンセル、削除エラー、枠・接続差し替え、借りた枠も検証 |
| #53 | `core/test_profile_absorption.py`：引継ぎ先のない旧形式や空のユーザー記憶は警告して原本を保持。ユーザー別取り込み・旧形式の互換性、DB書込失敗・原子的置換失敗後の再試行を一時DB・JSONで検証 |

修正前の実装に追加テストを適用し、投稿後の誤送出、設定の不一致、キャンセル後の枠・接続の残留、取り込めない原本の上書きを再現した。外部通信・実際の設定・会話・記憶は使わず、Discord境界と音声合成をモック化している。実際のDiscord・Gemini・ボイス／ステージ接続とLinuxでの実行は未確認。

Windows・Python 3.13で既存288件と追加22件の全310テストが省略なしで成功した。依存関係、全92ソースの構文とPython 3.10文法、メッセージ分割、構成図のXML、差分も確認した。Python 3.10での実行は未確認。

公開前に最新mainの修正を取り込んだ後も、Windows・Python 3.13.3で全321テストが省略なしで成功した。全94ソースの構文・Python 3.10文法、依存関係、メッセージ分割、構成図のXML、差分も再確認した。

## 感情タグの小文字とBOM付きファイルの確認（#54・#55）

| Issue | テスト | 確認内容 |
| --- | --- | --- |
| #54 | `services/test_affinity_service.AffinityTagTests` | 小文字・大小混在・空白入りのタグを抽出・除去し、変化量を返す。大文字タグとタグのない本文は従来どおり |
| #55 | `services/test_ng_word_service` | BOM付きの公開／非公開NGファイルで1行目のコメントを除外し、1行目の語を検知・マスクする。BOMの有無だけが違う同じ語は1件にまとめる。検出結果は重複を除いた出現順 |
| #55 | `config/test_prompt_loading` | BOM付きのペルソナを先頭のU+FEFFなしで読み、BOMなし・文字列パス指定も従来どおり |

NGワードは一時ディレクトリの架空の語だけを使い、実際の `data/ng/` は読まない。

2026-10-16、Windows・Python 3.13で修正前のソースに対して追加11件を実行し、10件が失敗した（小文字・混在タグの4パターン、BOM付きNGファイル3件、検出順1件、BOM付きペルソナ2件）。BOMなしと大文字タグの確認は修正前から成功する。修正後は `PYTHONHASHSEED` を0〜3とランダムに変えて追加分がすべて成功し、全299テストが省略なしで成功した。依存関係・全ソースの構文・メッセージ分割・`compileall` も確認した。Linux・Python 3.10での実行と、実際のDiscord・Geminiでの再現は未確認。

## ボット本体のフォルダ整理時の確認

`integration/test_package_layout.py` は、全ペルソナの既存の相対パスと `.env` の場所を保ち、DB・記憶・NG関連ファイルが `data/` 配下を参照することを確認する。起動の確認ではソースだけを一時プロジェクトへコピーし、架空の `.env` と一時DBを使って、従来の `src/bot.py` を子プロセス内で実行する。Geminiクライアントの生成とDiscordの接続入口をモック化し、コマンド登録まで進むことと外部通信・DNSの試行がないことを確認する。

Windows・Python 3.13の仮想環境で、既存286件と追加2件の全288テストが省略なしで成功した。依存関係、全79ソースの構文とPython 3.10文法、メッセージ分割、構成図のXML、差分も確認した。移動前後の全43ソースは、importとパス基準の調整を除いてASTが一致した。Linuxでの実行、Python 3.10での実行、実際のDiscord・Gemini・ボイス接続は未確認。

## 自動投稿・移行・パネル・JSONと自律行動の確認（#39〜#46）

| Issue | 確認内容 |
| --- | --- |
| #39 | 独自RESTは既にSDKへ移行済み。本番と同じfactory・SDKのHTTP境界をモック化し、検索の成功・接続障害・タイムアウト・HTTP例外・400/503でキーがヘッダーだけに入り、URLとログへ出ないことを確認 |
| #40 | 自発話題は通知を抑止し、記念日は対象者だけを通知可能に制限。長文の全チャンクにも同じ制限が渡ることを確認 |
| #41 | 実DBへ移行途中・版更新直前の失敗を注入。子プロセスをALTER直後に終了した場合も、DDL・版が戻り、既存データを保って再起動時に移行を再試行できることを確認 |
| #42 | サブパネル遷移・ハブ復帰後に通常メッセージの編集を禁止し、Webhookのメッセージで時間切れ表示とUI除去が行われることを確認 |
| #43 | 閲覧・送信権限不足、ボットMember未取得、DMで履歴・生成・判定・送信を省略。スレッド権限、権限復旧後の投稿、生成後のForbiddenの再生成抑制、一時障害の再試行を確認 |
| #44 | 不正・閉じていない先行候補、ネスト、文字列内の括弧・エスケープ、コードブロック、別形式のJSON例、整数桁数上限を確認。両モデレーション入口が後続のunsafe判定を使うことと、解析不能時の既存動作も確認 |
| #45 | 実際に呼ばれる `reminder_add` へBarrierを置き、並行した2件の到達・戻りID・登録件数を検証。サーバー/DMと繰り返し予定を混ぜた場合も最後の1枠に1件だけ成功することを確認 |
| #46 | 計算モジュールをDiscord・設定モジュールなしでimportし、ゲージ・活動窓・深夜帯の境界を検証。チャンネルの権限・ステージ優先・枠の作成/後片付けを確認し、既存のキャンセル・接続差し替え保護の回帰も実行 |

2026-10-16、Windows・Python 3.13の専用仮想環境へ `requirements.txt` の指定版を導入して全286テストが成功した。省略・実通信の試行はともに0件。HTTPX・aiohttpの実通信と外部DNSを遮断し、試行件数も確認した。依存関係・全Pythonソースの構文・Python 3.10文法・メッセージ分割・図のXML・差分の確認も成功。Linux・Python 3.10での実行と、実際のDiscord・Gemini・ボイス接続はローカルでは未確認。

## 修正前後の確認記録

2026-10-15、Windows・Python 3.13で確認。過去のソースだけを一時ディレクトリへ展開し、同じ代表テストを修正前後に適用した。実行時ファイルはコピーしていない。下表はいずれも問題に対応するアサーションが修正前に失敗し、修正後に成功した。

| Issue | 代表テスト | 修正前 → 修正後 | 検出した問題 |
| --- | --- | --- | --- |
| #2 | `services/test_memory_service.DocumentSummaryCompactionTests.test_addition_at_limit_is_preserved` | `08717c9` → `790b4f7` | 圧縮中に追加した要約の消失 |
| #3 | `services/test_document_service.DocumentTemporaryFileTests.test_read_failure_removes_temporary_file` | `08717c9` → `790b4f7` | 添付読込失敗後の一時ファイル残留 |
| #4 | `services/test_document_service.DocumentResponseTests.test_affinity_tag_is_removed_before_screening_and_storage` | `302c7c1` → `46abda2` | 文書応答の感情タグ残留 |
| #5 | `views/test_settings_view.ConversationResetTests.test_guild_reset_excludes_messages_before_button_press` | `302c7c1` → `46abda2` | リセット前の履歴混入 |
| #6 | `services/test_reminder_service.ReminderRegistrationTests.test_last_slot_accepts_only_one_concurrent_request` | `302c7c1` → `22571ac` | 残り1枠への同時二重登録 |
| #7 | `core/test_bot_reply_settings.ReplySettingsTests.test_generation_change_uses_old_settings_then_next_reply_uses_new` | `302c7c1` → `22571ac` | 生成と評価に使うモデル・ペルソナの不一致 |
| #8 | `services/test_autonomous_comments.AutonomousCommentAdmissionTests.test_five_comments_make_only_one_input_and_output_check` | `cb46a7b` → `6e9cdd9` | 連続5コメントで入力判定も5回実行 |
| #9 | `services/test_stats_service.UserStatsPersistenceTests.test_write_count_is_constant_for_one_hundred_and_thousand_users` | `cb46a7b` → `6e9cdd9` | ユーザー数に比例してDB書込が増加 |
| #11 | `utils/test_storage.JsonStorageTests` | `033cd92` → `c200681` | 書込中断で原本が変化し、破損時の退避がない |
| #12 | `views/test_reminder_view.ReminderStatusTextTests.test_long_lists_fit_limit_and_report_every_omitted_record` | `033cd92` → `c200681` | 長文の予定一覧が1900文字を超過 |
| #13 | `services/test_autonomous_session_cleanup` | `c200681` → 追加修正 | 接続喪失時の残留・停止中に差し替わった接続や枠の誤操作 |
| #14 | `integration/test_event_loop_io` | `c200681` → 追加修正 | DB・JSON・描画待機中にイベントループが停止 |
| #15 | `services/test_gemini_service.GeminiGenerationTests` | `c200681` → 追加修正 | フォールバックが期限をリセットし、障害時にも追加呼出 |
| #16 | `utils/test_graph_fonts` | `ea0f29f` → 追加修正 | Linux候補の欠落と、共有設定を使わない全5種類のグラフ |
| #18 | `services/test_affinity_service` | `6852d84` → 追加修正 | 親密度保存が全件削除・再挿入され、書込量がユーザー数に比例 |
| #19 | `config/test_tuning_constants` / `services/test_affinity_service.AffinityTuningTests` | `6852d84` → 追加修正 | 文書・記憶・統計・音声・親密度の定数変更が利用側へ反映されない |
| #20 | `utils/test_shared_helpers` | `6852d84` → 追加修正 | 共通送信・マスク処理が使われず、グラフ送信ヘルパーでバッファが閉じない |

#17は初回確認時に#8との重複としてクローズされており、既存11件とローカルNG判定の追加1件で、対応済みの動作を確認した。再オープン後は下記の追加検証を行った。

過去の `bot.py` 向け比較だけは、その時点のAST抽出用テスト補助を使った。#7の表示用MockではWindowsのパス区切りを `Path.as_posix()` で正規化し、設定不一致そのものを確認した。現在のテストは通常のimportによる実装を検証する。

DB新規初期化・初期化の再実行・サーバーとDM設定の保存復元・副作用なしのimportは、それぞれ `core/test_database.py` と `core/test_message_handler_import.py` で検証する。Discord・Geminiの実接続、ボイス出力、電源断・プロセス強制終了はこのローカル検証に含まない。

#13は先行16テスト中15件、#14は先行5テスト中4件の停止検出と一括取得入口未実装の1件、#15は先行15テスト中17失敗・1エラー（subTestを含む）を修正前に確認した。最終テストには、保存キャンセルと後続削除の競合、描画中の収集停止・削除・再記録、応答保留と画像の後始末、実際のPNG並行生成も含む。

同日のローカル単回計測では、実PNG3枚（時間帯・ランキング・利用量）の並行生成が合計0.742秒、その間の5ms間隔のheartbeatは80回・最大間隔21.1msだった。架空の一時データによる参考値であり、本番VPSの処理時間は未測定。

## 日本語フォントの検証

`utils/test_graph_fonts.py` はWindows／Linuxそれぞれのフォント一覧を模擬し、共通候補の優先順・未導入候補の除外・汎用フォントへのフォールバックを検証する。実際の日本語フォントがあれば、全5種類のグラフを共有設定で描画し、PNG・日本語字形の欠落警告・設定の復元を確認する。Linux系候補が導入済みなら優先して検証に使う。

修正前は5テストで2失敗・3エラー、修正後はすべて成功した。日本語フォント未導入の開発環境では字形テストを省略し、`REQUIRE_JAPANESE_GRAPH_FONT=1` を指定した場合は失敗させる。LinuxのCIは `fonts-noto-cjk` 導入後、この指定で省略なしの検証を行う。導入・再起動の手順はREADMEを参照する。

## 親密度保存と調整定数の検証

`services/test_affinity_service.py` は一時DBと架空ユーザーで、1人・100人・1000人のいずれでも1件の更新に対してSQL書込が1文・変更行が1行であることを確認する。DBトリガーで削除・他ユーザーの挿入や更新を拒否し、同値の再挿入も見逃さない。DB再起動時の4項目の復元、並行更新、保存キャンセル後の順序、保存失敗後の再試行も含む。先行12テストは修正前に12失敗・3エラー（subTest込み）。その後、旧JSON取り込みと全件保存の既定値も確認する1テストを加え、最終13テストが成功した。

`config/test_tuning_constants.py` は起動前に定数を変更した状態を再現し、文書プレビューのUTF-8境界・文書記憶の参照件数・ランキング人数・EQ既定値と上下限・既定EQ名・PCMクリップを検証する。参照件数が0以下なら文書要約を載せず、小さいブロックでもIIRの状態が途切れないことも確認する。先行7テストは修正前に6失敗・1エラー（subTest込み）、修正後は全件成功した。外部サービスには接続しない。

`config/constants.py` の定義と `src/` 以下のPythonソースの参照をASTで再監査し、外部にも内部にも参照されない定数は0件と確認した。外部から直接参照されない `PROJECT_ROOT` / `NG_DIR` / `EDGE_TTS_VOICE_MALE` は定数定義内で使われる。`STATS_HOURLY_BUCKETS` は1時間ごとの24要素という保存形式を表し、今回の対応では区間数変更や既存データの再区分は行っていない。

## 共通ヘルパーの検証

`utils/test_shared_helpers.py` の18テストで、長文の分割順と送信オプション、記念日・リマインダーのメンション、要約の公開／本人限定経路、管理者権限の再確認、出力マスクと文脈別ログを検証する。グラフ送信は初回応答とdefer後のfollowupの両方を確認し、送信・画像構築の失敗やキャンセルでもバッファを回収する。利用量・ランキングのボタンからの呼出も含む。

修正前の `6852d84` のソースだけを一時展開して代表3テストを実行し、グラフ送信ヘルパーのバッファ未回収、自発会話・記念日での送信ヘルパー未使用、出力マスクヘルパー未使用の失敗を確認した。修正後は全18件が成功した。

#18〜#20の追加後、Windows・Python 3.13で日本語フォント必須の指定を有効にし、全199テストが省略なしで成功した。依存関係・全Pythonソースの構文・Python 3.10文法・図のXML・メッセージ分割も確認した。Linux、Python 3.10の実行、Discord・Geminiの実接続は未確認。

## Gemini SDKの起動互換性と通信境界

`services/test_genai_client.py`は、`enterprise`を持たない旧クライアントでも生成でき、環境変数でクラウド向けAPIが指定されてもDeveloper APIを使うことを確認する。HTTPXトランスポートを明示し、APIキーのURL・ログへの漏れ、暗黙の再送、キャンセル時の回収と終了処理も検証する。

`helpers/network_guard.py`はSDKテストのDNS・ソケット接続を遮断する。Windowsのイベントループが使うループバックは許可し、HTTPモックが使われない場合も実サービスへ接続しない。SDKによってPIL画像のMIMEがPNG/JPEGに分かれるため、画像のテストは両方の対応形式を受け付ける。

google-genai 1.46.0と2.31.0でSDK関連47件、requirements指定版の環境で全554件が成功。最終検証はHTTPモックと通信遮断下で行い、初期化・終了時の`_api_client`関連の二次例外が出ないことも確認した。実Discord・Gemini接続は未確認。

## Gemini SDKとPDFの移行検証（#21）

2026-10-16、Windows・Python 3.13の新しい仮想環境へ固定した直接依存を導入し、旧 `google-generativeai`・`PyPDF2` が入っていないことと `pip check` の成功を確認した。`services/test_genai_client.py` / `services/test_gemini_service.py` / `integration/test_sdk_migration.py` で、実際の `google-genai` の型・シリアライズと `httpx.MockTransport` を使い、通常生成・Google検索・PIL画像・ファイル転送／要約／削除・モデレーション・記憶ルーティング／圧縮・安全ブロック・利用トークン集計を確認する。PDFは `pypdf` で作った架空の平文・暗号化PDFと破損データを検証する。

検索の全段で同じ締切を共有し、非対応4xx以外の障害では追加生成せず、ユーザーのキャンセルを伝播する。SDKの既定aiohttp経路にある接続時の再送を避けるため、factoryはHTTPXの公開設定を使う。upload応答の必須状態ヘッダーが欠けた場合も、SDK内の同期sleepや再送へ進まず、応答を閉じて失敗する。ファイル転送の期限による中断、SDK終了の失敗時や重複終了時の所有HTTPX回収も確認する。

移行前の比較テストでは、モック漏れにより空のAPIキーと架空の質問で1回のリクエストが送信され、403で拒否された。実キー・会話・記憶は使っていない。以後は通信禁止ガードを加えた。最終全体テストもHTTPX・aiohttpの実通信と外部DNSを遮断し、失敗が呼出側で吸収されても通信の試行件数を別途確認する。

最終確認は全235テストが省略なしで成功し、実通信の試行は0件だった。日本語フォント必須の指定も有効にし、全Pythonソースの構文・Python 3.10文法・図のXML・CIのYAML・メッセージ分割・差分を確認した。

直接依存の固定は推移依存まで含む完全なロックではない。Windows／Linux・Python 3.10／3.13のCIは導入結果を `pip freeze --all` の成果物へ記録する。今回のローカル検証ではLinux・Python 3.10の実行と、有効な認証を用いたDiscord・Gemini・音声合成サービスでの正常動作は未確認。

## 実装説明の照合（#22）

文書添付→統計・リマインダー→ルーティングの順序、検索設定の `GuildSettings` 所属、統計のSQLite永続化、`viewer_id` の受け渡し、違反回数の定数参照は先行修正で整合済みと確認した。残るテーブル一覧・スキーマ移行例・曜日表・ボイス接続中の読み上げ・記憶カテゴリの利用・親密度更新の順序を実装に合わせた。コメント／docstringだけを変えた6ソースは、docstringを除くASTが修正前と一致することを確認した。SDK移行で変わる配線・呼出経路はCLAUDEと図も更新した。

## 配信コメントの受付枠の再確認（#17）

2026-10-16、再オープンされた#17を `c0cf91d` の実装と照合した。ローカルNG判定→同期受付→入力AIの順序は#8の修正としてmainへ反映済みだったため、処理ロジックの追加修正は行っていない。

既存12件に次の3件を追加した。

- AIで不適切と判定した入力の連投でも入力判定は1回に抑え、受付枠を返さない。期限後の安全な入力は受け付ける。
- ローカルNG判定だけで拒否した入力はAPIも枠も使わず、直後の安全な入力を受け付ける。
- 直前の受付時刻からクールダウンの1マイクロ秒前は見送り、ちょうど境界に達した入力を受け付ける。

受付の基準を「応答後」ではなく「入力AI前の受付時刻」と明記し、同じユーザーにも適用されることと、見送った入力のAI違反を記録しないことをREADME・CLAUDE・コメントへ揃えた。

コメント受付の15件を含む全238テストが成功し、省略・実通信の試行はともに0件だった。依存関係・構文・Python 3.10文法・差分も確認し、変更した2ソースの実装部分はdocstringを除いたASTが変更前と一致した。Discord・Geminiを使った実環境での再現は行っていない。
