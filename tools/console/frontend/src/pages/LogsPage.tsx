import { useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { WavyProgress } from "../components/expressive";
import { Icon } from "../components/Icon";
import { Button, ConnectedButtonGroup, IconButton, InlineError, StatusChip, type GroupOption } from "../components/ui";
import { formatClock } from "../format";
import type { LogStream } from "../hooks";
import { LEVEL_LABELS, type LogLevel } from "../logs";
import { prepareLogEntries } from "../logPresentation";
import { redactError, usePrivacy } from "../privacy";
import type { Profile } from "../types";
import { NotConnected } from "./shared";

type LevelFilter = "all" | LogLevel;
const MAX_RENDERED = 1500;

export function LogsPage({
  stream,
  connected,
  connecting,
  onConnect,
  selectedProfile,
  filterInputRef,
}: {
  stream: LogStream;
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  filterInputRef: RefObject<HTMLInputElement | null>;
}) {
  const [query, setQuery] = useState("");
  const [level, setLevel] = useState<LevelFilter>("all");
  const [follow, setFollow] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const privacy = usePrivacy();

  // 伏せ字モードでは、会話の本文と分かっている名前を隠した文で表示し、絞り込みもその文に対して行う
  // （隠した中身が絞り込みの一致で分かってしまわないように）
  const withLevels = useMemo(() => {
    return prepareLogEntries(stream.entries, privacy);
  }, [stream.entries, privacy]);
  const counts = useMemo(() => {
    const result: Record<LevelFilter, number> = { all: withLevels.length, error: 0, warn: 0, info: 0, system: 0 };
    for (const item of withLevels) result[item.level] += 1;
    return result;
  }, [withLevels]);
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = withLevels.filter(
      (item) => (level === "all" || item.level === level) && (!needle || item.text.toLowerCase().includes(needle)),
    );
    return filtered.slice(-MAX_RENDERED);
  }, [withLevels, level, query]);

  // 末尾を追いかける（上へスクロールしたら追跡をやめる）
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element && follow) element.scrollTop = element.scrollHeight;
  }, [visible, follow]);

  if (!connected) {
    return <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />;
  }

  const options: GroupOption<LevelFilter>[] = [
    { value: "all", label: "すべて", count: counts.all },
    { value: "error", label: LEVEL_LABELS.error, count: counts.error, dot: "level-dot-error" },
    { value: "warn", label: LEVEL_LABELS.warn, count: counts.warn, dot: "level-dot-warn" },
    { value: "info", label: LEVEL_LABELS.info, count: counts.info, dot: "level-dot-info" },
    { value: "system", label: LEVEL_LABELS.system, count: counts.system, dot: "level-dot-system" },
  ];

  return (
    <div className="logs-page">
      <div className="log-toolbar">
        <label className="m3-search-bar">
          <Icon name="search" size={24} />
          <input
            ref={filterInputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="本文で絞り込む（/ キー）"
            aria-label="ログを本文で絞り込む"
          />
          {query && <IconButton icon="close" label="絞り込みを解除" size="xs" onClick={() => setQuery("")} />}
        </label>
        <ConnectedButtonGroup options={options} value={level} onChange={setLevel} label="重さで絞り込む" />
        <StreamIndicator stream={stream} />
      </div>

      {(stream.status === "error" || stream.status === "ended") && (
        <div className="log-reconnect">
          <InlineError>{redactError(stream.message, stream.messageSafe, privacy) || "ログの接続が終了しました。"}</InlineError>
          <Button icon="refresh" onClick={stream.retry}>ログを再接続</Button>
        </div>
      )}

      <div className="terminal-wrap">
        {stream.status === "connecting" && (
          <div className="terminal-progress">
            <WavyProgress label="ログに接続しています" />
          </div>
        )}
        <div
          className="terminal"
          ref={scrollRef}
          onScroll={(event) => {
            const element = event.currentTarget;
            const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 24;
            if (atBottom !== follow) setFollow(atBottom);
          }}
          role="log"
          aria-live="off"
        >
          {visible.length === 0 ? (
            <p className="terminal-empty">{stream.entries.length ? "条件に合うログはありません。" : "ログを待っています…"}</p>
          ) : (
            visible.map(({ entry, level: entryLevel, text }, index) => (
              <div key={`${entry.ts}-${index}`} className={`log-row log-${entryLevel}`}>
                <time>{formatClock(entry.ts)}</time>
                <span className={`level level-${entryLevel}`}>{LEVEL_LABELS[entryLevel]}</span>
                <span className="log-source">{entry.source || "-"}</span>
                <span className="log-message">{highlight(text, query)}</span>
              </div>
            ))
          )}
        </div>

        {/* M3 Expressive の浮かぶツールバー */}
        <div className="m3-floating-toolbar" role="toolbar" aria-label="ログの操作">
          <IconButton
            icon={stream.paused ? "play" : "pause"}
            label={stream.paused ? "ライブ表示を再開 (p)" : "ライブ表示を一時停止 (p)"}
            variant={stream.paused ? "filled" : "standard"}
            onClick={() => stream.setPaused(!stream.paused)}
          />
          <IconButton icon="arrowDown" label="最新へ" variant={follow ? "standard" : "tonal"} onClick={() => setFollow(true)} disabled={follow} />
          <IconButton icon="trash" label="表示をクリア（サーバーのログは消えません）" onClick={stream.clear} />
        </div>
      </div>
      <p className="body-small muted log-footnote">
        重さ（エラー / 警告）は、discord.py の logging 形式ならそのレベルを、ボットの print なら本文の言葉から推定しています（journald 上はどちらも「情報」になるため）。
      </p>
    </div>
  );
}

export function StreamIndicator({ stream }: { stream: LogStream }) {
  const privacy = usePrivacy();
  if (stream.paused && stream.status === "live") {
    return <StatusChip tone="warning" icon="pause" label={stream.pendingCount ? `一時停止中（新着 ${stream.pendingCount}）` : "一時停止中"} />;
  }
  switch (stream.status) {
    case "live":
      return <StatusChip tone="good" label="ライブ" pulse />;
    case "connecting":
      return <StatusChip tone="info" label="接続中…" />;
    case "error":
      return (
        <span title={redactError(stream.message, stream.messageSafe, privacy) || undefined}>
          <StatusChip tone="critical" label="接続エラー" />
        </span>
      );
    case "ended":
      return (
        <span title={redactError(stream.message, stream.messageSafe, privacy) || undefined}>
          <StatusChip tone="serious" label="受信終了" />
        </span>
      );
    default:
      return <StatusChip tone="neutral" label="停止" />;
  }
}

function highlight(text: string, query: string): ReactNode {
  const needle = query.trim();
  if (!needle) return text;
  const lower = text.toLowerCase();
  const target = needle.toLowerCase();
  const parts: ReactNode[] = [];
  let cursor = 0;
  let index = lower.indexOf(target);
  while (index !== -1) {
    if (index > cursor) parts.push(text.slice(cursor, index));
    parts.push(<mark key={index}>{text.slice(index, index + needle.length)}</mark>);
    cursor = index + needle.length;
    index = lower.indexOf(target, cursor);
  }
  parts.push(text.slice(cursor));
  return parts;
}
