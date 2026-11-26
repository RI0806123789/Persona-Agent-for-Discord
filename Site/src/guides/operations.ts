// 説明ページ「運用」の文言（Issue #528。旧 README の「7. 常時稼働の設定」「日本語グラフのフォント」
// 「運用ログ」「既に稼働している環境から移行する場合」「既存のデータの移行」から移した）。
// 書式とリンクの規則は types.ts の先頭を参照。値の import を足さないこと。
// ★ deploy/bot.service.example・deploy/*.py の使い方を変えたら、このページも直すこと。
// ★ 実際のパス・ユーザー名・ホスト名を書かない。<PROJECT_DIR>・<USER> などのプレースホルダで書く。
// ★ 節の id（data-migration など）は README・CONTRIBUTING.md・tools/console/README.md・
//   docs/release-notes/README.md からも指している。変えるときは参照元も直すこと。

import type { Guide } from "./types";

const REPOSITORY = "https://github.com/RI0806123789/Persona-Agent-for-Discord";

export const GUIDE: Guide = {
  id: "operations",
  title: "運用",
  lead:
    "サーバーでボットを動かし続けるための設定と、更新・古い版からの移行の手順です。" +
    "手元のパソコンで試すだけなら、このページは読まなくてもかまいません。",
  sections: [
    {
      id: "systemd",
      title: "常時稼働の設定（systemd / Linux）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "手動で起動した場合はターミナルを閉じると止まり、落ちても自動では戻りません。Linux のサーバーでは systemd に登録してください。" +
            `見本の [deploy/bot.service.example](${REPOSITORY}/blob/main/deploy/bot.service.example)（Git の管理対象）から、` +
            "実際の値を入れた `deploy/bot.service`（Git の管理対象外）を作って使います。",
        },
        {
          kind: "code",
          language: "bash",
          code: `cd <PROJECT_DIR>
cp deploy/bot.service.example deploy/bot.service
nano deploy/bot.service                     # <PROJECT_DIR> と <USER> を自分の環境の値に置き換える
sudo cp deploy/bot.service /etc/systemd/system/bot.service
sudo chmod 644 /etc/systemd/system/bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now bot             # 自動起動を有効にして起動する
systemctl status bot                        # "active (running)" なら成功`,
        },
        {
          kind: "table",
          columns: ["操作", "コマンド"],
          rows: [
            ["再起動（コードや `.env` を更新したとき）", "`sudo systemctl restart bot`"],
            ["停止 / 起動", "`sudo systemctl stop bot` / `sudo systemctl start bot`（`stop` した場合は自動で再起動しない）"],
            ["ログを追う / 直近 50 行を見る", "`journalctl -u bot -f` / `journalctl -u bot -n 50 --no-pager`"],
            [
              "ユニットを書き換えたとき",
              "`sudo cp deploy/bot.service /etc/systemd/system/bot.service` → `sudo systemctl daemon-reload` → `sudo systemctl restart bot`",
            ],
          ],
        },
        { kind: "heading", id: "systemd-notes", text: "運用上の注意" },
        {
          kind: "list",
          items: [
            "`WorkingDirectory` は必ず `src/` にし、`ExecStart` には仮想環境の `python` を絶対パスで書きます（systemd ではシェルの `activate` が効かないため）。反映先は `systemctl show bot -p WorkingDirectory --value` で確認できます。",
            "再起動の回数制限を外している（`StartLimitIntervalSec=0`）ため、設定ミスがあると**再起動を繰り返し続けます**。`systemctl status bot` が `activating (auto-restart)` のままなら、`journalctl -u bot -n 50` を確認してください。",
            "`ProtectSystem=strict` や `ProtectHome=yes` は追加しないでください。実行中に `data/memory/` と `data/ng/` へ書き込むため、記憶とモデレーションが黙って壊れます。",
            "過去に `sudo python bot.py` で動かしたことがあると、`data/memory/` が root の所有になって書き込めません。`sudo chown -R <USER>:<USER> <PROJECT_DIR>` で直せます。",
          ],
        },
      ],
    },
    {
      id: "fonts",
      title: "日本語グラフのフォント（Linux）",
      blocks: [
        {
          kind: "paragraph",
          text: "Linux で利用トークン・会話統計のグラフを使う場合は、日本語フォントを入れます（無いと日本語が欠けます）。",
        },
        {
          kind: "code",
          language: "bash",
          code: `sudo apt update
sudo apt install -y fonts-noto-cjk`,
        },
        {
          kind: "paragraph",
          text:
            "候補は `src/persona_bot/config/constants.py` の `GRAPH_FONT_FAMILY`（Yu Gothic・Meiryo・Noto Sans CJK JP・Noto Sans JP・IPAexGothic の順）で、" +
            "入っているものだけを使います。ボットと同じユーザー・仮想環境で、リポジトリのフォルダから確認できます。",
        },
        {
          kind: "code",
          language: "bash",
          code: `python -B -c "from matplotlib import font_manager; font_manager.findfont(font_manager.FontProperties(family=['Noto Sans CJK JP']), fallback_to_default=False); print('日本語フォント: OK')"
REQUIRE_JAPANESE_GRAPH_FONT=1 python -B -m unittest discover -s tests -p test_graph_fonts.py -v`,
        },
        {
          kind: "paragraph",
          text:
            "入れても見つからないときは、`python -c \"import matplotlib; print(matplotlib.get_cachedir())\"` のフォルダの中の `fontlist-v*.json` だけを退避して確認し直し、" +
            "`sudo systemctl restart bot` で再起動してください。",
        },
      ],
    },
    {
      id: "logs",
      title: "運用ログ",
      blocks: [
        {
          kind: "paragraph",
          text:
            "ログは時刻・レベル・モジュール名付きで標準出力へ出ます（systemd では journald）。" +
            "利用者・サーバー・チャンネルの ID、会話・添付の名前・応答の本文、例外の本文や絶対パスは出さず、例外は型と発生箇所（ファイル名・関数名・行番号）だけを残します。",
        },
        {
          kind: "paragraph",
          text:
            "会話件数とトークン数の集計用の `User (会話)：`・`Total token count：` と、起動の確認に使う「ログイン成功」の文言は変えていません。" +
            "ログを公開するときは、管理コンソールの伏せ字と公開前の確認を使ってください。",
        },
      ],
    },
    {
      id: "update",
      title: "更新する",
      blocks: [
        {
          kind: "paragraph",
          text: "コードを新しくしたら、依存のパッケージも入れ直してから再起動します（依存が変わっていなくても、同じコマンドで問題ありません）。",
        },
        {
          kind: "code",
          language: "bash",
          code: `cd <PROJECT_DIR>
git pull
.venv/bin/python -m pip install -r requirements.txt
sudo systemctl restart bot`,
        },
        {
          kind: "list",
          items: [
            `開発者用ダッシュボード（[tools/console/README.md](${REPOSITORY}/blob/main/tools/console/README.md)）からもデプロイできます。依存が変わったときだけ入れ直し、更新の後に起動できなければ前の版へ戻します。`,
            "**ver0.1.5 以前から**: 設定ファイルが `.env` に変わりました。「[旧設定から .env を作る](#migrate-config)」を行います。",
            "**ver0.1.7 以前から**: キャラ別のファイルが Git の管理対象外になり、更新で削除されます。**更新の前に**「[ペルソナの作り方](./persona.html#update-from-0-1-7)」の手順で退避してください。",
            "**ver0.1.8 以前から**: 保存データの置き場所が `data/` の下に変わりました。「[既存のデータの移行](#data-migration)」を行います。",
          ],
        },
      ],
    },
    {
      id: "migrate-config",
      title: "旧設定から .env を作る（ver0.1.5 以前からの更新）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "旧フォルダ `index/` に残っている `config_private.py` から `.env` を作れます。旧ファイルは import せず構文解析だけで読み、作った `.env` の権限は `600` になります。" +
            "移行した後は `index/config_private.py` を削除してかまいません。",
        },
        {
          kind: "code",
          language: "bash",
          code: `python3 deploy/migrate_config_to_env.py             # .env を作る（既にあれば上書きせず中断）
python3 deploy/migrate_config_to_env.py --dry-run   # 書き込まずに取得できるキー名だけ確認（値は表示しない）
python3 deploy/migrate_config_to_env.py --force     # 既存の .env を上書き`,
        },
      ],
    },
    {
      id: "data-migration",
      title: "既存のデータの移行（ver0.1.9）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "`memory/`・`ng/` は `data/memory/`・`data/ng/` へ移りました（新しく導入する場合は要りません）。" +
            "既存の環境では、記憶・設定・違反の記録を引き継ぐため、次の順で移します。",
        },
        {
          kind: "list",
          ordered: true,
          items: [
            "ボットを停止します（systemd では `sudo systemctl stop bot`）。移行中は管理コンソールからも起動・デプロイしないでください。",
            "念のため、旧 `memory/`・`ng/` をリポジトリの外の非公開の場所へ丸ごとコピーしておきます。",
            "コードを更新し、リポジトリのフォルダで確認してから移します（下のコマンド）。",
            "いつもの手順（または `sudo systemctl start bot`）で起動します。",
          ],
        },
        {
          kind: "code",
          language: "bash",
          code: `python deploy/migrate_data_directory.py --check  # 確認のみ。データは変更しない
python deploy/migrate_data_directory.py          # バックアップ・内容の照合の後に移動`,
        },
        {
          kind: "paragraph",
          text:
            "スクリプトは標準ライブラリだけで動き、旧ディレクトリを `data/.migration-backups/` へバックアップしてから移します。" +
            "移行先に内容の違う同じ名前のファイルがあると移動の前に止まるので、どちらを残すか決めてから実行し直してください。" +
            "移行済みの環境で実行し直してもデータは変わりません。旧配置が残っている間は、空の DB で動き始めないよう、ボットは起動を止めて移行を案内します。",
        },
      ],
    },
  ],
};
