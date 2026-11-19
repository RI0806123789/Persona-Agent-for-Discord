// データページのテーブルの中身の、セル・開いた行の描画。会話・記憶・リマインダーの本文と Discord の ID も
// 届くので、伏せ字モードでは列の種類ごとに隠す（dbTables.ts の isSecretKind）。取り寄せ・ページ送りは DbTableDetail.tsx。
import { Chip, StatusChip } from "../components/ui";
import {
  STATUS_LABELS,
  hourlyCounts,
  inlineText,
  isOn,
  isSecretKind,
  parseDbTime,
  personaName,
  readAnalysis,
  recurrenceLabel,
  respondModeLabel,
  statusTone,
  userName,
  type ColumnSpec,
} from "../dbTables";
import { formatFullDateTime, formatNumber, formatPercent } from "../format";
import { MASK, usePrivacy } from "../privacy";
import type { DbRow, DbTablePage, DbValue } from "../types";

// 行を開いたとき、表の幅いっぱいを使って見せる列の種類（長い本文やグラフ）
const WIDE_KINDS = new Set(["text", "json", "samples", "analysis", "hourly", "error", "raw"]);

/** 行を開いたときの、全部の列。 */
export function RowFields({ columns, row, page }: { columns: ColumnSpec[]; row: DbRow; page: DbTablePage }) {
  return (
    <dl className="db-fields">
      {columns.map((column) => (
        <div key={column.key} className={`db-field ${WIDE_KINDS.has(column.kind) ? "is-wide" : ""}`}>
          <dt className="label-medium">
            {column.label}
            {column.label !== column.key && <code className="db-field-key">{column.key}</code>}
          </dt>
          <dd className="body-medium">
            <CellValue column={column} row={row} page={page} detail />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function isEmpty(value: DbValue | undefined): boolean {
  return value === null || value === undefined || value === "";
}

function isDm(value: DbValue | undefined): boolean {
  return value === "0" || value === 0;
}

/** 1つのセル。一覧（detail なし）では1行に収め、行を開いたとき（detail）は全部を見せる。 */
export function CellValue({ column, row, page, detail = false }: { column: ColumnSpec; row: DbRow; page: DbTablePage; detail?: boolean }) {
  const privacy = usePrivacy();
  const value = row.cells[column.key];

  if (row.hidden?.includes(column.key)) {
    return <span className="body-small muted">統計の収集を止めているため、持ち帰っていません</span>;
  }
  if (column.kind === "guild" && isDm(value)) return <span>DM</span>;
  if (isEmpty(value)) return <span className="muted">—</span>;
  if (privacy.enabled && isSecretKind(column.kind)) {
    // 件数だけは分かっても困らないので残す（発言サンプルが何件あるか、など）
    if (column.kind === "samples" && Array.isArray(value)) return <span className="muted">{MASK}（{value.length} 件）</span>;
    return <span className="mono muted">{MASK}</span>;
  }

  switch (column.kind) {
    case "user": {
      const name = userName(page, value);
      return (
        <span className="db-user">
          {name && <span>{name}</span>}
          <span className="mono muted body-small">{String(value)}</span>
        </span>
      );
    }
    case "guild":
    case "id":
      return <span className="mono">{String(value)}</span>;
    case "name":
    case "label":
      return <span>{inlineText(value)}</span>;
    case "text":
      return detail ? <p className="db-text">{String(value)}</p> : <span className="db-clip">{inlineText(value)}</span>;
    case "error":
      return <p className="db-text db-error">{String(value)}</p>;
    case "isoTime":
    case "logTime":
    case "unixTime": {
      const ms = parseDbTime(value, column.kind);
      if (ms === null) return <span className="mono">{inlineText(value)}</span>;
      return (
        <time className="mono db-time" dateTime={new Date(ms).toISOString()} title={String(value)}>
          {formatFullDateTime(ms)}
        </time>
      );
    }
    case "date":
      return <span className="mono">{String(value)}</span>;
    case "level":
      return <Meter value={value} label={column.label} />;
    case "count":
      return <span className="mono">{typeof value === "number" ? formatNumber(value) : inlineText(value)}</span>;
    case "hours":
      return <span className="mono">{inlineText(value)} 時間</span>;
    case "flag":
      return isOn(value) ? <StatusChip tone="info" icon="check" label="ON" /> : <StatusChip tone="neutral" label="OFF" />;
    case "rating":
      return value === 1 ? (
        <span role="img" aria-label="良い（👍）">👍</span>
      ) : value === -1 ? (
        <span role="img" aria-label="良くない（👎）">👎</span>
      ) : (
        <span className="mono">{inlineText(value)}</span>
      );
    case "status":
      return <StatusChip tone={statusTone(value)} label={typeof value === "string" ? (STATUS_LABELS[value] ?? value) : inlineText(value)} />;
    case "persona":
      return <span title={String(value)}>{personaName(value)}</span>;
    case "model":
      return <span className="mono">{String(value)}</span>;
    case "respondMode":
      return <span>{respondModeLabel(value)}</span>;
    case "recurrence":
      return <span>{recurrenceLabel(value)}</span>;
    case "json":
      return detail ? <JsonValue value={value} /> : <span className="db-clip">{inlineText(value)}</span>;
    case "hourly":
      return detail ? <HourlyBars value={value} /> : <span className="mono">{formatNumber(hourlyCounts(value).reduce((a, b) => a + b, 0))}</span>;
    case "samples":
      return detail ? <SampleList value={value} /> : <span className="mono">{Array.isArray(value) ? `${value.length} 件` : "—"}</span>;
    case "analysis":
      return detail ? <AnalysisView value={value} /> : <span className="db-clip">{readAnalysis(value)?.topics.map((topic) => topic.label).join("、") || "—"}</span>;
    case "raw":
      return detail && typeof value === "object" ? <JsonValue value={value} /> : <span className="db-clip">{inlineText(value)}</span>;
  }
}

/** 0〜100 の値（親密度・機嫌・テンション）。数字と短い棒で示す。 */
function Meter({ value, label }: { value: DbValue; label: string }) {
  const number = typeof value === "number" && Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : null;
  if (number === null) return <span className="mono">{inlineText(value)}</span>;
  return (
    <span className="db-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={number} aria-label={label}>
      <span className="db-meter-track" aria-hidden="true">
        <span className="db-meter-fill" style={{ width: `${number}%` }} />
      </span>
      <span className="mono">{formatNumber(Math.round(number))}</span>
    </span>
  );
}

/** 記憶の値など、JSON の値。文字列はそのまま、文字列の配列は区切って、それ以外は整形した JSON で見せる。 */
function JsonValue({ value }: { value: DbValue }) {
  if (typeof value === "string") return <p className="db-text">{value}</p>;
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) {
    return (
      <span className="chips">
        {value.map((item, index) => (
          <Chip key={index}>{item}</Chip>
        ))}
      </span>
    );
  }
  return <pre className="db-json">{JSON.stringify(value, null, 2)}</pre>;
}

/** 時間帯別の発言回数（24 本の棒）。棒にフォーカス・ホバーすると、その時間の回数を出す。 */
function HourlyBars({ value }: { value: DbValue }) {
  const counts = hourlyCounts(value);
  const total = counts.reduce((a, b) => a + b, 0);
  const max = Math.max(1, ...counts);
  const peak = counts.indexOf(Math.max(...counts));
  return (
    <figure className="db-hourly">
      <div className="db-hourly-bars" role="group" aria-label={total ? `時間帯別の発言回数（合計 ${total} 回、最も多いのは ${peak} 時台）` : "時間帯別の発言回数（記録なし）"}>
        {counts.map((count, hour) => (
          <span key={hour} className="db-hourly-col" tabIndex={0} aria-label={`${hour} 時台: ${count} 回`}>
            <span className="db-hourly-bar" style={{ height: `${(count / max) * 100}%` }} />
            <span className="m3-tooltip db-hourly-tip">
              {hour} 時台: {count} 回
            </span>
          </span>
        ))}
      </div>
      <figcaption className="db-hourly-axis label-small muted" aria-hidden="true">
        <span>0</span>
        <span>6</span>
        <span>12</span>
        <span>18</span>
        <span>23 時</span>
      </figcaption>
    </figure>
  );
}

/** 直近の発言（ボットは古い順に保存しているので、新しい順に並べ替えて見せる）。 */
function SampleList({ value }: { value: DbValue }) {
  if (!Array.isArray(value) || value.length === 0) return <span className="muted">—</span>;
  const samples = [...value].reverse();
  return (
    <div className="db-samples">
      <span className="body-small muted">新しい順・{value.length} 件</span>
      <ol>
        {samples.map((sample, index) => (
          <li key={index} className="db-text">
            {inlineText(sample)}
          </li>
        ))}
      </ol>
    </div>
  );
}

/** 会話統計の解析（話題・ビッグファイブ・性格の説明）。 */
function AnalysisView({ value }: { value: DbValue }) {
  const analysis = readAnalysis(value);
  if (!analysis) return <JsonValue value={value} />;
  const computed = analysis.computedAt ? parseDbTime(analysis.computedAt, "isoTime") : null;
  const weightTotal = analysis.topics.reduce((sum, topic) => sum + Math.max(0, topic.weight), 0);
  return (
    <div className="db-analysis">
      {analysis.topics.length > 0 && (
        <div className="db-analysis-block">
          <span className="label-medium muted">よく話す話題</span>
          <span className="chips">
            {analysis.topics.map((topic) => (
              <Chip key={topic.label}>
                {topic.label}
                {weightTotal > 0 && <span className="mono muted">{formatPercent(Math.max(0, topic.weight) / weightTotal)}</span>}
              </Chip>
            ))}
          </span>
        </div>
      )}
      {analysis.bigFive.length > 0 && (
        <div className="db-analysis-block">
          <span className="label-medium muted">ビッグファイブ（0〜100）</span>
          <ul className="db-big-five">
            {analysis.bigFive.map((trait) => (
              <li key={trait.key}>
                <span className="body-medium">{trait.label}</span>
                <Meter value={trait.value} label={trait.label} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {analysis.personality && <p className="db-text">{analysis.personality}</p>}
      <span className="body-small muted">
        {computed !== null ? `${formatFullDateTime(computed)} に解析` : "解析した日時は不明"}
        {analysis.sampleCount !== null ? `（発言 ${analysis.sampleCount} 件から）` : ""}
      </span>
    </div>
  );
}
