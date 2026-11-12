// 疑似 Discord 画面の再生順の回帰テスト（ブラウザも外部通信も使わない）。
// Node.js が .ts の型注釈を読み飛ばして実行するので、ソースをそのまま読み込む。
import assert from "node:assert/strict";
import test from "node:test";
import { buildTimeline, finishedFrame, frameAt } from "../src/demoTimeline.ts";

const MESSAGES = [
  { author: "user", text: "こんにちは" },
  { author: "bot", text: "こんにちは。今日はどうしたの？" },
  { author: "friend", text: "やっほー" },
];

test("発言は順番どおりに、間を空けて出る", () => {
  const timeline = buildTimeline(MESSAGES);
  assert.equal(timeline.steps.length, MESSAGES.length);
  let previous = 0;
  for (const step of timeline.steps) {
    assert.ok(step.typingAt > previous, "前の発言より後に始まる");
    assert.ok(step.showAt >= step.typingAt);
    previous = step.showAt;
  }
  assert.ok(timeline.totalMs > previous, "最後の発言を読む時間が残る");
});

test("入力中を出すのはボット側の発言だけ", () => {
  const { steps } = buildTimeline([
    { author: "user", text: "質問" },
    { author: "bot", text: "回答" },
    { author: "helper", text: "報告" },
    { author: "friend", text: "感想" },
  ]);
  assert.equal(steps[0].showAt, steps[0].typingAt);
  assert.ok(steps[1].showAt > steps[1].typingAt);
  assert.ok(steps[2].showAt > steps[2].typingAt);
  assert.equal(steps[3].showAt, steps[3].typingAt);
});

test("入力中の長さは本文が長くても上限で止まる", () => {
  const short = buildTimeline([{ author: "bot", text: "あ" }]).steps[0];
  const long = buildTimeline([{ author: "bot", text: "あ".repeat(2000) }]).steps[0];
  const shortTyping = short.showAt - short.typingAt;
  const longTyping = long.showAt - long.typingAt;
  assert.ok(shortTyping >= 500, "短い返事でも一瞬で出さない");
  assert.ok(longTyping <= 2000, "長い返事でも待たせすぎない");
  assert.ok(longTyping >= shortTyping);
});

test("経過時間に応じて、表示済みの件数と入力中が決まる", () => {
  const timeline = buildTimeline(MESSAGES);
  const [first, second] = timeline.steps;

  assert.deepEqual(frameAt(timeline, 0), { visible: 0, typingIndex: null, finished: false });
  assert.deepEqual(frameAt(timeline, first.showAt), { visible: 1, typingIndex: null, finished: false });
  // ボットの返事を待っている間だけ入力中になる
  assert.deepEqual(frameAt(timeline, second.typingAt), { visible: 1, typingIndex: 1, finished: false });
  assert.deepEqual(frameAt(timeline, second.showAt), { visible: 2, typingIndex: null, finished: false });
  assert.deepEqual(frameAt(timeline, timeline.totalMs), {
    visible: MESSAGES.length,
    typingIndex: null,
    finished: true,
  });
});

test("時間を進めても表示済みの件数は減らない", () => {
  const timeline = buildTimeline(MESSAGES);
  let previous = 0;
  for (let elapsed = 0; elapsed <= timeline.totalMs + 500; elapsed += 50) {
    const frame = frameAt(timeline, elapsed);
    assert.ok(frame.visible >= previous);
    if (frame.typingIndex !== null) assert.equal(frame.typingIndex, frame.visible);
    previous = frame.visible;
  }
  assert.equal(previous, MESSAGES.length);
});

test("動きを減らす設定では、最初から全部の発言を出す", () => {
  const timeline = buildTimeline(MESSAGES);
  assert.deepEqual(finishedFrame(timeline), { visible: MESSAGES.length, typingIndex: null, finished: true });
});

test("発言が無い場面でも止まらない", () => {
  const timeline = buildTimeline([]);
  assert.deepEqual(timeline.steps, []);
  assert.deepEqual(frameAt(timeline, 0), { visible: 0, typingIndex: null, finished: true });
});
