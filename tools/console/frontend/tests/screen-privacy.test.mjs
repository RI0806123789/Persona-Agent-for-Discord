import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const source = async (name) => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
const sources = new Map();
async function collect(directory = "") {
  for (const entry of await readdir(new URL(`../src/${directory}`, import.meta.url), {withFileTypes: true})) {
    const name = `${directory}${entry.name}`;
    if (entry.isDirectory()) await collect(`${name}/`);
    else if (/\.tsx?$/.test(name)) sources.set(name, (await source(name)).replaceAll("\r\n", "\n"));
  }
}
await collect();
function compile(text, requireModule) {
  const exports = {};
  const output = ts.transpileModule(text, {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  }}).outputText;
  new Function("require", "exports", output)(requireModule, exports);
  return exports;
}
// 実TSXを依存先ごと読み込み、Reactのサーバー描画で表示文字列と全属性を検査する。
function loader(overrides = {}) {
  const cache = new Map();
  const external = {react: React, "react/jsx-runtime": jsxRuntime, ...overrides};
  function load(name) {
    if (name in external) return external[name];
    if (cache.has(name)) return cache.get(name);
    const loaded = compile(sources.get(name), (request) => {
      if (!request.startsWith(".")) {
        assert.ok(request in external, `未定義の依存: ${request}`);
        return external[request];
      }
      const base = path.posix.normalize(path.posix.join(path.posix.dirname(name), request));
      return load([base, `${base}.ts`, `${base}.tsx`].find((candidate) => candidate in external || sources.has(candidate)));
    });
    cache.set(name, loaded);
    return loaded;
  }
  return load;
}
const noop = () => {};
const originalWindow = globalThis.window;
globalThis.window = {matchMedia: () => ({matches: true}), addEventListener: noop, removeEventListener: noop};
test.after(() => {globalThis.window = originalWindow;});
const load = loader({
  "api.ts": {api: {}, errorMessage: (error) => error.message},
  "theme/theme.ts": {SEED_OPTIONS: [{id: "test", hex: "#123456", label: "架空色"}]},
});
const privacy = load("privacy.ts");
const label = "FICTIONAL_PRIVATE_ENVIRONMENT";
const branch = "private/fictional-current-branch";
const deployBranch = "private/fictional-deploy-branch";
const profile = {id: "fictional-profile", label, deploy_branch: deployBranch, service: "fictional-bot",
  host: "host.invalid", username: "fictional-user", bot_dir: "/opt/fictional-bot", environment: "production"};
const connection = {connected: true, profile, connected_at: 1};
const git = {branch, describe: `v0-fixture-${branch}`, subject: `更新 ${label} ${deployBranch}`, commit: "abcdef0",
  committed_at: "2026-10-24T00:00:00Z", dirty_files: 0};
const secrets = privacy.collectSecrets({profile, profiles: [profile], connection, git, insights: null});
const mode = (enabled) => privacy.createPrivacy(enabled, secrets);
const element = (component, props) => React.createElement(component, props);
function html(component, props, enabled) {
  return renderToStaticMarkup(element(privacy.PrivacyContext.Provider, {value: mode(enabled),
    children: element(component, props)}));
}
function checkModes(render, values) {
  const masked = render(true), plain = render(false);
  assert.ok(masked.includes(privacy.MASK));
  for (const value of values) {
    assert.equal(masked.includes(value), false, `伏せ字中に残った値: ${value}`);
    assert.ok(plain.includes(value), `解除時に戻らない値: ${value}`);
  }
}
const {OverviewPage} = load("pages/OverviewPage.tsx");
const {NavigationRail, ModalNavigationRail} = load("components/Sidebar.tsx");
const {TopAppBar} = load("components/TopBar.tsx");
const {ConnectionPage} = load("pages/ConnectionPage.tsx");
const {NotConnected} = load("pages/shared.tsx");
const {DeployCard} = load("pages/DeployCard.tsx");
const {GuildsPage} = load("pages/GuildsPage.tsx");

test("#292 配信先設定はIDを表示し、伏せ字の切替と旧版・自動指定を区別する", () => {
  const settings = {persona: null, persona_source: "default", persona_missing: null, persona_builtin: false,
    model: null, respond_mode: null, grounding: false, auto_topic_enabled: false,
    auto_topic_interval_hours: 6, autonomous_enabled: true, saved: true,
    autonomous_stage_channel_id: "7", autonomous_voice_channel_id: "9"};
  const props = {connected: true, connecting: false, onConnect: noop, selectedProfile: profile,
    insights: {discord: {ok: true, error: null, guilds: [{name: "架空サーバー", members: 1,
      online: 1, voice_channel: null, settings}]}, dm_settings: {...settings,
      autonomous_stage_channel_id: null, autonomous_voice_channel_id: undefined}},
    insightsFetchedAt: 1, insightsError: null, now: 1000};
  const plain = html(GuildsPage, props, false);
  assert.ok(plain.includes("配信先（ステージ）"));
  assert.ok(plain.includes("配信先（ボイス）"));
  assert.ok(plain.includes("ID: 7"));
  assert.ok(plain.includes("ID: 9"));
  assert.ok(plain.includes("指定なし（自動）"));
  assert.ok(plain.includes("この版には無い機能"));
  const masked = html(GuildsPage, props, true);
  assert.equal(masked.includes("ID: 7"), false);
  assert.equal(masked.includes("ID: 9"), false);
  assert.ok(masked.includes(`ID: ${privacy.MASK}`));
  assert.ok(masked.includes("指定なし（自動）"));
});

const guildPageSettings = {persona: `架空のペルソナ-${label}`, persona_source: "saved", persona_missing: `欠損-${label}`,
  persona_builtin: false, model: null, respond_mode: null, grounding: false, auto_topic_enabled: false,
  auto_topic_interval_hours: 6, autonomous_enabled: false, saved: true};
const guildPageProps = (guild) => ({connected: true, connecting: false, onConnect: noop, selectedProfile: profile,
  insights: {discord: {ok: true, error: null, guilds: [{members: 1, online: 1, voice_channel: null,
    settings: guildPageSettings, ...guild}]}, dm_settings: guildPageSettings},
  insightsFetchedAt: 1, insightsError: null, now: 1000});

test("#413 サーバー画面のペルソナ名と注記も、描画時に既知の秘密を伏せる", () => {
  // サーバー・DM のどちらの設定一覧にも出る。解除すると元の文面に戻る。
  const props = guildPageProps({name: "架空サーバー"});
  const plain = html(GuildsPage, props, false);
  const masked = html(GuildsPage, props, true);
  // 値の行（ペルソナ）と注記（保存値が見つからない旨）の両方に、秘密の語が含まれている
  assert.ok(plain.includes(`架空のペルソナ-${label}`));
  assert.ok(plain.includes(`欠損-${label}`));
  assert.equal(masked.includes(label), false);
  assert.ok(masked.includes(`架空のペルソナ-${privacy.MASK}`));
  assert.ok(masked.includes(`欠損-${privacy.MASK}`));
});

test("#413 空文字・空白だけのサーバー名は代替の表示にする", () => {
  for (const name of ["", "   ", "　\t"]) {
    const plain = html(GuildsPage, guildPageProps({name}), false);
    assert.ok(plain.includes("名前不明のサーバー"), JSON.stringify(name));
    assert.ok(plain.includes(">不<"), JSON.stringify(name));
  }
  // 通常の名前は先頭の1文字をアバターにする。
  assert.ok(html(GuildsPage, guildPageProps({name: "架空サーバー"}), false).includes(">架<"));
});

test("#452 前後の空白があるサーバー名でも、除いた値で見出しとアバターを出す", () => {
  const plain = html(GuildsPage, guildPageProps({name: " 架空サーバー "}), false);
  assert.ok(plain.includes(">架<"), "アバターの頭文字が空白にならない");
  assert.ok(plain.includes(">架空サーバー<"), "見出しにも前後の空白を残さない");
});

test("#292 サーバー・DMの配信先IDは自由文の伏せ字にも登録する", () => {
  const guildSettings = {autonomous_stage_channel_id: "FICTIONAL_STAGE_CHANNEL",
    autonomous_voice_channel_id: "FICTIONAL_VOICE_CHANNEL"};
  const dmSettings = {autonomous_stage_channel_id: "FICTIONAL_DM_STAGE",
    autonomous_voice_channel_id: "FICTIONAL_DM_VOICE"};
  const insights = {discord: {guilds: [{name: "架空サーバー", voice_channel: null, settings: guildSettings}]},
    dm_settings: dmSettings};
  const secrets = privacy.collectSecrets({profile: null, profiles: [], connection: null, insights});
  const values = [...Object.values(guildSettings), ...Object.values(dmSettings)];
  const text = `取得失敗 ${values.join(" / ")}`;
  assert.equal(privacy.createPrivacy(false, secrets).redact(text), text);
  const masked = privacy.createPrivacy(true, secrets).redact(text);
  for (const value of values) assert.equal(masked.includes(value), false);
});

test("#452 既定の bot 以外のサービス名を、自由文の伏せ字にも登録する", () => {
  const secrets = privacy.collectSecrets({profile, profiles: [profile], connection: null, insights: null});
  const command = `sudo -n systemctl restart ${profile.service}.service`;
  assert.equal(privacy.createPrivacy(false, secrets).redact(command), command);
  assert.equal(privacy.createPrivacy(true, secrets).redact(command), `sudo -n systemctl restart ${privacy.MASK}.service`);
  // 既定のサービス名 bot は、一般の単語と見分けが付かないので伏せない。
  const defaults = privacy.collectSecrets({profile: {...profile, service: "bot"}, profiles: [], connection: null, insights: null});
  assert.equal(privacy.createPrivacy(true, defaults).redact(command), command);
});

test("#452 サーバー名が取れないときの代替表示は、秘密に登録しない", () => {
  const insights = {discord: {ok: true, error: null, guilds: [
    {name: "架空サーバー", voice_channel: null},
    {name: "名前不明のサーバー", voice_channel: null},
    {name: "保存済みサーバー 1", voice_channel: null},
    {name: "保存済みサーバー 12", voice_channel: null},
  ]}, dm_settings: {}};
  const secrets = privacy.collectSecrets({profile: null, profiles: [], connection: null, insights});
  const text = "保存済みサーバー 1 と 保存済みサーバー 12 は、名前不明のサーバー として表示しています。架空サーバー は取得済みです。";
  assert.equal(privacy.createPrivacy(false, secrets).redact(text), text);
  const masked = privacy.createPrivacy(true, secrets).redact(text);
  assert.ok(masked.includes(privacy.MASK));
  assert.equal(masked.includes("架空サーバー"), false, "実際に取得できたサーバー名は伏せる");
  assert.ok(masked.includes("保存済みサーバー 1"), "代替の名前は一般の文として残す");
  assert.ok(masked.includes("保存済みサーバー 12"), "代替の名前は一般の文として残す");
  assert.ok(masked.includes("名前不明のサーバー"), "代替の名前は一般の文として残す");
});

test("#251 サーバー一覧の部分取得は既存行を表示しながら全件取得と区別する", () => {
  const settings = {persona: null, persona_source: "default", persona_missing: null, persona_builtin: false,
    model: null, respond_mode: null, grounding: false, auto_topic_enabled: false,
    auto_topic_interval_hours: 6, autonomous_enabled: false, saved: true};
  const props = {connected: true, connecting: false, onConnect: noop, selectedProfile: profile,
    insights: {discord: {ok: false, partial: true, error: "取得上限", guilds: [{name: "架空の取得済みサーバー",
      members: 1, online: 1, voice_channel: null, settings}]}, dm_settings: settings},
    insightsFetchedAt: 1, insightsError: null, now: 1000};
  const partial = html(GuildsPage, props, false);
  assert.ok(partial.includes("サーバー一覧は一部取得です"));
  assert.ok(partial.includes("取得済みの一覧と保存済み設定だけ"));
  assert.ok(partial.includes("架空の取得済みサーバー"));
  const unavailable = html(GuildsPage, {...props, insights: {...props.insights,
    discord: {...props.insights.discord, partial: false}}}, false);
  assert.ok(unavailable.includes("サーバー一覧を取得できませんでした"));
  assert.equal(unavailable.includes("サーバー一覧は一部取得です"), false);
  const complete = html(GuildsPage, {...props, insights: {...props.insights,
    discord: {...props.insights.discord, ok: true, partial: false, error: null}}}, false);
  assert.equal(complete.includes("サーバー一覧は一部取得です"), false);
  const overview = html(OverviewPage, {connected: true, status: null, samples: [], logs: [], now: 1000,
    insights: {...props.insights, errors: [], activity: {window_hours: 24, gemini_calls: 0, gemini_tokens: 0,
      conversations: 0, last_conversation_at: null, gateway_connects: 0, gateway_resumes: 0,
      gateway_invalidated: 0, heartbeat_blocked: 0, exceptions: 0, autonomous_events: []}}}, false);
  assert.ok(overview.includes("一部取得"));
  assert.ok(overview.includes("サーバー一覧で取得済みの情報を確認してください"));
});

test("#216 専用欄は短い値やredact対象外のラベルでも全体を隠す", () => {
  const shortProfile = {...profile, label: "xy"};
  const shortGit = {...git, branch: "zz", subject: "架空の更新", describe: "v0-fixture"};
  assert.equal(mode(true).redact("xy zz"), "xy zz");
  for (const enabled of [false, true]) {
    const overview = html(OverviewPage, {connected: true, status: {git: shortGit}, samples: [], logs: [], now: 0}, enabled);
    const top = html(TopAppBar, {...topProps, profiles: [shortProfile]}, enabled);
    assert.equal(overview.includes(">zz<"), !enabled);
    assert.equal(top.includes('aria-label="接続先: xy"'), !enabled);
    if (enabled) assert.ok(top.includes(`aria-label="接続先: ${privacy.MASK}"`));
  }
});

test("#216 概要Gitカードの現在ブランチと周辺自由文を描画時に伏せる", () => {
  const props = {connected: true, status: {git}, samples: [], logs: [], insights: null, now: 0};
  checkModes((enabled) => html(OverviewPage, props, enabled), [label, branch, deployBranch]);
});
test("#216 概要Gitカードの取得エラーにも既知の秘密を残さない", () => {
  const props = {connected: true, status: {git_error: `失敗 ${branch}`}, samples: [], logs: [], insights: null, now: 0};
  checkModes((enabled) => html(OverviewPage, props, enabled), [branch]);
});
const insightActivity = {window_hours: 24, gemini_calls: 0, gemini_tokens: 0, conversations: 0,
  last_conversation_at: null, gateway_connects: 0, gateway_resumes: 0, gateway_invalidated: 0,
  heartbeat_blocked: 0, exceptions: 0, autonomous_events: []};
test("#452 概要のボット欄のヒント（配備先ディレクトリ入りの文）も伏せる", () => {
  const insights = {source_dir: "/opt/fictional-bot/src", version: "ver0.2.0(BETA)", persona_estimated: false,
    discord: {ok: true, error: null, guilds: []}, dm_settings: {}, errors: [],
    activity: insightActivity, autonomous_supported: true};
  const props = {connected: true, status: null, samples: [], logs: [], insights, now: 0};
  const plain = html(OverviewPage, props, false);
  assert.ok(plain.includes("ver0.2.0(BETA)（/opt/fictional-bot/src/）"), "解除時は元の文面");
  const masked = html(OverviewPage, props, true);
  assert.equal(masked.includes("/opt/fictional-bot"), false, "配備先のディレクトリを伏せる");
  assert.ok(masked.includes(`（${privacy.MASK}/src/）`), "bot_dir 配下の残りは残す");
  assert.ok(masked.includes("ver0.2.0(BETA)"), "版の表示は残す");
});
const railProps = {page: "overview", connected: true, profile, onNavigate: noop, onHelp: noop,
  onMenu: noop, fab: {label: "更新", icon: "refresh", onClick: noop}};
for (const [name, component, extra] of [["常設", NavigationRail, {expanded: true}],
  ["モーダル", ModalNavigationRail, {open: true, onClose: noop}]]) {
  test(`#216 ${name}ナビゲーションの接続先ラベルを伏せる`, () => {
    checkModes((enabled) => html(component, {...railProps, ...extra}, enabled), [label]);
  });
}
const topProps = {page: {label: "概要", subtitle: "架空の概要"}, connection: null, profiles: [profile],
  profilesSource: "profiles.json", selectedProfileId: profile.id, onSelectProfile: noop, onConnect: noop,
  onDisconnect: noop, onRefresh: noop, onToggleAuto: noop, onTogglePrivacy: noop, onSelectTheme: noop,
  onSelectSeed: noop, onOpenConnection: noop, onMenu: noop, now: 0};
test("#216 接続先選択の表示・メニュー・aria属性を同じ伏せ字にする", () => {
  checkModes((enabled) => html(TopAppBar, topProps, enabled), [label]);
  assert.ok(html(TopAppBar, topProps, true).includes(`aria-label="接続先: ${privacy.MASK}"`));
});
test("#216 接続ページのラベルと説明・エラーを伏せる", () => {
  const props = {profiles: [{...profile, note: `配備先 ${deployBranch}`}], selectedProfileId: profile.id,
    profilesError: `設定 ${branch}`, connection: {connected: false, last_error: `接続 ${label}`}, now: 0};
  checkModes((enabled) => html(ConnectionPage, props, enabled), [label, branch, deployBranch]);
});
for (const unset_fields of [[], ["host"]]) {
  test(`#216 未接続案内${unset_fields.length ? "の見本設定" : "の通常設定"}のラベルを伏せる`, () => {
    checkModes((enabled) => html(NotConnected, {profile: {...profile, unset_fields}}, enabled), [label]);
  });
}
const step = (phase) => ({key: phase, phase, title: `取り込む ${deployBranch}`, description: `${label} の ${branch} から更新`,
  status: "running", checks: [{ok: true, label: `${branch} と ${deployBranch} を確認`, detail: label}],
  commands: [], output: "", started_at: 1});
const deploy = {branch: deployBranch, steps: [step("deploy"), step("rollback")], status: "running", commits: [], started_at: 1};
test("#216 デプロイ進捗・見出し・説明・確認ラベルと読み上げ属性を伏せる", () => {
  const props = {deploy, running: true, profile, git, connected: true, now: 1000};
  checkModes((enabled) => html(DeployCard, props, enabled), [label, branch, deployBranch]);
  const masked = html(DeployCard, props, true);
  assert.ok(masked.includes(`aria-label="取り込む ${privacy.MASK}を実行中"`));
});

// 状態保持と非同期境界だけ置換し、実Console・コントローラー・ConfirmDialogのコールバックを実行する。
function runtime() {
  const slots = []; let index = 0, activePrivacy;
  const hooks = {
    useState(initial) { const slot = index++; if (!(slot in slots)) slots[slot] = typeof initial === "function" ? initial() : initial;
      return [slots[slot], (value) => {slots[slot] = typeof value === "function" ? value(slots[slot]) : value;}]; },
    useRef(initial) {const slot = index++; return slots[slot] ??= {current: initial};},
    useMemo: (factory) => factory(), useCallback: (callback) => callback, useEffect: noop,
    createContext: (value) => ({value}), useContext: () => activePrivacy,
  };
  return {hooks, render(component, props) {index = 0; return component(props);}, setPrivacy(value) {activePrivacy = value;}};
}
const nodes = (tree) => Array.isArray(tree) ? tree.flatMap(nodes)
  : tree && typeof tree === "object" ? [tree, ...nodes(tree.props?.children)] : [];
function appHarness() {
  const rt = runtime(), dialogRuntime = runtime(), calls = [], notices = [];
  const jsx = (type, props) => ({type, props});
  const actualPrivacy = compile(sources.get("privacy.ts"), (name) => name === "react" ? rt.hooks : load("logPrivacy.ts"));
  const api = {service: async (...args) => {calls.push(["service", ...args]); return {ok: true, message: "完了"};},
    startDeploy: async (...args) => {calls.push(["deploy", ...args]); return {deploy: null, running: false};},
    operations: async () => ({operations: []}), status: async () => ({fetched_at: 1}),
    insights: async () => ({data: {discord: {guilds: []}}, fetched_at: 1})};
  const modules = {react: rt.hooks, "react/jsx-runtime": {jsx, jsxs: jsx}, "./privacy": actualPrivacy,
    "./useConsoleShortcuts": {useConsoleShortcuts: () => false},
    "./hooks": {useStoredState: (_key, initial) => rt.hooks.useState(initial), useNow: () => 0,
      useMediaQuery: () => true, useLogStream: () => ({}), usePolling: noop},
    "./api": {api, errorMessage: (error) => error.message, ApiError: Error},
    "./components/toast": {useToast: () => (notice) => notices.push(notice)},
    "./requestScope": load("requestScope.ts"), "./navigation": {PAGES: [{id: "overview"}], isPageId: () => true},
    "./profiles": load("profiles.ts"), "./theme/theme": {SEED_OPTIONS: [{id: "test"}]},
    "./components/Icon": {Icon: "svg"}};
  const resolve = (name) => modules[name] ?? {};
  modules["./controllers"] = compile(sources.get("controllers.ts"), resolve);
  modules["./confirmations"] = compile(sources.get("confirmations.tsx"), resolve);
  const controls = "requestAction, requestDeploy, confirm, confirmOpen, applyConnection, setPrivacyOn, privacy, setProfiles, notify";
  const instrumented = sources.get("App.tsx").replace("function Console() {", "export function Console() {")
    .replace("  return (\n    <PrivacyContext.Provider", `  return {${controls}};\n  return (\n    <PrivacyContext.Provider`);
  assert.ok(instrumented.includes(`return {${controls}}`));
  const component = compile(instrumented, resolve).Console;
  const dialog = compile(sources.get("components/overlays.tsx"), (name) => ({
    react: dialogRuntime.hooks, "react/jsx-runtime": {jsx, jsxs: jsx},
    "../privacy": {usePrivacy: () => current.privacy}, "../hooks": {useEmergeFrom: noop, useModalFocus: noop},
    "./ui": {Button: "button"}, "./Icon": {Icon: "svg"},
  })[name] ?? {}).ConfirmDialog;
  let current;
  function render() { current = rt.render(component); return current; }
  function renderDialog(request = current.confirm) {
    return dialogRuntime.render(dialog, {request, open: current.confirmOpen, onClose: noop});
  }
  render().applyConnection(connection); render().setProfiles([profile]); render();
  return {render, dialog: renderDialog, calls, notices};
}
function dialogDisplay(tree) {return JSON.stringify(tree);}
for (const action of ["stop", "restart", "deploy"]) {
  test(`#216 ${action}確認を開いた後の伏せ字切替が本文と属性へ追従する`, () => {
    const h = appHarness();
    if (action === "deploy") h.render().requestDeploy(); else h.render().requestAction(action);
    const request = h.render().confirm;
    assert.equal(h.render().confirmOpen, true);
    assert.deepEqual(h.calls, []);
    for (const enabled of [false, true, false, true]) {
      h.render().setPrivacyOn(enabled); const current = h.render();
      assert.equal(current.confirm, request, "確認要求を作り直さず切り替える");
      const text = dialogDisplay(h.dialog());
      for (const value of action === "deploy" ? [label, deployBranch, profile.service] : [label, profile.service]) {
        assert.equal(text.includes(value), !enabled, value);
      }
    }
  });
}
test("#216 確認ダイアログ自体の題名・実行ラベル・確認入力案内にもredactを適用する", () => {
  const h = appHarness(); h.render().setPrivacyOn(true); h.render();
  const tree = h.dialog({title: `確認 ${label}`, body: () => "架空の説明", confirmLabel: `実行 ${deployBranch}`,
    tone: "danger", requireText: deployBranch, icon: "stop", onConfirm: noop});
  const text = dialogDisplay(tree);
  for (const value of [label, branch, deployBranch]) assert.equal(text.includes(value), false);
  assert.ok(text.includes(privacy.MASK));
});
test("#216 伏せ字切替後も確認入力の照合とAPIへ渡す操作対象を保持する", async (t) => {
  const previous = globalThis.window;
  globalThis.window = {setTimeout: () => 1, clearTimeout: noop};
  t.after(() => {globalThis.window = previous;});
  for (const action of ["stop", "deploy"]) {
    const h = appHarness();
    if (action === "deploy") h.render().requestDeploy(); else h.render().requestAction(action);
    h.render().setPrivacyOn(true); h.render();
    let tree = h.dialog();
    const input = nodes(tree).find((node) => node.type === "input");
    assert.ok(input);
    assert.equal(nodes(tree).find((node) => node.props?.type === "submit").props.disabled, true);
    input.props.onChange({target: {value: profile.service}});
    h.render().setPrivacyOn(false); h.render(); tree = h.dialog();
    assert.equal(nodes(tree).find((node) => node.props?.type === "submit").props.disabled, false);
    h.render().setPrivacyOn(true); h.render(); tree = h.dialog();
    nodes(tree).find((node) => node.type === "form").props.onSubmit({preventDefault: noop});
    await Promise.resolve();
    assert.deepEqual(h.calls, action === "deploy" ? [["deploy", {profile_id: profile.id, connected_at: 1}]]
      : [["service", "stop", {profile_id: profile.id, connected_at: 1}]]);
  }
});
test("#216 接続変更後は確認を閉じ古いサービス・デプロイcallbackを拒否する", async () => {
  for (const action of ["restart", "deploy"]) {
    for (const next of [{...connection, connected_at: 2}, {connected: false, profile: null},
      {...connection, profile: {...profile, id: "another-fictional-profile"}}]) {
      const h = appHarness();
      if (action === "deploy") h.render().requestDeploy(); else h.render().requestAction(action);
      const old = h.render().confirm;
      h.render().setPrivacyOn(true); h.render(); h.render().applyConnection(next);
      assert.equal(h.render().confirmOpen, false);
      old.onConfirm(); await Promise.resolve(); assert.deepEqual(h.calls, []);
    }
  }
});
// 通知は原文で保持し、描画時の伏せ字状態で隠す（#217）。
const {Snackbar} = compile(sources.get("components/toast.tsx").replace("function Snackbar(", "export function Snackbar("),
  (name) => ({react: React, "react/jsx-runtime": jsxRuntime, "../privacy": privacy, "./Icon": {Icon: () => null}})[name] ?? {});
test("#216 既存通知の伏せ字と実接続先のIDを維持する", () => {
  const h = appHarness(); h.render().setPrivacyOn(true); h.render();
  h.render().notify({title: `通知 ${label}`, detail: deployBranch});
  const notice = h.notices.at(-1);
  assert.equal(notice.title, `通知 ${label}`);
  assert.equal(notice.detail, deployBranch);
  const display = () => renderToStaticMarkup(React.createElement(privacy.PrivacyContext.Provider, {value: h.render().privacy},
    React.createElement(Snackbar, {toast: {...notice, id: 1, leaving: false}, onDismiss: noop})));
  const masked = display();
  for (const value of [label, deployBranch]) assert.equal(masked.includes(value), false);
  assert.ok(masked.includes(`通知 ${privacy.MASK}`));
  h.render().setPrivacyOn(false); h.render();
  const plain = display();
  for (const value of [label, deployBranch]) assert.ok(plain.includes(value));
});
