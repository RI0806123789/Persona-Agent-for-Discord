// データページの集計カードの中身（スキーマの版・状態ごとの件数・評価の割合と推移）。
// 取得・選択・開閉の状態は DataPage.tsx が持ち、ここは受け取った値を描くだけ。
import { useState } from "react";
import { WavyProgress } from "../components/expressive";
import { Chip, ConnectedButtonGroup, StatusChip } from "../components/ui";
import { lastSevenDays } from "../data";
import { STATUS_LABELS } from "../dbTables";
import { formatNumber, formatPercent } from "../format";

/** 本番のスキーマの版を、手元のリポジトリ（src/persona_bot/core/database.py）の版と比べる。 */
export function SchemaChip({ deployed, latest }: { deployed: number | undefined; latest: number | null | undefined }) {
  if (latest === null || latest === undefined) return <StatusChip tone="neutral" label="手元の版を読めません" />;
  if (deployed === null || deployed === undefined) return <StatusChip tone="neutral" label="サーバーの版を読めません" />;
  if (deployed === latest) return <StatusChip tone="good" icon="checkCircle" label="手元と同じ" />;
  if (deployed !== undefined && deployed > latest) return <StatusChip tone="warning" icon="alert" label={`手元（v${latest}）より新しい`} />;
  return <StatusChip tone="warning" icon="alert" label={`手元は v${latest}（未デプロイ）`} />;
}

export function StatusCounts({ title, counts }: { title: string; counts: Record<string, number> | null | undefined }) {
  const entries = Object.entries(counts ?? {});
  return (
    <div className="status-counts">
      <span className="label-large muted">{title}</span>
      {entries.length ? (
        <div className="chips">
          {entries.map(([status, count]) => (
            <Chip key={status}>
              {STATUS_LABELS[status] ?? status} <strong className="mono">{count}</strong>
            </Chip>
          ))}
        </div>
      ) : (
        <span className="body-medium muted">{counts === null ? "取得できません" : counts === undefined ? "集計しています" : "なし"}</span>
      )}
    </div>
  );
}

export function FeedbackSummary({ feedback }: { feedback: { good: number; bad: number } | null | undefined }) {
  if (feedback === null) return <span className="body-medium muted">取得できません</span>;
  if (!feedback) return <WavyProgress label="集計しています" />;
  const total = feedback.good + feedback.bad;
  const ratio = total ? feedback.good / total : null;
  return (
    <>
      <div className="metric">
        <span className="display-small">{formatPercent(ratio)}</span>
        <span className="body-medium muted">が 👍（{total} 件中）</span>
      </div>
      <div className="split-bar" role="img" aria-label={`👍 ${feedback.good} 件、👎 ${feedback.bad} 件`}>
        <span className="split-good" style={{ flexGrow: feedback.good || 0.0001 }} />
        <span className="split-bad" style={{ flexGrow: feedback.bad || 0.0001 }} />
      </div>
      <div className="legend body-medium">
        <span>
          <i className="swatch swatch-good" /> 👍 {formatNumber(feedback.good)}
        </span>
        <span>
          <i className="swatch swatch-bad" /> 👎 {formatNumber(feedback.bad)}
        </span>
      </div>
    </>
  );
}

type TrendView = "chart" | "table";

export function FeedbackTrend({ days }: { days: { day: string; good: number; bad: number }[] }) {
  const [view, setView] = useState<TrendView>("chart");
  const series = lastSevenDays(days);
  const max = Math.max(1, ...series.map((day) => day.good + day.bad));
  const total = series.reduce((sum, day) => sum + day.good + day.bad, 0);
  return (
    <div className="trend">
      <div className="trend-head">
        <div className="legend body-medium">
          <span>
            <i className="swatch swatch-good" /> 👍
          </span>
          <span>
            <i className="swatch swatch-bad" /> 👎
          </span>
          <span className="muted">直近7日で {total} 件</span>
        </div>
        <ConnectedButtonGroup
          label="表示のしかた"
          value={view}
          onChange={setView}
          options={[
            { value: "chart", label: "グラフ", icon: "overview" },
            { value: "table", label: "表", icon: "layers" },
          ]}
        />
      </div>
      {view === "table" ? (
        <div className="table-wrap">
          <table className="m3-table is-compact">
            <caption className="visually-hidden">直近7日の評価（日本時間）</caption>
            <thead>
              <tr>
                <th>日付</th>
                <th className="num">👍</th>
                <th className="num">👎</th>
                <th className="num">計</th>
                <th className="num">満足度</th>
              </tr>
            </thead>
            <tbody>
              {series.map((day) => {
                const sum = day.good + day.bad;
                return (
                  <tr key={day.key}>
                    <td className="mono">{day.label}</td>
                    <td className="num mono">{day.good}</td>
                    <td className="num mono">{day.bad}</td>
                    <td className="num mono">{sum}</td>
                    <td className="num mono">{sum ? formatPercent(day.good / sum) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
      <div className="trend-bars" role="group" aria-label={`直近7日の評価の推移（合計 ${total} 件）`}>
        {series.map((day) => (
          <div key={day.key} className="trend-col" tabIndex={0} aria-label={`${day.label}: 👍 ${day.good} 件、👎 ${day.bad} 件`}>
            <div className="trend-stack" style={{ height: `${((day.good + day.bad) / max) * 100}%` }}>
              {day.bad > 0 && <span className="trend-bad" style={{ flexGrow: day.bad }} />}
              {day.good > 0 && <span className="trend-good" style={{ flexGrow: day.good }} />}
            </div>
            <span className="trend-label label-medium">{day.label}</span>
            <span className="m3-tooltip trend-tip">
              <strong>{day.label}</strong>
              👍 {day.good} / 👎 {day.bad}
            </span>
          </div>
        ))}
      </div>
      )}
      {total === 0 && <span className="body-small muted">直近7日の評価はありません。</span>}
    </div>
  );
}
