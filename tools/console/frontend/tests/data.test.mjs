import assert from "node:assert/strict";
import test from "node:test";
import { compileTs, readSource } from "./helpers/compile.mjs";

const { lastSevenDays } = compileTs(await readSource("data.ts"));

test("日本時間の午前0時で表示日を切り替える", () => {
  assert.equal(lastSevenDays([], Date.UTC(2026, 0, 1, 14, 59, 59)).at(-1).key, "2026-01-01");
  assert.equal(lastSevenDays([], Date.UTC(2026, 0, 1, 15)).at(-1).key, "2026-01-02");
});

test("端末のタイムゾーンによらず年をまたぐ7日分を表示する", () => {
  const original = process.env.TZ;
  try {
    for (const zone of ["UTC", "America/Los_Angeles", "Asia/Tokyo"]) {
      process.env.TZ = zone;
      const rows = lastSevenDays([], Date.UTC(2026, 0, 1, 15));
      assert.equal(rows.length, 7);
      assert.equal(rows[0].key, "2025-12-27");
      assert.equal(rows.at(-1).key, "2026-01-02");
    }
  } finally {
    if (original === undefined) delete process.env.TZ;
    else process.env.TZ = original;
  }
});

test("評価の無い日を0件で補い、過去と未来の範囲外の日は含めない", () => {
  const days = [{ day: "2026-01-02", good: 2, bad: 1 }, { day: "2025-12-26", good: 9, bad: 9 },
    { day: "2026-01-03", good: 9, bad: 9 }];
  const rows = lastSevenDays(days, Date.UTC(2026, 0, 1, 15));
  assert.equal(rows.reduce((sum, row) => sum + row.good + row.bad, 0), 3);
  assert.deepEqual(rows[0], { key: "2025-12-27", label: "12/27", good: 0, bad: 0 });
});
