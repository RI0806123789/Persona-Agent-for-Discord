// ボタン・アイコンボタン・分割ボタン・つながったボタングループ・スイッチ。見た目は styles/buttons.css。
import { useEffect, type ButtonHTMLAttributes, type ReactNode, type Ref } from "react";
import { Icon, type IconName } from "../Icon";
import { MenuSurface, useMenu } from "../menu";

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
