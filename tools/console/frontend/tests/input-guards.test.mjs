// キーボード・ポインター操作の境界値と、ログ分類・「動きを減らす」設定の確認（#494）。
//   - メニュー: 項目にフォーカスが無いときの ↑ / k は末尾へ、有効な項目が無いときは何も起こさない。
//   - Sparkline: 描画幅 0 のポインター操作や、点が減った後に残った添字で落ちない。
//   - ログ: 例外情報を読めなかったときの定型文が、直前の警告レベルを引き継ぐ。
import assert from "node:assert/strict";
import test from "node:test";
import { compile, compileTs, readSource as source } from "./helpers/compile.mjs";

const jsx = (type, props, key) => ({ type, props, key });
const runtime = { jsx, jsxs: jsx, Fragment: Symbol("Fragment") };

// ---------------------------------------------------------------- メニュー

function loadMenu() {
  const react = {
    useState: (initial) => [initial, () => {}],
    useRef: (initial) => ({ current: initial }),
    useCallback: (callback) => callback,
    useEffect: () => {},
    useId: () => "menu-id",
  };
  return compile(await_source_menu, { react, "react/jsx-runtime": runtime }, "menu.tsx").useMenu;
}
const await_source_menu = await source("components/menu.tsx");

function pressKey(key, { items, active }) {
  const focused = [];
  const elements = items.map((name) => ({ name, focus: () => focused.push(name) }));
  globalThis.document = { activeElement: elements[active] ?? null };
  const menu = loadMenu()();
  menu.menuRef.current = { querySelectorAll: () => elements };
  let prevented = false;
  menu.menuProps.onKeyDown({ key, preventDefault: () => { prevented = true; }, stopPropagation() {}, nativeEvent: {} });
  return { focused, prevented };
}

test("#494 項目にフォーカスが無いときの ↑ / k は末尾の項目へ移る", () => {
  for (const key of ["ArrowUp", "k"]) {
    assert.deepEqual(pressKey(key, { items: ["a", "b", "c"], active: -1 }).focused, ["c"], key);
  }
});

test("#494 項目にフォーカスが無いときの ↓ / j は先頭の項目へ移る", () => {
  for (const key of ["ArrowDown", "j"]) {
    assert.deepEqual(pressKey(key, { items: ["a", "b", "c"], active: -1 }).focused, ["a"], key);
  }
});

test("#494 フォーカス中の項目からの移動は従来どおり前後へ循環する", () => {
  assert.deepEqual(pressKey("ArrowUp", { items: ["a", "b", "c"], active: 0 }).focused, ["c"]);
  assert.deepEqual(pressKey("ArrowUp", { items: ["a", "b", "c"], active: 2 }).focused, ["b"]);
  assert.deepEqual(pressKey("ArrowDown", { items: ["a", "b", "c"], active: 2 }).focused, ["a"]);
});

test("#494 有効な項目が無いときは移動せず、例外も出さない", () => {
  for (const key of ["ArrowUp", "ArrowDown", "k", "j", "Home", "End"]) {
    assert.deepEqual(pressKey(key, { items: [], active: -1 }).focused, [], key);
  }
});

// ---------------------------------------------------------------- Sparkline

const points = [{ t: 1, v: 10 }, { t: 2, v: 20 }, { t: 3, v: 15 }];

function sparklineHarness(initialHover = null) {
  let hover = initialHover;
  const react = { useState: () => [hover, (value) => { hover = value; }] };
  const Sparkline = compile(
    // 実装の見た目に関わる import は、この確認に不要なので空にする
    sourceSparkline, { react, "react/jsx-runtime": runtime, "../../format": { formatClock: (t) => `t${t}` } }, "sparkline.tsx",
  ).Sparkline;
  return {
    render: () => Sparkline({ points, format: String, label: "試験" }),
    hover: () => hover,
  };
}
const sourceSparkline = await source("components/primitives/sparkline.tsx");

const findSvg = (tree) => (Array.isArray(tree.props.children) ? tree.props.children : [tree.props.children]).find((child) => child?.type === "svg");

test("#494 描画幅 0 のポインター操作は NaN の添字を作らない", () => {
  const harness = sparklineHarness();
  const svg = findSvg(harness.render());
  svg.props.onPointerMove({ clientX: 10, currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 0 }) } });
  assert.equal(harness.hover(), null);
});

test("#494 通常のポインター操作は最寄りの点を選ぶ", () => {
  const harness = sparklineHarness();
  const svg = findSvg(harness.render());
  svg.props.onPointerMove({ clientX: 100, currentTarget: { getBoundingClientRect: () => ({ left: 0, width: 100 }) } });
  assert.equal(harness.hover(), points.length - 1);
});

test("#494 範囲外の添字（NaN や点が減った後の古い値）が残っても描画で落ちない", () => {
  for (const stale of [Number.NaN, 7, -1]) {
    const harness = sparklineHarness(stale);
    assert.doesNotThrow(() => harness.render(), String(stale));
  }
});

// ---------------------------------------------------------------- ログの分類

const logs = compileTs(await source("logs.ts"), {});
const entry = (message) => ({ ts: 0, message, priority: 6, pid: 42, source: "python" });

test("#494 例外情報を読み取れなかった定型文は、直前の警告レベルを引き継ぐ", () => {
  const rows = [
    entry("[2026-10-18T07:32:26+0900] [WARNING] [persona_bot.core.example] 処理を続けます"),
    entry("例外情報を読み取れませんでした。"),
  ];
  assert.deepEqual(logs.classifyEntries(rows), ["warn", "warn"]);
});

// ---------------------------------------------------------------- 動きを減らす設定

test("#494 動きを減らす設定は、アニメーション・トランジションの遅延も 0 にする", async () => {
  const css = await source("styles/motion.css");
  const block = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
  assert.match(block, /animation-delay:\s*0s\s*!important/);
  assert.match(block, /transition-delay:\s*0s\s*!important/);
});
