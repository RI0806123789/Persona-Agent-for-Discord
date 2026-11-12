import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// GitHub Pages では https://<アカウント>.github.io/<リポジトリ名>/ の下で配信される。
// base を相対パスにしておくと、リポジトリ名や独自ドメインが変わってもビルドし直さずに動く
// （1ページ完結でルーターを使わないため、相対パスで困る場面が無い）。
export default defineConfig({
  base: "./",
  plugins: [react()],
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
