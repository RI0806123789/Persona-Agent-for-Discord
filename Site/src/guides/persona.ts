// 説明ページ「ペルソナの作り方」の文言（Issue #528。旧 README の「🎭 ペルソナ（キャラクター）を作る」から移した）。
// 書式とリンクの規則は types.ts の先頭を参照。値の import を足さないこと。
// ★ テンプレートの名前（00_prompt_format.txt / 00_prompt_format.json）や personas.json の項目を変えたら、
//   このページも直すこと（CLAUDE.md の「ペルソナの一覧」の項）。
// ★ 特定のキャラクターの名前・口調・プロンプトを載せない（キャラ別のファイルを公開しない方針。#189）。
//   例は架空の「わたしのキャラ」だけにする。

import type { Guide } from "./types";

const REPOSITORY = "https://github.com/RI0806123789/Persona-Agent-for-Discord";

export const GUIDE: Guide = {
  id: "persona",
  title: "ペルソナの作り方",
  lead:
    "ボットがなりきるキャラクター（ペルソナ）は、性格・口調を書いたテキストのファイルで作ります。" +
    "表示名や声も、ファイル 1 つで決められます。",
  sections: [
    {
      id: "overview",
      title: "同梱しているのは見本だけ",
      blocks: [
        {
          kind: "paragraph",
          text:
            `リポジトリに含めているのは、プロンプトの書式見本 [prompts/00_prompt_format.txt](${REPOSITORY}/blob/main/prompts/00_prompt_format.txt) と、` +
            `表示名・声の設定（\`personas.json\`）の書式見本 [prompts/00_prompt_format.json](${REPOSITORY}/blob/main/prompts/00_prompt_format.json) だけです。` +
            "キャラクターのプロンプトと設定は、これらの見本をもとに各自で作ります。",
        },
        {
          kind: "paragraph",
          text:
            "`prompts/` に作ったファイルは Git の管理対象外（`.gitignore` 済み）なので、コミットされず、`git pull` で上書きも削除もされません。" +
            "作るまでは、キャラ付けのない標準のプロンプトで応答します。",
        },
      ],
    },
    {
      id: "create",
      title: "作り方",
      blocks: [
        {
          kind: "list",
          ordered: true,
          items: [
            "見本をコピーして、キャラクターのファイルを作ります（ファイル名は自由。日本語も使えます）。",
            "各項目にキャラクターの設定・口調・応答のルールを書き込みます（UTF-8 で保存。メモ帳の「UTF-8 (BOM 付き)」でも読めます）。",
            "Discord で `/functions` → ⚙️ キャラ設定 を開くと、👤 ペルソナに表示されます。選んで「確定」を押してください（再起動は要りません）。",
          ],
        },
        {
          kind: "code",
          language: "powershell",
          title: "Windows（PowerShell）",
          code: "Copy-Item prompts/00_prompt_format.txt prompts/my_character.txt",
        },
        {
          kind: "code",
          language: "bash",
          title: "Linux / macOS",
          code: "cp prompts/00_prompt_format.txt prompts/my_character.txt",
        },
        {
          kind: "list",
          items: [
            "`prompts/` の直下の `.txt` がすべて選択肢になります（見本と、`.` で始まるファイルは除きます）。表示名は拡張子を除いたファイル名、並び順はファイル名の順です。",
            "選択肢に出せるのは先頭の 25 件までです（Discord の制限）。ファイル名は拡張子を含めて 89 文字までにしてください。",
            "各サーバーの選択はファイル名で保存されます。ファイル名を変えると、そのペルソナを選んでいたサーバーは既定のペルソナで応答するようになります。",
          ],
        },
      ],
    },
    {
      id: "personas-json",
      title: "表示名・声・既定のペルソナ（任意）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "`prompts/personas.json` を作ると、表示名・声（音声会話と自律配信の読み上げ）・既定のペルソナを決められます（無くても動きます）。",
        },
        {
          kind: "code",
          language: "powershell",
          title: "Windows（PowerShell）",
          code: "Copy-Item prompts/00_prompt_format.json prompts/personas.json",
        },
        {
          kind: "code",
          language: "bash",
          title: "Linux / macOS",
          code: "cp prompts/00_prompt_format.json prompts/personas.json",
        },
        {
          kind: "paragraph",
          text:
            "コピーしたら、ファイル名・表示名・声を自分のキャラクターに合わせて書き換えます（UTF-8 で保存）。" +
            "キャラの数だけ `personas` の中の項目を増やし、使わない項目は消します。" +
            "見本そのものは読み込まれず、先頭の `_説明`（各項目の説明）は読み込むときに無視されます。キャラクター 1 人分の例です。",
        },
        {
          kind: "code",
          language: "json",
          title: "prompts/personas.json",
          code: `{
  "default": "my_character.txt",
  "personas": {
    "my_character.txt": {
      "name": "わたしのキャラ",
      "voice": { "voice": "ja-JP-NanamiNeural", "pitch": "+10Hz", "rate": "+5%", "eq": "bright", "live_voice": "Kore" }
    }
  }
}`,
        },
        {
          kind: "table",
          columns: ["項目", "内容"],
          rows: [
            ["`default`", "まだペルソナを選んでいないサーバーで使うファイル。省略すると選択肢の先頭"],
            ["`personas`", "ファイル名ごとの設定。書いた順に選択肢へ並び、書いていないファイルはその後ろにファイル名の順で続く"],
            ["`name`", "表示名（省略するとファイル名）"],
            [
              "`voice.live_voice`",
              "`/join` の音声会話（Gemini Live）の声の名前（例: `Kore`・`Puck`・`Charon`）。省略したときや、モデルが受け付けない名前のときは、モデルの既定の声",
            ],
            [
              "`voice.voice`",
              "自律配信の読み上げの話者。`ja-JP-NanamiNeural`（女性）/ `ja-JP-KeitaNeural`（男性）。下の `pitch` / `rate` / `eq` も読み上げ専用",
            ],
            [
              "`voice.pitch`",
              "声の高さ（`+10Hz` の形式）。Nanami は 10Hz でおよそ半音 1 つ分。Keita は上げると +6 半音付近で頭打ちになり、下げてもほとんど変わらない",
            ],
            ["`voice.rate`", "話す速さ（`+5%` の形式）"],
            ["`voice.eq`", "音色。`flat`（加工なし）/ `bright`（明るい）/ `warm`（落ち着いた）/ `robotic`（機械的）"],
            [
              "`agent`",
              "サブエージェントの作業中に出す定型文（AI は使わない）と、作業スレッドでの表示名（`name`）・アイコン（`avatar_url`）。項目と差し込める値（`{role}`・`{step}` など）は見本の `_説明` を参照",
            ],
          ],
        },
        {
          kind: "list",
          items: [
            "省略した項目は既定値（`ja-JP-NanamiNeural`・`+0Hz`・`+0%`・`flat`・モデルの既定の声・標準の丁寧語の文面）になります。",
            "形式が誤っている項目はログに警告を出し、その項目だけ既定値を使います。JSON として読めないときは、ファイルを書き換えずに、表示名と声の設定を使わずに動きます。",
            "`personas.json` の変更も、⚙️ キャラ設定を開き直すと反映されます。",
          ],
        },
      ],
    },
    {
      id: "transfer",
      title: "サーバー（Linux）へ送る",
      blocks: [
        {
          kind: "paragraph",
          text:
            "パソコンで作ったファイルは Git では届かないので、SCP か SFTP で送ります（Windows 10 以降は PowerShell から `scp` / `sftp` を使えます）。" +
            "**見本の `00_prompt_format.txt`・`00_prompt_format.json` は送らないでください**（サーバー側の Git の管理対象のファイルが変わり、管理コンソールのデプロイが止まります）。",
        },
        {
          kind: "code",
          language: "bash",
          code: "scp prompts/my_character.txt prompts/personas.json <USER>@<HOST>:<PROJECT_DIR>/prompts/",
        },
        {
          kind: "list",
          items: [
            "送った後は ⚙️ キャラ設定を開き直すと反映されます（`sudo systemctl restart bot` でもかまいません）。",
            "ボットを動かすユーザーと別のユーザーで送った場合は、`sudo chown <USER>:<USER> <PROJECT_DIR>/prompts/*` で所有者を合わせてください。",
            "取り戻すときは `scp <USER>@<HOST>:<PROJECT_DIR>/prompts/my_character.txt prompts/` のように、送り先と送り元を入れ替えます。",
          ],
        },
      ],
    },
    {
      id: "update-from-0-1-7",
      title: "既存の環境を更新する（ver0.1.7 以前から）",
      blocks: [
        {
          kind: "paragraph",
          text:
            "ver0.1.7 までは、キャラ別のファイル（`prompts/prompt_*.txt`）を Git で管理していました。ver0.1.8 から Git の管理対象外になったため、" +
            "**更新（`git pull`・管理コンソールのデプロイ）の際に、手元のこれらのファイルが削除されます**。" +
            "パソコン・サーバーとも、更新の前にリポジトリの外へ退避し、更新の後に同じファイル名で戻してください（各サーバーで選んでいたペルソナはそのまま引き継がれます）。",
        },
        {
          kind: "code",
          language: "powershell",
          title: "Windows（PowerShell。リポジトリのフォルダで実行）",
          code: String.raw`Copy-Item prompts "$HOME\persona-prompts-backup" -Recurse        # 1. 退避（同じ名前のフォルダが無いことを先に確認）
git pull                                                          # 2. 更新
Copy-Item "$HOME\persona-prompts-backup\prompt_*.txt" prompts\     # 3. キャラ別のファイルだけを戻す`,
        },
        {
          kind: "code",
          language: "bash",
          title: "Linux（サーバー）",
          code: `cd <PROJECT_DIR>
cp -a prompts ~/persona-prompts-backup                    # 1. 退避（管理コンソールでデプロイする場合も、その前に行う）
git pull                                                  # 2. 更新（またはデプロイ）
cp -p ~/persona-prompts-backup/prompt_*.txt prompts/       # 3. キャラ別のファイルだけを戻す
sudo systemctl restart bot                                # 4. 読み直す`,
        },
        {
          kind: "list",
          items: [
            "戻すまでの間も、各サーバーのペルソナの選択は消えません（その間は既定のペルソナで応答します）。",
            "表示名と声は `personas.json` で設定します。ver0.1.7 と同じにしたい場合は、ver0.1.7 の `src/persona_bot/config/choices.py`（`PERSONA_CHOICES`）と `constants.py`（`VOICE_PROFILES`・`DEFAULT_PROMPT_FILE`）を参考に書き、サーバーにも送ってください。",
            "戻し終えたら、`git status` に `prompts/` のファイルが出ないことを確認できます。",
          ],
        },
      ],
    },
  ],
};
