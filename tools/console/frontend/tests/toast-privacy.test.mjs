import assert from "node:assert/strict";
import test from "node:test";
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import {renderToStaticMarkup} from "react-dom/server";
import { compile, readSource as source } from "./helpers/compile.mjs";
import { nodes } from "./helpers/tree.mjs";

const privacySource = await source("privacy.ts"), toastSource = await source("components/toast.tsx");
const privacy = compile(privacySource, {react: React});
const {Snackbar} = compile(toastSource, {
  react: React, "react/jsx-runtime": jsxRuntime, "../privacy": privacy, "./Icon": {Icon: () => null},
});
const secret = "FICTIONAL_PRIVATE_LABEL", laterSecret = "FICTIONAL_LATER_BRANCH";
const noop = () => {};
const saved = Object.freeze({id: 1, leaving: false, tone: "critical", title: `通知 ${secret}`,
  detail: `詳細 ${secret} ${laterSecret}`, action: Object.freeze({label: `再試行 ${secret}`, onClick: noop})});
const mode = (enabled, secrets = [secret]) => privacy.createPrivacy(enabled, secrets);
function html(toast, current) {
  return renderToStaticMarkup(React.createElement(privacy.PrivacyContext.Provider, {value: current},
    React.createElement(Snackbar, {toast, onDismiss: noop})));
}

test("#217 同じ通知の見出し・詳細・操作ラベルと全属性は現在の伏せ字設定に従う", () => {
  for (const enabled of [false, true, false, true]) {
    const displayed = html(saved, mode(enabled));
    assert.equal(displayed.includes(secret), !enabled);
    assert.ok(displayed.includes('role="alert"'));
    if (enabled) {
      assert.ok(displayed.includes(`通知 ${privacy.MASK}`));
      assert.ok(displayed.includes(`詳細 ${privacy.MASK}`));
      assert.ok(displayed.includes(`再試行 ${privacy.MASK}`));
    }
  }
  assert.equal(saved.title, `通知 ${secret}`);
  assert.equal(saved.action.label, `再試行 ${secret}`);
});

test("#217 通知作成後に判明した秘密値も既存通知の描画へ反映する", () => {
  assert.ok(html(saved, mode(true)).includes(laterSecret));
  assert.equal(html(saved, mode(true, [secret, laterSecret])).includes(laterSecret), false);
  assert.ok(html(saved, mode(false, [secret, laterSecret])).includes(laterSecret));
});

test("#217 原文と安全な診断を保持して切替ごとに現在の秘密値も適用する", () => {
  const raw = `架空のローカル診断 LOCAL_OWNER ${secret}`;
  const safe = `設定先 <LOCAL_PATH> ${secret}`;
  const toast = Object.freeze({...saved, detail: raw, detailSafe: safe});
  for (const enabled of [false, true, false]) {
    const displayed = html(toast, mode(enabled));
    assert.equal(displayed.includes("LOCAL_OWNER"), !enabled);
    assert.equal(displayed.includes(secret), !enabled);
    if (enabled) assert.ok(displayed.includes("&lt;LOCAL_PATH&gt;"));
  }
  assert.equal(toast.detail, raw);
  assert.equal(toast.detailSafe, safe);
});

// 状態・Effect・時計を置換し、実Provider/useToast/Viewport/Snackbarの保持と操作を実行する。
function stateRuntime() {
  const slots = []; let cursor = 0; const effects = [];
  return {slots,
    useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
      return [slots[index], (value) => {slots[index] = typeof value === "function" ? value(slots[index]) : value;}];},
    useRef(initial) {const index = cursor++; return slots[index] ??= {current: initial};},
    useCallback(callback, deps) {const index = cursor++; const prior = slots[index];
      if (!prior || deps.some((value, i) => value !== prior.deps[i])) slots[index] = {value: callback, deps};
      return slots[index].value;},
    useEffect(callback, deps) {const index = cursor++; const prior = slots[index];
      if (!prior || deps.some((value, i) => value !== prior.deps[i])) effects.push(() => {
        prior?.cleanup?.(); slots[index] = {deps, cleanup: callback()};
      });},
    render(component, props) {cursor = 0; return component(props);},
    commit() {effects.splice(0).forEach((effect) => effect());},
    dispose() {slots.forEach((slot) => slot?.cleanup?.());},
  };
}
function harness(t) {
  let active;
  const react = {createContext(value) {const context = {value}; context.Provider = {context}; return context;},
    useContext: (context) => context.value};
  for (const name of ["useState", "useRef", "useCallback", "useEffect"]) react[name] = (...args) => active[name](...args);
  const p = compile(privacySource, {react});
  const jsx = (type, props) => ({type, props});
  const toast = compile(toastSource, {react, "react/jsx-runtime": {jsx, jsxs: jsx}, "../privacy": p, "./Icon": {Icon: "svg"}});
  const providerState = stateRuntime(), bars = new Map();
  const render = (state, component, props) => {active = state; const tree = state.render(component, props); state.commit(); return tree;};
  let enabled = false, secrets = [secret], now = 0, nextId = 1;
  const timers = new Map(), previousWindow = globalThis.window, previousNow = Date.now;
  globalThis.window = {
    setTimeout(callback, delay) {const id = nextId++; timers.set(id, {callback, at: now + delay}); return id;},
    clearTimeout(id) {timers.delete(id);},
  };
  Date.now = () => now;
  t.after(() => {bars.forEach((state) => state.dispose()); globalThis.window = previousWindow; Date.now = previousNow;});
  function store() {
    const tree = render(providerState, toast.ToastProvider, {children: "架空の画面"});
    for (const node of nodes(tree)) if (node.type?.context) node.type.context.value = node.props.value;
    p.PrivacyContext.value = p.createPrivacy(enabled, secrets);
    return tree;
  }
  function view() {const root = store(); return typeof toast.ToastViewport === "function"
    ? render(stateRuntime(), toast.ToastViewport) : root;}
  function snapshot() {
    const items = nodes(view()).filter((node) => node.type?.name === "Snackbar");
    return items.map((node) => {
      const state = bars.get(node.props.toast.id) ?? stateRuntime(); bars.set(node.props.toast.id, state);
      return render(state, node.type, node.props);
    });
  }
  store(); const notify = render(stateRuntime(), toast.useToast);
  return {notify, snapshot, store, saved: () => providerState.slots[0], timers,
    mode(next, known = secrets) {enabled = next; secrets = known;},
    advance(delta) {const end = now + delta;
      for (;;) {const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > end) break; timers.delete(next[0]); now = next[1].at; next[1].callback();}
      now = end;},
  };
}
const display = (tree) => JSON.stringify(tree);

test("#217 伏せ字中の登録でも原文を保存し解除後は同じ通知で復帰する", (t) => {
  const h = harness(t); h.mode(true); h.notify(saved);
  assert.equal(h.saved()[0].title, saved.title);
  assert.equal(h.saved()[0].detail, saved.detail);
  assert.equal(h.saved()[0].action, saved.action);
  assert.equal(display(h.snapshot()).includes(secret), false);
  const id = h.saved()[0].id;
  h.mode(false); assert.ok(display(h.snapshot()).includes(secret));
  assert.equal(h.saved()[0].id, id);
});

test("#217 表示済み通知を再登録せずoffからonへ切り替える", (t) => {
  const h = harness(t); h.notify(saved);
  assert.ok(display(h.snapshot()).includes(secret));
  const stored = h.saved()[0];
  h.mode(true); assert.equal(display(h.snapshot()).includes(secret), false);
  assert.equal(h.saved().length, 1); assert.equal(h.saved()[0], stored);
  h.mode(false); assert.ok(display(h.snapshot()).includes(secret));
});

test("#217 保持された通知は後から増えた秘密値へ追従する", (t) => {
  const h = harness(t); h.mode(true); h.notify(saved);
  assert.ok(display(h.snapshot()).includes(laterSecret));
  const stored = h.saved()[0];
  h.mode(true, [secret, laterSecret]);
  const displayed = display(h.snapshot());
  assert.equal(displayed.includes(laterSecret), false);
  assert.equal(displayed.includes(secret), false);
  assert.equal(h.saved()[0], stored);
});

for (const hold of ["hover", "focus"]) {
  test(`#217 ${hold}で保持した通知も切替に追従し残り表示時間を維持する`, (t) => {
    const h = harness(t); h.notify(saved); let tree = h.snapshot()[0];
    h.advance(1200);
    tree.props[hold === "hover" ? "onMouseEnter" : "onFocus"](); h.snapshot();
    assert.equal(h.timers.size, 0);
    h.advance(20000); h.mode(true); tree = h.snapshot()[0];
    assert.equal(display(tree).includes(secret), false);
    assert.equal(h.saved()[0].leaving, false);
    // フォーカスが内部へ移る場合は保持を続ける。
    if (hold === "focus") {tree.props.onBlur({currentTarget: {contains: () => true}, relatedTarget: {}}); h.snapshot(); assert.equal(h.timers.size, 0);}
    h.mode(false); tree = h.snapshot()[0]; assert.ok(display(tree).includes(secret));
    if (hold === "hover") tree.props.onMouseLeave();
    else tree.props.onBlur({currentTarget: {contains: () => false}, relatedTarget: null});
    h.snapshot();
    assert.equal([...h.timers.values()][0].at, 28000);
    h.advance(6799); assert.equal(h.saved()[0].leaving, false);
    h.advance(1); assert.equal(h.saved()[0].leaving, true);
    h.snapshot(); h.advance(260); assert.equal(h.saved().length, 0);
  });
}

test("#217 伏せ字と秘密値の変更では通知の消去タイマーを作り直さない", (t) => {
  const h = harness(t); h.notify({...saved, action: undefined}); h.snapshot();
  const timer = [...h.timers][0]; assert.equal(timer[1].at, 5000);
  h.advance(2300); h.mode(true, [secret, laterSecret]); h.snapshot();
  assert.equal([...h.timers][0][0], timer[0]);
  assert.equal([...h.timers][0][1].at, 5000);
  h.advance(2700); assert.equal(h.saved()[0].leaving, true);
  h.snapshot(); h.advance(260); assert.equal(h.saved().length, 0);
});

test("#217 通知の上限と手動消去の退出時間を維持する", (t) => {
  const h = harness(t);
  for (let i = 0; i < 5; i++) h.notify({...saved, title: `架空通知${i}`});
  assert.deepEqual(h.saved().map((item) => item.title), ["架空通知2", "架空通知3", "架空通知4"]);
  const tree = h.snapshot()[0];
  nodes(tree).find((node) => node.props?.["aria-label"] === "閉じる").props.onClick();
  assert.equal(h.saved()[0].leaving, true);
  assert.equal(h.saved().length, 3);
  h.snapshot(); h.advance(259); assert.equal(h.saved().length, 3);
  h.advance(1); assert.equal(h.saved().length, 2);
});

test("#217 伏せ字後も再試行actionをそのまま呼び通知を閉じる", (t) => {
  const h = harness(t); const calls = [];
  const retry = () => calls.push("架空の再試行");
  h.notify({...saved, action: {label: `再試行 ${secret}`, onClick: retry}}); h.mode(true);
  const tree = h.snapshot()[0]; assert.equal(display(tree).includes(secret), false);
  nodes(tree).find((node) => node.props?.className === "m3-snackbar-action label-large").props.onClick();
  assert.deepEqual(calls, ["架空の再試行"]);
  assert.equal(h.saved()[0].action.onClick, retry);
  assert.equal(h.saved()[0].leaving, true);
});

test("#217 通知の保存Providerは描画せず現在の伏せ字設定内のViewportへ渡す", (t) => {
  const h = harness(t); h.notify(saved);
  assert.equal(nodes(h.store()).filter((node) => node.type?.name === "Snackbar").length, 0);
  assert.equal(h.snapshot().length, 1);
});
