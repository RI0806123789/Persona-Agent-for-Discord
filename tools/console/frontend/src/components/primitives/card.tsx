// カードと、SSH で取った実データであることを示すバッジ。見た目は styles/components.css の .m3-card。
import type { ReactNode } from "react";
import { Icon, type IconName } from "../Icon";

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
