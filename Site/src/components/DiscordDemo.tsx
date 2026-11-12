import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import {
  DEMO_CAST,
  DEMO_NOTE,
  DEMO_SCENARIOS,
  type DemoChannelKind,
  type DemoScenario,
} from "../content";
import {
  buildTimeline,
  finishedFrame,
  frameAt,
  type DemoAuthor,
  type DemoMessage,
  type TimelineFrame,
} from "../demoTimeline";
import { useInView, usePrefersReducedMotion } from "../hooks";
import {
  HashIcon,
  PaperclipIcon,
  PauseIcon,
  PlayIcon,
  SpeakerIcon,
  StageIcon,
  ThreadIcon,
} from "./icons";

// 再生の刻み。発言の出る時刻は demoTimeline.ts が決め、ここは経過時間を進めるだけ
const TICK_MS = 100;
// タブが裏に回っている間はタイマーが間引かれる。戻ったときに一気に進まないよう、1 回の進みに上限を置く
const MAX_STEP_MS = 400;

function ChannelIcon({ kind, size }: { kind: DemoChannelKind; size?: number }) {
  if (kind === "voice") return <SpeakerIcon size={size} />;
  if (kind === "stage") return <StageIcon size={size} />;
  if (kind === "thread") return <ThreadIcon size={size} />;
  return <HashIcon size={size} />;
}

function Avatar({ author, speaking = false }: { author: DemoAuthor; speaking?: boolean }) {
  return (
    <span className={`dc-avatar dc-avatar--${author}${speaking ? " is-speaking" : ""}`} aria-hidden>
      {DEMO_CAST[author].initial}
    </span>
  );
}

function SpokenTag() {
  return (
    <span className="dc-spoken">
      <span className="dc-wave" aria-hidden>
        <i />
        <i />
        <i />
        <i />
      </span>
      声
    </span>
  );
}

function Message({ message }: { message: DemoMessage }) {
  const member = DEMO_CAST[message.author];
  return (
    <>
      {message.dividerBefore && (
        <div className="dc-divider">
          <span>{message.dividerBefore}</span>
        </div>
      )}
      <div className="dc-message">
        <Avatar author={message.author} />
        <div className="dc-message__body">
          <div className="dc-message__head">
            <span className={`dc-name dc-name--${message.author}`}>{member.name}</span>
            {member.badge && <span className="dc-badge">{member.badge}</span>}
            {message.spoken && <SpokenTag />}
          </div>
          <p className="dc-text">
            {message.mention && <span className="dc-mention">@{DEMO_CAST.bot.name}</span>}
            {message.mention && " "}
            {message.text}
          </p>
          {message.attachment && (
            <span className="dc-attachment">
              <PaperclipIcon />
              {message.attachment}
            </span>
          )}
          {message.links && (
            <div className="dc-chips">
              {message.links.map((link) => (
                <span key={link} className="dc-link">
                  {link}
                </span>
              ))}
            </div>
          )}
          {message.reactions && (
            <div className="dc-chips">
              {message.reactions.map((reaction) => (
                <span key={reaction} className="dc-reaction">
                  {reaction} <b>1</b>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function TypingRow({ message }: { message: DemoMessage }) {
  const name = DEMO_CAST[message.author].name;
  return (
    <div className="dc-typing">
      <span className="dc-typing__dots" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      <span>
        <b>{name}</b> が{message.spoken ? "話しています" : "入力中"}…
      </span>
    </div>
  );
}

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
    const seen: DemoAuthor[] = [];
    for (const message of scenario.messages) {
      if (!seen.includes(message.author)) seen.push(message.author);
    }
    // ボットを先頭に置く（ステージでは話し手として見せたい）
    return seen.sort((a, b) => Number(b === "bot") - Number(a === "bot"));
  }, [scenario]);
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
function ScenarioPlayer({ scenario, running, reducedMotion, onFinished }: PlayerProps) {
  const timeline = useMemo(() => buildTimeline(scenario.messages), [scenario]);
  const [frame, setFrame] = useState<TimelineFrame>(() =>
    reducedMotion ? finishedFrame(timeline) : frameAt(timeline, 0),
  );
  const elapsedRef = useRef(0);
  // 親が毎回新しい関数を渡しても、タイマーを張り直さずに最新のものを呼ぶ
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  useEffect(() => {
    // 動きを減らす設定では、流れる表示をやめて全部の発言を最初から出す
    if (reducedMotion) setFrame(finishedFrame(timeline));
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

function composerLabel(scenario: DemoScenario): ReactNode {
  if (scenario.channelKind === "voice") return "通話中のチャットへメッセージを送信";
  if (scenario.channelKind === "stage") return "ステージのチャットへコメントを送信";
  if (scenario.channelKind === "thread") return "スレッドへメッセージを送信";
  return `#${scenario.channelName} へメッセージを送信`;
}

/** 疑似 Discord 画面。場面をタブで切り替えられ、放っておくと順番に流れる */
export function DiscordDemo() {
  const reducedMotion = usePrefersReducedMotion();
  const [rootRef, inView] = useInView<HTMLDivElement>({ once: false, threshold: 0.25 });
  const [index, setIndex] = useState(0);
  const [paused, setPaused] = useState(false);
  // 同じ場面をもう一度選んだときにも最初から再生できるよう、再生のたびに番号を進める
  const [playCount, setPlayCount] = useState(0);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const scenario = DEMO_SCENARIOS[index];

  const select = (next: number) => {
    setIndex(next);
    setPlayCount((count) => count + 1);
  };

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const count = DEMO_SCENARIOS.length;
    let next: number | null = null;
    if (event.key === "ArrowRight") next = (index + 1) % count;
    if (event.key === "ArrowLeft") next = (index - 1 + count) % count;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = count - 1;
    if (next === null) return;
    event.preventDefault();
    select(next);
    tabRefs.current[next]?.focus();
  };

  // サイドバーには、場面に出てくるチャンネルを重複なしで並べる
  const channels = useMemo(() => {
    const list: { kind: DemoChannelKind; name: string }[] = [];
    for (const item of DEMO_SCENARIOS) {
      if (!list.some((channel) => channel.kind === item.channelKind && channel.name === item.channelName)) {
        list.push({ kind: item.channelKind, name: item.channelName });
      }
    }
    return list;
  }, []);

  return (
    <div className="demo" ref={rootRef}>
      <div className="demo__tabs" role="tablist" aria-label="動作イメージの場面">
        {DEMO_SCENARIOS.map((item, itemIndex) => (
          <button
            key={item.id}
            ref={(element) => {
              tabRefs.current[itemIndex] = element;
            }}
            type="button"
            role="tab"
            id={`demo-tab-${item.id}`}
            aria-selected={itemIndex === index}
            aria-controls="demo-panel"
            tabIndex={itemIndex === index ? 0 : -1}
            className={`demo__tab${itemIndex === index ? " is-active" : ""}`}
            onClick={() => select(itemIndex)}
            onKeyDown={onTabKeyDown}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="dc" role="tabpanel" id="demo-panel" aria-labelledby={`demo-tab-${scenario.id}`}>
        <aside className="dc-side" aria-hidden>
          <div className="dc-side__server">みんなの部屋</div>
          {channels.map((channel) => {
            const active = channel.kind === scenario.channelKind && channel.name === scenario.channelName;
            return (
              <div key={`${channel.kind}:${channel.name}`} className={`dc-side__channel${active ? " is-active" : ""}`}>
                <ChannelIcon kind={channel.kind} size={15} />
                <span>{channel.name}</span>
              </div>
            );
          })}
        </aside>
        <div className="dc-main">
          <div className="dc-header">
            <ChannelIcon kind={scenario.channelKind} size={17} />
            <span className="dc-header__name">{scenario.channelName}</span>
          </div>
          <ScenarioPlayer
            key={`${scenario.id}:${playCount}`}
            scenario={scenario}
            running={!paused && inView}
            reducedMotion={reducedMotion}
            onFinished={() => select((index + 1) % DEMO_SCENARIOS.length)}
          />
          <div className="dc-composer" aria-hidden>
            {composerLabel(scenario)}
          </div>
        </div>
      </div>

      <div className="demo__foot">
        <p className="demo__caption">{scenario.caption}</p>
        {!reducedMotion && (
          <button
            type="button"
            className="demo__toggle"
            onClick={() => setPaused((value) => !value)}
            aria-pressed={paused}
          >
            {paused ? <PlayIcon /> : <PauseIcon />}
            {paused ? "再生する" : "止める"}
          </button>
        )}
      </div>
      <p className="demo__note">{DEMO_NOTE}</p>
    </div>
  );
}
