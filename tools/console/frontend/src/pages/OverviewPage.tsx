import { WavyProgress, LoadingIndicator } from "../components/expressive";
import { Icon } from "../components/Icon";
import { Button, Card, Chip, InlineError, Notice, Sparkline, SplitButton, Stat, StatusChip } from "../components/ui";
import { formatBytes, formatClock, formatDuration, formatNumber, formatPercent, formatRelative } from "../format";
import { LEVEL_LABELS } from "../logs";
import { prepareLogEntries } from "../logPresentation";
import { usePrivacy } from "../privacy";
import type { PageId } from "../navigation";
import { canStopService, ratioOf, serviceTone, usageTone } from "../status";
import type { ConnectionState, Insights, LogEntry, Profile, Sample, ServiceAction, StatusResponse } from "../types";
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

      {tone.hint && <InlineError>{privacy.redact(tone.hint)}</InlineError>}
      {statusError && <InlineError>{privacy.redact(statusError)}</InlineError>}
      {status?.service_error && <InlineError>{privacy.redact(status.service_error)}</InlineError>}

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

function SshCard({ connection, samples, now }: { connection: ConnectionState | null; samples: Sample[]; now: number }) {
  const privacy = usePrivacy();
  const latest = samples.length ? samples[samples.length - 1].rtt : null;
  return (
    <Card title="SSH 接続" icon="plug" source="ssh" className="span-4">
      <div className="metric">
        <span className="metric-value">
          <span className="display-small">{latest ?? "—"}</span>
          <span className="title-medium muted">ms 往復</span>
        </span>
      </div>
      <Sparkline label="SSH の往復時間" points={samples.map((s) => ({ t: s.t, v: s.rtt }))} format={(v) => `${Math.round(v)} ms`} min={0} />
      <dl className="kv">
        <dt>ホスト</dt>
        <dd className="mono">{privacy.mask(connection?.server?.hostname || connection?.profile?.host)}</dd>
        <dt>ホスト鍵</dt>
        <dd className="mono truncate" title={privacy.enabled ? undefined : (connection?.fingerprint ?? undefined)}>
          {privacy.mask(connection?.fingerprint)}
        </dd>
        <dt>接続</dt>
        <dd>{connection?.connected_at ? `${formatRelative(connection.connected_at * 1000, now)}から` : "—"}</dd>
      </dl>
    </Card>
  );
}

function ResourceCards({ status, samples }: { status: StatusResponse | null; samples: Sample[] }) {
  const privacy = usePrivacy();
  const resources = status?.resources ?? null;
  const memoryRatio = ratioOf(resources?.memory?.used, resources?.memory?.total);
  const diskRatio = ratioOf(resources?.disk?.used, resources?.disk?.total);
  const swapRatio = ratioOf(resources?.swap?.used, resources?.swap?.total);
  const cpuCount = resources?.cpu_count ?? 1;
  const loadRatio = resources?.load ? Math.min(1, resources.load[0] / cpuCount) : null;
  const memoryTone = usageTone(memoryRatio);
  const diskTone = usageTone(diskRatio);
  const loadTone = usageTone(loadRatio);
  const progressTone = (tone: string) => (tone === "critical" ? "error" : tone === "warning" ? "warning" : "primary");

  return (
    <>
      <Card title="CPU 負荷" icon="cpu" source="ssh" className="span-3">
        <div className="metric">
          <span className="headline-large">{resources?.load ? resources.load[0].toFixed(2) : "—"}</span>
          <StatusChip tone={loadTone.tone} label={loadTone.label} />
        </div>
        <span className="body-small muted">
          5分 {resources?.load?.[1].toFixed(2) ?? "—"} / 15分 {resources?.load?.[2].toFixed(2) ?? "—"} · {resources?.cpu_count ?? "—"} コア
        </span>
        <Sparkline label="CPU 負荷（1分平均）" points={samples.map((s) => ({ t: s.t, v: s.load1 }))} format={(v) => v.toFixed(2)} height={36} min={0} />
      </Card>

      <Card title="メモリ" icon="memory" source="ssh" className="span-3">
        <div className="metric">
          <span className="headline-large">{formatPercent(memoryRatio)}</span>
          <StatusChip tone={memoryTone.tone} label={memoryTone.label} />
        </div>
        <WavyProgress value={memoryRatio} tone={progressTone(memoryTone.tone)} label="メモリ使用率" />
        <span className="body-small muted">
          {formatBytes(resources?.memory?.used)} / {formatBytes(resources?.memory?.total)}（空き {formatBytes(resources?.memory?.available)}）
        </span>
        <span className="body-small muted">
          {resources?.swap && resources.swap.total > 0 ? `スワップ ${formatBytes(resources.swap.used)}（${formatPercent(swapRatio)}）` : "スワップなし"}
        </span>
      </Card>

      <Card title="ディスク（/）" icon="disk" source="ssh" className="span-3">
        <div className="metric">
          <span className="headline-large">{formatPercent(diskRatio)}</span>
          <StatusChip tone={diskTone.tone} label={diskTone.label} />
        </div>
        <WavyProgress value={diskRatio} tone={progressTone(diskTone.tone)} label="ディスク使用率" />
        <span className="body-small muted">
          {formatBytes(resources?.disk?.used)} / {formatBytes(resources?.disk?.total)}
        </span>
      </Card>

      <Card title="サーバー稼働時間" icon="clock" source="ssh" className="span-3">
        <span className="headline-large">{formatDuration(resources?.uptime_seconds)}</span>
        <span className="body-small muted">OS の再起動からの経過時間</span>
        {status?.resources_error && <InlineError>{privacy.redact(status.resources_error)}</InlineError>}
      </Card>
    </>
  );
}

/** Discord API・bot.db・journald から集めた「ボットの中の状態」（ボットのコードは変えない）。 */
function BotInsightsCard({
  insights,
  fetchedAt,
  error,
  now,
  onNavigate,
}: {
  insights: Insights | null;
  fetchedAt: number | null;
  error: string | null;
  now: number;
  onNavigate: (page: PageId) => void;
}) {
  const privacy = usePrivacy();
  if (!insights) {
    return (
      <Card title="ボットの中の状態" icon="bot" source="ssh" className="span-12">
        {error ? (
          <InlineError>{privacy.redact(error)}</InlineError>
        ) : (
          <div className="loading-row">
            <LoadingIndicator size={40} />
            <span className="body-medium muted">Discord API・DB・ログから集めています…</span>
          </div>
        )}
      </Card>
    );
  }
  const discord = insights.discord;
  const activity = insights.activity;
  const guilds = discord.guilds ?? [];
  const voiceGuilds = guilds.filter((guild) => guild.voice_channel);
  const voiceUnavailable = !discord.ok || guilds.some((guild) => guild.voice_error);
  const members = guilds.reduce((sum, guild) => sum + (guild.members ?? 0), 0);
  const online = guilds.reduce((sum, guild) => sum + (guild.online ?? 0), 0);
  const gatewayTrouble = activity.gateway_invalidated + activity.heartbeat_blocked;

  return (
    <Card
      title="ボットの中の状態"
      icon="bot"
      source="ssh"
      className="span-12"
      actions={
        <Button variant="text" icon="guilds" size="xs" onClick={() => onNavigate("guilds")}>
          サーバーごとに見る
        </Button>
      }
    >
      {!discord.ok && <InlineError>Discord API: {privacy.redact(discord.error)}</InlineError>}
      <div className="insight-grid">
        <Stat
          label="ボット"
          value={privacy.mask(discord.bot_user ?? null)}
          hint={`${insights.version ?? "版不明"}${insights.source_dir ? `（${insights.source_dir}/）` : ""}`}
        />
        <Stat label="Discord API の応答" value={discord.api_rtt_ms != null ? `${discord.api_rtt_ms} ms` : "—"} hint="サーバー → Discord の往復" />
        <Stat
          label="参加サーバー"
          value={discord.ok ? `${guilds.length}` : discord.partial ? "一部取得" : "—"}
          hint={discord.ok ? `メンバー ${members} 人 · オンライン ${online} 人` : discord.partial ? "サーバー一覧で取得済みの情報を確認してください" : undefined}
        />
        <Stat
          label="ボイス接続"
          value={discord.ok ? `${voiceGuilds.length}` : "—"}
          hint={voiceUnavailable ? "取得できていないボイス状態があります" : voiceGuilds.length ? privacy.mask(voiceGuilds.map((guild) => guild.voice_channel).join("、")) : "接続していません"}
        />
        <Stat
          label={`Gemini（${activity.window_hours}時間）`}
          value={`${formatNumber(activity.gemini_calls)} 回`}
          hint={`${formatNumber(activity.gemini_tokens)} トークン`}
        />
        <Stat
          label={`会話（${activity.window_hours}時間）`}
          value={`${formatNumber(activity.conversations)} 件`}
          hint={activity.last_conversation_at ? `最後: ${formatRelative(activity.last_conversation_at * 1000, now)}` : "記録なし"}
        />
        <Stat
          label="Discord との接続"
          value={gatewayTrouble ? `乱れ ${gatewayTrouble} 回` : "安定"}
          hint={`再接続 ${activity.gateway_connects} · 再開 ${activity.gateway_resumes} · 停止 ${activity.heartbeat_blocked}`}
        />
        <Stat label={`例外（${activity.window_hours}時間）`} value={`${activity.exceptions} 件`} hint={activity.exceptions ? "ログのページで確認" : "Traceback なし"} />
      </div>
      <div className="insight-foot">
        <Chip icon={insights.autonomous_supported ? "stage" : "block"}>
          {insights.autonomous_supported ? "自律行動に対応した版" : "この版には自律行動がありません"}
        </Chip>
        {activity.autonomous_events.slice(-1).map((event) => (
          <Chip key={event.at} icon="history" title={privacy.enabled ? undefined : event.text}>
            {formatRelative(event.at * 1000, now)}: {privacy.enabled ? "配信のイベント" : event.text}
          </Chip>
        ))}
        <span className="body-small muted insight-source">
          Discord API・bot.db・journald から取得（{fetchedAt ? formatRelative(fetchedAt * 1000, now) : "—"}、60秒ごとに更新）
        </span>
      </div>
      {insights.errors.length > 0 && <Notice icon="alert">{privacy.redact(insights.errors.join(" / "))}</Notice>}
    </Card>
  );
}

function GitCard({ status, now }: { status: StatusResponse | null; now: number }) {
  const privacy = usePrivacy();
  const git = status?.git ?? null;
  const committed = git?.committed_at ? Date.parse(git.committed_at) : NaN;
  return (
    <Card title="デプロイ中のコード" icon="git" source="ssh" className="span-5">
      {git ? (
        <>
          <div className="chips">
            <Chip icon="git">{privacy.mask(git.branch)}</Chip>
            <Chip>{privacy.redact(git.describe)}</Chip>
            {git.dirty_files > 0 ? <StatusChip tone="warning" label={`未コミット ${git.dirty_files} 件`} /> : <StatusChip tone="good" label="クリーン" />}
          </div>
          <p className="title-medium">{privacy.redact(git.subject)}</p>
          <span className="body-small muted mono">
            {git.commit} · {Number.isNaN(committed) ? git.committed_at : formatRelative(committed, now)}
          </span>
        </>
      ) : status?.git_error ? (
        <InlineError>{privacy.redact(status.git_error)}</InlineError>
      ) : (
        <LoadingIndicator size={36} />
      )}
    </Card>
  );
}

function RecentLogs({ logs, onNavigate }: { logs: LogEntry[]; onNavigate: (page: PageId) => void }) {
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
