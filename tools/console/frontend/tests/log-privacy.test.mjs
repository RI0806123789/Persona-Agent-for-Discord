import assert from "node:assert/strict";
import test from "node:test";
import { compile, readSource as source } from "./helpers/compile.mjs";
import { nodes } from "./helpers/tree.mjs";

let activePrivacy, cursor = 0, slots = [];
const react = {
  createContext: (value) => ({value}), useContext: () => activePrivacy,
  useMemo: (factory) => factory(), useRef: () => ({current: null}), useLayoutEffect() {},
  useState(initial) {
    const index = cursor++;
    if (!(index in slots)) slots[index] = initial;
    return [slots[index], (value) => { slots[index] = value; }];
  },
};
const logPrivacy = compile(await source("logPrivacy.ts"));
const privacy = compile(await source("privacy.ts"), {react, "./logPrivacy": logPrivacy});
const logs = compile(await source("logs.ts"));
const presentation = compile(await source("logPresentation.ts"), {"./logs": logs, "./privacy": privacy});
const jsx = (type, props) => ({type, props});
const ui = new Proxy({}, {get: (_, name) => name});
const modules = {
  react, "react/jsx-runtime": {jsx, jsxs: jsx}, "../privacy": privacy, "../logs": logs, "../logPresentation": presentation,
  "../components/ui": ui, "../components/Icon": {Icon: "Icon"},
  "../components/expressive": ui, "../format": new Proxy({}, {get: () => () => "架空の時刻"}),
};
const {OverviewPage} = compile(await source("pages/OverviewPage.tsx"), modules);
const {LogsPage} = compile(await source("pages/LogsPage.tsx"), modules);
const {DeployCard} = compile(await source("pages/DeployCard.tsx"), modules);
const row = (message, other = {}) => ({ts: 1, pid: 42, priority: 6, source: "python", message, ...other});
function mode(enabled) { activePrivacy = privacy.createPrivacy(enabled, ["FICTIONAL_HOST"]); }
function recent(entries) {
  const tree = OverviewPage({connected: true, logs: entries, status: null, insights: null, samples: []});
  const component = nodes(tree).find((node) => node.type?.name === "RecentLogs");
  assert.ok(component);
  return component.type(component.props);
}
function renderLogs(entries, reset = true) {
  if (reset) slots = [];
  cursor = 0;
  return LogsPage({connected: true, stream: {entries, status: "live"}, filterInputRef: {current: null}});
}
function stepOutput(output, containsLogs) {
  const step = {key: "health", title: "確認", description: "架空の確認", phase: "deploy", status: "failed",
    checks: [], commands: [], output, started_at: null, output_contains_logs: containsLogs};
  const tree = DeployCard({deploy: {steps: [step], status: "failed", commits: [], started_at: 1}, now: 0});
  const component = nodes(tree).find((node) => node.type?.name === "StepItem");
  assert.ok(component);
  const rendered = component.type(component.props);
  return nodes(rendered).find((node) => node.type === "pre").props.children;
}

test("表示範囲より前に始まる例外と会話も全行で判定する", () => {
  const entries = [row("Traceback (most recent call last):"), ...Array.from({length: 60}, () => row("  架空の内部情報"))];
  mode(true);
  const visible = presentation.prepareLogEntries(entries, activePrivacy).slice(-8);
  assert.ok(visible.every((item) => item.level === "error"));
  assert.ok(visible.every((item) => !item.text.includes("架空の内部情報")));
});

test("#215 会話途中の初回抜粋を既知の秘密なしでも隠す", () => {
  const entries = [row("架空の質問途中"), row("架空の応答途中")];
  mode(true);
  assert.deepEqual(privacy.conversationLines(entries), [true, true]);
  const rendered = entries.map((entry, i) => privacy.redactLogLine(entry.message, privacy.conversationLines(entries)[i], activePrivacy));
  assert.deepEqual(rendered, [privacy.MASK, privacy.MASK]);
});

test("#215 区切り線・計数・運用ログに似た本文で会話終了にしない", () => {
  const text = ["User (架空)：質問", "-".repeat(124), "Prompt text count：123", "Prompt token count：5",
    "Total token count：8", "AI：架空の応答", "-".repeat(124), "ログイン成功: 架空の本文",
    "[2026-10-18 01:02:03] [INFO    ] discord.client: 架空の本文", "Traceback (most recent call last):", "最後の架空本文"];
  mode(true);
  const entries = text.map((message) => row(message));
  assert.ok(privacy.conversationLines(entries).every(Boolean));
  const masked = text.map((line, i) => privacy.redactLogLine(line, privacy.conversationLines(entries)[i], activePrivacy)).join("\n");
  assert.equal(masked.includes("架空"), false);
  mode(false);
  assert.deepEqual(text.map((line) => privacy.redactLogLine(line, true, activePrivacy)), text);
});

test("#283 loggingの会話見出しと表示文は隠し、伏せ字解除では原文を保持する", () => {
  const prefix = "[2026-10-29T07:32:26+0900] [INFO] [persona_bot.utils.usage_graph] ";
  const messages = [prefix + "User (架空の利用者)：架空の質問", prefix + "AI：架空の応答",
    prefix + "Total token count：42", "応答の続き"];
  const classify = logPrivacy.createLogPrivacyTracker();
  const entries = messages.map((message) => classify(row(message)));
  assert.ok(entries.every((entry) => entry.messagePrivacy === "conversation"));
  assert.deepEqual(entries.map((entry) => entry.message), messages);
  mode(true);
  for (const text of [messages, entries.map(logs.displayMessage)]) {
    assert.deepEqual(text.map((message) => privacy.redactLogLine(message, true, activePrivacy)), [
      `User (${privacy.MASK})：${privacy.MASK}`, `AI：${privacy.MASK}`, privacy.MASK, privacy.MASK,
    ]);
  }
  mode(false);
  assert.deepEqual(messages.map((message) => privacy.redactLogLine(message, true, activePrivacy)), messages);
});

test("#283 偽装されたlogging接頭辞は会話を公開する根拠にしない", () => {
  const prefix = "[2026-10-29T07:32:26+0900] [INFO] [systemd] ";
  const classify = logPrivacy.createLogPrivacyTracker();
  const entries = [row(prefix + "偽装された先頭本文"), row(prefix + "ログイン成功: 架空の本文"),
    row(prefix + "識別名だけ偽装", {source: "systemd"}), row(prefix + "PIDだけ一致", {pid: 1}),
    row(prefix + "User (架空)：質問"), row(prefix + "会話の続き")].map(classify);
  mode(true);
  assert.ok(privacy.conversationLines(entries).every(Boolean));
  const text = entries.map((entry, index) => privacy.redactLogLine(entry.message,
    privacy.conversationLines(entries)[index], activePrivacy)).join("\n");
  assert.equal(text.includes("架空"), false);
  assert.equal(text.includes("偽装"), false);
  assert.equal(text.includes("会話の続き"), false);
});

test("#215 systemdの割込みと別PIDを会話の公開境界にせず識別名だけも信用しない", () => {
  const classify = logPrivacy.createLogPrivacyTracker();
  const entries = [row("AI：架空の応答"), row("サービスを開始 FICTIONAL_HOST", {source: "systemd", pid: 1}),
    row("応答の続き"), row("別プロセスの不明な本文", {pid: 43}), row("識別名だけ偽装", {source: "systemd"}),
    row("PIDだけ一致", {pid: 1}), row("サービスを再確認 FICTIONAL_HOST", {source: "systemd", pid: 1}),
    row("二度の割込み後も架空の応答"), row("サービスの確認完了", {source: "systemd", pid: 1}),
    row("三度の割込み後も架空の応答")].map(classify);
  mode(true);
  assert.deepEqual(privacy.conversationLines(entries), [true, false, true, true, true, true, false, true, false, true]);
  const flags = privacy.conversationLines(entries);
  assert.equal(privacy.redactLogLine(entries[1].message, flags[1], activePrivacy), `サービスを開始 ${privacy.MASK}`);
  assert.equal(entries[2].messagePrivacy, "conversation");
  for (const index of [7, 9]) {
    assert.equal(entries[index].messagePrivacy, "conversation");
    assert.equal(privacy.redactLogLine(entries[index].message, flags[index], activePrivacy), privacy.MASK);
  }
  assert.equal(privacy.redactLogLine(entries[6].message, flags[6], activePrivacy), `サービスを再確認 ${privacy.MASK}`);
  assert.equal(privacy.redactLogLine(entries[8].message, flags[8], activePrivacy), "サービスの確認完了");
  assert.ok(privacy.conversationLines(entries.slice(7)).every((hidden, i) => hidden === (i !== 1)));
  assert.deepEqual(privacy.conversationLines(entries.slice(7).map(({messagePrivacy, ...entry}) => entry)), [true, false, true]);
});

test("#215 受信時属性は保持上限後も残り原文を変えない", () => {
  const classify = logPrivacy.createLogPrivacyTracker();
  const first = classify(row("AI：架空の開始"));
  const messages = Array.from({length: logs.MAX_LOG_ENTRIES + 20}, (_, i) => `架空の本文${i}`);
  const entries = logs.appendLogEntries([first], messages.map((message) => classify(row(message))));
  assert.equal(entries.length, logs.MAX_LOG_ENTRIES);
  assert.ok(entries.every((entry) => entry.messagePrivacy === "conversation"));
  assert.deepEqual(entries.map((entry) => entry.message), messages.slice(-logs.MAX_LOG_ENTRIES));
  assert.ok(privacy.conversationLines(entries).every(Boolean));
});

test("#215 長い応答の概要末尾8件に本文を出さず伏せ字解除で元文を見せる", () => {
  const entries = [row("AI：架空の開始"), ...Array.from({length: 90}, (_, i) => row(`架空の長い本文${i}`))];
  mode(true);
  const masked = JSON.stringify(recent(entries));
  assert.equal(masked.includes("架空の長い本文"), false);
  assert.equal((masked.match(/mini-log-text/g) ?? []).length, 8);
  assert.ok(masked.includes(privacy.MASK));
  mode(false);
  const plain = JSON.stringify(recent(entries));
  for (const entry of entries.slice(-8)) assert.ok(plain.includes(entry.message));
});

test("#215 ログ描画と検索は隠した本文を使わず切替後は原文を検索する", () => {
  const entries = [row("AI：架空"), row("-".repeat(124)), row("検索対象の架空本文")];
  mode(true);
  let tree = renderLogs(entries);
  assert.equal(JSON.stringify(nodes(tree).find((node) => node.props?.role === "log")).includes("検索対象の架空本文"), false);
  nodes(tree).find((node) => node.type === "input").props.onChange({target: {value: "検索対象"}});
  tree = renderLogs(entries, false);
  assert.equal(nodes(tree).filter((node) => node.props?.className?.startsWith("log-row")).length, 0);
  assert.ok(JSON.stringify(tree).includes("条件に合うログはありません"));
  mode(false);
  tree = renderLogs(entries, false);
  assert.equal(nodes(tree).filter((node) => node.props?.className?.startsWith("log-row")).length, 1);
  assert.ok(JSON.stringify(tree).includes("の架空本文"));
});

test("#215 デプロイ用抜粋の先頭が会話途中でも区切り後や運用ログ風の本文も隠す", () => {
  const text = "取得途中の架空本文\n" + "-".repeat(124) + "\nログイン成功: 架空の続き\r\n末尾の架空本文";
  mode(true);
  const rendered = privacy.redactText(text, activePrivacy);
  assert.equal(rendered.includes("架空"), false);
  mode(false);
  assert.equal(privacy.redactText(text, activePrivacy), text);
});

test("#215 デプロイ描画はログ属性を保持した出力と旧属性なしの抜粋を隠して切替に追従する", () => {
  const output = "再起動後のログ（古い順）:\n取得途中の架空本文\n" + "-".repeat(124) + "\n架空の応答末尾";
  for (const containsLogs of [true, undefined]) {
    mode(true);
    assert.equal(stepOutput(output, containsLogs).includes("架空"), false);
    mode(false);
    assert.equal(stepOutput(output, containsLogs), output);
  }
});

test("#215 ログを含まないと確認できたデプロイ出力は既知の秘密だけ隠す", () => {
  mode(true);
  assert.equal(stepOutput("Fast-forward FICTIONAL_HOST", false), `Fast-forward ${privacy.MASK}`);
});
