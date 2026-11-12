import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// フォントは外部の配信元から読み込まず、ビルドに同梱する（閲覧者の通信先をこのサイトだけにする）
import "@fontsource-variable/noto-sans-jp/index.css";
import "./styles.css";
import { App } from "./App";

const container = document.getElementById("root");
if (!container) throw new Error("描画先の要素（#root）が見つかりません。");

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
