// 状態の表示（チップ・統計・エラー・案内・空の状態）。見た目は styles/components.css。
// エラーと案内の文は伏せ字モードで、分かっている秘密の文字列を隠す（redactNode）。
import { Children, cloneElement, isValidElement, type ReactNode } from "react";
import { usePrivacy } from "../../privacy";
import type { Tone } from "../../status";
import { Icon, type IconName } from "../Icon";

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
