// 説明ページ（ガイド。Issue #528）の入口。setup.html などの 4 つの HTML が共通で読み込み、
// <body data-guide="…"> の値で表示するページを選ぶ（ルーターのライブラリは使わない）。
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// フォントは外部の配信元から読み込まず、ビルドに同梱する（閲覧者の通信先をこのサイトだけにする）
import "@fontsource-variable/noto-sans-jp/index.css";
import "./styles.css";
import { GuidePage } from "./components/guide/GuidePage";
import { GUIDE as operations } from "./guides/operations";
import { GUIDE as persona } from "./guides/persona";
import { GUIDE as setup } from "./guides/setup";
import type { Guide, GuideId } from "./guides/types";
import { GUIDE as usage } from "./guides/usage";

/** ページの id → 文言。content.ts の GUIDES と同じ 4 つ（tests/guides.test.mjs が照合する） */
const GUIDE_CONTENT: Record<GuideId, Guide> = { setup, persona, usage, operations };

const container = document.getElementById("root");
if (!container) throw new Error("描画先の要素（#root）が見つかりません。");

const id = document.body.dataset.guide as GuideId | undefined;
const guide = id ? GUIDE_CONTENT[id] : undefined;
if (!guide) throw new Error(`説明ページが見つかりません（data-guide="${id ?? ""}"）。`);

createRoot(container).render(
  <StrictMode>
    <GuidePage guide={guide} />
  </StrictMode>,
);
