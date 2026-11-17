import { useCallback, useState } from "react";
import { api } from "../api";
import { Icon } from "../components/Icon";
import { UnsetTag } from "../components/TopBar";
import { Button, Card, EmptyState, InlineError, Notice, StatusChip } from "../components/ui";
import { formatDateTime, formatRelative } from "../format";
import { usePolling } from "../hooks";
import { redactError, usePrivacy } from "../privacy";
import type { CommandRecord, ConnectionState, Profile } from "../types";

const HISTORY_INTERVAL_MS = 3000;

export function ConnectionPage({
  profiles,
  profilesSource,
  profilesError,
  selectedProfileId,
  onSelectProfile,
  connection,
  connecting,
  onConnect,
  onDisconnect,
  now,
}: {
  profiles: Profile[];
  profilesSource: string;
  profilesError: string | null;
  selectedProfileId: string;
  onSelectProfile: (id: string) => void;
  connection: ConnectionState | null;
  connecting: boolean;
  onConnect: () => void;
  onDisconnect: () => void;
  now: number;
}) {
  const privacy = usePrivacy();
  const [history, setHistory] = useState<CommandRecord[]>([]);
  const connected = connection?.connected ?? false;

  const loadHistory = useCallback(async () => {
    try {
      setHistory((await api.history()).commands);
    } catch {
      // 履歴は補助情報なので、取れなくても画面は止めない
    }
  }, []);
  usePolling(loadHistory, HISTORY_INTERVAL_MS, true);

  return (
    <div className="grid">
      <Card title="接続先" icon="plug" className="span-7">
        {profilesError && <InlineError>{profilesError}</InlineError>}
        <div className="m3-list" role="radiogroup" aria-label="接続先">
          {profiles.map((profile) => {
            const selected = profile.id === selectedProfileId;
            const isCurrent = connected && connection?.profile?.id === profile.id;
            return (
              <button
                key={profile.id}
                type="button"
                role="radio"
                aria-checked={selected}
                className={`m3-list-item ${selected ? "is-selected" : ""}`}
                onClick={() => onSelectProfile(profile.id)}
                disabled={connected || connecting}
              >
                <Icon name={selected ? "radioChecked" : "radio"} size={24} filled={selected} className="m3-list-leading" />
                <span className="m3-list-text">
                  <span className="m3-list-headline">
                    <strong className="title-medium">{privacy.mask(profile.label)}</strong>
                    <span className={`m3-tag ${profile.environment === "production" ? "is-prod" : "is-dev"}`}>
                      {profile.environment === "production" ? "本番" : "開発"}
                    </span>
                    <UnsetTag profile={profile} />
                    {isCurrent && <StatusChip tone="good" label="接続中" pulse />}
                  </span>
                  <span className="body-medium mono">
                    {profile.username ? `${privacy.mask(profile.username)}@` : ""}
                    {privacy.mask(profile.host)}
                    {profile.port ? `:${profile.port}` : ""}
                    {!profile.username && !profile.port ? "（~/.ssh/config を参照）" : ""}
                  </span>
                  <span className="body-small muted">
                    サービス <code>{privacy.redact(profile.service)}</code> · <code>{privacy.redact(profile.bot_dir)}</code>
                  </span>
                  {profile.note && <span className="body-small muted">{privacy.redact(profile.note)}</span>}
                </span>
              </button>
            );
          })}
        </div>
        <div className="profile-actions">
          {connected ? (
            <Button variant="tonal" icon="linkOff" onClick={onDisconnect}>
              切断する
            </Button>
          ) : (
            <Button variant="filled" size="m" icon="plug" onClick={onConnect} disabled={!selectedProfileId} busy={connecting}>
              {connecting ? "接続しています…" : "選んだ接続先に接続"}
            </Button>
          )}
          <span className="body-small muted">
            設定ファイル: <code>backend/{profilesSource || "profiles.json"}</code>（書き換えたら接続し直すだけで反映）
          </span>
        </div>
      </Card>

      <Card title="現在のセッション" icon="shield" source="ssh" className="span-5">
        <div className="session-state">
          <StatusChip
            tone={connected ? "good" : connection?.last_error ? "critical" : "neutral"}
            label={connected ? "接続中" : connection?.last_error ? "切断（エラー）" : "未接続"}
            pulse={connected}
          />
          {connected && connection?.connected_at && <span className="body-small muted">{formatRelative(connection.connected_at * 1000, now)}から</span>}
        </div>
        {connection?.last_error && !connected && <InlineError>{redactError(connection.last_error, connection.last_error_safe, privacy)}</InlineError>}
        <dl className="kv">
          <dt>ホスト鍵</dt>
          <dd className="mono break">{privacy.mask(connection?.fingerprint)}</dd>
          <dt>ホスト名</dt>
          <dd className="mono">{privacy.mask(connection?.server?.hostname)}</dd>
          <dt>OS</dt>
          <dd>{connection?.server?.os || "—"}</dd>
          <dt>カーネル</dt>
          <dd className="mono">{connection?.server?.kernel || "—"}</dd>
        </dl>
        <Notice>
          ホスト鍵は known_hosts と照合済みです。初めての接続先は、先に PowerShell で <code>ssh &lt;ホスト&gt;</code> を実行して登録してください。
        </Notice>
      </Card>

      <Card title="SSH コマンド履歴" icon="terminal" source="ssh" className="span-12">
        {history.length ? (
          <div className="table-wrap">
            <table className="m3-table is-compact">
              <thead>
                <tr>
                  <th>時刻</th>
                  <th>内容</th>
                  <th>コマンド</th>
                  <th className="num">終了コード</th>
                  <th className="num">所要</th>
                </tr>
              </thead>
              <tbody>
                {history.slice(0, 60).map((record, index) => (
                  <tr key={`${record.at}-${index}`} className={record.ok ? "" : "is-error"}>
                    <td className="mono nowrap">{formatDateTime(record.at * 1000)}</td>
                    <td className="nowrap">
                      <Icon name={record.ok ? "checkCircle" : "error"} size={18} filled className={record.ok ? "ok-icon" : "err-icon"} /> {privacy.redact(record.label)}
                    </td>
                    <td className="mono command-cell" title={redactError(record.error ?? record.command, record.error_safe, privacy)}>
                      {redactError(record.error ?? record.command, record.error_safe, privacy)}
                    </td>
                    <td className="num mono">{record.exit_status ?? "—"}</td>
                    <td className="num mono">{record.duration_ms} ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState icon="terminal" title="まだコマンドを送っていません">
            <p className="body-medium">接続すると、送ったコマンドと所要時間がここに並びます。</p>
          </EmptyState>
        )}
      </Card>
    </div>
  );
}
