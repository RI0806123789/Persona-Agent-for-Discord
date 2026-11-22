// 小さな折れ線（直近5分の推移）。見た目は styles/components.css の .sparkline。
import { useState, type PointerEvent } from "react";
import { formatClock } from "../../format";

// ---------------------------------------------------------------- スパークライン（直近5分の推移）

export interface SparkPoint {
  t: number;
  v: number | null;
}

/** 小さな折れ線。ホバーした位置の値と時刻を出す。min を省略すると値の範囲に合わせて縦軸を決める。 */
export function Sparkline({
  points,
  format,
  label,
  height = 44,
  min,
  max,
}: {
  points: SparkPoint[];
  format: (value: number) => string;
  label: string;
  height?: number;
  min?: number;
  max?: number;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const valid = points.filter((p): p is { t: number; v: number } => p.v !== null && Number.isFinite(p.v));
  if (valid.length < 2) {
    return (
      <div className="sparkline-empty body-small" style={{ height }}>
        データを集めています…
      </div>
    );
  }

  const width = 240;
  const values = valid.map((p) => p.v);
  const highest = Math.max(...values);
  const lowest = Math.min(...values);
  const padding = (highest - lowest) * 0.25 || Math.abs(highest) * 0.05 || 1;
  const bottom = min ?? lowest - padding;
  const top = max ?? (min === undefined ? highest + padding : highest * 1.2);
  const span = top - bottom || 1;
  const step = width / (valid.length - 1);
  const coords = valid.map((p, i) => ({ x: i * step, y: height - 3 - ((p.v - bottom) / span) * (height - 8) }));
  const line = coords.map((c, i) => `${i ? "L" : "M"}${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(" ");
  const area = `${line} L${width} ${height} L0 ${height} Z`;

  const onMove = (event: PointerEvent<SVGSVGElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!(rect.width > 0)) return; // 描画幅 0 だと NaN になる（#494）
    const ratio = (event.clientX - rect.left) / rect.width;
    setHover(Math.max(0, Math.min(valid.length - 1, Math.round(ratio * (valid.length - 1)))));
  };
  // 点が減った後に古い添字が残っても落ちないよう、座標と点の両方がある場合だけ使う
  const hoverCoord = hover === null ? undefined : coords[hover];
  const hoverPoint = hover === null ? undefined : valid[hover];
  const active = hoverCoord && hoverPoint ? { ...hoverCoord, point: hoverPoint } : null;
  const leftPercent = active ? (active.x / width) * 100 : 0;

  return (
    <div className="sparkline">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        style={{ height }}
        role="img"
        aria-label={`${label}の推移（最新 ${format(valid[valid.length - 1].v)}）`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <path className="sparkline-area" d={area} />
        <path className="sparkline-line" d={line} vectorEffect="non-scaling-stroke" />
        {active && <line className="sparkline-cursor" x1={active.x} x2={active.x} y1={0} y2={height} vectorEffect="non-scaling-stroke" />}
      </svg>
      {active && (
        <>
          <span className="sparkline-dot" style={{ left: `${leftPercent}%`, top: active.y }} />
          <div className={`m3-tooltip sparkline-tip ${leftPercent > 70 ? "is-left" : ""}`} style={{ left: `${leftPercent}%` }}>
            <strong>{format(active.point.v)}</strong>
            <span>{formatClock(active.point.t)}</span>
          </div>
        </>
      )}
    </div>
  );
}
