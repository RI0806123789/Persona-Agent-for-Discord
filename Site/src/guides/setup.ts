// 説明ページ「セットアップ」の文言（Issue #528。旧 README の「セットアップ 1〜6」から移した）。
// 書式とリンクの規則は types.ts の先頭を参照。値の import を足さないこと。
// ★ 手順・既定値を変えたら、このページも直すこと（本文が実装と食い違わないことが最優先）。
// ★ 実際のパス・ユーザー名・キーを書かない。<PROJECT_DIR> などのプレースホルダで書く。

import type { Guide } from "./types";

export const GUIDE: Guide = {
  id: "setup",
  title: "セットアップ",
  lead:
    "自分のパソコンやサーバーで、自分のボットとして動かすまでの手順です。" +
    "Windows と Linux（Ubuntu など）の両方で動きます。",
  sections: [
    {
      id: "requirements",
      title: "1. 事前に用意するもの",
      blocks: [
        {
          kind: "list",
          items: [
            "**Python 3.11 以上**",
            "**Discord のボットのトークン**（Discord Developer Portal `https://discord.com/developers/applications` で取得。次の「[Discord 側の準備](#discord)」）",
            "**Gemini の API キー**（Google AI Studio `https://aistudio.google.com/app/apikey` で取得）",
            "**Opus 共有ライブラリ**（Linux でボイスを使う場合だけ。手順 5）",
            "**日本語フォント**（Linux でグラフを使う場合だけ。「[運用](./operations.html#fonts)」の日本語グラフのフォント）",
          ],
        },
        {
          kind: "note",
          tone: "info",
          title: "FFmpeg は要りません",
          text: "読み上げの音声の変換は、Python のパッケージ `miniaudio` がプロセスの中で行います。",
        },
        {
          kind: "paragraph",
          text:
            "このページから外部のサイトへはリンクしていません。取得先は名前と URL だけを書いているので、" +
            "必要に応じてブラウザに入力して開いてください。",
        },
      ],
    },
    {
      id: "discord",
      title: "2. Discord 側の準備",
      blocks: [
        {
          kind: "list",
          ordered: true,
          items: [
            "Discord Developer Portal でアプリケーションを作り、**Bot** の画面でトークンを発行します（手順 6 で `.env` に書きます）。トークンは誰にも見せないでください。",
            "同じ画面の **Privileged Gateway Intents** で、**Message Content Intent** を有効にします（メンションへの返事や会話の履歴に発言の本文を使うため。無効のままだと起動できません）。",
            "**OAuth2** の URL 生成で、スコープに `bot` と `applications.commands` を選び、作った URL から自分のサーバーへボットを招待します。",
          ],
        },
        {
          kind: "paragraph",
          text: "ボットに与える主な権限は次のとおりです。使わない機能の権限は外してかまいません。",
        },
        {
          kind: "table",
          columns: ["使う場面", "権限"],
          rows: [
            ["会話・要約・話題の提供・リマインダー", "チャンネルを見る・メッセージを送信・メッセージ履歴を読む"],
            ["返信の評価（👍👎）", "リアクションの追加"],
            ["サブエージェントの作業スレッド", "公開スレッドの作成・スレッドでメッセージを送信・ウェブフックの管理"],
            ["音声会話（`/join`）・自律行動のボイス", "接続・発言"],
            ["自律行動のステージ", "メンバーをミュート（配信の枠を自分で開くには、さらにチャンネルの管理・メンバーの移動）"],
          ],
        },
      ],
    },
    {
      id: "python",
      title: "3. Python と仮想環境の準備",
      blocks: [
        {
          kind: "paragraph",
          text:
            "**Windows**: python.org（`https://www.python.org/downloads/`）から Python をインストールし" +
            "（「Add python.exe to PATH」にチェック）、プロジェクトのフォルダで実行します。",
        },
        {
          kind: "code",
          language: "powershell",
          title: "Windows（PowerShell）",
          code: String.raw`python -m venv .venv
.venv\Scripts\Activate.ps1`,
        },
        {
          kind: "paragraph",
          text: "**Ubuntu / Debian**: `venv` は Python 本体と別のパッケージなので、一緒に入れます。",
        },
        {
          kind: "code",
          language: "bash",
          title: "Ubuntu / Debian",
          code: `sudo apt update
sudo apt install -y python3 python3-pip python3-venv
python3 -m venv .venv
source .venv/bin/activate`,
        },
      ],
    },
    {
      id: "packages",
      title: "4. パッケージのインストール",
      blocks: [
        { kind: "paragraph", text: "仮想環境を有効にした状態で実行します。" },
        { kind: "code", language: "bash", code: "python -m pip install -r requirements.txt" },
        {
          kind: "list",
          items: [
            "直接依存は `requirements.txt`、推移依存は `constraints.txt` で版を固定しています。`requirements.txt` の末尾の `-c constraints.txt` で一緒に効くので、上のコマンドだけで同じ版が入ります。導入結果は `python -m pip check` で確認できます。",
            "`tzdata` も入るので、タイムゾーンの DB が無い Windows でも日本時間を使えます。「タイムゾーン(Asia/Tokyo)の読み込みに失敗」と出たら、このインストールをやり直してボットを再起動してください。",
            "旧 SDK を使っていた環境は、新しい仮想環境へ入れ直してください（Gemini は `google-genai`、PDF は `pypdf` を使います。`.env` はそのまま使えます）。参考: Google の移行ガイド `https://ai.google.dev/gemini-api/docs/migrate`・pypdf の履歴 `https://pypdf.readthedocs.io/en/stable/meta/history.html`",
          ],
        },
      ],
    },
    {
      id: "opus",
      title: "5. Opus ライブラリの準備（Linux のみ）",
      blocks: [
        {
          kind: "paragraph",
          text: "ボイスチャンネルの機能（`/join`・自律行動）に必要です。Windows は discord.py が DLL を同梱しているため要りません。",
        },
        { kind: "code", language: "bash", code: "sudo apt install -y libopus0" },
        {
          kind: "note",
          tone: "warning",
          title: "discord.py は必ず [voice] 付きで",
          text:
            "`requirements.txt` では指定済みです。Discord はボイスの E2E 暗号化（DAVE）を必須にしており、" +
            "対応パッケージの `davey` が無いと `/join` が `RuntimeError: davey library needed in order to use voice` で失敗します。",
        },
      ],
    },
    {
      id: "env",
      title: "6. API キーの設定（.env）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "API キーは**プロジェクトのフォルダの直下の `.env`** に書きます（`src/` の中ではありません）。" +
            "見本をコピーし、`=` の右側を実際のキーに書き換えます。",
        },
        { kind: "code", language: "powershell", title: "Windows（PowerShell）", code: "Copy-Item .env.example .env" },
        { kind: "code", language: "bash", title: "Ubuntu / Debian", code: "cp .env.example .env" },
        {
          kind: "code",
          language: "text",
          title: ".env",
          code: `API_KEY_GEMINI=（Google AI Studio で取得したキー）
TOKEN_DISCORD=（Discord Developer Portal で取得したトークン）`,
        },
        {
          kind: "list",
          items: [
            "`=` の前後に空白を入れないでください（クォートは要りません）。実際のキーを見本の `.env.example` に書かないでください。",
            "キーが未設定（ファイルが無い・値が空・見本のまま）だと、`キー未設定` と表示して**起動を止めます**。",
            "OS の環境変数に同じ名前のキーがあれば、そちらが優先されます（systemd の `EnvironmentFile=` などでも渡せます）。",
            "ver0.1.5 以前の `config_private.py` から移す場合は、「[運用](./operations.html#migrate-config)」の手順で `.env` を作れます。",
          ],
        },
      ],
    },
    {
      id: "start",
      title: "7. ボットの起動",
      blocks: [
        { kind: "paragraph", text: "仮想環境の Python を指定して、**`src/` の中から**起動します。" },
        {
          kind: "code",
          language: "powershell",
          title: "Windows（PowerShell）",
          code: String.raw`cd src
..\.venv\Scripts\python.exe bot.py`,
        },
        {
          kind: "code",
          language: "bash",
          title: "Ubuntu / Debian",
          code: `cd src
../.venv/bin/python bot.py`,
        },
        {
          kind: "paragraph",
          text: "仮想環境を有効にしていれば `python bot.py` でも起動できます。`ログイン成功: BotName#0000` と表示されれば起動できています。",
        },
        {
          kind: "list",
          items: [
            "キャラクターのプロンプトは同梱していないため、作るまではキャラ付けのない標準のプロンプトで応答します。作り方は「[ペルソナの作り方](./persona.html)」へ。",
            "ターミナルを閉じるとボットは止まります。サーバーで動かし続けるには「[運用](./operations.html#systemd)」の常時稼働の設定を行ってください。",
            "ボイス・ステージで動かすときの権限や、各機能の使い方は「[使い方](./usage.html)」にあります。",
          ],
        },
      ],
    },
  ],
};
