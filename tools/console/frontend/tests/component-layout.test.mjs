// components/ui.tsx と、大きかったページの分割（#481）が、利用側の import 先を変えていないことを確かめる。
// 名前の欠落は tsc（利用側が import できなくなる）が検出する。ここでは「入口に処理が戻らない」ことと、
// 切り出した部品が従来の入口から取れることを確かめる。
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

const read = async (name) => (await readFile(new URL(`../src/${name}`, import.meta.url), "utf8")).replaceAll("\r\n", "\n");
const withoutComments = (text) => text.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
const exportedNames = (text) => [...withoutComments(text).matchAll(/^export (?:async )?(?:function|const|interface|type) (\w+)/gm)].map((m) => m[1]);

test("components/ui.tsx は primitives/ の部品を再公開するだけの入口にする", async () => {
  const entry = withoutComments(await read("components/ui.tsx")).trim();
  const lines = entry.split("\n").map((line) => line.trim()).filter(Boolean);
  assert.ok(lines.every((line) => /^export \* from "\.\/primitives\/[\w-]+";$/.test(line)), lines.join("\n"));
  const reexported = lines.map((line) => line.match(/primitives\/([\w-]+)"/)[1]).sort();
  const files = (await readdir(new URL("../src/components/primitives/", import.meta.url)))
    .filter((name) => name.endsWith(".tsx")).map((name) => name.replace(/\.tsx$/, "")).sort();
  assert.deepEqual(reexported, files);
});

test("分けた基本部品は、従来 ui.tsx から取れていた名前をそれぞれ1か所で公開する", async () => {
  const expected = {
    card: ["Card", "SourceBadge"],
    buttons: ["Button", "ConnectedButtonGroup", "GroupOption", "IconButton", "MenuItem", "SplitButton", "Switch"],
    status: ["Chip", "EmptyState", "InlineError", "Kbd", "Notice", "Stat", "StatusChip"],
    sparkline: ["SparkPoint", "Sparkline"],
  };
  const seen = new Map();
  for (const [file, names] of Object.entries(expected)) {
    const actual = exportedNames(await read(`components/primitives/${file}.tsx`));
    assert.deepEqual([...actual].sort(), names, file);
    for (const name of actual) {
      assert.equal(seen.has(name), false, `${name} が ${seen.get(name)} と ${file} の両方にあります`);
      seen.set(name, file);
    }
  }
});

test("redactNode は伏せ字を掛けるエラーと案内の部品と同じファイルに置く", async () => {
  // InlineError / Notice の自動の伏せ字（redactNode）が、別のファイルへ離れて掛け忘れが起きないようにする。
  const status = await read("components/primitives/status.tsx");
  assert.match(status, /function redactNode\(/);
  assert.match(status, /redactNode\(children, privacy\.redact\)/g);
  assert.equal([...status.matchAll(/redactNode\(children, privacy\.redact\)/g)].length, 2);
  for (const other of ["card", "buttons", "sparkline"]) {
    assert.doesNotMatch(await read(`components/primitives/${other}.tsx`), /redactNode/, other);
  }
});

test("切り出したページの部品は、従来の入口から取れる名前を保つ", async () => {
  // テストが DbTableDetail から RowFields を、OverviewPage から RecentLogs を取り出す（依存表の更新が要る分割）。
  assert.ok(exportedNames(await read("pages/DbTableDetail.tsx")).includes("DbTableDetail"));
  assert.match(await read("pages/DbTableDetail.tsx"), /^export \{ RowFields \};$/m);
  assert.ok(exportedNames(await read("pages/DbCells.tsx")).includes("RowFields"));
  assert.ok(exportedNames(await read("pages/OverviewPage.tsx")).includes("RecentLogs"));
  assert.deepEqual(
    exportedNames(await read("pages/OverviewCards.tsx")).sort(), ["BotInsightsCard", "GitCard", "ResourceCards", "SshCard"],
  );
  assert.deepEqual(
    exportedNames(await read("pages/DataSummaryParts.tsx")).sort(),
    ["FeedbackSummary", "FeedbackTrend", "SchemaChip", "StatusCounts"],
  );
  // 名前で探される部品は、テストが木から見つけられるよう元のページに残す。
  assert.match(await read("pages/OverviewPage.tsx"), /^function ServiceHero\(/m);
  assert.match(await read("pages/DataPage.tsx"), /^function TableRows\(/m);
});
