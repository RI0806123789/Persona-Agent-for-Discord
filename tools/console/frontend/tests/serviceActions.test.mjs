import assert from "node:assert/strict";
import test from "node:test";
import { compile, readSource as source } from "./helpers/compile.mjs";
import { nodes } from "./helpers/tree.mjs";


const status = compile(await source("status.ts"), {});
const ui = Object.fromEntries(["Button", "Card", "EmptyState", "StatusChip", "Chip", "InlineError", "Notice", "Sparkline", "SplitButton", "Stat"]
  .map((name) => [name, name]));
let masked = false;
const modules = {
  "react/jsx-runtime": { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) },
  "../status": status, "../components/ui": ui, "../components/Icon": { Icon: "Icon" },
  "../components/expressive": { LoadingIndicator: "LoadingIndicator", WavyProgress: "WavyProgress" },
  "../privacy": { usePrivacy: () => ({ redact: (value) => value, mask: (value) => masked ? "MASKED" : value }) },
  "../format": new Proxy({}, { get: () => () => "架空の表示値" }),
  "../deployPlan": compile(await source("deployPlan.ts")),
  "./DeployCard": { DeployCard: "DeployCard" }, "./shared": { NotConnected: "NotConnected" },
};
const { OperationsPage } = compile(await source("pages/OperationsPage.tsx"), modules);
const { OverviewPage } = compile(await source("pages/OverviewPage.tsx"), modules);
const { DeployCard } = compile(await source("pages/DeployCard.tsx"), modules);

test("デプロイのブランチと操作履歴の接続先は伏せ字設定に従う", () => {
  try {
    for (masked of [false, true]) {
      const branch = "private/fictional-branch", profileId = "fictional-private-profile";
      const card = DeployCard({deploy: null, profile: {deploy_branch: branch}, git: null, now: 0});
      const history = OperationsPage({profile: null, service: null, now: 0, operations: [
        {id: 1, profile_id: profileId, at: 1, action: "stop", ok: true, duration_ms: 1},
      ]});
      const cardText = JSON.stringify(card), historyText = JSON.stringify(history);
      assert.equal(cardText.includes(branch), !masked);
      assert.equal(historyText.includes(profileId), !masked);
      if (masked) {assert.ok(cardText.includes("MASKED")); assert.ok(historyText.includes("MASKED"));}
    }
  } finally { masked = false; }
});


function stopControl(page, service, overrides = {}) {
  const actions = [];
  const props = { connected: true, connecting: false, selectedProfile: null, profile: null, service,
    busyAction: null, deployRunning: false, operations: [], git: null, deploy: null,
    status: service ? { service } : null, samples: [], logs: [], connection: null, insights: null,
    statusError: null, now: 0, onNavigate: () => {}, onRequestAction: (action) => actions.push(action), ...overrides };
  if (page === "operations") {
    const stop = nodes(OperationsPage(props)).find((node) => node.type === "Button" && node.props.icon === "stop");
    return { disabled: !stop || !!stop.props.disabled, click: () => stop.props.onClick(), actions };
  }
  const hero = nodes(OverviewPage(props)).find((node) => node.type?.name === "ServiceHero");
  if (!hero) return { disabled: true, actions };
  const split = nodes(hero.type(hero.props)).find((node) => node.type === "SplitButton");
  const stop = split.props.items.find((item) => item.label === "停止");
  return { disabled: !!(split.props.disabled || split.props.busy || stop.disabled), click: () => stop.onSelect(), actions };
}

const service = (active_state, sub_state, load_state = "loaded") => ({
  name: "fictional-bot", active_state, sub_state, load_state, main_pid: 0,
});

test("再起動待ち・起動中・再読み込み中から両画面の停止操作へ進める", () => {
  for (const [active, sub] of [["active", "running"], ["activating", "auto-restart"], ["activating", "start"], ["reloading", "reload"]]) {
    const current = service(active, sub);
    assert.equal(status.canStopService(current), true);
    for (const page of ["operations", "overview"]) {
      const control = stopControl(page, current);
      assert.equal(control.disabled, false, `${page}: ${active}/${sub}`);
      control.click();
      assert.deepEqual(control.actions, ["stop"]);
    }
  }
  // マスク後も既に動いているサービスは停止できる。
  assert.equal(status.canStopService(service("active", "running", "masked")), true);
});

test("停止済み・停止処理中・未登録・状態不明からの停止は両画面で無効", () => {
  for (const current of [null, service("inactive", "dead"), service("failed", "failed"),
    service("deactivating", "stop"), service("unknown", "unknown"), service("active", "running", "not-found")]) {
    assert.equal(status.canStopService(current), false);
    for (const page of ["operations", "overview"]) assert.equal(stopControl(page, current).disabled, true);
  }
});

test("接続前・操作中・デプロイ中の停止ガードを両画面で維持する", () => {
  const current = service("activating", "auto-restart");
  for (const overrides of [{ connected: false }, { busyAction: "start" }, { busyAction: "stop" }, { deployRunning: true }]) {
    for (const page of ["operations", "overview"]) assert.equal(stopControl(page, current, overrides).disabled, true);
  }
});

test("状態不明でタスク完了後も両画面のサービス操作とデプロイを無効にする", () => {
  const deploy = { status: "unknown", profile_id: "A", steps: [], commits: [], message: "架空の終了未確認" };
  const current = service("active", "running");
  const props = { connected: true, connecting: false, profile: null, service: current, status: { service: current },
    samples: [], deployRunning: false, deployUnknown: true, deploy, busyAction: null, operations: [], now: 0 };
  const operations = nodes(OperationsPage(props));
  const actions = operations.filter((node) => node.type === "Button" && ["stop", "restart", "play"].includes(node.props.icon));
  assert.equal(actions.length, 3);
  assert.ok(actions.every((node) => node.props.disabled));
  const hero = nodes(OverviewPage(props)).find((node) => node.type?.name === "ServiceHero");
  const overview = nodes(hero.type(hero.props));
  assert.equal(overview.find((node) => node.type === "SplitButton").props.disabled, true);
  for (const page of [operations, overview]) {
    assert.ok(page.some((node) => node.type === "StatusChip" && /状態不明/.test(node.props.label)));
  }
  const card = DeployCard({ deploy, running: false, connected: true, disabled: false, profile: null, git: null, now: 0 });
  assert.equal(card.props.actions.props.disabled, true);
  assert.equal(card.props.actions.props.busy, false);
});

test("再接続後にデプロイ記録がなくてもセッションの状態不明で全更新ボタンを止める", () => {
  const current = service("active", "running");
  const props = { connected: true, profile: { id: "B" }, service: current, status: { service: current },
    samples: [], deploy: null, deployRunning: false, stateUnknown: true, deployUnknown: true,
    busyAction: null, operations: [], now: 0 };
  const operations = nodes(OperationsPage(props));
  const actions = operations.filter((node) => node.type === "Button" && ["stop", "restart", "play"].includes(node.props.icon));
  assert.equal(actions.length, 3);
  assert.ok(actions.every((node) => node.props.disabled));
  const cardProps = operations.find((node) => node.type === "DeployCard").props;
  assert.equal(cardProps.stateUnknown, true);
  const card = DeployCard(cardProps);
  assert.equal(card.props.actions.props.disabled, true);
  assert.equal(card.props.actions.props.busy, false);
  assert.match(card.props.actions.props.children, /状態不明/);
  const hero = nodes(OverviewPage(props)).find((node) => node.type?.name === "ServiceHero");
  assert.equal(nodes(hero.type(hero.props)).find((node) => node.type === "SplitButton").props.disabled, true);
  // 状態不明がない通常の接続ではデプロイを許可する。
  assert.equal(DeployCard({ ...cardProps, stateUnknown: false }).props.actions.props.disabled, false);
});
