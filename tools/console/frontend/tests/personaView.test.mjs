import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { compileTs, readSource } from "./helpers/compile.mjs";

const { describePersona, PERSONA_ESTIMATE_NOTE } = compileTs(await readSource("personaView.ts"));

function settings(overrides) {
  return {
    persona: "架空のキャラ",
    persona_source: "saved",
    persona_missing: null,
    persona_builtin: false,
    model: null,
    respond_mode: null,
    grounding: true,
    auto_topic_enabled: false,
    auto_topic_interval_hours: 6,
    autonomous_enabled: false,
    saved: true,
    ...overrides,
  };
}

test("保存した選択はそのまま、既定に従う場合は（既定）を添える", () => {
  assert.deepEqual(describePersona(settings({})), { value: "架空のキャラ", notes: [] });
  assert.deepEqual(describePersona(settings({ persona_source: "default", saved: false })), {
    value: "架空のキャラ（既定）",
    notes: [],
  });
  assert.deepEqual(describePersona(settings({ persona: null, persona_source: "default" })), { value: "—", notes: [] });
});

test("見つからない保存値は、実効のペルソナと分けて注記する", () => {
  const view = describePersona(settings({ persona_source: "missing", persona_missing: "架空の消えたキャラ" }));
  assert.equal(view.value, "架空のキャラ（既定）");
  assert.equal(view.notes.length, 1);
  assert.match(view.notes[0], /「架空の消えたキャラ」が prompts\/ に見つからない/);
  assert.match(view.notes[0], /既定のペルソナで応答/);
});

test("ペルソナが無いときは組み込みの標準であることを示す", () => {
  const builtin = describePersona(settings({ persona: "標準（組み込み）", persona_source: "default", persona_builtin: true }));
  assert.equal(builtin.value, "標準（組み込み）");
  assert.match(builtin.notes[0], /組み込みの標準プロンプトで応答/);

  const missing = describePersona(
    settings({ persona: "標準（組み込み）", persona_source: "missing", persona_missing: "架空の消えたキャラ", persona_builtin: true }),
  );
  assert.equal(missing.value, "標準（組み込み）");
  assert.equal(missing.notes.length, 1);
  assert.match(missing.notes[0], /「架空の消えたキャラ」.*組み込みの標準プロンプトで応答/);
});

test("推定であることの案内は、一覧の読み直しの時点に触れる", () => {
  assert.match(PERSONA_ESTIMATE_NOTE, /推定/);
  assert.match(PERSONA_ESTIMATE_NOTE, /キャラ設定パネル/);
});

test("ペルソナの項目は、バックエンドの出力と画面の型で一致する", async () => {
  const backend = await readFile(new URL("../../backend/remote_scripts/insights.py", import.meta.url), "utf8");
  const types = await readFile(new URL("../src/types.ts", import.meta.url), "utf8");
  for (const key of ["persona_source", "persona_missing", "persona_builtin", "persona_estimated"]) {
    assert.ok(backend.includes(`"${key}"`), `insights.py に ${key} がありません`);
    assert.ok(new RegExp(`^\\s+${key}: `, "m").test(types), `types.ts に ${key} がありません`);
  }
  for (const source of ["saved", "default", "missing"]) {
    assert.ok(backend.includes(`"${source}"`), `insights.py に persona_source の値 ${source} がありません`);
  }
});
