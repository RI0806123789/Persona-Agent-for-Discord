# 紹介サイト（Site/）

Persona Agent for Discord の紹介用ホームページです（Issue #410）。React + TypeScript + Vite で作った 1 ページの静的サイトで、GitHub Pages で公開します。ボット本体（`src/`）とは依存関係もビルドも分かれています。

## 決めたこと

| 項目 | 内容 |
| --- | --- |
| 公開先 | GitHub Pages（GitHub Actions でビルドして公開） |
| 対象 | 技術者ではなく、一般の利用者 |
| 配色 | 紹介動画と同じ「暗い地＋ティール」を土台に、琥珀色とクリーム色で温かみを足す。ライト／ダーク両対応（端末の設定に従い、右上のボタンで切り替え） |
| 動作イメージ | 実際の画面ではなく、疑似的な Discord 画面のアニメーション（登場人物・会話はすべて架空） |
| 紹介動画 | 載せない |
| ボットの招待 | 載せない（自分でホストする前提の説明にとどめる） |
| アクセスの計測 | 行わない。外部のスクリプト・フォント・画像も読み込まない |
| 言語 | 日本語のみ |

## 開発

Node.js 24 を使います（CI と同じ）。

```bash
cd Site
npm ci           # 固定済みの依存を入れる
npm run dev      # http://127.0.0.1:5174 で開発用サーバーを起動
npm test         # 外部通信不要の回帰テスト
npm run build    # 型の検査と本番ビルド（dist/ に出力）
npm run preview  # http://127.0.0.1:4174 でビルド結果を確認
```

## 構成

```text
Site/
├── index.html            # 最初の描画より前に配色を決めるスクリプトを含む
├── public/               # そのまま配信するファイル（favicon）
├── src/
│   ├── content.ts        # 画面に出す文言の唯一の置き場所
│   ├── demoTimeline.ts   # 疑似 Discord 画面の再生順（表示に依存しない計算）
│   ├── App.tsx           # ページの構成（ヘッダー・各セクション・フッター）
│   ├── components/       # 疑似 Discord 画面・アイコン・出現アニメーション
│   ├── hooks.ts          # 配色の切り替え・動きを減らす設定・画面内の判定
│   └── styles.css        # 配色のトークンとスタイル
└── tests/                # Node.js 標準のテスト
```

## 文言を直すとき

- 画面の文言は **`src/content.ts` だけ**を直します。部品（`App.tsx` など）に文を直接書かないこと。
- **ボットの機能を足したり既定値を変えたりしたら、README・ヘルプ（`src/persona_bot/views/help_view.py`）と合わせて `content.ts` も直します。**
- `content.ts` の「制作背景」「目的」（`STORY`・`PURPOSE`）は**仮の文**です。ボットの機能から言える範囲の下書きなので、公開する前に制作者が書き直してください。
- 公開ページなので、個人情報・Discord の ID・API キー・サーバーの実パス・実際の会話や記憶・キャラ別のプロンプトを書かないこと。疑似 Discord 画面の登場人物と会話は架空のものにします。`tests/content.test.mjs` が、ID・メールアドレス・キー・招待 URL・リポジトリ外へのリンクに見える値を検出します。
- 色は `styles.css` の先頭のトークン（`--accent` など）から使います。部品に直接の色を書くと、ライト／ダークの片方で読めなくなります。

## 依存

管理コンソール（`tools/console/frontend`）と**同じ版だけ**を使っています（React・Vite・TypeScript・Noto Sans JP）。リポジトリに新しいパッケージを増やさないための方針で、UI ライブラリや CSS フレームワークは入れていません。依存を足す前に相談してください（公開から 2 週間以上たった版に、`.npmrc` の `save-exact` で固定します）。

更新の提案（Dependabot）と脆弱性の検査（`dependency-audit.yml` の `npm audit`）は、管理コンソールと同じ設定の対象です。

## 公開の手順

`.github/workflows/site.yml` が、`Site/` を変えた PR と push でテストとビルドを確かめます。**公開は、次の設定をした後の `main` でだけ動きます**（マージしただけでは公開されません）。

1. リポジトリの **Settings → Pages → Build and deployment → Source** を「**GitHub Actions**」にする。
2. **Settings → Secrets and variables → Actions → Variables** に、リポジトリの変数 `SITE_DEPLOY_ENABLED` を値 `true` で登録する。
3. **Actions → 紹介サイト → Run workflow** を `main` で実行する（以後は、`main` の `Site/` が変わるたびに自動で公開されます）。
4. 公開された URL を、リポジトリの **About → Website**（`homepage` 欄）に設定する。

公開を止めるときは、変数 `SITE_DEPLOY_ENABLED` を消すか `true` 以外にします（公開済みのページを消すには、Pages の設定で公開を解除します）。

`vite.config.ts` の `base` は相対パス（`./`）なので、リポジトリ名や独自ドメインが変わってもビルドし直さずに動きます。
