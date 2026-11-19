import assert from "node:assert/strict";
import test from "node:test";
import ts from "typescript";
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import { compile, readSource as source } from "./helpers/compile.mjs";
import { nodes } from "./helpers/tree.mjs";


const scopes = compile(await source("requestScope.ts"), {});
const statusModule = compile(await source("status.ts"));
const deployPlan = compile(await source("deployPlan.ts"));
const appSource = await source("App.tsx");
const confirmationsSource = await source("confirmations.tsx");
const controllerSource = await source("controllers.ts");
const dataSource = await source("pages/DataPage.tsx");
const privacySource = await source("privacy.ts");
const displayPrivacy = compile(privacySource, {react: React});
const {Snackbar} = compile((await source("components/toast.tsx")), {
  react: React, "react/jsx-runtime": jsxRuntime, "../privacy": displayPrivacy, "./Icon": {Icon: () => null},
});

function noticeHtml(harness, notice = harness.notices.at(-1)) {
  return renderToStaticMarkup(React.createElement(displayPrivacy.PrivacyContext.Provider, {value: harness.render().privacy},
    React.createElement(Snackbar, {toast: {...notice, id: 1, leaving: false}, onDismiss() {}})));
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

// Reactの状態とEffectの境界だけを置き換え、実際の取得・切断コールバックを実行する。
function renderer() {
  const state = [];
  const effects = [];
  let index = 0;
  const hooks = {
    useState(initial) {
      const slot = index++;
      if (!(slot in state)) state[slot] = typeof initial === "function" ? initial() : initial;
      return [state[slot], (next) => { state[slot] = typeof next === "function" ? next(state[slot]) : next; }];
    },
    useRef(initial) {
      const slot = index++;
      return state[slot] ??= { current: initial };
    },
    useMemo: (factory) => factory(),
    useCallback: (callback) => callback,
    useEffect(callback, dependencies) {
      const slot = index++;
      const previous = state[slot];
      if (!previous || dependencies.some((value, i) => value !== previous.dependencies[i])) {
        effects.push(() => { previous?.cleanup?.(); state[slot] = { dependencies, cleanup: callback() }; });
      }
    },
    createContext: (value) => ({ value }),
    useContext: (context) => context.value,
  };
  return { hooks, unmount: () => state.forEach((slot) => slot?.cleanup?.()), render: (component, props) => { index = 0; return component(props); },
    commit: () => { effects.splice(0).forEach((effect) => effect()); } };
}

const connection = (id) => ({ connected: !!id, profile: id ? { id, label: `架空の${id}` } : null, connected_at: id ? 1 : null });

function consoleHarness(api, renderTree = false) {
  const runtime = renderer();
  const notices = [];
  const privacy = compile(privacySource, { react: runtime.hooks });
  const modules = {
    react: runtime.hooks,
    "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
    "./hooks": { useStoredState: (_key, initial) => runtime.hooks.useState(initial), useNow: () => 0,
      useMediaQuery: () => true, usePolling: () => {} },
    "./useLogStream": { useLogStream: () => ({}) },
    "./api": { api, errorMessage: (error) => error.message, ApiError: Error },
    "./useConsoleShortcuts": { useConsoleShortcuts: () => false },
    "./components/toast": { useToast: () => (notice) => notices.push(notice), ToastViewport: "ToastViewport" },
    "./privacy": privacy, "./requestScope": scopes,
    "./navigation": { PAGES: [{ id: "overview" }], isPageId: () => true },
    "./profiles": { unsetFieldsOf: () => [] }, "./status": statusModule, "./deployPlan": deployPlan,
    "./theme/theme": { SEED_OPTIONS: [{ id: "test", hex: "#123456" }] },
    "./pages/OverviewPage": { OverviewPage: "OverviewPage" },
    "./pages/OperationsPage": { OperationsPage: "OperationsPage" },
  };
  modules["./controllers"] = compile(controllerSource, modules);
  modules["./confirmations"] = compile(confirmationsSource, modules);
  const controls = "busyAction, requestAction, confirmOpen, requestDeploy, runDeploy, confirm, applyConnection, refreshStatus, refreshInsights, refreshDeploy, handleConnect, handleDisconnect, runAction, status, insights, deploy, stateUnknown, statusError, insightsError, setSelectedProfileId, setProfiles, setPrivacyOn, setStoredPage, notify, privacy";
  const exported = appSource;
  const instrumented = renderTree
    ? exported.replace("  return (\n    <PrivacyContext.Provider", "  const tree = (\n    <PrivacyContext.Provider")
      .replace("    </PrivacyContext.Provider>\n  );", `    </PrivacyContext.Provider>\n  );\n  return {${controls}, tree};`)
    : exported.replace("  return (\n    <PrivacyContext.Provider", `  return {${controls}};\n  return (\n    <PrivacyContext.Provider`);
  assert.ok(instrumented.includes(`return {${controls}`));
  const component = compile(instrumented, modules).Console;
  return { render: () => runtime.render(component), notices, privacy };
}

test("切断完了後に古い状態・インサイト・デプロイの成功やエラーを反映しない", async () => {
  for (const fail of [false, true]) {
    const status = deferred(), insights = deferred(), deploy = deferred();
    const api = { status: () => status.promise, insights: () => insights.promise, deploy: () => deploy.promise,
      disconnect: async () => connection(null), connection: async () => connection(null) };
    const harness = consoleHarness(api);
    harness.render().applyConnection(connection("A"));
    const current = harness.render();
    const pending = [current.refreshStatus(), current.refreshInsights(), current.refreshDeploy()];
    await current.handleDisconnect();
    if (fail) for (const response of [status, insights, deploy]) response.reject(new Error("古い接続のエラー"));
    else { status.resolve({ fetched_at: 1 }); insights.resolve({ data: { old: true }, fetched_at: 1 });
      deploy.resolve({ deploy: { profile_id: "A" }, running: true }); }
    await Promise.all(pending);
    const after = harness.render();
    assert.equal(after.status, null);
    assert.equal(after.insights, null);
    assert.equal(after.deploy, null);
    assert.equal(after.statusError, null);
    assert.equal(after.insightsError, null);
  }
});

test("停止はサービス名入力による確認を開き、確認前にAPIを実行しない", () => {
  const calls = [];
  const harness = consoleHarness({ service: (...args) => { calls.push(args); } });
  const state = connection("A");
  state.profile = { ...state.profile, service: "fictional-bot", environment: "production" };
  harness.render().applyConnection(state);
  harness.render().requestAction("stop");
  const after = harness.render();
  assert.equal(after.confirmOpen, true);
  assert.equal(after.confirm.requireText, "fictional-bot");
  assert.equal(after.confirm.confirmLabel, "停止する");
  assert.deepEqual(calls, []);
});

test("接続切替・同じ接続先への再接続・切断で古いサービス操作の確認を無効にする", async (t) => {
  const original = globalThis.window;
  globalThis.window = {setTimeout: () => 1, clearTimeout: () => {}};
  t.after(() => { globalThis.window = original; });
  for (const next of [connection("B"), {...connection("A"), connected_at: 2}, connection(null)]) {
    const targets = [];
    const harness = consoleHarness({
      service: async (...args) => {targets.push(args); return {ok: true, message: "架空の完了"};},
      operations: async () => ({operations: []}), status: async () => ({fetched_at: 1}),
    });
    harness.render().applyConnection(connection("A"));
    harness.render().requestAction("restart");
    const oldConfirm = harness.render().confirm;
    harness.render().applyConnection(next);
    assert.equal(harness.render().confirmOpen, false);
    oldConfirm.onConfirm();
    await Promise.resolve();
    assert.deepEqual(targets, []);
  }
  const targets = [];
  const harness = consoleHarness({
    service: async (...args) => { targets.push(args); return {ok: true, message: "架空の完了"}; },
    operations: async () => ({operations: []}), status: async () => ({fetched_at: 1}),
  });
  harness.render().applyConnection(connection("A"));
  harness.render().requestAction("stop");
  harness.render().confirm.onConfirm();
  await Promise.resolve();
  assert.deepEqual(targets, [["stop", {profile_id: "A", connected_at: 1}]]);
});

test("切り替え前に予約したサービス操作後のタイマーは新しい接続で取得しない", async () => {
  const timers = [];
  const original = globalThis.window;
  globalThis.window = { setTimeout: (callback) => timers.push(callback), clearTimeout: () => {} };
  try {
    let statusCalls = 0;
    const api = { service: async () => ({ ok: true, message: "架空の完了" }), operations: async () => ({ operations: [] }),
      status: async () => { statusCalls++; return { fetched_at: 1 }; } };
    const harness = consoleHarness(api);
    harness.render().applyConnection(connection("A"));
    await harness.render().runAction("restart", { profile_id: "A", connected_at: 1 });
    await Promise.resolve();
    assert.equal(timers.length, 1);
    const before = statusCalls;
    harness.render().applyConnection(connection("B"));
    timers[0]();
    await Promise.resolve();
    assert.equal(statusCalls, before);
  } finally { globalThis.window = original; }
});

test("切断の応答を待っている間に届いた状態も破棄する", async () => {
  const status = deferred(), disconnect = deferred();
  const harness = consoleHarness({ status: () => status.promise, disconnect: () => disconnect.promise,
    connection: async () => connection(null) });
  harness.render().applyConnection(connection("A"));
  const current = harness.render();
  const pending = current.refreshStatus();
  const closing = current.handleDisconnect();
  status.resolve({ fetched_at: 1 });
  await pending;
  assert.equal(harness.render().status, null);
  disconnect.resolve(connection(null));
  await closing;
});

test("同じ接続内でも後から開始した取得を古い応答が上書きしない", async () => {
  const old = deferred(), fresh = deferred();
  let calls = 0;
  const harness = consoleHarness({ insights: () => (++calls === 1 ? old.promise : fresh.promise) });
  harness.render().applyConnection(connection("A"));
  const current = harness.render();
  const first = current.refreshInsights(), second = current.refreshInsights();
  fresh.resolve({ data: { value: "新しい結果", discord: { guilds: [] } }, fetched_at: 2 });
  await second;
  old.resolve({ data: { value: "古い結果", discord: { guilds: [] } }, fetched_at: 1 });
  await first;
  assert.equal(harness.render().insights.value, "新しい結果");
});

test("データ画面は切り替え前のDB集計を破棄し、切断後も再表示しない", async () => {
  const runtime = renderer();
  const old = deferred(), fresh = deferred();
  let calls = 0;
  const instrumented = dataSource.replace("  if (!connected) {", "  return {summary, loading, error};\n  if (!connected) {");
  const component = compile(instrumented, { react: runtime.hooks, "../api": {
    api: { db: () => (++calls === 1 ? old.promise : fresh.promise) }, errorMessage: (error) => error.message,
  }, "../privacy": { usePrivacy: () => ({}) } }).DataPage;
  const render = (connected, connectionKey) => runtime.render(component, { connected, connectionKey, refreshToken: 0 });
  render(true, "A"); runtime.commit();
  render(true, "B"); runtime.commit();
  fresh.resolve({ value: "新しいDB" }); await fresh.promise; await Promise.resolve();
  old.resolve({ value: "古いDB" }); await old.promise; await Promise.resolve();
  assert.equal(render(true, "B").summary.value, "新しいDB");
  render(false, ""); runtime.commit();
  assert.equal(render(false, "").summary, null);
});

test("データ画面は接続先が変わったら、開いていたテーブルの中身を閉じる", () => {
  const runtime = renderer();
  const instrumented = dataSource.replace("  if (!connected) {", "  return {openTable, selectTable};\n  if (!connected) {");
  assert.notEqual(instrumented, dataSource);
  const component = compile(instrumented, { react: runtime.hooks, "../api": {
    api: { db: () => new Promise(() => {}) }, errorMessage: (error) => error.message,
  }, "../privacy": { usePrivacy: () => ({}) } }).DataPage;
  const render = (connectionKey) => runtime.render(component, { connected: true, connectionKey, refreshToken: 0 });
  const originalFrame = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = () => 0; // スクロールは描画の後なので、ここでは呼ばない
  try {
    render("A"); runtime.commit();
    render("A").selectTable("feedback");
    assert.equal(render("A").openTable, "feedback");
    render("A").selectTable("feedback");
    assert.equal(render("A").openTable, null, "同じテーブルをもう一度選ぶと閉じる");
    render("A").selectTable("reminders");
    runtime.commit();
    render("B"); runtime.commit();
    assert.equal(render("B").openTable, null);
  } finally {
    globalThis.requestAnimationFrame = originalFrame;
  }
});

test("接続切替の最初の描画から、子のテーブル取得を明示的な選択まで止める", async () => {
  const parent = renderer();
  const requests = [];
  let connectionKey = "A";
  const api = {
    db: async () => ({ exists: false, tables: { feedback: 51 }, errors: [] }),
    dbTable: async (table, offset, limit) => {
      requests.push({ connectionKey, table, offset, limit });
      return { table, offset, limit, total: 51, columns: ["user_id"],
        rows: [{ cells: { user_id: "11" } }], users: {}, errors: [] };
    },
  };
  const jsx = (type, props, key) => ({ type, props, key });
  const tableSpecs = compile(await source("dbTables.ts"), {});
  let masked = true;
  const privacy = { MASK: "MASKED", usePrivacy: () => ({ enabled: masked, mask: () => "MASKED" }) };
  const modules = {
    "react/jsx-runtime": { jsx, jsxs: jsx },
    "../api": { api, errorMessage: (error) => error.message },
    "../privacy": privacy, "../dbTables": tableSpecs,
    "../format": { formatNumber: String },
    "../components/Collapse": { Collapse: "Collapse" },
    "../components/ui": Object.fromEntries(["Card", "Button", "IconButton", "Notice", "Chip"].map((name) => [name, name])),
    "../components/expressive": {},
    "./DbTableDetail": { DbTableDetail: "DbTableDetail" },
  };
  // CellValue は DbCells.tsx へ分けた（#481）。DbTableDetail が描く実物を、同じ模擬の表で読み込む。
  modules["./DbCells"] = compile(await source("pages/DbCells.tsx"), modules);
  const stableHooks = (runtime) => ({
    ...runtime.hooks,
    useCallback(callback, dependencies) {
      const saved = runtime.hooks.useRef(null);
      if (!saved.current || dependencies.some((value, i) => value !== saved.current.dependencies[i])) {
        saved.current = { callback, dependencies };
      }
      return saved.current.callback;
    },
  });
  const DataPage = compile(dataSource, { ...modules, react: stableHooks(parent) }).DataPage;
  const detailSource = await source("pages/DbTableDetail.tsx");
  let child, childKey, Detail, detailTree;
  const onNames = () => {};
  const render = () => parent.render(DataPage, { connected: true, connectionKey, refreshToken: 0, onNames });
  // Collapse の閉じる動きで子が残る場合も扱う。子のeffectを親より先に実行する。
  const commit = async (tree) => {
    const detail = nodes(tree).find((node) => node.type === "DbTableDetail");
    if (!detail || childKey !== detail.key) {
      child?.unmount();
      child = null;
    }
    if (detail) {
      if (!child) {
        child = renderer();
        childKey = detail.key;
        Detail = compile(detailSource, { ...modules, react: stableHooks(child) }).DbTableDetail;
      }
      detailTree = child.render(Detail, detail.props);
      child.commit();
    }
    parent.commit();
    await Promise.resolve();
    return detail;
  };
  const select = (tree) => {
    const rows = nodes(tree).find((node) => node.type?.name === "TableRows");
    assert.ok(rows);
    rows.props.onSelect("feedback");
  };
  const originalFrame = globalThis.requestAnimationFrame;
  globalThis.requestAnimationFrame = () => 0;
  try {
    await commit(render());
    assert.deepEqual(requests, []);
    select(render());
    await commit(render());
    assert.deepEqual(requests.map((request) => request.connectionKey), ["A"]);

    connectionKey = "B";
    // 親の選択解除effect後の状態だけでなく、最初の子のeffectまで検証する。
    await commit(render());
    assert.deepEqual(requests.map((request) => request.connectionKey), ["A"]);
    assert.equal(nodes(render()).some((node) => node.type === "DbTableDetail"), false);

    select(render());
    const detail = await commit(render());
    assert.deepEqual(requests.map((request) => request.connectionKey), ["A", "B"]);
    detailTree = child.render(Detail, detail.props);
    const expand = nodes(detailTree).find((node) => node.props?.label === "この行の全部の列を見る");
    expand.props.onClick({ stopPropagation() {} });
    detailTree = child.render(Detail, detail.props);
    assert.equal(nodes(detailTree).find((node) => node.type?.name === "DetailRow").props.open, true);
    const cell = nodes(detailTree).find((node) => node.type?.name === "CellValue");
    assert.ok(JSON.stringify(cell.type(cell.props)).includes("MASKED"));
    masked = false;
    assert.ok(JSON.stringify(cell.type(cell.props)).includes("11"));
    assert.equal(JSON.stringify(cell.type(cell.props)).includes("MASKED"), false);
    const next = nodes(detailTree).find((node) => node.props?.label === "次の 50 行");
    assert.equal(next.props.disabled, false);
    next.props.onClick();
    await commit(render());
    assert.deepEqual(requests.at(-1), { connectionKey: "B", table: "feedback", offset: 50, limit: 50 });
    detailTree = child.render(Detail, detail.props);
    assert.equal(nodes(detailTree).find((node) => node.type?.name === "DetailRow").props.open, false);

    // 閉じるアニメーション用に保持した中身も、新接続へは引き継がない。
    detail.props.onClose();
    const closed = render();
    assert.equal(nodes(closed).find((node) => node.type === "Collapse").props.open, false);
    assert.ok(nodes(closed).some((node) => node.type === "DbTableDetail"));
    const beforeSwitch = requests.length;
    connectionKey = "C";
    await commit(render());
    assert.equal(requests.length, beforeSwitch);
  } finally {
    child?.unmount();
    parent.unmount();
    globalThis.requestAnimationFrame = originalFrame;
  }
});

test("SSH設定に任せる場合もホーム所有者・パス・ラベルを伏せ字にする", () => {
  const harness = consoleHarness({});
  for (const home of ["home", "Users"]) {
    const path = ["", home, "FICTIONAL_OWNER", "project"].join("/");
    const profile = { host: "host.invalid", username: null, label: "架空の接続先", bot_dir: path };
    const secrets = harness.privacy.collectSecrets({ profile, profiles: [], connection: null, insights: null });
    const privacy = harness.privacy.createPrivacy(true, secrets);
    assert.equal(privacy.redact(`FICTIONAL_OWNER: ${path}`), `${harness.privacy.MASK}: ${harness.privacy.MASK}`);
    assert.equal(privacy.redact(profile.label), harness.privacy.MASK);
    assert.equal(harness.privacy.createPrivacy(false, secrets).redact(path), path);
  }
});

test("接続先ID・配備先ブランチ・現在のGitブランチはメッセージ内でも隠す", () => {
  const {privacy} = consoleHarness({});
  const profile = {id: "fictional-profile", deploy_branch: "private/fictional-deploy"};
  const git = {branch: "private/fictional-current"};
  const secrets = privacy.collectSecrets({profile, profiles: [], insights: null, git});
  const text = `${profile.id} ${profile.deploy_branch} ${git.branch}`;
  assert.equal(privacy.createPrivacy(true, secrets).redact(text), `${privacy.MASK} ${privacy.MASK} ${privacy.MASK}`);
  assert.equal(privacy.createPrivacy(false, secrets).redact(text), text);
});

test("状態取得で判明したブランチをConsoleの通知でも隠す", async () => {
  const branch = "private/fictional-current";
  const harness = consoleHarness({status: async () => ({git: {branch}, fetched_at: 1})});
  harness.render().applyConnection(connection("A"));
  await harness.render().refreshStatus();
  harness.render().setPrivacyOn(true);
  harness.render().notify({title: `更新先 ${branch}`, detail: branch});
  const notice = harness.notices.at(-1);
  assert.equal(notice.title, `更新先 ${branch}`);
  assert.equal(notice.detail, branch);
  const html = noticeHtml(harness, notice);
  assert.equal(html.includes(branch), false);
  assert.ok(html.includes(`更新先 ${harness.privacy.MASK}`));
  assert.ok(html.includes(`<p class="body-small">${harness.privacy.MASK}</p>`));
  harness.render().setPrivacyOn(false);
  assert.ok(noticeHtml(harness, notice).includes(branch));
});

test("伏せ字モードで通知の見出しと詳細、短いホーム名のパスも隠す", () => {
  const harness = consoleHarness({});
  const home = ["", "home", "ab"].join("/");
  const profile = { host: "host.invalid", username: null, label: "架空の接続先", bot_dir: `${home}/project` };
  harness.render().setProfiles([profile]);
  harness.render().setPrivacyOn(true);
  harness.render().notify({ title: `架空の接続先 host.invalid`, detail: `${home}/.ssh/config` });
  const notice = harness.notices.at(-1);
  assert.equal(notice.title, "架空の接続先 host.invalid");
  assert.equal(notice.detail, `${home}/.ssh/config`);
  const html = noticeHtml(harness, notice);
  for (const value of [profile.label, profile.host, home]) assert.equal(html.includes(value), false);
  assert.ok(html.includes(`${harness.privacy.MASK} ${harness.privacy.MASK}`));
  assert.ok(html.includes(`${harness.privacy.MASK}/.ssh/config`));
  harness.render().setPrivacyOn(false);
  assert.ok(noticeHtml(harness, notice).includes(`${home}/.ssh/config`));
});

test("#217 通知の描画先はConsoleの現在のPrivacyContext内に一つだけ配置する", () => {
  const current = consoleHarness({}, true).render();
  const children = (node) => Array.isArray(node) ? node.flatMap(children)
    : node && typeof node === "object" ? [node, ...children(node.props?.children)] : [];
  assert.equal(current.tree.props.value, current.privacy);
  assert.equal(children(current.tree).filter((node) => node.type === "ToastViewport").length, 1);
});

test("#217 接続成功時に表示した通知も後から伏せ字へ切り替わる", async () => {
  const profile = {id: "fixture", label: "FICTIONAL_CONNECTED_TARGET"};
  const state = {...connection(profile.id), profile};
  const harness = consoleHarness({connect: async () => state, connection: async () => state});
  harness.render().setSelectedProfileId(profile.id);
  await harness.render().handleConnect();
  const notice = harness.notices.at(-1);
  assert.equal(notice.title, "SSH で接続しました");
  assert.equal(notice.detail, profile.label);
  for (const enabled of [false, true, false]) {
    harness.render().setPrivacyOn(enabled);
    assert.equal(noticeHtml(harness, notice).includes(profile.label), !enabled);
  }
  assert.equal(harness.notices.length, 1);
});

test("#217 通知を保持した後の状態取得で判明したブランチも描画時に隠す", async () => {
  const branch = "private/fictional-later-branch";
  const harness = consoleHarness({status: async () => ({git: {branch}, fetched_at: 1})});
  harness.render().applyConnection(connection("A"));
  harness.render().setPrivacyOn(true);
  harness.render().notify({title: `更新 ${branch}`, detail: branch, action: {label: `確認 ${branch}`, onClick() {}}});
  const notice = harness.notices.at(-1);
  assert.ok(noticeHtml(harness, notice).includes(branch));
  await harness.render().refreshStatus();
  const displayed = noticeHtml(harness, notice);
  assert.equal(displayed.includes(branch), false);
  assert.ok(displayed.includes(`更新 ${harness.privacy.MASK}`));
  assert.ok(displayed.includes(`確認 ${harness.privacy.MASK}`));
  harness.render().setPrivacyOn(false);
  assert.ok(noticeHtml(harness, notice).includes(branch));
  assert.equal(notice.detail, branch);
});

test("#217 保持した再試行通知は切替後も最新の選択先を使う", async () => {
  const calls = [];
  const harness = consoleHarness({connect: async (id) => {calls.push(id); throw new Error("架空の接続失敗");},
    connection: async () => connection(null)});
  harness.render().setSelectedProfileId("fixture-A");
  await harness.render().handleConnect();
  const notice = harness.notices.at(-1);
  harness.render().setSelectedProfileId("fixture-B");
  harness.render().setPrivacyOn(true);
  noticeHtml(harness, notice);
  notice.action.onClick();
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(calls, ["fixture-A", "fixture-B"]);
});

test("ログが途切れたときのツールチップにも伏せ字を適用する", async () => {
  const privacy = { redact: (text) => text.replaceAll("FICTIONAL_HOST", "MASKED") };
  const privacyModule = compile(await source("privacy.ts"), {react: {createContext: (value) => value}});
  const sourceText = (await source("pages/LogsPage.tsx"));
  const jsx = (type, props) => ({ type, props });
  const component = compile(sourceText, { "../privacy": {...privacyModule, usePrivacy: () => privacy },
    "react/jsx-runtime": { jsx, jsxs: jsx }, "../logs": { LEVEL_LABELS: {} } }).StreamIndicator;
  for (const status of ["error", "ended"]) {
    const node = component({ stream: { status, paused: false, message: "FICTIONAL_HOST の接続が切れました" } });
    assert.equal(node.props.title, "MASKED の接続が切れました");
  }
});


test("デプロイ確認は接続先と接続時刻を送信し、古いダイアログは操作しない", async () => {
  const targets = [];
  const harness = consoleHarness({ startDeploy: async (target) => { targets.push(target); return {deploy: null, running: true}; } });
  harness.render().applyConnection(connection("A"));
  harness.render().requestDeploy();
  const first = harness.render().confirm;
  first.onConfirm();
  await Promise.resolve();
  assert.deepEqual(targets, [{profile_id: "A", connected_at: 1}]);
  harness.render().applyConnection(connection("B"));
  first.onConfirm();
  await Promise.resolve();
  assert.equal(targets.length, 1);
});

test("接続変更とアンマウントでサービス操作後のタイマーを解除する", () => {
  const original = globalThis.window;
  const pending = new Map();
  let id = 0;
  globalThis.window = {setTimeout: (callback) => {pending.set(++id, callback); return id;}, clearTimeout: (key) => pending.delete(key)};
  try {
    const runtime = renderer();
    const controllers = compile(controllerSource, {react: runtime.hooks, "./requestScope": scopes});
    const current = runtime.render(controllers.useConnectionScope);
    runtime.commit();
    current.requestScope.current.schedule(() => assert.fail("古いタイマー"), 2500);
    assert.equal(pending.size, 1);
    current.requestScope.current.invalidate();
    assert.equal(pending.size, 0);
    current.requestScope.current.schedule(() => assert.fail("破棄済み画面"), 2500);
    runtime.unmount();
    assert.equal(pending.size, 0);
  } finally { globalThis.window = original; }
});

test("デプロイ完了時には自動更新OFFでもインサイトを強制更新する", async () => {
  let count = 0;
  const forces = [];
  const harness = consoleHarness({
    deploy: async () => ({deploy: {profile_id: "A", status: ++count === 1 ? "running" : "succeeded"}, running: count === 1}),
    status: async () => ({fetched_at: 1}), insights: async (force) => {forces.push(force); return {data: {}, fetched_at: 1};},
  });
  harness.render().applyConnection(connection("A"));
  await harness.render().refreshDeploy();
  await harness.render().refreshDeploy();
  await Promise.resolve();
  assert.deepEqual(forces, [true]);
});

test("状態不明で完了したデプロイは一度だけ通知・状態更新し、定期取得を止める", async (t) => {
  const previousWindow = globalThis.window;
  const timers = new Map();
  let nextTimer = 0;
  globalThis.window = {
    addEventListener: () => {},
    setTimeout: (callback, delay) => { timers.set(++nextTimer, { callback, delay }); return nextTimer; },
    clearTimeout: (id) => timers.delete(id),
  };
  const runtime = renderer();
  t.after(() => { runtime.unmount(); globalThis.window = previousWindow; });
  // 実際のusePollingも動かし、実行中の1秒タイマーが完了後に解除されることを確かめる。
  const hooks = compile(await source("hooks.ts"), { react: runtime.hooks });
  const notices = [];
  let fetches = 0, refreshes = 0, finished = false;
  const { useDeployController } = compile(controllerSource, {
    react: runtime.hooks, "./hooks": hooks, "./status": statusModule,
    "./api": { api: { deploy: async () => {
      fetches++;
      return { running: !finished, deploy: { profile_id: "A", status: finished ? "unknown" : "running", message: "架空の終了未確認" } };
    } } },
  });
  const scope = { requestScope: { current: new scopes.RequestScope() }, wasConnected: { current: true },
    connectionChanging: { current: false }, connectionProfileId: { current: "A" } };
  const render = () => runtime.render(() => useDeployController(scope, (notice) => notices.push(notice), () => refreshes++, true, "A:1"));
  const settle = () => new Promise(setImmediate);
  render(); runtime.commit(); await settle();
  assert.equal(render().deployRunning, true);
  runtime.commit(); await settle();
  assert.deepEqual([...timers.values()].map(({ delay }) => delay), [1000]);
  finished = true;
  const [timerId, timer] = [...timers][0];
  timers.delete(timerId);
  await timer.callback();
  assert.equal(render().deployRunning, false);
  runtime.commit(); await settle();
  assert.equal(timers.size, 0);
  assert.equal(notices.length, 1);
  assert.equal(notices[0].tone, "critical");
  assert.match(notices[0].title, /状態不明/);
  assert.equal(refreshes, 1);
  const stoppedAt = fetches;
  render(); runtime.commit(); await settle();
  assert.equal(fetches, stoppedAt);
  await render().refreshDeploy();
  assert.equal(notices.length, 1);
  assert.equal(refreshes, 1);
});

test("状態不明でタスク完了後もデプロイとサービス操作の確認を開かない", async () => {
  const harness = consoleHarness({
    deploy: async () => ({ running: false, deploy: { profile_id: "A", status: "unknown" } }),
  });
  harness.render().applyConnection(connection("A"));
  await harness.render().refreshDeploy();
  harness.render().requestDeploy();
  assert.equal(harness.render().confirmOpen, false);
  harness.render().requestAction("restart");
  assert.equal(harness.render().confirmOpen, false);
  assert.equal(harness.notices.at(-1).tone, "critical");
  assert.match(harness.notices.at(-1).title, /状態不明/);
});

test("別接続先へ移ってデプロイ記録を捨てても状態不明の警告・案内と操作停止を維持する", async () => {
  const calls = [];
  let snapshot = { ...connection("A"), state_unknown: true };
  const harness = consoleHarness({
    connection: async () => snapshot,
    disconnect: async () => (snapshot = { ...connection(null), state_unknown: true }),
    connect: async (id) => (snapshot = { ...connection(id), state_unknown: true }),
    deploy: async () => ({ running: false, deploy: { profile_id: "A", status: "unknown" } }),
    service: (...args) => calls.push(args), startDeploy: (...args) => calls.push(args),
  }, true);
  harness.render().applyConnection(snapshot);
  await harness.render().refreshDeploy();
  assert.equal(harness.render().deploy.status, "unknown");
  harness.render().setSelectedProfileId("B");
  for (const action of ["handleDisconnect", "handleConnect"]) {
    await harness.render()[action]();
    await harness.render().refreshDeploy();
    const current = harness.render();
    assert.equal(current.deploy, null);
    assert.equal(current.stateUnknown, true);
    const warning = nodes(current.tree).find((node) => node.props?.role === "alert");
    assert.ok(warning);
    const text = JSON.stringify(warning.props.children);
    assert.match(text, /このセッション全体/);
    assert.match(text, /接続先を変えても解除されません/);
    assert.match(text, /残留処理の終了とコード・依存関係・サービスの状態を確認してから/);
    assert.match(text, /バックエンドを起動し直して/);
    current.requestDeploy();
    for (const action of ["start", "stop", "restart"]) harness.render().requestAction(action);
    assert.equal(harness.render().confirmOpen, false);
  }
  const overview = nodes(harness.render().tree).find((node) => node.type === "OverviewPage");
  assert.equal(overview.props.deployUnknown, true);
  harness.render().setStoredPage("operations");
  const operations = nodes(harness.render().tree).find((node) => node.type === "OperationsPage");
  assert.equal(operations.props.stateUnknown, true);
  assert.equal(operations.props.deploy, null);
  assert.deepEqual(calls, []);
  // 手動確認後に再起動したバックエンドが通常状態を返したときだけ解除される。
  harness.render().applyConnection({ ...connection("B"), state_unknown: false });
  const recovered = harness.render();
  assert.equal(recovered.stateUnknown, false);
  assert.equal(nodes(recovered.tree).some((node) => node.props?.role === "alert"), false);
  recovered.requestDeploy();
  assert.equal(harness.render().confirmOpen, true);
});


test("開始前のデプロイGETは開始POSTより遅れても実行中表示を戻さない", async () => {
  const old = deferred(), started = deferred();
  const harness = consoleHarness({deploy: () => old.promise, startDeploy: () => started.promise});
  harness.render().applyConnection(connection("A"));
  const pending = harness.render().refreshDeploy();
  const posting = harness.render().runDeploy({profile_id: "A", connected_at: 1});
  started.resolve({deploy: {profile_id: "A", status: "running"}, running: true});
  await posting;
  old.resolve({deploy: null, running: false});
  await pending;
  assert.equal(harness.render().deploy.status, "running");
});


test("接続前のサービス操作の完了は新接続の実行中状態を解除しない", async () => {
  const old = deferred(), fresh = deferred();
  let calls = 0;
  const harness = consoleHarness({service: () => (++calls === 1 ? old.promise : fresh.promise),
    operations: async () => ({operations: []}), status: async () => ({fetched_at: 1}), insights: async () => ({data: {}, fetched_at: 1})});
  harness.render().applyConnection(connection("A"));
  const first = harness.render().runAction("restart", {profile_id: "A", connected_at: 1});
  assert.equal(harness.render().busyAction, "restart");
  harness.render().applyConnection(connection("B"));
  assert.equal(harness.render().busyAction, null);
  const second = harness.render().runAction("stop", {profile_id: "B", connected_at: 1});
  old.resolve({ok: true, message: "古い完了"});
  await first;
  assert.equal(harness.render().busyAction, "stop");
  // 新操作のfinallyはタイマーを作るので、破棄済み世代にして回収する。
  harness.render().applyConnection(connection(null));
  fresh.resolve({ok: true, message: "新しい完了"});
  await second;
});


test("#218 接続失敗通知はAPIの安全な診断と原文を両方保持する", async () => {
  const owner = "LOCAL_OWNER";
  const raw = "秘密鍵が見つかりません: /" + ["home", owner, ".ssh", "fixture-key"].join("/");
  const safe = "秘密鍵が見つかりません: <LOCAL_PATH>（key_path を確認）";
  const error = Object.assign(new Error(raw), {safeMessage: safe});
  const harness = consoleHarness({connect: async () => {throw error;}, connection: async () => connection(null)});
  harness.render().setSelectedProfileId("fixture");
  harness.render().setPrivacyOn(true);
  await harness.render().handleConnect();
  const notice = harness.notices.at(-1);
  assert.equal(notice.detail, raw);
  assert.equal(notice.detailSafe, safe);
  assert.equal(noticeHtml(harness, notice).includes(owner), false);
  assert.ok(noticeHtml(harness, notice).includes("key_path"));
  harness.render().setPrivacyOn(false);
  assert.ok(noticeHtml(harness, notice).includes(raw));
  assert.equal(harness.notices.length, 1);
});

test("#218 接続状態からの通知も保持された安全な診断を使う", () => {
  const harness = consoleHarness({});
  const raw = "known_hosts が見つかりません: /" + ["home", "LOCAL_OWNER", ".ssh", "known_hosts"].join("/");
  const safe = "known_hosts が見つかりません: <LOCAL_PATH>（場所を確認）";
  harness.render().applyConnection(connection("A"));
  harness.render().setPrivacyOn(true);
  harness.render().applyConnection({...connection(null), last_error: raw, last_error_safe: safe});
  const notice = harness.notices.at(-1);
  assert.equal(notice.detail, raw);
  assert.equal(notice.detailSafe, safe);
  assert.equal(noticeHtml(harness, notice).includes("LOCAL_OWNER"), false);
  assert.ok(noticeHtml(harness, notice).includes("場所を確認"));
  harness.render().setPrivacyOn(false);
  assert.ok(noticeHtml(harness, notice).includes(raw));
});
