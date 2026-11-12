import type { ReactNode } from "react";
import { LoadingIndicator } from "../components/expressive";
import { Icon, type IconName } from "../components/Icon";
import { Button, Card, EmptyState, StatusChip } from "../components/ui";
import { formatDateTime } from "../format";
import { usePrivacy } from "../privacy";
import { canStopService, serviceTone } from "../status";
import type { DeployRecord, GitInfo, Operation, Profile, ServiceAction, ServiceStatus } from "../types";
import { DeployCard } from "./DeployCard";
import { NotConnected } from "./shared";

const ACTIONS: { action: ServiceAction; title: string; description: string; icon: IconName; variant: "filled" | "danger" | "tonal" }[] = [
  { action: "restart", title: "再起動", description: "設定や .env を変えたあとに。数秒間ボットが応答しなくなります。", icon: "restart", variant: "filled" },
  { action: "stop", title: "停止", description: "ボットを止めます。自分で起動するまで Discord に戻りません。", icon: "stop", variant: "danger" },
  { action: "start", title: "起動", description: "止まっているボットを起動します。", icon: "play", variant: "tonal" },
];

const ACTION_LABELS: Record<ServiceAction, string> = { start: "起動", stop: "停止", restart: "再起動" };

const SAFETY: { icon: IconName; text: ReactNode }[] = [
  { icon: "shield", text: <>実行できるのは <code>start / stop / restart</code> とデプロイの決まった手順だけ。画面から任意のコマンドは送れません（バックエンドのホワイトリスト）。</> },
  { icon: "bolt", text: <><code>sudo -n</code> で実行するため、パスワードを求められたら待たずに失敗します（入力待ちで固まらない）。</> },
  { icon: "check", text: <>停止はサービス名の入力で確認し、本番環境では確認ダイアログに警告を出します。</> },
  { icon: "plug", text: <>バックエンドは 127.0.0.1 だけで待ち受け、専用ヘッダーの無い要求（ほかのサイトからの呼び出し）を断ります。</> },
  {
    icon: "rocket",
    text: <>デプロイは、サーバーに未コミットの変更が無く、決めたブランチにいるときだけ始めます。途中で失敗したら、自動で前の版へ戻します（bot.db は戻しません）。</>,
  },
];

export function OperationsPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  profile,
  service,
  busyAction,
  onRequestAction,
  operations,
  git,
  deploy,
  deployRunning,
  stateUnknown,
  onDeploy,
  now,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  profile: Profile | null;
  service: ServiceStatus | null;
  busyAction: ServiceAction | null;
  onRequestAction: (action: ServiceAction) => void;
  operations: Operation[];
  git: GitInfo | null;
  deploy: DeployRecord | null;
  deployRunning: boolean;
  stateUnknown: boolean;
  onDeploy: () => void;
  now: number;
}) {
  const privacy = usePrivacy();
  const tone = serviceTone(service);
  const active = service?.active_state === "active";
  const deployUnknown = stateUnknown || deploy?.status === "unknown";

  return (
    <div className="grid">
      {connected ? (
        <Card title="サービス操作" icon="power" source="ssh" className="span-12">
          <div className="ops-status">
            <span className="label-large muted">現在の状態</span>
            <StatusChip tone={tone.tone} label={tone.label} pulse={active} />
            <code className="body-small">{profile ? privacy.redact(`sudo -n systemctl <操作> ${profile.service}`) : ""}</code>
            {profile?.environment === "production" && <StatusChip tone="serious" icon="shield" label="本番環境に接続中" />}
            {(deployRunning || deployUnknown) && (
              <StatusChip tone={deployUnknown ? "critical" : "info"} icon="rocket"
                label={deployUnknown ? "デプロイの状態不明のためサービス操作を停止しています" : "デプロイ中は操作できません"} />
            )}
          </div>
          <div className="action-grid">
            {ACTIONS.map((item) => {
              const disabled =
                busyAction !== null || deployRunning || deployUnknown || !service || (item.action === "start" && active) || (item.action === "stop" && !canStopService(service));
              return (
                <div key={item.action} className={`action-card action-${item.variant}`}>
                  <span className="action-icon">{busyAction === item.action ? <LoadingIndicator size={40} /> : <Icon name={item.icon} size={28} />}</span>
                  <strong className="title-large">{item.title}</strong>
                  <span className="body-medium muted">{item.description}</span>
                  <Button variant={item.variant} size="m" icon={item.icon} onClick={() => onRequestAction(item.action)} disabled={disabled}>
                    {busyAction === item.action ? `${item.title}しています…` : item.title}
                  </Button>
                </div>
              );
            })}
          </div>
        </Card>
      ) : (
        <div className="span-12">
          <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />
        </div>
      )}

      <DeployCard
        deploy={deploy}
        running={deployRunning}
        stateUnknown={deployUnknown}
        connected={connected}
        profile={profile ?? selectedProfile}
        git={git}
        disabled={busyAction !== null}
        onDeploy={onDeploy}
        now={now}
      />

      <Card title="操作の記録（このセッション）" icon="history" source="ssh" className="span-12">
        {operations.length ? (
          <div className="table-wrap">
            <table className="m3-table">
              <thead>
                <tr>
                  <th>時刻</th>
                  <th>操作</th>
                  <th>接続先</th>
                  <th>結果</th>
                  <th className="num">所要</th>
                </tr>
              </thead>
              <tbody>
                {operations.map((operation) => (
                  <tr key={operation.id}>
                    <td className="mono nowrap">{formatDateTime(operation.at * 1000)}</td>
                    <td>{ACTION_LABELS[operation.action]}</td>
                    <td>
                      {privacy.mask(operation.profile_id)}
                      {operation.environment === "production" && <span className="m3-tag is-prod">本番</span>}
                    </td>
                    <td>
                      <StatusChip tone={operation.ok ? "good" : "critical"} label={operation.ok ? "成功" : "失敗"} />
                      {!operation.ok && <p className="body-small muted">{privacy.redact(operation.message)}</p>}
                    </td>
                    <td className="num mono">{(operation.duration_ms / 1000).toFixed(1)}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon="history" title="まだ操作していません">
            <p className="body-medium">起動・停止・再起動をすると、ここに記録されます（バックエンドを再起動すると消えます）。</p>
          </EmptyState>
        )}
      </Card>

      <Card title="誤操作を防ぐ仕組み" icon="shield" className="span-12" variant="outlined">
        <ul className="safety-list">
          {SAFETY.map((item, index) => (
            <li key={index}>
              <span className="safety-icon">
                <Icon name={item.icon} size={20} />
              </span>
              <span className="body-medium">{item.text}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
