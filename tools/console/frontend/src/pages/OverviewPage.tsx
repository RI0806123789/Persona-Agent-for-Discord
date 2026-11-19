import { LoadingIndicator } from "../components/expressive";
import { Icon } from "../components/Icon";
import { Button, Card, InlineError, Sparkline, SplitButton, Stat, StatusChip } from "../components/ui";
import { formatBytes, formatClock, formatDuration, formatNumber } from "../format";
import { LEVEL_LABELS } from "../logs";
import { prepareLogEntries } from "../logPresentation";
import { usePrivacy } from "../privacy";
import type { PageId } from "../navigation";
import { canStopService, serviceTone } from "../status";
import type { ConnectionState, Insights, LogEntry, Profile, Sample, ServiceAction, StatusResponse } from "../types";
import { BotInsightsCard, GitCard, ResourceCards, SshCard } from "./OverviewCards";
import { NotConnected } from "./shared";

export function OverviewPage({
  connected,
  connecting,
  status,
  statusError,
  samples,
  connection,
  logs,
  insights,
  insightsFetchedAt,
  insightsError,
  onNavigate,
  onRequestAction,
  busyAction,
  deployRunning,
  deployUnknown,
  onConnect,
  selectedProfile,
  now,
}: {
  connected: boolean;
  connecting: boolean;
  status: StatusResponse | null;
  statusError: string | null;
  samples: Sample[];
  connection: ConnectionState | null;
  logs: LogEntry[];
  insights: Insights | null;
  insightsFetchedAt: number | null;
  insightsError: string | null;
  onNavigate: (page: PageId) => void;
  onRequestAction: (action: ServiceAction) => void;
  busyAction: ServiceAction | null;
  deployRunning: boolean;
  deployUnknown: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  now: number;
}) {
  if (!connected) {
    return <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />;
  }
  return (
    <div className="grid">
      <ServiceHero
        status={status}
        statusError={statusError}
        samples={samples}
        onRequestAction={onRequestAction}
        busyAction={busyAction}
        deployRunning={deployRunning}
        deployUnknown={deployUnknown}
        onNavigate={onNavigate}
      />
      <SshCard connection={connection} samples={samples} now={now} />
      <ResourceCards status={status} samples={samples} />
      <BotInsightsCard insights={insights} fetchedAt={insightsFetchedAt} error={insightsError} now={now} onNavigate={onNavigate} />
      <GitCard status={status} now={now} />
      <RecentLogs logs={logs} onNavigate={onNavigate} />
    </div>
  );
}

function ServiceHero({
  status,
  statusError,
  samples,
  onRequestAction,
  busyAction,
  deployRunning,
  deployUnknown,
  onNavigate,
}: {
  status: StatusResponse | null;
  statusError: string | null;
  samples: Sample[];
  onRequestAction: (action: ServiceAction) => void;
  busyAction: ServiceAction | null;
  deployRunning: boolean;
  deployUnknown: boolean;
  onNavigate: (page: PageId) => void;
}) {
  const privacy = usePrivacy();
  const service = status?.service ?? null;
  const tone = serviceTone(service);
  const active = service?.active_state === "active";

  return (
    <section className={`m3-card m3-card-filled hero span-8 tone-${tone.tone}`}>
      <div className="hero-layout">
        <div className={`hero-shape tone-${tone.tone} ${active ? "is-alive" : ""}`} aria-hidden="true">
          <svg viewBox="0 0 120 120">
            <path d={COOKIE_PATH} />
          </svg>
          {service ? <Icon name={active ? "bot" : "power"} size={44} filled /> : <LoadingIndicator size={44} />}
        </div>

        <div className="hero-text">
          <div className="hero-top">
            <StatusChip tone={tone.tone} label={tone.label} pulse={active} />
            {(deployRunning || deployUnknown) && <StatusChip tone={deployUnknown ? "critical" : "info"} icon="rocket" label={deployUnknown ? "デプロイの状態不明" : "デプロイ中"} />}
            <span className="label-large mono hero-unit">{service ? privacy.redact(`${service.name}.service`) : "—"}</span>
          </div>
          <span className="label-large hero-figure-label">{active ? "起動してから" : "状態"}</span>
          <span className="display-small hero-figure">{active ? formatDuration(service?.uptime_seconds) : tone.label}</span>
          <span className="body-medium hero-desc">{privacy.redact(service?.description)}</span>
        </div>

        <div className="hero-spark">
          <span className="label-medium">ボットのメモリ使用量（直近5分）</span>
          <Sparkline label="ボットのメモリ使用量" points={samples.map((s) => ({ t: s.t, v: s.botMemory }))} format={(v) => formatBytes(v)} height={56} />
        </div>
      </div>

      {tone.hint && <InlineError>{tone.hint}</InlineError>}
      {statusError && <InlineError>{statusError}</InlineError>}
      {status?.service_error && <InlineError>{status.service_error}</InlineError>}

      <div className="stat-row">
        <Stat label="PID" value={service?.main_pid ?? "—"} mono />
        <Stat label="メモリ" value={formatBytes(service?.memory_bytes)} />
        <Stat label="CPU 時間" value={service?.cpu_seconds != null ? formatDuration(service.cpu_seconds) : "—"} />
        <Stat label="自動再起動" value={`${formatNumber(service?.restarts)} 回`} hint={service?.restarts ? "落ちて復帰した回数" : undefined} />
        <Stat label="自動起動" value={service?.unit_file_state === "enabled" ? "有効" : service?.unit_file_state || "—"} />
      </div>

      <div className="hero-actions">
        <SplitButton
          label={active ? "再起動" : "起動"}
          icon={active ? "restart" : "play"}
          onClick={() => onRequestAction(active ? "restart" : "start")}
          disabled={!service || deployRunning || deployUnknown}
          busy={busyAction !== null}
          items={[
            { label: "停止", icon: "stop", onSelect: () => onRequestAction("stop"), disabled: !canStopService(service), danger: true },
            { label: "起動", icon: "play", onSelect: () => onRequestAction("start"), disabled: active },
            { label: "再起動", icon: "restart", onSelect: () => onRequestAction("restart"), disabled: !active },
          ]}
        />
        <Button variant="outlined" icon="logs" onClick={() => onNavigate("logs")}>
          ログを見る
        </Button>
        {(deployRunning || deployUnknown) && (
          <Button variant="text" icon="rocket" onClick={() => onNavigate("operations")}>
            デプロイの記録を見る
          </Button>
        )}
      </div>
    </section>
  );
}

// M3 Expressive の形のライブラリにある「クッキー（9つの山）」に近い形
const COOKIE_PATH = (() => {
  const points: string[] = [];
  for (let i = 0; i <= 180; i += 1) {
    const angle = (i / 180) * Math.PI * 2;
    const radius = 52 + 4 * Math.cos(angle * 9);
    points.push(`${(60 + Math.cos(angle) * radius).toFixed(2)} ${(60 + Math.sin(angle) * radius).toFixed(2)}`);
  }
  return `M${points.join("L")}Z`;
})();

export function RecentLogs({ logs, onNavigate }: { logs: LogEntry[]; onNavigate: (page: PageId) => void }) {
  const privacy = usePrivacy();
  const recent = prepareLogEntries(logs, privacy).slice(-8);
  return (
    <Card
      title="最近のログ"
      icon="logs"
      source="ssh"
      className="span-7"
      actions={
        <Button variant="text" size="xs" onClick={() => onNavigate("logs")}>
          すべて見る
        </Button>
      }
    >
      {recent.length ? (
        <ul className="mini-log">
          {recent.map(({ entry, level, text }, index) => (
            <li key={`${entry.ts}-${index}`} className={`log-${level}`}>
              <time className="mono body-small">{formatClock(entry.ts)}</time>
              <span className={`level level-${level}`}>{LEVEL_LABELS[level]}</span>
              <span className="mini-log-text body-medium">{text}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="loading-row">
          <LoadingIndicator size={32} />
          <span className="body-medium muted">ログを待っています…</span>
        </div>
      )}
    </Card>
  );
}
