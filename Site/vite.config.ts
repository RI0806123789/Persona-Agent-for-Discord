import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { GUIDES } from "./src/content";

// GitHub Pages では https://<アカウント>.github.io/<リポジトリ名>/ の下で配信される。
// base を相対パスにしておくと、リポジトリ名や独自ドメインが変わってもビルドし直さずに動く
// （ページは Site/ の直下に並べた HTML だけで、ルーターを使わないため、相対パスで困る場面が無い）。
//
// ビルドの入口は、トップページ（index.html）と説明ページ（Issue #528）の HTML。説明ページの一覧は
// content.ts の GUIDES が唯一の置き場所で、ここではそれを読むだけにする（ページを足したときに入口の書き忘れを防ぐ）。
// ★ 入口のパスは作業フォルダ（npm のスクリプトでは Site/）からの相対で書く。Site/ の外から vite を動かさないこと。
const pages: Record<string, string> = {
  index: "./index.html",
  ...Object.fromEntries(GUIDES.map((guide) => [guide.id, `./${guide.id}.html`])),
};

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    rolldownOptions: {
      input: pages,
    },
  },
  server: {
    host: "127.0.0.1",
    // 管理コンソール（5173）と同時に開けるよう、番号をずらしている
    port: 5174,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: 4174,
    strictPort: true,
  },
});
