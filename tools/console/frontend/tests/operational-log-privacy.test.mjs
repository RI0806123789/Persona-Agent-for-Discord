import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";
import ts from "typescript";
import * as React from "react";
import * as jsxRuntime from "react/jsx-runtime";
import {renderToStaticMarkup} from "react-dom/server";

const source = async (name) => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
function compile(text, modules = {}) {
  const exports = {};
  const output = ts.transpileModule(text, {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  }}).outputText;
  new Function("require", "exports", output)((name) => modules[name] ?? {}, exports);
  return exports;
}
const logPrivacy = compile(await source("logPrivacy.ts"));
const privacy = compile(await source("privacy.ts"), {react: React, "./logPrivacy": logPrivacy});
const logs = compile(await source("logs.ts"));
const presentation = compile(await source("logPresentation.ts"), {"./logs": logs, "./privacy": privacy});
const oldName = "架空の過去チャンネル", otherName = "架空の移動先";
// 実際の識別子は使わず、架空の値を断片から作る。
const ids = ["1", "2", "3"].map((end) => ["81", "0000", "0000", "0000", "000", end].join(""));
const start = (kind = "ボイスチャンネル", name = oldName) => `自律行動: ${kind}「${name}」で配信を開始しました（guild=${ids[0]}）。`;
const move = `ボイス接続の保護: 別のチャンネルへ移動されたことを検知しました（guild=${ids[0]} / 移動前=${ids[1]} / 移動後=${ids[2]}）。`;
const entry = (message, index = 0) => ({ts: index + 1, message, priority: 6, pid: 42, source: "python"});
const current = (enabled, insights = null) => privacy.createPrivacy(enabled,
  privacy.collectSecrets({profile: null, profiles: [], connection: null, insights}));
const afterStop = {discord: {bot_user: null, guilds: [{name: "架空の別サーバー", voice_channel: null}]}};
function hidden(text, secrets = [oldName, otherName, ...ids]) {
  for (const secret of secrets) assert.equal(text.includes(secret), false, "対象の名前・識別子は表示しない");
  assert.ok(text.includes(privacy.MASK));
}

const wrap = ({children}) => React.createElement("section", null, children);
const ui = {Card: wrap, Button: wrap, InlineError: wrap, IconButton: () => null, ConnectedButtonGroup: () => null,
  StatusChip: ({label}) => React.createElement("span", null, label)};
const modules = {react: React, "react/jsx-runtime": jsxRuntime, "../privacy": privacy, "../logs": logs, "../logPresentation": presentation,
  "../components/ui": ui, "../components/Icon": {Icon: () => null},
  "../components/expressive": {WavyProgress: () => null, LoadingIndicator: () => null},
  "../format": {formatClock: () => "架空の時刻"}};
const {RecentLogs} = compile((await source("pages/OverviewPage.tsx")).replace("function RecentLogs(", "export function RecentLogs("), modules);
const {StepItem} = compile((await source("pages/DeployCard.tsx")).replace("function StepItem(", "export function StepItem("), modules);
const logsSource = await source("pages/LogsPage.tsx");
function html(component, props, enabled, insights = null) {
  return renderToStaticMarkup(React.createElement(privacy.PrivacyContext.Provider, {value: current(enabled, insights)},
    React.createElement(component, props)));
}
function logPage(entries, enabled, query = "", error = null, messageSafe = null, status = "error") {
  let index = 0;
  const states = [query, "all", true];
  const {LogsPage} = compile(logsSource, {...modules, react: {...React,
    useState(initial) {return [states[index++] ?? initial, () => {}];},
  }});
  return html(LogsPage, {connected: true, stream: {entries, status: error ? status : "live", message: error, messageSafe},
    filterInputRef: {current: null}}, enabled);
}

test("#219 配信終了後とインサイトなしでも過去の配信先を隠す", () => {
  for (const insights of [null, afterStop]) {
    for (const kind of ["ボイスチャンネル", "ステージ", "ボイス"]) {
      const text = start(kind);
      hidden(privacy.redactLogLine(text, false, current(true, insights)));
      assert.equal(privacy.redactLogLine(text, false, current(false, insights)), text);
    }
  }
});

test("#283 logging接頭辞付きの運用文も名前と識別子を隠し、会話ログは全体を保護する", () => {
  const prefix = "[2026-10-29T07:32:26+0900] [INFO] [persona_bot.services.autonomous_service] ";
  for (const text of [prefix + start(), prefix + move]) {
    hidden(privacy.redactLogLine(text, false, current(true)));
    assert.equal(privacy.redactLogLine(text, false, current(false)), text);
    const row = entry(text);
    assert.deepEqual(privacy.conversationLines([row]), [true]);
    assert.equal(privacy.redactLogLine(text, true, current(true)), privacy.MASK);
  }
});

test("#219 接続失敗・準備失敗・復帰のチャンネル名も書式から隠す", () => {
  const text = [
    `自律行動: ${oldName} への接続を拒否されました（権限不足）。`,
    `自律行動: ${oldName} への接続がタイムアウトしました。`,
    `自律行動: ${oldName} への接続に失敗しました（RuntimeError: 架空の診断）。`,
    `自律行動: ステージ(${oldName})の準備に失敗しました（RuntimeError: 架空の診断）。`,
    `自律行動: 中止時のステージ枠の終了に失敗しました（${oldName}）: RuntimeError: 架空の診断`,
    `ボイス接続の保護: 元のチャンネル「${oldName}」へ戻ります（guild=${ids[0]}）。`,
    `ボイス接続の保護: 元のチャンネル「${oldName}」へ戻りました（guild=${ids[0]}）。`,
  ];
  for (const line of text) {
    hidden(privacy.redactLogLine(line, false, current(true)));
    assert.equal(privacy.redactLogLine(line, false, current(false)), line);
  }
});

test("#219 移動前後とサーバー・チャンネルの複数識別子を伏せる", () => {
  hidden(privacy.redactLogLine(move, false, current(true)));
  const text = `guild = ${ids[0]} / channel=${ids[1]} / guild_id=<GUILD_ID> / channel_id=<CHANNEL_ID> / user_id=${ids[2]} / 移動前=${ids[1]} / 移動後=${ids[2]}`;
  const masked = privacy.redactLogLine(text, false, current(true));
  hidden(masked);
  assert.equal(masked.includes("<GUILD_ID>"), false);
  assert.equal(masked.includes("<CHANNEL_ID>"), false);
  assert.ok(masked.includes(`移動前=${privacy.MASK} / 移動後=${privacy.MASK}`));
  assert.equal(privacy.redactLogLine(text, false, current(false)), text);
});

test("#219 名前の中の装飾・括弧・改行と複数対象も一部を残さない", () => {
  const decorated = oldName + "「内側」(補足) ✨\n" + otherName;
  for (const text of [start("ステージ", decorated),
    `ボイス接続の保護: 元のチャンネル「${decorated}」へ戻りました（guild=${ids[0]}）。`,
    `自律行動: ステージ(${decorated})の準備に失敗しました（架空の診断）。`,
    start("ボイスチャンネル", decorated) + "\n" + start("ステージ", otherName)]) {
    hidden(privacy.redactLogLine(text, false, current(true)));
    hidden(privacy.redactText(text, current(true)));
    assert.equal(privacy.redactText(text, current(false)), text);
  }
});

test("#219 通常の運用文・既知値の置換・会話判定と非伏せ字の原文を維持する", () => {
  const normal = "自律行動: 配信の開始に失敗しました（連続 2 回目）。次に試すのは約 5 分後です。";
  assert.equal(privacy.redactLogLine(normal, false, current(true)), normal);
  const p = privacy.createPrivacy(true, ["FICTIONAL_HOST"]);
  assert.equal(privacy.redactLogLine("通常の診断 FICTIONAL_HOST", false, p), `通常の診断 ${privacy.MASK}`);
  // 受信時の属性による保守的な本文秘匿（systemd 以外を隠す）を緩めない。
  const rows = ["User (架空)：架空の質問", "Prompt text count：10", "AI：架空の応答", "架空の続き", "-".repeat(124), normal].map(entry);
  const system = {...entry("架空のサービス通知"), pid: 1, source: "systemd"};
  assert.deepEqual(privacy.conversationLines([...rows, system]), [...rows.map(() => true), false]);
  assert.equal(privacy.redactLogLine(rows[3].message, true, p), privacy.MASK);
  assert.equal(privacy.redactText(normal + "\n" + move, current(false)), normal + "\n" + move);
});

test("#219 デプロイ出力はログを含まなければ通常の診断を残し、含めば本文ごと隠す", () => {
  const text = "起動確認の診断\n" + start() + "\n" + move + "\n終了確認の診断";
  const plain = privacy.createPrivacy(true, []).redact(text);
  hidden(plain);
  assert.ok(plain.includes("起動確認の診断"));
  assert.ok(plain.includes("終了確認の診断"));
  assert.equal(plain.split("\n").length, text.split("\n").length);
  const logs = privacy.redactText(text, current(true));
  hidden(logs);
  assert.equal(logs.includes("起動確認の診断"), false);
  assert.equal(logs.split("\n").length, text.split("\n").length);
});

test("#219 行をまたぐ名前を概要の抽出前に隠して切替に追従する", () => {
  // 概要が見せる末尾8件より前から名前が始まる。
  const text = start("ボイスチャンネル", [oldName, ...Array.from({length: 10}, () => otherName)].join("\n"));
  const entries = text.split("\n").map(entry);
  hidden(html(RecentLogs, {logs: entries, onNavigate() {}}, true));
  const plain = html(RecentLogs, {logs: entries, onNavigate() {}}, false);
  assert.ok(plain.includes(otherName));
});

test("#219 ログ本文と切断理由のtooltipも現在の伏せ字設定で表示する", () => {
  const entries = [entry(start()), entry(move)];
  for (const enabled of [false, true, false]) {
    const displayed = logPage(entries, enabled, "", start());
    if (enabled) {
      hidden(displayed);
      assert.ok(displayed.includes('title="自律行動: ボイスチャンネル「' + privacy.MASK));
    } else {
      assert.ok(displayed.includes(oldName));
      for (const id of ids) assert.ok(displayed.includes(id));
    }
  }
});

test("#219 ログ検索は伏せた文字列へ適用し解除後に原文を検索する", () => {
  const entries = [entry(start()), entry(move)];
  const masked = logPage(entries, true, oldName);
  assert.ok(masked.includes("条件に合うログはありません"));
  assert.equal(masked.includes("log-row log-"), false);
  const plain = logPage(entries, false, oldName);
  assert.ok(plain.includes("log-row log-"));
  assert.equal(plain.includes("条件に合うログはありません"), false);
});

test("#219 デプロイの実描画も取得済みの過去名と識別子を隠す", () => {
  const step = {key: "health", title: "起動確認", description: "架空の確認", status: "failed", started_at: null,
    message: "", checks: [], commands: [], output: start("ステージ") + "\n" + move};
  for (const output_contains_logs of [undefined, false]) {
    const current = {...step, output_contains_logs, message: start()};
    const masked = html(StepItem, {step: current, index: 1, now: 0}, true);
    hidden(masked);
    if (output_contains_logs === false) assert.ok(masked.includes("自律行動: ステージ「"));
    const plain = html(StepItem, {step: current, index: 1, now: 0}, false);
    assert.ok(plain.includes(oldName));
    for (const id of ids) assert.ok(plain.includes(id));
  }
});

test("#219 複数エントリーへの分割とCRLFを保って名前の続きを隠す", () => {
  assert.equal(typeof privacy.redactOperationalMessages, "function");
  const messages = start("ボイスチャンネル", oldName + "\r\n" + otherName).split("\n");
  const masked = privacy.redactOperationalMessages(messages, current(true));
  assert.equal(masked.length, messages.length);
  hidden(masked.join("\n"));
  assert.equal(masked[0].endsWith("\r"), true);
  assert.deepEqual(privacy.redactOperationalMessages(messages, current(false)), messages);
  const crlf = ["架空の診断\r", start() + "\r"];
  const tail = privacy.redactOperationalMessages(crlf, current(true));
  assert.equal(tail[0], crlf[0]);
  assert.equal(tail[1].endsWith("\r"), true);
});

test("#219 離れた行の接尾辞までは名前として扱わず長いログの通常文を残す", () => {
  const normal = "自律行動: 配信の開始に失敗しました（連続 2 回目）。次に試すのは約 5 分後です。";
  const messages = [...Array.from({length: 2000}, () => normal), `自律行動: ${oldName} への接続に失敗しました。`];
  const masked = privacy.redactOperationalMessages(messages, current(true));
  assert.equal(masked.length, messages.length);
  assert.deepEqual(masked.slice(0, 1950), messages.slice(0, 1950));
  hidden(masked.at(-1), [oldName]);
});

test("#219 複数の運用ログで照合範囲が重なっても後続の名前を残さない", () => {
  const formats = [
    ["自律行動: ボイスチャンネル「", "」で配信を開始しました"],
    ["自律行動: ", " への接続に失敗しました"],
    ["自律行動: ステージ(", ")の準備に失敗しました"],
    ["自律行動: 中止時のステージ枠の終了に失敗しました（", "）:"],
    ["ボイス接続の保護: 元のチャンネル「", "」へ戻りました"],
  ];
  for (const [prefix, suffix] of formats) {
    for (const [nextPrefix, nextSuffix] of formats) {
      const name = "架空の前半" + suffix + "架空の後半";
      for (let length = 430; length <= 530; length += 5) {
        const text = prefix + oldName + suffix + "\r\n" + "診".repeat(length) + "\r\n"
          + nextPrefix + name + nextSuffix;
        const masked = current(true).redact(text);
        hidden(masked, [oldName, "架空の前半", "架空の後半"]);
        assert.equal(masked.split("\r\n").length, text.split("\r\n").length);
        hidden(privacy.redactOperationalMessages(text.split("\n"), current(true)).join("\n"),
          [oldName, "架空の前半", "架空の後半"]);
        assert.equal(current(false).redact(text), text);
      }
    }
  }
});

test("#219 名前に別の運用書式や補助面文字が含まれても名前全体を隠す", () => {
  for (const name of ["🧪".repeat(100), "架空の前半\r\n自律行動: 架空の後半 への接続に失敗しました"]) {
    const text = start("ステージ", name);
    hidden(current(true).redact(text), ["🧪", "架空の前半", "架空の後半"]);
    assert.equal(current(false).redact(text), text);
  }
});


test("#283 現行の配信開始ログは名前と識別子を含まず接頭辞でも公開に戻らない", async () => {
  const bot = await source("../../../../src/persona_bot/services/autonomous_service.py");
  const kinds = bot.match(/kind = "([^"\r\n]+)" if is_stage else "([^"\r\n]+)"/);
  const format = bot.match(/logger\.info\("(自律行動: %s[^"\r\n]+)", kind\)/);
  assert.ok(kinds);
  assert.ok(format);
  for (const kind of kinds.slice(1)) {
    const text = format[1].replace("%s", kind);
    assert.equal(text, `自律行動: ${kind}で配信を開始しました。`);
    for (const privateField of ["channel", "guild", oldName, ...ids]) assert.equal(text.includes(privateField), false);
    const prefix = "[2026-10-29T07:32:26+0900] [INFO] [persona_bot.services.autonomous_service] ";
    for (const message of [text, prefix + text]) {
      const row = {...entry(message), source: "systemd"}; // 識別名だけの偽装は信用しない。
      assert.deepEqual(privacy.conversationLines([row]), [true]);
      assert.equal(privacy.redactLogLine(message, true, current(true)), privacy.MASK);
      assert.equal(privacy.redactLogLine(message, true, current(false)), message);
    }
  }
});

test("#283 現行のステージ終了失敗ログは名前と例外本文を含まず接頭辞でも公開に戻らない", async () => {
  const bot = await source("../../../../src/persona_bot/services/autonomous_stage.py");
  const format = bot.match(/logger\.error\("(自律行動: 中止時のステージ枠[^"\r\n]+)", exc_info=error\)/);
  assert.ok(format);
  const text = format[1];
  assert.equal(text, "自律行動: 中止時のステージ枠の終了に失敗しました。");
  for (const privateField of ["channel", "guild", "error", oldName, ...ids]) assert.equal(text.includes(privateField), false);
  const prefix = "[2026-10-29T07:32:26+0900] [ERROR] [persona_bot.services.autonomous_stage] ";
  for (const message of [text, prefix + text]) {
    const row = {...entry(message), source: "systemd"};
    assert.deepEqual(privacy.conversationLines([row]), [true]);
    assert.equal(privacy.redactLogLine(message, true, current(true)), privacy.MASK);
    assert.equal(privacy.redactLogLine(message, true, current(false)), message);
  }
});

test("#288 user_idの出力は既知値なしでも伏せ、プロフィール統合は件数だけ表示する", async () => {
  const text = `サブエージェント: user_id=${ids[0]} / user_id = <USER_ID>`;
  const masked = current(true).redact(text);
  hidden(masked, [ids[0], "<USER_ID>"]);
  assert.equal(current(false).redact(text), text);
  const bot = await source("../../../../src/persona_bot/core/database.py");
  assert.equal(bot.includes('profile.json の内容 {absorbed}'), false);
  const counts = "データベース: profile.json から 2 人分・3 項目を profile テーブルへ追加しました。";
  assert.equal(current(true).redact(counts), counts);
});

test("#288 ログ切断の診断本文とtooltipは安全な文面へ切り替え、解除で原文に戻る", () => {
  const owner = "FICTIONAL_LOCAL_OWNER";
  const raw = `秘密鍵が見つかりません: ${["Q:", "Users", owner, ".ssh", "fixture-key"].join(String.fromCharCode(92))}`;
  const safe = "秘密鍵が見つかりません: <LOCAL_PATH>（key_path を確認してください）";
  for (const status of ["error", "ended"]) {
    for (const enabled of [false, true, false]) {
      const displayed = logPage([], enabled, "", raw, safe, status);
      assert.equal(displayed.includes(owner), !enabled);
      if (enabled) {
        assert.ok(displayed.includes("&lt;LOCAL_PATH&gt;"));
        assert.ok(displayed.includes('title="秘密鍵が見つかりません: &lt;LOCAL_PATH&gt;'));
        assert.ok(displayed.includes("key_path"));
      }
    }
  }
});

test("#288 未収集の解決済みホスト名もログ開始失敗の安全な案内へ切り替える", () => {
  const host = "uncollected-resolved.fixture.invalid";
  const raw = `ログ追跡（ライブ）: 開始できませんでした（${host}）。`;
  const safe = "ログの接続を開始できませんでした。SSHの接続設定と接続状態を確認してから再試行してください。";
  for (const enabled of [false, true, false]) {
    const displayed = logPage([], enabled, "", raw, safe);
    assert.equal(displayed.includes(host), !enabled);
    if (enabled) {
      assert.ok(displayed.includes(safe));
      assert.ok(displayed.includes(`title="${safe}"`));
    }
  }
});

test("#433 ログイン成功のボット名は既知の秘密に入らなくても構造で隠す", () => {
  // insights が未取得、または名前が短くて既知の秘密にならない場合を再現する
  for (const botName of ["架空のボット名", "ab"]) {
    const line = `PID 1234: journalctl で起動完了を確認しました（ログイン成功: ${botName}）。`;
    const on = current(true).redact(line);
    assert.equal(current(false).redact(line), line, "伏せ字OFFは原文");
    assert.equal(on.includes(botName), false, "ボット名は表示しない");
    assert.ok(on.includes(`ログイン成功: ${privacy.MASK}`));
    assert.ok(on.endsWith("）。"), "末尾の句読点は残す");
  }
  // 複数行でも前後の行は壊さない
  const multi = "前の行\nログイン成功: 架空のボット名\n後ろの行";
  assert.equal(current(true).redact(multi), `前の行\nログイン成功: ${privacy.MASK}\n後ろの行`);
});
