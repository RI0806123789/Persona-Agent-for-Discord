import assert from "node:assert/strict";
import test from "node:test";
import { compileTs, readSource as source } from "./helpers/compile.mjs";

const logs = compileTs(await source("logs.ts"));
const logPrivacy = compileTs(await source("logPrivacy.ts"));
const privacy = compileTs(await source("privacy.ts"), {
  react: { createContext: (value) => value }, "./logPrivacy": logPrivacy,
});
const hooksSource = await source("useLogStream.ts");

// Reactの状態とEffect、WebSocket、時計だけを置き換え、実際のフックのイベント処理を実行する。
function harness(t, { throws = false } = {}) {
  const slots = [];
  const effects = [];
  const sockets = [];
  const timeouts = new Map();
  const intervals = new Map();
  let cursor = 0;
  let timer = 0;
  let enabled = true;
  let key = "first";
  const previous = { window: globalThis.window, WebSocket: globalThis.WebSocket };
  globalThis.window = {
    addEventListener() {},
    setTimeout: (callback) => { timeouts.set(++timer, callback); return timer; },
    clearTimeout: (id) => timeouts.delete(id),
    setInterval: (callback) => { intervals.set(++timer, callback); return timer; },
    clearInterval: (id) => intervals.delete(id),
  };
  globalThis.WebSocket = class {
    constructor() {
      if (throws) throw new Error("架空の接続拒否");
      this.closed = false;
      sockets.push(this);
    }
    close() { this.closed = true; }
    message(data) { this.onmessage?.({ data: JSON.stringify(data) }); }
  };
  const react = {
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], (next) => { slots[index] = typeof next === "function" ? next(slots[index]) : next; }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
    useCallback: (callback) => callback,
    useEffect(callback, dependencies) {
      const index = cursor++;
      const old = slots[index];
      if (!old || dependencies.some((value, i) => value !== old.dependencies[i])) {
        effects.push(() => {
          old?.cleanup?.();
          slots[index] = { dependencies, cleanup: callback() };
        });
      }
    },
  };
  const exports = compileTs(hooksSource, {
    react,
    "./api": { logStreamUrl: () => "ws://console.invalid/logs" },
    "./logs": logs, "./logPrivacy": logPrivacy,
  });
  const render = () => { cursor = 0; return exports.useLogStream(enabled, 300, key); };
  const read = () => { render(); effects.splice(0).forEach((effect) => effect()); return render(); };
  t.after(() => {
    slots.forEach((slot) => slot?.cleanup?.());
    globalThis.window = previous.window;
    globalThis.WebSocket = previous.WebSocket;
  });
  read();
  return {
    sockets, timeouts, intervals, read,
    disable() { enabled = false; return read(); },
    switchConnection() { key = "second"; return read(); },
    flush() { for (const callback of intervals.values()) callback(); return read(); },
  };
}

const entry = { ts: 1, message: "架空のログ", priority: 6, source: "python" };

test("壊れたログの項目と終了理由はエラーにして既読ログを保持する", async (t) => {
  for (const broken of [null, {}, {...entry, ts: "1"}, {...entry, message: []},
    {...entry, source: {}}, {...entry, priority: "6"}, {...entry, pid: []}]) {
    await t.test(JSON.stringify(broken), (child) => {
      const h = harness(child);
      const socket = h.sockets[0];
      socket.onopen();
      socket.message({type: "entry", entry});
      socket.message({type: "entry", entry: broken});
      const stream = h.flush();
      assert.equal(stream.status, "error");
      assert.deepEqual(stream.entries.map(({messagePrivacy, ...raw}) => raw), [entry]);
      assert.equal(socket.closed, true);
      assert.equal(h.timeouts.size, 0);
    });
  }
  const h = harness(t);
  h.sockets[0].message({type: "end", message: {invalid: true}});
  assert.equal(h.read().status, "error");
});

test("バックエンドのエラー理由と受信済みログを残し、自動再接続しない", (t) => {
  const h = harness(t);
  const socket = h.sockets[0];
  socket.onopen();
  socket.message({ type: "entry", entry });
  socket.message({ type: "error", message: "架空のログ取得権限エラー" });
  socket.onerror();
  socket.onclose({ code: 1008 });
  const stream = h.flush();
  assert.equal(stream.status, "error");
  assert.equal(stream.message, "架空のログ取得権限エラー");
  assert.deepEqual(stream.entries.map(({messagePrivacy, ...raw}) => raw), [entry]);
  assert.equal(h.timeouts.size, 0);
  assert.equal(h.sockets.length, 1);
});

test("#288 SSHエラーの原文と安全な文面を保持し、再接続・接続切替で解除する", (t) => {
  const h = harness(t);
  const raw = "架空のローカル鍵の診断";
  const safe = "秘密鍵が見つかりません: <LOCAL_PATH>";
  const fail = () => h.sockets.at(-1).message({type: "error", message: raw, message_safe: safe});
  fail();
  h.sockets[0].onclose({code: 1000});
  assert.equal(h.read().message, raw);
  assert.equal(h.read().messageSafe, safe);
  h.read().retry();
  assert.equal(h.read().messageSafe, null);
  fail();
  assert.equal(h.read().messageSafe, safe);
  h.switchConnection();
  assert.equal(h.read().messageSafe, null);
  assert.equal(h.read().message, null);
});

test("#288 安全な文面が無い旧形式や不正な型は原文だけを保持する", (t) => {
  const h = harness(t);
  for (const message_safe of [undefined, null, {text: "不正な型"}]) {
    h.sockets.at(-1).message({type: "error", message: "架空の一般エラー", message_safe});
    assert.equal(h.read().message, "架空の一般エラー");
    assert.equal(h.read().messageSafe, null);
    h.read().retry();
    h.read();
  }
});

test("Origin拒絶・異常切断は理由を表示して停止する", (t) => {
  const h = harness(t);
  h.sockets[0].onclose({ code: 1008 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /1008/);
  assert.equal(h.timeouts.size, 0);
});

test("WebSocketのerrorが先に届いても、close後にエラーを維持する", (t) => {
  const h = harness(t);
  h.sockets[0].onerror();
  h.sockets[0].onclose({ code: 1006 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /通信に失敗/);
  assert.equal(h.timeouts.size, 0);
});

test("正常終了とサーバーの終了通知でも自動再接続しない", (t) => {
  const h = harness(t);
  h.sockets[0].message({ type: "end", message: "架空のサーバー終了" });
  h.sockets[0].onclose({ code: 1000 });
  assert.equal(h.read().status, "ended");
  assert.equal(h.read().message, "架空のサーバー終了");
  assert.equal(h.timeouts.size, 0);
});

test("手動再接続で原因を解除し、旧ソケットの遅れたイベントを無視する", (t) => {
  const h = harness(t);
  const old = h.sockets[0];
  old.onerror();
  h.read().retry();
  assert.equal(h.read().status, "connecting");
  assert.equal(h.read().message, null);
  assert.equal(h.sockets.length, 2);
  assert.equal(old.closed, true);
  old.message({ type: "error", message: "古いエラー" });
  old.onclose({ code: 1008 });
  old.onopen();
  h.sockets[1].onopen();
  assert.equal(h.read().status, "live");
  assert.equal(h.read().message, null);
  assert.equal(h.timeouts.size, 0);
});

test("SSH切断で受信を閉じ、接続先の切替では新しいソケットだけを使う", (t) => {
  const h = harness(t);
  const old = h.sockets[0];
  h.switchConnection();
  assert.equal(old.closed, true);
  assert.equal(h.sockets.length, 2);
  h.disable();
  assert.equal(h.read().status, "idle");
  assert.equal(h.sockets[1].closed, true);
  assert.equal(h.intervals.size, 0);
  old.onerror();
  assert.equal(h.read().status, "idle");
});

test("切断直前の未反映ログを残し、接続先の切替では混ぜない", (t) => {
  const h = harness(t);
  h.sockets[0].message({type: "entry", entry});
  assert.deepEqual(h.disable().entries.map(({message}) => message), [entry.message]);
  assert.equal(h.intervals.size, 0);
  h.switchConnection();
  assert.deepEqual(h.read().entries.map(({message}) => message), [entry.message]);
});

test("接続先を直接切り替えると未反映ログは新しい表示へ混ざらない", (t) => {
  const h = harness(t);
  h.sockets[0].message({type: "entry", entry});
  assert.deepEqual(h.switchConnection().entries, []);
  h.sockets[1].message({type: "entry", entry: {...entry, message: "新しい接続先"}});
  assert.deepEqual(h.flush().entries.map(({message}) => message), ["新しい接続先"]);
});

test("ソケット作成失敗と壊れた受信はエラーとして停止する", (t) => {
  const h = harness(t, { throws: true });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /ログに接続できません/);
  assert.equal(h.timeouts.size, 0);
});

test("不正JSONは例外を外へ出さずに原因を残す", (t) => {
  const h = harness(t);
  const socket = h.sockets[0];
  socket.onmessage({ data: "not-json" });
  socket.onclose({ code: 1000 });
  assert.equal(h.read().status, "error");
  assert.match(h.read().message, /応答を読み取れません/);
  assert.equal(socket.closed, true);
  assert.equal(h.timeouts.size, 0);
});

test("一時停止中の新着は再開時に反映できる", (t) => {
  const h = harness(t);
  h.read().setPaused(true);
  h.sockets[0].message({ type: "entry", entry });
  assert.equal(h.flush().pendingCount, 1);
  assert.equal(h.read().entries.length, 0);
  h.read().setPaused(false);
  assert.deepEqual(h.flush().entries.map(({messagePrivacy, ...raw}) => raw), [entry]);
  assert.equal(h.read().pendingCount, 0);
});

// #215は実際のフックで受信・保持・クリア・停止を通し、実際の伏せ字関数で確かめる。
const privateEntry = (message, overrides = {}) => ({ ...entry, pid: 42, message, ...overrides });
function assertPrivate(stream) {
  const flags = privacy.conversationLines(stream.entries);
  for (const [index, row] of stream.entries.entries()) {
    assert.ok(row.messagePrivacy === "unknown" || row.messagePrivacy === "conversation");
    assert.equal(privacy.redactLogLine(row.message, flags[index], privacy.createPrivacy(true, [])), privacy.MASK);
    assert.equal(privacy.redactLogLine(row.message, flags[index], privacy.createPrivacy(false, [])), row.message);
  }
}

test("#215 初回途中からの受信はサーバーの偽属性を採用せず隠す", (t) => {
  const h = harness(t);
  h.sockets[0].message({ type: "entry", entry: privateEntry("架空の応答途中", {messagePrivacy: "system"}) });
  assertPrivate(h.flush());
});

test("#215 保持上限と表示クリア後も受信時の会話属性を維持する", (t) => {
  const h = harness(t), socket = h.sockets[0];
  socket.message({ type: "entry", entry: privateEntry("AI：架空の開始") });
  for (let i = 0; i < logs.MAX_LOG_ENTRIES + 10; i++) {
    socket.message({ type: "entry", entry: privateEntry(`架空の本文${i}`) });
  }
  let stream = h.flush();
  assert.equal(stream.entries.length, logs.MAX_LOG_ENTRIES);
  assert.ok(stream.entries.every((row) => row.messagePrivacy === "conversation"));
  assertPrivate(stream);
  stream.clear();
  socket.message({ type: "entry", entry: privateEntry("区切り後も架空の本文") });
  stream = h.flush();
  assert.equal(stream.entries[0].messagePrivacy, "conversation");
  assertPrivate(stream);
});

test("#215 一時停止中の保持上限とクリア後の続きも再開後に隠す", (t) => {
  const h = harness(t), socket = h.sockets[0];
  socket.message({ type: "entry", entry: privateEntry("User (架空)：質問") });
  h.read().setPaused(true);
  for (let i = 0; i < logs.MAX_LOG_ENTRIES + 10; i++) {
    socket.message({ type: "entry", entry: privateEntry(`停止中の本文${i}`) });
  }
  assert.equal(h.flush().entries.length, 0);
  assert.equal(h.read().pendingCount, logs.MAX_LOG_ENTRIES + 11);
  h.read().setPaused(false);
  assertPrivate(h.flush());
  h.read().setPaused(true);
  h.read().clear();
  socket.message({ type: "entry", entry: privateEntry("クリア後の停止中の本文") });
  h.read().setPaused(false);
  const stream = h.flush();
  assert.equal(stream.entries.length, 1);
  assert.equal(stream.entries[0].messagePrivacy, "conversation");
  assertPrivate(stream);
});

test("#215 手動再接続と接続先切替では新しい取得境界を不明に戻す", (t) => {
  const h = harness(t);
  h.sockets[0].message({ type: "entry", entry: privateEntry("AI：前の接続") });
  h.flush();
  for (const change of [() => h.read().retry(), () => h.switchConnection()]) {
    const old = h.sockets.at(-1);
    change(); h.read();
    old.message({ type: "entry", entry: privateEntry("古い応答") });
    h.sockets.at(-1).message({ type: "entry", entry: privateEntry("新しい取得途中") });
    const stream = h.flush();
    assert.equal(stream.entries.length, 1);
    assert.equal(stream.entries[0].messagePrivacy, "unknown");
    assertPrivate(stream);
  }
});


test("接続切替と再接続で一時停止を解除し初期ログを表示する", (t) => {
  const h = harness(t);
  h.read().setPaused(true);
  h.read().retry();
  assert.equal(h.read().paused, false);
  h.sockets.at(-1).message({type: "entry", entry});
  assert.equal(h.flush().entries.length, 1);
  h.read().setPaused(true);
  assert.equal(h.switchConnection().paused, false);
  h.sockets.at(-1).message({type: "entry", entry});
  assert.equal(h.flush().entries.length, 1);
});

test("切断後に再開しても保留ログをすぐ表示する", (t) => {
  const h = harness(t);
  h.read().setPaused(true);
  h.sockets[0].message({type: "entry", entry});
  h.disable();
  h.read().setPaused(false);
  assert.equal(h.read().entries.length, 1);
});
