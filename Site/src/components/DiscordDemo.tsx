import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactNode } from "react";
import { DEMO_NOTE, DEMO_SCENARIOS, type DemoChannelKind, type DemoScenario } from "../content";
import { useInView, usePrefersReducedMotion } from "../hooks";
import { ChannelIcon } from "./demo/DemoMessages";
import { ScenarioPlayer } from "./demo/ScenarioPlayer";
import { PauseIcon, PlayIcon } from "./icons";

type TabBox = { left: number; top: number; width: number; height: number };

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
  const tabListRef = useRef<HTMLDivElement | null>(null);
  // 選んでいるタブの位置。測れたら、背景の色をタブからタブへ滑らせる（測れるまではタブ自身を塗る）
  const [indicator, setIndicator] = useState<TabBox | null>(null);
  // 前回の選択。最初の表示（StrictMode の2回目の effect 実行も含む）ではタブを横に送らない
  const lastSelectedRef = useRef<number | null>(null);
  const scenario = DEMO_SCENARIOS[index];

  useLayoutEffect(() => {
    const measure = () => {
      const tab = tabRefs.current[index];
      if (!tab) return;
      const next = { left: tab.offsetLeft, top: tab.offsetTop, width: tab.offsetWidth, height: tab.offsetHeight };
      setIndicator((current) =>
        current &&
        current.left === next.left &&
        current.top === next.top &&
        current.width === next.width &&
        current.height === next.height
          ? current
          : next,
      );
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    // フォントの読み込みや画面の幅でタブの大きさが変わったら測り直す
    const observer = new ResizeObserver(measure);
    for (const tab of tabRefs.current) {
      if (tab) observer.observe(tab);
    }
    return () => observer.disconnect();
  }, [index]);

  useEffect(() => {
    const previous = lastSelectedRef.current;
    lastSelectedRef.current = index;
    // 選択が変わったときだけ送る（視差効果の切り替えや StrictMode の2回目の実行では動かさない）
    if (previous === null || previous === index) return;
    // 幅の狭い画面ではタブが横にはみ出す。自動で場面が進んでも、選ばれたタブが見えるよう横にだけ送る
    // （scrollIntoView はページの縦の位置まで動かすので使わない）
    const list = tabListRef.current;
    const tab = tabRefs.current[index];
    if (!list || !tab || typeof list.scrollTo !== "function") return;
    const left = tab.offsetLeft - (list.clientWidth - tab.offsetWidth) / 2;
    list.scrollTo({ left: Math.max(0, left), behavior: reducedMotion ? "auto" : "smooth" });
  }, [index, reducedMotion]);

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
      <div
        ref={tabListRef}
        className={`demo__tabs${indicator ? " has-indicator" : ""}`}
        role="tablist"
        aria-label="動作イメージの場面"
      >
        {indicator && (
          <span
            className="demo__tab-indicator"
            aria-hidden
            style={
              {
                "--tab-x": `${indicator.left}px`,
                "--tab-y": `${indicator.top}px`,
                width: indicator.width,
                height: indicator.height,
              } as CSSProperties
            }
          />
        )}
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
            <span key={scenario.id} className="dc-header__name demo-fade">
              {scenario.channelName}
            </span>
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
        <p key={scenario.id} className="demo__caption demo-fade">
          {scenario.caption}
        </p>
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
