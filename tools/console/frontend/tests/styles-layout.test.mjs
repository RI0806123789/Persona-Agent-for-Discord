// styles.css を styles/ へ意味のまとまりで分けた（#481）後も、カスケードの順番が変わらないことを確かめる。
// 後ろにある規則が前の規則を上書きするため、読み込む順番を入れ替えると見た目が変わる。
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const read = async (name) => (await readFile(new URL(`../src/${name}`, import.meta.url), "utf8")).replaceAll("\r\n", "\n");

// 分割前の styles.css と同じ並び。足したら、ここと styles.css の両方へ同じ位置に足す。
const ORDER = [
  "tokens", "layout", "navigation", "buttons", "components", "page-overview", "page-logs", "page-operations",
  "deploy", "tables", "page-data", "page-guilds", "dialogs", "utilities", "responsive", "motion",
];

const withoutCommentsAndStrings = (css) => css
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/g, '""');

test("styles.css は styles/ のファイルを決まった順に @import するだけにする", async () => {
  const entry = await read("styles.css");
  const imported = [...entry.matchAll(/^@import "\.\/styles\/([\w-]+)\.css";$/gm)].map((match) => match[1]);
  assert.deepEqual(imported, ORDER);
  // @import は他の規則より前に置かないと効かない。入口には @import 以外の規則を書かない。
  const rest = withoutCommentsAndStrings(entry).replace(/^@import "";?$/gm, "").replace(/^@import .*;$/gm, "").trim();
  assert.equal(rest, "");
});

test("styles/ のファイルはすべて1回ずつ読み込まれる（取りこぼしがない）", async () => {
  const files = (await readdir(new URL("../src/styles/", import.meta.url)))
    .filter((name) => name.endsWith(".css")).map((name) => name.replace(/\.css$/, ""));
  assert.deepEqual([...files].sort(), [...ORDER].sort());
});

test("画面幅ごとの調整は各部品・ページより後、動きを減らす設定は最後に読み込む", async () => {
  assert.equal(ORDER.at(-1), "motion");
  assert.equal(ORDER.at(-2), "responsive");
  const motion = withoutCommentsAndStrings(await read("styles/motion.css"));
  assert.match(motion, /@media \(prefers-reduced-motion: reduce\)/);
  // 「動きを減らす」は最後の1か所にだけ置く。ほかのファイルに書くと、後ろの規則に上書きされうる。
  for (const name of ORDER.slice(0, -1)) {
    assert.doesNotMatch(withoutCommentsAndStrings(await read(`styles/${name}.css`)), /prefers-reduced-motion/, name);
  }
  // 画面幅の調整は @media だけで成り立つ（部品の基本スタイルをここへ置かない）。
  const responsive = withoutCommentsAndStrings(await read("styles/responsive.css")).trim();
  assert.match(responsive, /^@media /);
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

test("デザイントークンは最初のファイルにある", async () => {
  assert.match(await read("styles/tokens.css"), /^:root \{/m);
  for (const name of ORDER.slice(1)) {
    assert.doesNotMatch(await read(`styles/${name}.css`), /^:root \{\n  \/\* ---- 形/m, name);
  }
});
