import assert from "node:assert/strict";
import test from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import * as jsxRuntime from "react/jsx-runtime";
import { compile, readSource as source } from "./helpers/compile.mjs";

const client = compile(await source("api.ts"), {}, "module.ts");
const privacy = compile(await source("privacy.ts"), {react: React});
const connectionSource = await source("pages/ConnectionPage.tsx");
const toast = compile((await source("components/toast.tsx")), {
  react: React, "react/jsx-runtime": jsxRuntime, "../privacy": privacy, "./Icon": {Icon: () => null},
});
const owner = "LOCAL_OWNER";
const path = "/" + ["home", owner, ".ssh", "fixture-key"].join("/");
const raw = `秘密鍵が見つかりません: ${path}`;
const safe = "秘密鍵が見つかりません: <LOCAL_PATH>（profiles.json の key_path の場所を確かめてください）";
const remote = {id: "remote", username: "REMOTE_OWNER", host: "remote.invalid", bot_dir: "/srv/fixture"};

async function apiFailure(t, body) {
  const before = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(body), {status: 502});
  try {await client.api.connect("remote"); assert.fail("接続エラーになるはずです");}
  catch(error) {assert.ok(error instanceof client.ApiError); return error;}
  finally {globalThis.fetch = before;}
}

function page(state, history, enabled) {
  const p = privacy.createPrivacy(enabled, privacy.collectSecrets({profile: remote, profiles: [remote], connection: null, insights: null}));
  const wrap = ({children}) => React.createElement("section", null, children);
  const {ConnectionPage} = compile(connectionSource, {
    react: {...React, useState: () => [history, () => {}]}, "react/jsx-runtime": jsxRuntime,
    "../privacy": privacy, "../hooks": {usePolling() {}}, "../api": {api: {}},
    "../components/Icon": {Icon: () => null}, "../components/TopBar": {UnsetTag: () => null},
    "../components/ui": {Card: wrap, Button: wrap, InlineError: wrap, Notice: wrap, EmptyState: wrap, StatusChip: () => null},
    "../format": {formatDateTime: () => "架空の時刻", formatRelative: () => "架空の時間"},
  });
  return renderToStaticMarkup(React.createElement(privacy.PrivacyContext.Provider, {value: p},
    React.createElement(ConnectionPage, {profiles: [], connection: state, now: 0})));
}
const historyEntry = (error, error_safe) => ({at: 1, label: "SSH 接続", command: "ssh remote.invalid", exit_status: null,
  duration_ms: 1, ok: false, error, error_safe});

test("#218 APIは通常の診断を維持して安全な文面も保持する", async (t) => {
  const error = await apiFailure(t, {detail: raw, detail_safe: safe});
  assert.equal(error.message, raw);
  assert.equal(client.errorMessage(error), raw);
  assert.equal(error.status, 502);
  assert.equal(error.safeMessage, safe);
});

test("#218 安全な文面なしのAPIと型が違う付加情報は従来の診断を維持する", async (t) => {
  for (const detail_safe of [undefined, null, {text: "不正な型"}]) {
    const error = await apiFailure(t, {detail: "架空の既存エラー", detail_safe});
    assert.equal(error.message, "架空の既存エラー");
    assert.equal(error.safeMessage, undefined);
  }
});

test("#218 遠隔の値に依存せず安全な診断を選択し非伏せ字で原文へ戻す", () => {
  assert.equal(typeof privacy.redactError, "function");
  const secrets = privacy.collectSecrets({profile: remote, profiles: [remote], connection: null, insights: null});
  assert.equal(secrets.includes(owner), false);
  assert.equal(privacy.redactError(raw, safe, privacy.createPrivacy(true, secrets)), safe);
  assert.equal(privacy.redactError(raw, safe, privacy.createPrivacy(false, secrets)), raw);
  assert.equal(privacy.redactError("remote.invalid でエラー", undefined, privacy.createPrivacy(true, secrets)), `${privacy.MASK} でエラー`);
});

test("#218 接続状態と履歴の本文・tooltipは同じ安全な診断に切り替わる", () => {
  const state = {connected: false, last_error: raw, last_error_safe: safe};
  const history = [historyEntry(raw, safe)];
  for (const enabled of [false, true, false]) {
    const html = page(state, history, enabled);
    assert.equal(html.includes(owner), !enabled);
    if (enabled) {
      assert.ok(html.includes("key_path"));
      assert.ok(html.includes("&lt;LOCAL_PATH&gt;"));
      assert.ok(html.includes(`title="秘密鍵が見つかりません: &lt;LOCAL_PATH&gt;`));
    } else assert.ok(html.includes(path));
  }
  assert.equal(state.last_error, raw);
  assert.equal(history[0].error, raw);
});

test("#218 接続切替後も保持した履歴は安全に表示し通常エラー・通常コマンドを保つ", () => {
  const history = [historyEntry(raw, safe), historyEntry("架空の一般エラー", null), {...historyEntry(null, null), ok: true}];
  const masked = page({connected: true, profile: remote, last_error: null}, history, true);
  assert.equal(masked.includes(owner), false);
  assert.ok(masked.includes("架空の一般エラー"));
  assert.ok(masked.includes(`ssh ${privacy.MASK}`));
  assert.ok(page({connected: true, profile: remote, last_error: null}, history, false).includes(path));
});

test("#218 通知の安全表示でも原文と安全な文面の保存情報を変えない", () => {
  const saved = {tone: "critical", title: "接続できませんでした", detail: raw, detailSafe: safe,
    id: 1, leaving: false};
  for (const enabled of [false, true, false]) {
    const html = renderToStaticMarkup(React.createElement(privacy.PrivacyContext.Provider, {value: privacy.createPrivacy(enabled, [])},
      React.createElement(toast.Snackbar, {toast: saved, onDismiss() {}})));
    assert.equal(html.includes(owner), !enabled);
    if (enabled) assert.ok(html.includes("key_path"));
    else assert.ok(html.includes(path));
  }
  assert.equal(saved.detail, raw);
  assert.equal(saved.detailSafe, safe);
});


test("#218 Windows形式の診断もAPIから状態・履歴・描画まで安全な文面を維持する", async (t) => {
  const windowsPath = ["Q:", "Users", owner, ".ssh", "fixture-key"].join(String.fromCharCode(92));
  const windowsRaw = `秘密鍵が見つかりません: ${windowsPath}`;
  const error = await apiFailure(t, {detail: windowsRaw, detail_safe: safe});
  assert.equal(error.safeMessage, safe);
  const state = {connected: false, last_error: error.message, last_error_safe: error.safeMessage};
  const history = [historyEntry(error.message, error.safeMessage)];
  assert.equal(page(state, history, true).includes(owner), false);
  assert.ok(page(state, history, false).includes(windowsPath));
});
