import assert from "node:assert/strict";
import test from "node:test";
import { compileTs, readSource } from "./helpers/compile.mjs";

test("ショートカットは最新状態を使い、終了時にリスナーとタイマーを回収する", async () => {
  const source = await readSource("useConsoleShortcuts.ts");
  let state = false, handler, cleanup, timer, refs = [], index = 0;
  const react = {
    useState: () => [state, (value) => {state = value;}],
    useRef: (value) => refs[index++] ??= {current: value},
    useEffect: (fn) => {if (!cleanup) cleanup = fn();},
  };
  const oldWindow = globalThis.window;
  globalThis.window = {
    addEventListener: (_, fn) => {handler = fn;}, removeEventListener: (_, fn) => {assert.equal(fn, handler); handler = null;},
    setTimeout: (fn) => {timer = fn; return 1;}, clearTimeout: () => {timer = null;},
  };
  try {
    const exports = compileTs(source, (name) => name === "react" ? react : {
      PAGES: [{id: "overview", goKey: "o"}, {id: "logs", goKey: "l"}],
    });
    const pages = [], refreshed = [];
    const options = {page: "overview", connected: true, paused: false, setPaused() {}, confirmOpen: false,
      setConfirmOpen() {}, helpOpen: false, setHelpOpen() {}, modalRailOpen: false, setModalRailOpen() {},
      setAutoRefresh() {}, filterInputRef: {current: null}, navigate: (page) => pages.push(page),
      manualRefresh: () => refreshed.push(true), toggleRail() {}, togglePrivacy() {}, toggleTheme() {}};
    const render = () => {index = 0; return exports.useConsoleShortcuts(options);};
    const key = (value, target = null) => handler({key: value, target, preventDefault() {}});
    render();
    key("g");
    assert.equal(render(), true);
    key("l");
    assert.deepEqual(pages, ["logs"]);
    render();
    key("r", {tagName: "INPUT", closest: () => false});
    assert.equal(refreshed.length, 0);
    key("r");
    assert.equal(refreshed.length, 1);
    options.modalRailOpen = true;
    const closes = [];
    options.setModalRailOpen = (value) => closes.push(value);
    render();
    key("r");
    assert.equal(refreshed.length, 1);
    key("j");
    assert.deepEqual(closes, [false]);
    assert.equal(pages.at(-1), "logs");
    options.modalRailOpen = false;
    render();
    key("g");
    assert.ok(timer);
    cleanup();
    assert.equal(handler, null);
    assert.equal(timer, null);
  } finally {
    globalThis.window = oldWindow;
  }
});
