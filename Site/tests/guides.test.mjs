// 説明ページ（ガイド。Issue #528）が、ページの一覧・入口の HTML・公開時の約束（外部へリンクしない・
// 個人情報やキーを載せない）を守っていることを確かめる。
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import * as content from "../src/content.ts";
import { collectInlineLinks, parseInline, splitShellComment } from "../src/guides/markup.ts";

const REPOSITORY_PREFIX = "https://github.com/RI0806123789/Persona-Agent-for-Discord";

/**
 * 文字としてだけ書いてよい外部の URL（Issue #528 の決定: サイトから外部へはリンクしない）。
 * ★ ここに足した URL も、リンク（href・[文字](行き先)）にはできない。足すのは、キーの取得先など、
 *   読む人が自分で開く必要のある公式のページだけにする。
 */
const TEXT_ONLY_URLS = new Set([
  "https://discord.com/developers/applications",
  "https://aistudio.google.com/app/apikey",
  "https://www.python.org/downloads/",
  "https://ai.google.dev/gemini-api/docs/migrate",
  "https://pypdf.readthedocs.io/en/stable/meta/history.html",
]);

const siteFile = (name) => new URL(`../${name}`, import.meta.url);
const readSite = async (name) => (await readFile(siteFile(name), "utf8")).replaceAll("\r\n", "\n");

/** 入れ子の値から、文字列をすべて取り出す */
function collectStrings(value, found = []) {
  if (typeof value === "string") found.push(value);
  else if (Array.isArray(value)) value.forEach((item) => collectStrings(item, found));
  else if (value && typeof value === "object") Object.values(value).forEach((item) => collectStrings(item, found));
  return found;
}

/** 入れ子の値から、href という名前の値をすべて取り出す */
function collectHrefs(value, found = []) {
  if (Array.isArray(value)) value.forEach((item) => collectHrefs(item, found));
  else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      if (key === "href" && typeof item === "string") found.push(item);
      else collectHrefs(item, found);
    }
  }
  return found;
}

const guides = await Promise.all(
  content.GUIDES.map(async (link) => ({ link, guide: (await import(`../src/guides/${link.id}.ts`)).GUIDE })),
);

/** ページごとの、リンクの行き先にできる id（節と小見出し） */
const anchorsByPage = new Map(
  guides.map(({ link, guide }) => [
    `${link.id}.html`,
    new Set(
      guide.sections.flatMap((section) => [
        section.id,
        ...section.blocks.filter((block) => block.kind === "heading").map((block) => block.id),
      ]),
    ),
  ]),
);

test("説明ページの一覧と、Site/ の直下の HTML が 1 対 1 で対応する", async () => {
  const htmlFiles = (await readdir(siteFile(""))).filter((name) => name.endsWith(".html")).sort();
  const expected = ["index.html", ...content.GUIDES.map((guide) => `${guide.id}.html`)].sort();
  assert.deepEqual(htmlFiles, expected);
  for (const link of content.GUIDES) {
    assert.equal(link.href, `./${link.id}.html`, `${link.id} の href がファイル名と食い違っています`);
    const html = await readSite(`${link.id}.html`);
    assert.match(html, new RegExp(`<body data-guide="${link.id}">`));
    assert.match(html, /<script type="module" src="\/src\/guide\.tsx"><\/script>/);
  }
});

test("説明ページの HTML は、トップページと同じ配色のスクリプトを持ち、外部から何も読み込まない", async () => {
  const scriptOf = (html) => /<script>\n([\s\S]*?)<\/script>/.exec(html)?.[1];
  const homeScript = scriptOf(await readSite("index.html"));
  assert.ok(homeScript?.includes("site-theme"));
  for (const link of content.GUIDES) {
    const html = await readSite(`${link.id}.html`);
    // 最初の描画より前に配色を決めるスクリプト。片方だけ直すと、ページごとに一瞬見える配色が変わる
    assert.equal(scriptOf(html), homeScript, `${link.id}.html の配色のスクリプトが index.html と食い違っています`);
    assert.match(html, /<link rel="icon" type="image\/svg\+xml" href="\.\/favicon\.svg" \/>/);
    assert.match(html, /<html lang="ja">/);
    assert.doesNotMatch(html, /(?:src|href)="(?:https?:)?\/\//);
    assert.doesNotMatch(html, /gtag|googletagmanager|google-analytics|plausible|clarity/i);
  }
});

test("説明ページの入口（guide.tsx）が、一覧のすべてのページの文言を読み込む", async () => {
  const entry = await readSite("src/guide.tsx");
  for (const link of content.GUIDES) {
    assert.match(entry, new RegExp(`import \\{ GUIDE as ${link.id} \\} from "\\./guides/${link.id}";`));
  }
  const table = /const GUIDE_CONTENT: Record<GuideId, Guide> = \{ ([^}]+) \};/.exec(entry)?.[1];
  assert.deepEqual(table?.split(", ").sort(), content.GUIDES.map((guide) => guide.id).sort());
});

test("各ページの文言がそろっていて、節と小見出しの id がページの中で重ならない", () => {
  for (const { link, guide } of guides) {
    assert.equal(guide.id, link.id);
    assert.ok(guide.title && guide.lead && guide.sections.length > 0);
    const ids = new Set();
    for (const section of guide.sections) {
      const own = [section.id, ...section.blocks.filter((block) => block.kind === "heading").map((block) => block.id)];
      for (const id of own) {
        assert.match(id, /^[a-z0-9-]+$/, `${link.id}: id は英小文字・数字・ハイフンだけにする: ${id}`);
        // 見出しの要素には `${id}-title` も使う
        assert.ok(!ids.has(id) && !ids.has(`${id}-title`), `${link.id}: id が重なっています: ${id}`);
        ids.add(id);
        ids.add(`${id}-title`);
      }
      assert.ok(section.title && section.blocks.length > 0, `${link.id}#${section.id} が空です`);
      for (const block of section.blocks) {
        if (block.kind === "code") assert.ok(block.code.trim(), `${link.id}#${section.id}: 空のコードの例があります`);
        if (block.kind === "table") {
          for (const row of block.rows) assert.equal(row.length, block.columns.length, `${link.id}#${section.id}: 表の列の数が合いません`);
        }
      }
    }
  }
});

const ALL_STRINGS = [...collectStrings({ ...content }), ...guides.flatMap(({ guide }) => collectStrings(guide))];

test("ID・メールアドレス・キー・ローカルのパスに見える値を、説明ページにも載せない", () => {
  const patterns = [
    /[0-9]{17,20}/, // Discord の ID
    /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/, // メールアドレス
    /AIza[0-9A-Za-z_-]{20,}/, // Gemini の API キー
    /[A-Za-z]:[\\/]Users[\\/]|\/home\//, // OS のユーザー名を含むパス
    /discord\.gg|discord(?:app)?\.com\/(?:oauth2|invite|api)/i, // ボットの招待 URL
  ];
  for (const text of ALL_STRINGS) {
    for (const pattern of patterns) assert.doesNotMatch(text, pattern, `載せてはいけない値に見えます: ${text.slice(0, 80)}`);
  }
});

test("文中の外部の URL は、リポジトリの中か、文字として書いてよいものだけ", () => {
  const urls = ALL_STRINGS.flatMap((text) => text.match(/https?:\/\/[^\s`）)」、。]+/g) ?? []);
  assert.ok(urls.length > 0);
  for (const url of urls) {
    assert.ok(url.startsWith(REPOSITORY_PREFIX) || TEXT_ONLY_URLS.has(url), `載せてよいか確かめていない URL です: ${url}`);
  }
});

/** リンクの行き先が、サイト内のページ・節か、このリポジトリの中を指しているか */
async function assertLinkTarget(target, currentPage) {
  if (target.startsWith("http")) {
    assert.ok(target.startsWith(REPOSITORY_PREFIX), `外部のサイトへのリンクがあります: ${target}`);
    const blob = /^https:\/\/github\.com\/[^/]+\/[^/]+\/blob\/main\/(.+)$/.exec(target);
    if (blob) assert.ok(existsSync(new URL(`../../${blob[1]}`, import.meta.url)), `リンク先がリポジトリにありません: ${blob[1]}`);
    return;
  }
  const match = /^(?:\.\/([a-z-]+\.html))?(?:#([a-z0-9-]+))?$/.exec(target);
  assert.ok(match && (match[1] || match[2]), `サイト内のリンクの形が違います: ${target}`);
  const page = match[1] ?? currentPage;
  const anchor = match[2];
  if (page === "index.html") {
    if (anchor) {
      const app = await readSite("src/App.tsx");
      assert.ok(anchor === "top" ? /id="top"/.test(app) : new RegExp(`id="${anchor}"`).test(app), `トップページに #${anchor} がありません`);
    }
    return;
  }
  assert.ok(anchorsByPage.has(page), `リンク先のページがありません: ${target}`);
  if (anchor) assert.ok(anchorsByPage.get(page).has(anchor), `${page} に #${anchor} がありません（${target}）`);
}

test("リンクの行き先は、サイト内のページ・節か、このリポジトリの中だけ", async () => {
  // 部品の中のリンク（content.ts の href）
  const hrefs = [...collectHrefs({ ...content }), ...Object.values(content.LINKS)];
  for (const href of hrefs) {
    if (href.startsWith("#")) continue; // トップページの節は content.test.mjs が確かめる
    await assertLinkTarget(href, "index.html");
  }
  // 文中のリンク（[文字](行き先)）
  let inlineCount = 0;
  for (const { link, guide } of guides) {
    for (const text of collectStrings(guide)) {
      for (const target of collectInlineLinks(text)) {
        inlineCount += 1;
        assert.ok(!TEXT_ONLY_URLS.has(target), `文字として書く URL をリンクにしています: ${target}`);
        await assertLinkTarget(target, `${link.id}.html`);
      }
    }
  }
  assert.ok(inlineCount > 0);
});

test("文中の書式は、コード・強調・リンクの 3 つだけを読み分ける", () => {
  assert.deepEqual(parseInline("`a**b` と **`c` を強く** と [ページ](./setup.html#env)。[括弧] はそのまま"), [
    { type: "code", text: "a**b" },
    { type: "text", text: " と " },
    { type: "strong", children: [{ type: "code", text: "c" }, { type: "text", text: " を強く" }] },
    { type: "text", text: " と " },
    { type: "link", href: "./setup.html#env", children: [{ type: "text", text: "ページ" }] },
    { type: "text", text: "。[括弧] はそのまま" },
  ]);
  // コードの中の [ ] はリンクとして読まない
  assert.deepEqual(collectInlineLinks("`discord.py[voice]` と `[a](b)`"), []);
  assert.deepEqual(parseInline("書式の無い文"), [{ type: "text", text: "書式の無い文" }]);
});

test("シェルの例は、行末の # から後ろだけをコメントとして分ける", () => {
  assert.deepEqual(splitShellComment("cd src   # 移動する"), ["cd src   ", "# 移動する"]);
  assert.deepEqual(splitShellComment("# 行の全体"), ["", "# 行の全体"]);
  // 引用符の中と、語の途中の # はコメントにしない
  assert.deepEqual(splitShellComment(`echo "a # b" c#d`), [`echo "a # b" c#d`, ""]);
  assert.deepEqual(splitShellComment("python -c \"print('#')\" # 確認"), ["python -c \"print('#')\" ", "# 確認"]);
});

test("使い方のページは、/functions のボタンの一覧を表にして載せる", async () => {
  const usage = await import("../src/guides/usage.ts");
  const section = usage.GUIDE.sections.find((item) => item.id === "functions");
  const table = section?.blocks.find((block) => block.kind === "table");
  assert.deepEqual(
    table?.rows.map((row) => row[0]),
    usage.FUNCTIONS_BUTTONS.map((item) => item.button),
  );
});
