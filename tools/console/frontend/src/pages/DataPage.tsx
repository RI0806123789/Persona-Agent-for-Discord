import { useCallback, useEffect, useRef, useState, type CSSProperties } from "react";
import { api, errorMessage } from "../api";
import { Collapse } from "../components/Collapse";
import { LoadingIndicator, WavyProgress } from "../components/expressive";
import { Icon } from "../components/Icon";
import { Button, Card, InlineError, Notice, Stat } from "../components/ui";
import { formatBytes, formatNumber } from "../format";
import { DB_TABLE_SPECS, isViewableTable } from "../dbTables";
import { prefersReducedMotion } from "../motionPreference";
import { usePrivacy } from "../privacy";
import type { DbSummary, Profile } from "../types";
import { FeedbackSummary, FeedbackTrend, SchemaChip, StatusCounts } from "./DataSummaryParts";
import { DbTableDetail } from "./DbTableDetail";
import { NotConnected } from "./shared";

export function DataPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  refreshToken,
  connectionKey,
  onNames,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  refreshToken: number;
  connectionKey: string;
  /** テーブルの中身に出てきた利用者の表示名（伏せ字モードで、ほかの画面の文からも隠す） */
  onNames: (names: string[]) => void;
}) {
  const [summary, setSummary] = useState<DbSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 中身を開いているテーブル。中身には会話や記憶が入るので、開くのは利用者が選んだときだけにする
  // （覚えておいて、ページを開いただけで取り寄せることはしない）
  const [openSelection, setOpenSelection] = useState<{ connectionKey: string; table: string } | null>(null);
  // 閉じる動きの間も、最後に開いていたテーブルの中身を残しておく
  const [shownSelection, setShownSelection] = useState<{ connectionKey: string; table: string } | null>(null);
  // effect より先に子が取得を始めるため、描画時点で現在の接続の選択だけを渡す。
  const openTable = openSelection?.connectionKey === connectionKey ? openSelection.table : null;
  const shownTable = shownSelection?.connectionKey === connectionKey ? shownSelection.table : null;
  const privacy = usePrivacy();
  const requestId = useRef(0);

  // 接続先が変わったら閉じる（別のサーバーの中身を、選ばないうちに取り寄せない）
  // （閉じる動きの間に新しい接続先から取り寄せないよう、中身もすぐに外す）
  useEffect(() => {
    setOpenSelection(null);
    setShownSelection(null);
  }, [connectionKey]);

  const selectTable = (name: string) => {
    const next = openTable === name ? null : name;
    setOpenSelection(next ? { connectionKey, table: next } : null);
    if (next) {
      setShownSelection({ connectionKey, table: next });
      // 開いた中身のカードが見えるところまで送る（描画の後に。動きを減らす設定ならすぐに移る）
      requestAnimationFrame(() => {
        document.getElementById("db-table-detail")?.scrollIntoView({ behavior: prefersReducedMotion() ? "auto" : "smooth", block: "start" });
      });
    }
  };
  const closeTable = useCallback(() => setOpenSelection(null), []);

  const load = useCallback(async () => {
    const current = ++requestId.current;
    setLoading(true);
    try {
      const data = await api.db();
      if (current !== requestId.current) return;
      setSummary(data);
      setError(null);
    } catch (caught) {
      if (current !== requestId.current) return;
      setError(errorMessage(caught));
    } finally {
      if (current === requestId.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setSummary(null);
    setError(null);
    if (connected) void load();
    return () => { requestId.current += 1; };
  }, [connected, load, refreshToken, connectionKey]);

  if (!connected) {
    return <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />;
  }

  return (
    <div className="grid">
      <Card
        title="bot.db"
        icon="database"
        source="ssh"
        className="span-4"
        actions={
          <Button variant="text" size="xs" icon="refresh" onClick={() => void load()} busy={loading}>
            集計し直す
          </Button>
        }
      >
        {error && <InlineError>{error}</InlineError>}
        {summary && !summary.exists && <InlineError>{privacy.mask(summary.path)} が見つかりません。</InlineError>}
        {!summary && !error && <LoadingIndicator size={40} />}
        {summary?.exists && (
          <>
            <div className="stat-row">
              <Stat label="ファイル" value={formatBytes(summary.size_bytes)} />
              <Stat label="WAL" value={formatBytes(summary.wal_bytes)} />
            </div>
            <div className="schema-row">
              <span className="label-large muted">スキーマ</span>
              <span className="title-medium mono">v{summary.schema_version ?? "?"}</span>
              <SchemaChip deployed={summary.schema_version} latest={summary.latest_schema_version} />
            </div>
            <span className="body-small muted mono truncate" title={privacy.mask(summary.path)}>
              {privacy.mask(summary.path)}
            </span>
          </>
        )}
        <Notice icon="shield">
          読み取り専用（mode=ro）で開きます。集計では件数だけを持ち帰り、会話や記憶の中身は、テーブルを選んだときだけ取り寄せます。
        </Notice>
      </Card>

      <Card title="テーブルごとの行数" icon="layers" source="ssh" className="span-8">
        {summary?.tables ? (
          <>
            <TableRows tables={summary.tables} openTable={openTable} onSelect={selectTable} />
            <span className="body-small muted">テーブルを選ぶと、その中身（行）を下に表示します。もう一度選ぶと閉じます。</span>
          </>
        ) : summary ? (
          <span className="body-medium muted">取得できません</span>
        ) : (
          <WavyProgress label="集計しています" />
        )}
      </Card>

      <Collapse open={openTable !== null} className="span-12 db-detail-collapse">
        {shownTable && (
          <DbTableDetail
            key={`${shownTable}:${connectionKey}`}
            table={shownTable}
            refreshToken={refreshToken}
            onClose={closeTable}
            onNames={onNames}
          />
        )}
      </Collapse>

      <Card title="リマインダー / 文書キュー" icon="alarm" source="ssh" className="span-4">
        <StatusCounts title="リマインダー" counts={summary ? summary.reminders_by_status ?? null : undefined} />
        <StatusCounts title="文書処理キュー" counts={summary ? summary.queue_by_status ?? null : undefined} />
      </Card>

      <Card title="返信への評価" icon="thumbUp" source="ssh" className="span-4">
        <FeedbackSummary feedback={summary ? summary.feedback ?? null : undefined} />
      </Card>

      <Card title="ブラックリスト" icon="block" source="ssh" className="span-4">
        {summary?.blacklist ? (
          <div className="stat-row">
            <Stat label="ブロック中" value={`${summary.blacklist.blocked} 人`} />
            <Stat label="違反記録あり" value={`${summary.blacklist.users} 人`} />
          </div>
        ) : (
          <span className="body-medium muted">違反記録は未作成、または取得できません。</span>
        )}
      </Card>

      <Card title="評価の推移（直近7日・日本時間）" icon="overview" source="ssh" className="span-12">
        {!summary ? (
          <WavyProgress label="集計しています" />
        ) : summary.feedback_by_day ? (
          <FeedbackTrend days={summary.feedback_by_day} />
        ) : (
          <span className="body-medium muted">日別の評価を取得できません</span>
        )}
      </Card>

      {summary && summary.errors.length > 0 && (
        <div className="span-12">
          {summary.errors.map((message) => (
            <InlineError key={message}>{message}</InlineError>
          ))}
        </div>
      )}
    </div>
  );
}

function TableRows({
  tables,
  openTable,
  onSelect,
}: {
  tables: Record<string, number>;
  openTable: string | null;
  onSelect: (name: string) => void;
}) {
  const rows = Object.entries(tables).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map(([, count]) => count));
  return (
    <ul className="bar-list">
      {rows.map(([name, count], index) => {
        const content = (
          <>
            <span className="bar-label">
              <span className="body-medium mono">{name}</span>
              <span className="body-small muted">{DB_TABLE_SPECS[name]?.label ?? ""}</span>
            </span>
            <span className="bar-track">
              <span className="bar-fill" style={{ width: `${(count / max) * 100}%` }} />
            </span>
            <span className="bar-value title-small mono">{formatNumber(count)}</span>
          </>
        );
        // 中身を見せられるのはボットのテーブルだけ（ほかは件数だけ）
        return (
          <li key={name} style={{ "--row-index": index } as CSSProperties}>
            {isViewableTable(name) ? (
              <button
                type="button"
                className={`bar-row is-button ${openTable === name ? "is-selected" : ""}`}
                aria-pressed={openTable === name}
                aria-label={`${name}（${DB_TABLE_SPECS[name].label}・${count} 行）の中身を${openTable === name ? "閉じる" : "見る"}`}
                onClick={() => onSelect(name)}
              >
                {content}
                <Icon name={openTable === name ? "expandMore" : "chevronRight"} size={20} className="bar-row-icon" />
              </button>
            ) : (
              <div className="bar-row">
                {content}
                <span className="bar-row-icon" aria-hidden="true" />
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
