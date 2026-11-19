// 疑似 Discord 画面の、1つの場面の再生。発言の出る時刻は demoTimeline.ts が決め、ここは経過時間を進めて
// 表示を組み立てるだけ（再生順の計算を表示に持ち込まない）。場面を切り替えるときは key を変えて作り直す。
import { useEffect, useMemo, useRef, useState } from "react";
import { DEMO_CAST, type DemoScenario } from "../../content";
import { buildTimeline, finishedFrame, frameAt, type DemoAuthor, type TimelineFrame } from "../../demoTimeline";
import { Avatar, Message, TypingRow } from "./DemoMessages";

// 再生の刻み。発言の出る時刻は demoTimeline.ts が決め、ここは経過時間を進めるだけ
const TICK_MS = 100;
// タブが裏に回っている間はタイマーが間引かれる。戻ったときに一気に進まないよう、1 回の進みに上限を置く
const MAX_STEP_MS = 400;

/** いま声を出している人（ボイス・ステージの参加者の輪を光らせる） */
function speakingAuthor(scenario: DemoScenario, frame: TimelineFrame): DemoAuthor | null {
  if (frame.typingIndex !== null) {
    const upcoming = scenario.messages[frame.typingIndex];
    return upcoming.spoken ? upcoming.author : null;
  }
  if (frame.finished || frame.visible === 0) return null;
  const latest = scenario.messages[frame.visible - 1];
  return latest.spoken ? latest.author : null;
}

function VoiceStrip({ scenario, frame }: { scenario: DemoScenario; frame: TimelineFrame }) {
  const members = useMemo(() => {
    // 今までに発言の見えた人だけを並べる。途中参加の人は、その発言が出たときに加わる。
    // ボットは、呼び出した通話・自分ではじめた配信のどちらでも最初からいるので先頭に置く
    const seen: DemoAuthor[] = ["bot"];
    for (const message of scenario.messages.slice(0, frame.visible)) {
      if (!seen.includes(message.author)) seen.push(message.author);
    }
    return seen;
  }, [scenario, frame.visible]);
  const speaking = speakingAuthor(scenario, frame);
  return (
    <div className="dc-voice">
      {members.map((author) => (
        <div key={author} className="dc-voice__member">
          <Avatar author={author} speaking={speaking === author} />
          <span>{DEMO_CAST[author].name}</span>
        </div>
      ))}
    </div>
  );
}

type PlayerProps = {
  scenario: DemoScenario;
  running: boolean;
  reducedMotion: boolean;
  onFinished: () => void;
};

/** 1 つの場面を再生する。場面を切り替えるときは key を変えて作り直す（経過時間を持ち越さない） */
export function ScenarioPlayer({ scenario, running, reducedMotion, onFinished }: PlayerProps) {
  const timeline = useMemo(() => buildTimeline(scenario.messages), [scenario]);
  const [frame, setFrame] = useState<TimelineFrame>(() =>
    reducedMotion ? finishedFrame(timeline) : frameAt(timeline, 0),
  );
  const elapsedRef = useRef(0);
  // 親が毎回新しい関数を渡しても、タイマーを張り直さずに最新のものを呼ぶ
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  // この場面で最後に反映した視差効果の設定。切り替わったときだけ表示を組み直す
  const appliedReducedRef = useRef(reducedMotion);
  useEffect(() => {
    if (appliedReducedRef.current === reducedMotion) return;
    appliedReducedRef.current = reducedMotion;
    // 動きを減らす設定では、流れる表示をやめて全部の発言を最初から出す。
    // 戻すときは、ここまで進めた経過時間の表示へそろえ直す（最終場面のまま固まったり、再生中に急に巻き戻らないように）
    setFrame(reducedMotion ? finishedFrame(timeline) : frameAt(timeline, elapsedRef.current));
  }, [reducedMotion, timeline]);

  useEffect(() => {
    if (!running || reducedMotion) return;
    let last = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      elapsedRef.current += Math.min(now - last, MAX_STEP_MS);
      last = now;
      const next = frameAt(timeline, elapsedRef.current);
      setFrame((current) =>
        current.visible === next.visible &&
        current.typingIndex === next.typingIndex &&
        current.finished === next.finished
          ? current
          : next,
      );
      if (next.finished) {
        window.clearInterval(timer);
        onFinishedRef.current();
      }
    }, TICK_MS);
    return () => window.clearInterval(timer);
  }, [running, reducedMotion, timeline]);

  const visible = scenario.messages.slice(0, frame.visible);
  const typing = frame.typingIndex === null ? null : scenario.messages[frame.typingIndex];
  const isVoice = scenario.channelKind === "voice" || scenario.channelKind === "stage";

  return (
    <>
      {isVoice && <VoiceStrip scenario={scenario} frame={frame} />}
      <div className="dc-log">
        {visible.map((message, index) => (
          <Message key={index} message={message} />
        ))}
      </div>
      <div className="dc-typing-slot">{typing && <TypingRow message={typing} />}</div>
    </>
  );
}
