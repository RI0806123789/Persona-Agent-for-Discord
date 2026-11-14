// 公開ページの文言が、Issue #410 の決定事項と公開時の約束を守っていることを確かめる。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import test from "node:test";
import * as content from "../src/content.ts";
import { buildTimeline } from "../src/demoTimeline.ts";

const REPOSITORY_PREFIX = "https://github.com/RI0806123789/Persona-Agent-for-Discord";

/** 入れ子の値から、文字列をすべて取り出す */
function collectStrings(value, found = []) {
  if (typeof value === "string") found.push(value);
  else if (Array.isArray(value)) value.forEach((item) => collectStrings(item, found));
  else if (value && typeof value === "object") Object.values(value).forEach((item) => collectStrings(item, found));
  return found;
}

const ALL_STRINGS = collectStrings({ ...content });

test("外部へのリンクは、このリポジトリの中だけを指す", () => {
  const urls = ALL_STRINGS.filter((text) => /^[a-z][a-z0-9+.-]*:/i.test(text));
  assert.ok(urls.length > 0);
  for (const url of urls) {
    assert.ok(url.startsWith(REPOSITORY_PREFIX), `リポジトリの外を指すリンクがあります: ${url}`);
  }
});

test("blob/main のリンク先のファイルが、リポジトリに実際にある", () => {
  // 実在しないファイルを指すと GitHub で 404 になる（#452。LICENSE.md のリンク切れの再発防止）
  const links = Object.entries(content.LINKS);
  assert.ok(links.length > 0);
  for (const [name, url] of links) {
    const match = /^https:\/\/github\.com\/[^/]+\/[^/]+\/blob\/main\/(.+)$/.exec(url);
    if (!match) continue;
    const file = new URL(`../../${match[1]}`, import.meta.url);
    const exists = existsSync(file);
    assert.ok(exists, `LINKS.${name} の行き先がリポジトリにありません: ${match[1]}`);
  }
});

test("ボットの招待 URL を載せない", () => {
  for (const text of ALL_STRINGS) {
    assert.doesNotMatch(text, /discord\.gg|discord(?:app)?\.com\/(?:oauth2|invite|api)/i);
  }
});

test("ID・メールアドレス・キー・ローカルのパスに見える値を載せない", () => {
  const patterns = [
    /[0-9]{17,20}/, // Discord の ID
    /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/, // メールアドレス
    /AIza[0-9A-Za-z_-]{20,}/, // Gemini の API キー
    /[A-Za-z]:[\\/]Users[\\/]|\/home\//, // OS のユーザー名を含むパス
  ];
  for (const text of ALL_STRINGS) {
    for (const pattern of patterns) assert.doesNotMatch(text, pattern);
  }
});

test("疑似 Discord 画面の場面は、登場人物がそろっていて再生できる", () => {
  const ids = new Set();
  for (const scenario of content.DEMO_SCENARIOS) {
    assert.ok(!ids.has(scenario.id), `場面の ID が重複しています: ${scenario.id}`);
    ids.add(scenario.id);
    assert.ok(scenario.label && scenario.caption && scenario.channelName);
    assert.ok(scenario.messages.length > 0);
    for (const message of scenario.messages) {
      assert.ok(content.DEMO_CAST[message.author], `登場人物が未定義です: ${message.author}`);
      assert.ok(message.text.trim());
    }
    const timeline = buildTimeline(scenario.messages);
    assert.ok(timeline.totalMs > 0 && timeline.totalMs < 30_000, "1 場面は 30 秒以内に収める");
  }
});

test("ページ内リンクの行き先が、画面のどこかにある", async () => {
  const app = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const anchors = ALL_STRINGS.filter((text) => /^#[a-z-]+$/.test(text));
  assert.ok(anchors.length > 0);
  for (const anchor of new Set(anchors)) {
    assert.match(app, new RegExp(`id="${anchor.slice(1)}"`), `${anchor} の行き先がありません`);
  }
});

test("免責事項で、ペルソナが架空であることを伝える", () => {
  assert.match(content.DISCLAIMER.body, /架空のキャラクター/);
  assert.match(content.DISCLAIMER.body, /開発者の見解を反映するものではありません/);
});

test("アクセスの計測や外部の読み込みを入れていない", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  // 外部から読み込むスクリプト・スタイル・フォントが無いこと（閲覧者の通信先をこのサイトだけにする）
  assert.doesNotMatch(html, /(?:src|href)="(?:https?:)?\/\//);
  assert.doesNotMatch(html, /gtag|googletagmanager|google-analytics|plausible|clarity/i);
});
