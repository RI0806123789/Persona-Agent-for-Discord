// データ画面・概要・ログ画面の表示状態を確かめる（#478）。
//   - データ画面: 初回の取得に失敗したとき、進捗のまま固めない。切断で loading を取り残さない。
//   - テーブルの中身: 行が0件のページで範囲を逆転表示せず、次のページへ進ませない。
//   - 概要の最近のログ: 毎秒の再描画で、ログも伏せ字も変わらないなら分類・伏せ字を再計算しない。
//   - ログ画面の検索: 小文字化で長さが変わる文字があっても、一致の位置がずれない。
import assert from "node:assert/strict";
import test from "node:test";
import { compile, compileTs, readSource as source } from "./helpers/compile.mjs";
import { nodes } from "./helpers/tree.mjs";

const jsx = (type, props, key) => ({ type, props, key });
const names = new Proxy({}, { get: (_, name) => name });
const tick = () => new Promise((resolve) => setImmediate(resolve));

/** 状態・参照・メモ化・Effect の境界だけを置き換える（メモ化は依存の一致で再利用する）。 */
function hookRenderer() {
  const slots = [];
  const effects = [];
  let index = 0;
  const same = (a, b) => a && b && a.length === b.length && a.every((value, i) => Object.is(value, b[i]));
  const hooks = {
    Fragment: Symbol("Fragment"),
    useState(initial) {
      const slot = index++;
      slots[slot] ??= { value: typeof initial === "function" ? initial() : initial };
      const cell = slots[slot];
      return [cell.value, (next) => { cell.value = typeof next === "function" ? next(cell.value) : next; }];
    },
    useRef(initial) {
      const slot = index++;
      return (slots[slot] ??= { current: initial });
    },
    useMemo(factory, dependencies) {
      const slot = index++;
      const saved = slots[slot];
      if (saved && same(saved.dependencies, dependencies)) return saved.value;
      const value = factory();
      slots[slot] = { dependencies, value };
      return value;
    },
    useCallback(callback, dependencies) {
      return hooks.useMemo(() => callback, dependencies);
    },
    useEffect(callback, dependencies) {
      const slot = index++;
      const saved = slots[slot];
      if (!saved || !same(saved.dependencies, dependencies)) {
        effects.push(() => { saved?.cleanup?.(); slots[slot] = { dependencies, cleanup: callback() }; });
      }
    },
  };
  return {
    hooks,
    render: (component, props) => { index = 0; return component(props); },
    commit: () => { effects.splice(0).forEach((effect) => effect()); },
    unmount: () => slots.forEach((slot) => slot?.cleanup?.()),
  };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// ---------------------------------------------------------------- データ画面

const dataSource = await source("pages/DataPage.tsx");
const dbTables = compile(await source("dbTables.ts"), {});

function loadDataPage(db, { instrument = "" } = {}) {
  const runtime = hookRenderer();
  const text = instrument ? dataSource.replace("  if (!connected) {", `  ${instrument}\n  if (!connected) {`) : dataSource;
  assert.ok(!instrument || text !== dataSource);
  const DataPage = compile(text, {
    react: runtime.hooks, "react/jsx-runtime": { jsx, jsxs: jsx },
    "../api": { api: { db }, errorMessage: (error) => error.message, errorSafeMessage: (error) => error.safeMessage ?? null },
    "../components/Collapse": { Collapse: "Collapse" },
    "../components/expressive": names, "../components/Icon": { Icon: "Icon" }, "../components/ui": names,
    "../format": { formatBytes: String, formatNumber: String }, "../dbTables": dbTables,
    "../motionPreference": { prefersReducedMotion: () => true },
    "../privacy": { usePrivacy: () => ({ mask: String }), redactError: (text, safe, privacy) => (privacy.enabled && safe != null ? safe : text) },
    "./DataSummaryParts": names, "./DbTableDetail": { DbTableDetail: "DbTableDetail" },
    "./shared": { NotConnected: "NotConnected" },
  }).DataPage;
  const render = (props = {}) => runtime.render(DataPage, {
    connected: true, connecting: false, onConnect() {}, selectedProfile: null, refreshToken: 0, connectionKey: "A",
    onNames() {}, ...props,
  });
  return { runtime, render };
}

const textOf = (tree) => nodes(tree).flatMap((node) => (typeof node.props?.children === "string" ? [node.props.children] : []));

test("データ画面は初回の取得に失敗したら、各カードを進捗のまま固めず取得できないことを示す", async () => {
  const { runtime, render } = loadDataPage(async () => { throw new Error("架空の取得失敗"); });
  try {
    const first = render();
    // 取得の前は、どのカードも進捗を出す
    assert.ok(nodes(first).filter((node) => node.type === "WavyProgress").length >= 3);
    runtime.commit();
    await tick();
    const tree = render();
    // 進捗の表示は1つも残らない（「集計しています」のまま固まらない）
    assert.equal(nodes(tree).filter((node) => node.type === "WavyProgress").length, 0);
    assert.equal(nodes(tree).filter((node) => node.type === "LoadingIndicator").length, 0);
    // エラーは bot.db のカードに出る
    assert.ok(nodes(tree).some((node) => node.type === "InlineError" && node.props.children === "架空の取得失敗"));
    // 件数・評価は「取得できません」を示す null（未取得の undefined ではない）
    const counts = nodes(tree).filter((node) => node.type === "StatusCounts");
    assert.equal(counts.length, 2);
    for (const node of counts) assert.equal(node.props.counts, null);
    assert.equal(nodes(tree).find((node) => node.type === "FeedbackSummary").props.feedback, null);
    const messages = textOf(tree);
    assert.ok(messages.filter((message) => message === "取得できません").length >= 3, messages.join("|"));
    // 「違反記録は未作成」とは言わない（取得できなかっただけ）
    assert.equal(messages.includes("違反記録は未作成、または取得できません。"), false);
  } finally {
    runtime.unmount();
  }
});

test("データ画面は取得の途中でも、ブラックリストを未作成と案内せず進捗を出す", async () => {
  const pending = deferred();
  const { runtime, render } = loadDataPage(() => pending.promise);
  try {
    render(); runtime.commit();
    const tree = render();
    assert.equal(textOf(tree).includes("違反記録は未作成、または取得できません。"), false);
    assert.ok(nodes(tree).filter((node) => node.type === "WavyProgress").length >= 3);
    // 取得できたら、ブラックリストの記録が無い場合だけ「未作成」と案内する
    pending.resolve({ exists: true, errors: [], tables: {}, blacklist: null, feedback_by_day: [] });
    await tick();
    assert.ok(textOf(render()).includes("違反記録は未作成、または取得できません。"));
  } finally {
    runtime.unmount();
  }
});

test("データ画面は取得中に切断されても loading を取り残さない", async () => {
  const pending = deferred();
  const { runtime, render } = loadDataPage(() => pending.promise, { instrument: "return {summary, loading, error};" });
  try {
    render(); runtime.commit();
    assert.equal(render().loading, true);
    // 取得の完了前に切断される
    render({ connected: false }); runtime.commit();
    assert.equal(render({ connected: false }).loading, false);
    pending.resolve({ exists: true, errors: [] });
    await tick();
    // 古い取得の結果は捨てられ、loading も戻ったまま
    const after = render({ connected: false });
    assert.equal(after.loading, false);
    assert.equal(after.summary, null);
  } finally {
    runtime.unmount();
  }
});

// ---------------------------------------------------------------- テーブルの中身

const detailSource = await source("pages/DbTableDetail.tsx");

async function renderDetail(page) {
  const runtime = hookRenderer();
  const { DbTableDetail } = compile(detailSource, {
    react: runtime.hooks, "react/jsx-runtime": { jsx, jsxs: jsx },
    "../api": { api: { dbTable: async () => page }, errorMessage: (error) => error.message, errorSafeMessage: (error) => error.safeMessage ?? null },
    "../components/Collapse": names, "../components/expressive": names, "../components/ui": names,
    "../dbTables": dbTables, "../format": { formatNumber: String },
    "../hooks": { usePresence: () => ({}) }, "../privacy": { usePrivacy: () => ({ enabled: false }), redactError: (text, safe, privacy) => (privacy.enabled && safe != null ? safe : text) },
    "./DbCells": names,
  });
  const props = { table: "feedback", refreshToken: 0, onClose() {}, onNames() {} };
  runtime.render(DbTableDetail, props);
  runtime.commit();
  await tick();
  const tree = runtime.render(DbTableDetail, props);
  runtime.unmount();
  const find = (predicate) => nodes(tree).find(predicate);
  return {
    range: find((node) => String(node.props?.className ?? "").includes("db-range")).props.children,
    next: find((node) => node.props?.label === "次の 50 行").props,
    previous: find((node) => node.props?.label === "前の 50 行").props,
  };
}

const rowsOf = (count) => Array.from({ length: count }, (_, i) => ({ cells: { id: String(i) } }));
const page = (offset, count, total) => ({
  table: "feedback", offset, limit: 50, total, columns: ["message_id"], rows: rowsOf(count), users: {}, errors: [],
});

test("テーブルの中身は、0件のページで範囲を逆転表示せず、次のページへ進ませない", async () => {
  // 2ページ目を見ている間に総行数が30行へ減り、このページが空になった
  const shown = await renderDetail(page(50, 0, 30));
  assert.equal(shown.range, "0–0 / 30 行");
  assert.equal(shown.next.disabled, true);
  // 前のページへは戻れる
  assert.equal(shown.previous.disabled, false);
});

test("テーブルの中身の範囲とページ送りは、行がある通常のページでは従来どおり", async () => {
  const middle = await renderDetail(page(0, 50, 120));
  assert.equal(middle.range, "1–50 / 120 行");
  assert.equal(middle.next.disabled, false);
  assert.equal(middle.previous.disabled, true);
  const last = await renderDetail(page(100, 20, 120));
  assert.equal(last.range, "101–120 / 120 行");
  assert.equal(last.next.disabled, true);
  assert.equal(last.previous.disabled, false);
  const empty = await renderDetail(page(0, 0, 0));
  assert.equal(empty.range, "0–0 / 0 行");
  assert.equal(empty.next.disabled, true);
});

// ---------------------------------------------------------------- 概要の最近のログ

test("概要の最近のログは、ログも伏せ字も変わらない再描画では再計算しない", async () => {
  const runtime = hookRenderer();
  let calls = 0;
  let activePrivacy = { enabled: false };
  const { RecentLogs } = compile(await source("pages/OverviewPage.tsx"), {
    react: runtime.hooks, "react/jsx-runtime": { jsx, jsxs: jsx },
    "../components/ui": names, "../components/Icon": { Icon: "Icon" }, "../components/expressive": names,
    "../format": new Proxy({}, { get: () => () => "架空の時刻" }),
    "../logs": compileTs(await source("logs.ts")),
    // 保持行すべてを判定してから末尾を切り出す順序は、コンポーネント側（slice(-8)）で守る。
    "../logPresentation": { prepareLogEntries: (logs) => { calls += 1; return logs.map((entry) => ({ entry, level: "info", text: entry.message })); } },
    "../privacy": { usePrivacy: () => activePrivacy, redactError: (text, safe, privacy) => (privacy.enabled && safe != null ? safe : text) },
  });
  const row = (message) => ({ ts: 1, pid: 42, priority: 6, source: "python", message });
  const logsA = Array.from({ length: 12 }, (_, i) => row(`行${i}`));
  const draw = (logs) => runtime.render(RecentLogs, { logs, onNavigate() {} });
  const shownTexts = (tree) => nodes(tree).filter((node) => node.props?.className === "mini-log-text body-medium").map((node) => node.props.children);
  try {
    assert.deepEqual(shownTexts(draw(logsA)), ["行4", "行5", "行6", "行7", "行8", "行9", "行10", "行11"]);
    assert.equal(calls, 1);
    // 概要が毎秒再描画されても（同じログ・同じ伏せ字設定）、分類と伏せ字をやり直さない
    for (let second = 0; second < 5; second += 1) draw(logsA);
    assert.equal(calls, 1);
    // ログが変わったら計算し直す
    const logsB = [...logsA, row("行12")];
    assert.equal(shownTexts(draw(logsB)).at(-1), "行12");
    assert.equal(calls, 2);
    // 伏せ字の設定が変わったら計算し直す
    activePrivacy = { enabled: true };
    draw(logsB);
    assert.equal(calls, 3);
  } finally {
    runtime.unmount();
  }
});

// ---------------------------------------------------------------- ログ画面の検索

const { findMatchRanges } = compileTs(await source("logs.ts"));
const sliced = (text, query) => findMatchRanges(text, query).map(([start, end]) => text.slice(start, end));

test("検索の一致位置は、小文字化で長さが変わる文字があっても元の文字列でずれない", () => {
  // "İ"（U+0130）は toLowerCase() で2単位（"i" と結合の点）になり、後ろの位置が1つずれていた
  const text = "İİİ error と İ ERROR";
  assert.equal(text.toLowerCase().indexOf("error") - text.indexOf("error"), 3, "従来の位置計算がずれる前提の確認");
  assert.deepEqual(sliced(text, "error"), ["error", "ERROR"]);
  assert.deepEqual(findMatchRanges(text, "error").map(([start]) => start), [text.indexOf("error"), text.indexOf("ERROR")]);
  assert.deepEqual(findMatchRanges(text, "error").map(([start]) => start), [4, 14]);
});

test("検索は大文字小文字を区別せず、前後の空白を除き、正規表現の記号は文字として探す", () => {
  assert.deepEqual(sliced("Error error ERROR", "  error "), ["Error", "error", "ERROR"]);
  assert.deepEqual(sliced("a.b axb a.b", "a.b"), ["a.b", "a.b"]);
  assert.deepEqual(sliced("f(x) [1] f(x)", "(x)"), ["(x)", "(x)"]);
  assert.deepEqual(sliced("日本語のログ", "ログ"), ["ログ"]);
  assert.deepEqual(findMatchRanges("何もない", "ない"), [[2, 4]]);
});

test("検索語が空か一致が無いときは、範囲を返さない", () => {
  assert.deepEqual(findMatchRanges("error", ""), []);
  assert.deepEqual(findMatchRanges("error", "   "), []);
  assert.deepEqual(findMatchRanges("error", "warn"), []);
});
