import assert from "node:assert/strict";
import test from "node:test";
import { compileTs, readSource } from "./helpers/compile.mjs";

test("モーダル表示中に初期フォーカス参照が変わっても元の要素へ戻る", async () => {
  const source = await readSource("hooks.ts");
  const slots = []; let cursor = 0, queued = [], timer;
  const react = {
    useRef(value) {const index = cursor++; return slots[index] ??= {current: value};},
    useEffect(callback, deps) {
      const index = cursor++, old = slots[index];
      if (!old || deps.some((value, i) => value !== old.deps[i])) {
        queued.push(() => {old?.cleanup?.(); slots[index] = {deps, cleanup: callback()};});
      }
    },
  };
  const original = {window: globalThis.window, document: globalThis.document};
  const previous = {focus() {globalThis.document.activeElement = this;}};
  const inside = {focus() {globalThis.document.activeElement = this;}};
  globalThis.document = {activeElement: previous, contains: () => true};
  globalThis.window = {setTimeout(fn) {timer = fn; return 1;}, clearTimeout() {}, addEventListener() {}, removeEventListener() {}};
  try {
    const exports = compileTs(source, { react });
    const container = {current: {querySelector: () => inside}};
    const render = (open, initial) => {cursor = 0; exports.useModalFocus(open, container, initial); queued.splice(0).forEach((fn) => fn());};
    render(true, {current: inside});
    timer();
    assert.equal(document.activeElement, inside);
    render(true, {current: inside});
    render(false, {current: inside});
    assert.equal(document.activeElement, previous);
  } finally {
    globalThis.window = original.window;
    globalThis.document = original.document;
  }
});
