// styles.css を styles/ へ意味のまとまりで分けた（#481）後も、カスケードの順番と、部品の分割が崩れないことを確かめる。
// 後ろにある規則が前の規則を上書きするため、読み込む順番を入れ替えると見た目が変わる。
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const read = async (name) => (await readFile(new URL(`../src/${name}`, import.meta.url), "utf8")).replaceAll("\r\n", "\n");

// 分割前の styles.css と同じ並び。足したら、ここと styles.css の両方へ同じ位置に足す。
const ORDER = [
  "tokens", "base", "header", "buttons", "hero", "demo", "sections", "features", "story", "howto", "tech",
  "changelog", "guide", "prose", "footer", "motion",
];

const withoutCommentsAndStrings = (css) => css
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, '""');

test("styles.css は styles/ のファイルを決まった順に @import するだけにする", async () => {
  const entry = await read("styles.css");
  const imported = [...entry.matchAll(/^@import "\.\/styles\/([\w-]+)\.css";$/gm)].map((match) => match[1]);
  assert.deepEqual(imported, ORDER);
  // @import は他の規則より前に置かないと効かない。入口には @import 以外の規則を書かない。
  const rest = withoutCommentsAndStrings(entry).replace(/^@import .*;$/gm, "").trim();
  assert.equal(rest, "");
});

test("styles/ のファイルはすべて1回ずつ読み込まれる（取りこぼしがない）", async () => {
  const files = (await readdir(new URL("../src/styles/", import.meta.url)))
    .filter((name) => name.endsWith(".css")).map((name) => name.replace(/\.css$/, ""));
  assert.deepEqual([...files].sort(), [...ORDER].sort());
});

test("動きを減らす設定は最後のファイルにだけ書く", async () => {
  assert.equal(ORDER.at(-1), "motion");
  assert.match(withoutCommentsAndStrings(await read("styles/motion.css")), /@media \(prefers-reduced-motion: reduce\)/);
  for (const name of ORDER.slice(0, -1)) {
    assert.doesNotMatch(withoutCommentsAndStrings(await read(`styles/${name}.css`)), /prefers-reduced-motion/, name);
  }
});

test("配色のトークン（ライト・ダーク）は最初のファイルにだけある", async () => {
  const tokens = await read("styles/tokens.css");
  assert.match(tokens, /^:root \{/m);
  assert.match(tokens, /^:root\[data-theme="dark"\] \{/m);
  // 疑似 Discord 画面は --dc-* でどちらの配色でも暗いまま見せる。トークンの定義は tokens.css だけ。
  assert.match(tokens, /--dc-bg:/);
  for (const name of ORDER.slice(1)) {
    const css = withoutCommentsAndStrings(await read(`styles/${name}.css`));
    assert.doesNotMatch(css, /^:root(\[data-theme="dark"\])? \{/m, `${name}.css にトークンの定義が混ざっています`);
  }
});

test("各ファイルは規則の途中で切れていない（波括弧が釣り合う）", async () => {
  for (const name of ORDER) {
    const css = withoutCommentsAndStrings(await read(`styles/${name}.css`));
    let depth = 0;
    for (const character of css) {
      if (character === "{") depth += 1;
      if (character === "}") depth -= 1;
      assert.ok(depth >= 0, `${name}.css: 閉じ括弧が多すぎます`);
    }
    assert.equal(depth, 0, `${name}.css: 閉じていない規則があります`);
  }
});

test("疑似 Discord 画面は、発言の描画・場面の再生・タブと全体に分かれていて、入口の名前は変わらない", async () => {
  const exported = (text) => [...text.matchAll(/^export (?:async )?(?:function|const|type) (\w+)/gm)].map((m) => m[1]).sort();
  assert.deepEqual(exported(await read("components/DiscordDemo.tsx")), ["DiscordDemo"]);
  assert.deepEqual(exported(await read("components/demo/ScenarioPlayer.tsx")), ["ScenarioPlayer"]);
  assert.deepEqual(exported(await read("components/demo/DemoMessages.tsx")), ["Avatar", "ChannelIcon", "Message", "TypingRow"]);
  // 再生の進め方は ScenarioPlayer だけが持ち、発言の描画は状態も時間も持たない。
  const messages = await read("components/demo/DemoMessages.tsx");
  assert.doesNotMatch(messages, /useState|useEffect|setInterval|performance\.now/);
  const player = await read("components/demo/ScenarioPlayer.tsx");
  assert.match(player, /setInterval/);
  // 再生順の計算（時刻の決め方）は demoTimeline.ts にあり、表示側へ持ち込まない。
  assert.match(player, /frameAt\(timeline/);
});
