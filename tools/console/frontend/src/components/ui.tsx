// Material 3 Expressive の基本部品。見た目は styles.css の .m3-* クラスが担う。
import { Children, cloneElement, isValidElement, useEffect, useState, type ButtonHTMLAttributes, type PointerEvent, type ReactNode, type Ref } from "react";
import { formatClock } from "../format";
import { usePrivacy } from "../privacy";
import type { Tone } from "../status";
import { Icon, type IconName } from "./Icon";
import { MenuSurface, useMenu } from "./menu";

// ---------------------------------------------------------------- カード

export function Card({
  title,
  icon,
  source,
  actions,
  children,
  className = "",
  variant = "filled",
  id,
}: {
  title?: string;
  icon?: IconName;
  source?: "ssh";
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** filled = 色の面で区切る / outlined = 枠線で区切る / elevated = 影で浮かせる */
  variant?: "filled" | "outlined" | "elevated";
  /** ページ内のリンクやスクロールの行き先にするとき */
  id?: string;
}) {
  return (
    <section id={id} className={`m3-card m3-card-${variant} ${className}`}>
      {(title || actions) && (
        <header className="m3-card-head">
          {icon && (
            <span className="m3-card-icon">
              <Icon name={icon} size={20} />
            </span>
          )}
          {title && <h2 className="title-medium">{title}</h2>}
          {source && <SourceBadge />}
          {actions && <div className="m3-card-actions">{actions}</div>}
        </header>
      )}
      <div className="m3-card-body">{children}</div>
    </section>
  );
}

/** SSH でサーバーから取った実データであることを示す。 */
export function SourceBadge() {
  return (
    <span className="m3-source" title="SSH でサーバーから取得した実データ">
      <Icon name="plug" size={14} />
      SSH
    </span>
  );
}

// ---------------------------------------------------------------- ボタン

type ButtonVariant = "filled" | "tonal" | "outlined" | "text" | "elevated" | "danger";

export function Button({
  variant = "filled",
  size = "s",
  icon,
  busy = false,
  children,
  className = "",
  ref,
  ...rest
}: {
  /** React 19 では関数コンポーネントも ref を普通の引数として受け取れる */
  ref?: Ref<HTMLButtonElement>;
  variant?: ButtonVariant;
  /** M3 Expressive のボタンの大きさ（xs 32 / s 40 / m 56 / l 96） */
  size?: "xs" | "s" | "m" | "l";
  icon?: IconName;
  busy?: boolean;
  children?: ReactNode;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button ref={ref} type="button" className={`m3-btn m3-btn-${variant} m3-btn-${size} ${className}`} {...rest}>
      {busy ? <span className="m3-btn-spinner" aria-hidden="true" /> : icon && <Icon name={icon} size={size === "m" ? 24 : size === "l" ? 32 : 20} />}
      {children && <span className="m3-btn-label">{children}</span>}
    </button>
  );
}

export function IconButton({
  icon,
  label,
  variant = "standard",
  selected,
  size = "s",
  className = "",
  ref,
  ...rest
}: {
  ref?: Ref<HTMLButtonElement>;
  icon: IconName;
  label: string;
  variant?: "standard" | "filled" | "tonal" | "outlined";
  /** 切り替え式のときだけ渡す。選ぶと丸から角丸の四角へ形が変わる（M3 Expressive） */
  selected?: boolean;
  size?: "xs" | "s" | "m";
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  const toggle = selected !== undefined;
  return (
    <button
      ref={ref}
      type="button"
      className={`m3-icon-btn m3-icon-btn-${variant} m3-icon-btn-${size} ${toggle ? "is-toggle" : ""} ${selected ? "is-selected" : ""} ${className}`}
      aria-label={label}
      aria-pressed={toggle ? selected : undefined}
      title={label}
      {...rest}
    >
      <Icon name={icon} size={size === "m" ? 24 : size === "xs" ? 18 : 22} filled={!!selected} />
    </button>
  );
}

// ---------------------------------------------------------------- 分割ボタン（主な操作 + メニュー）

export interface MenuItem {
  label: string;
  icon: IconName;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
}

export function SplitButton({
  label,
  icon,
  onClick,
  items,
  disabled = false,
  busy = false,
}: {
  label: string;
  icon: IconName;
  onClick: () => void;
  items: MenuItem[];
  disabled?: boolean;
  busy?: boolean;
}) {
  const menu = useMenu();
  const unavailable = disabled || busy;
  // 操作中や状態が変わってボタンが使えなくなったら、開いていたメニューも閉じる
  useEffect(() => {
    if (unavailable) menu.close();
  }, [unavailable, menu.close]);

  return (
    <div className="m3-split" ref={menu.anchorRef}>
      <button type="button" className="m3-split-leading" onClick={onClick} disabled={unavailable}>
        {busy ? <span className="m3-btn-spinner" aria-hidden="true" /> : <Icon name={icon} size={20} />}
        <span>{label}</span>
      </button>
      <button
        ref={menu.triggerRef}
        type="button"
        className={`m3-split-trailing ${menu.open ? "is-open" : ""}`}
        onClick={menu.toggle}
        disabled={unavailable}
        aria-label="ほかの操作"
        {...menu.triggerProps}
      >
        <Icon name="expand" size={22} />
      </button>
      <MenuSurface controller={menu} label="ほかの操作" align="start">
        {items.map((item) => (
          <button
            key={item.label}
            type="button"
            role="menuitem"
            tabIndex={-1}
            className={`m3-menu-item ${item.danger ? "is-danger" : ""}`}
            disabled={item.disabled}
            onClick={() => {
              menu.close(true);
              item.onSelect();
            }}
          >
            <Icon name={item.icon} size={20} />
            {item.label}
          </button>
        ))}
      </MenuSurface>
    </div>
  );
}

// ---------------------------------------------------------------- つながったボタングループ（単一選択）

export interface GroupOption<T extends string> {
  value: T;
  label: string;
  icon?: IconName;
  count?: number;
  dot?: string;
}

export function ConnectedButtonGroup<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: GroupOption<T>[];
  value: T;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="m3-connected-group" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          role="radio"
          aria-checked={value === option.value}
          className={value === option.value ? "is-selected" : ""}
          onClick={() => onChange(option.value)}
        >
          {value === option.value ? (
            <Icon name="check" size={18} />
          ) : option.icon ? (
            <Icon name={option.icon} size={18} />
          ) : (
            option.dot && <span className={`m3-group-dot ${option.dot}`} aria-hidden="true" />
          )}
          {option.label}
          {option.count !== undefined && <span className="m3-group-count">{option.count}</span>}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- スイッチ

export function Switch({
  checked,
  onChange,
  label,
  title,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  title?: string;
}) {
  return (
    <label className="m3-switch-wrap" title={title}>
      <input
        type="checkbox"
        role="switch"
        className="m3-switch-input"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="m3-switch" aria-hidden="true">
        <span className="m3-switch-handle">
          <Icon name={checked ? "check" : "close"} size={16} />
        </span>
      </span>
      <span className="label-large">{label}</span>
    </label>
  );
}

// ---------------------------------------------------------------- 状態の表示

/** 状態を色と文字で示す（色だけで意味を伝えない）。 */
export function StatusChip({ tone, label, pulse = false, icon }: { tone: Tone; label: string; pulse?: boolean; icon?: IconName }) {
  return (
    <span className={`m3-status tone-${tone}`}>
      {icon ? <Icon name={icon} size={16} filled /> : <span className={`m3-status-dot ${pulse ? "is-pulsing" : ""}`} aria-hidden="true" />}
      {label}
    </span>
  );
}

export function Chip({ icon, children, title }: { icon?: IconName; children: ReactNode; title?: string }) {
  return (
    <span className="m3-chip" title={title}>
      {icon && <Icon name={icon} size={18} />}
      {children}
    </span>
  );
}

export function Stat({ label, value, hint, mono = false }: { label: string; value: ReactNode; hint?: ReactNode; mono?: boolean }) {
  return (
    <div className="m3-stat">
      <span className="label-medium m3-stat-label">{label}</span>
      <span className={`title-large m3-stat-value ${mono ? "mono" : ""}`}>{value}</span>
      {hint && <span className="body-small m3-stat-hint">{hint}</span>}
    </div>
  );
}

/**
 * 文字列の部分を伏せ字モードで伏せる（エラー文の中のホスト名などを隠すため）。
 * 配列と、`<code>` のような子要素の中の文字列にも再帰して掛ける。`Children.map` は
 * 子に key を付け直すので、配列を返しても key の警告が出ない。
 */
function redactNode(node: ReactNode, redact: (text: string) => string): ReactNode {
  if (typeof node === "string") return redact(node);
  if (Array.isArray(node)) return Children.map(node, (child) => redactNode(child, redact));
  if (isValidElement<{ children?: ReactNode }>(node) && node.props.children != null) {
    return cloneElement(node, undefined, redactNode(node.props.children, redact));
  }
  return node;
}

/** エラーの表示。サーバーから届いた文をそのまま出すので、伏せ字モードでは分かっている秘密の文字列を隠す。 */
export function InlineError({ children }: { children: ReactNode }) {
  const privacy = usePrivacy();
  return (
    <p className="m3-inline-error body-medium" role="alert">
      <Icon name="error" size={20} filled />
      <span>{redactNode(children, privacy.redact)}</span>
    </p>
  );
}

export function Notice({ icon = "info", children }: { icon?: IconName; children: ReactNode }) {
  const privacy = usePrivacy();
  return (
    <p className="m3-notice body-small">
      <Icon name={icon} size={18} />
      <span>{redactNode(children, privacy.redact)}</span>
    </p>
  );
}

export function EmptyState({ icon, title, children }: { icon: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="m3-empty">
      <span className="m3-empty-icon">
        <Icon name={icon} size={32} />
      </span>
      <h3 className="title-large">{title}</h3>
      {children}
    </div>
  );
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="m3-kbd">{children}</kbd>;
}

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
    const ratio = (event.clientX - rect.left) / rect.width;
    setHover(Math.max(0, Math.min(valid.length - 1, Math.round(ratio * (valid.length - 1)))));
  };
  const active = hover === null ? null : { ...coords[hover], point: valid[hover] };
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
