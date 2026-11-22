// M3 のメニュー（分割ボタン・接続先の選択・表示の設定で共有する）。
// キーボードだけで操作できるようにする:
//   開く: ボタンで Enter / Space / ↓（↑ なら最後の項目から）
//   移動: ↓ ↑ / j k / Home End、選ぶ: Enter / Space、閉じる: Esc（ボタンへフォーカスを戻す）/ Tab / 外側のクリック
import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode, type RefObject } from "react";

const ITEM_SELECTOR = '[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled)';

export interface MenuController {
  open: boolean;
  menuId: string;
  anchorRef: RefObject<HTMLDivElement | null>;
  triggerRef: RefObject<HTMLButtonElement | null>;
  menuRef: RefObject<HTMLDivElement | null>;
  openMenu: (focus?: "first" | "last" | "selected") => void;
  close: (restoreFocus?: boolean) => void;
  toggle: () => void;
  /** 開くボタンに付ける属性 */
  triggerProps: {
    "aria-haspopup": "menu";
    "aria-expanded": boolean;
    "aria-controls": string;
    onKeyDown: (event: KeyboardEvent<HTMLButtonElement>) => void;
  };
  /** メニュー本体に付ける属性 */
  menuProps: {
    id: string;
    role: "menu";
    inert: boolean;
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  };
}

export function useMenu(): MenuController {
  const [open, setOpen] = useState(false);
  const pendingFocus = useRef<"first" | "last" | "selected">("selected");
  const anchorRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  const items = useCallback(() => Array.from(menuRef.current?.querySelectorAll<HTMLElement>(ITEM_SELECTOR) ?? []), []);

  const close = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const openMenu = useCallback((focus: "first" | "last" | "selected" = "selected") => {
    pendingFocus.current = focus;
    setOpen(true);
  }, []);

  const toggle = useCallback(() => {
    setOpen((value) => {
      if (!value) pendingFocus.current = "selected";
      return !value;
    });
  }, []);

  // 開いたら、選択中（無ければ先頭）の項目へフォーカスを移す
  useEffect(() => {
    if (!open) return;
    const list = items();
    const target =
      pendingFocus.current === "last"
        ? list[list.length - 1]
        : pendingFocus.current === "selected"
          ? (list.find((item) => item.getAttribute("aria-checked") === "true") ?? list[0])
          : list[0];
    target?.focus({ preventScroll: true });

    const onPointerDown = (event: PointerEvent) => {
      if (!anchorRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("pointerdown", onPointerDown);
    return () => window.removeEventListener("pointerdown", onPointerDown);
  }, [open, items]);

  const onTriggerKeyDown = useCallback(
    (event: KeyboardEvent<HTMLButtonElement>) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        openMenu(event.key === "ArrowDown" ? "first" : "last");
      }
    },
    [openMenu],
  );

  const onMenuKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const list = items();
      const index = list.indexOf(document.activeElement as HTMLElement);
      const move = (next: number) => {
        event.preventDefault();
        if (list.length === 0) return; // 有効な項目が無いと % 0 で NaN になる（#494）
        list[(next + list.length) % list.length]?.focus({ preventScroll: true });
      };
      switch (event.key) {
        case "ArrowDown":
        case "j":
          move(index + 1);
          break;
        case "ArrowUp":
        case "k":
          // 項目にフォーカスが無い（index が -1）ときは末尾へ。そのまま引くと下から2番目へ飛ぶ（#494）
          move(index < 0 ? list.length - 1 : index - 1);
          break;
        case "Home":
          move(0);
          break;
        case "End":
          move(list.length - 1);
          break;
        case "Escape":
          event.preventDefault();
          close(true);
          break;
        case "Tab":
          setOpen(false);
          return; // フォーカスの移動はブラウザに任せる
        default:
          break;
      }
      // メニューの中で押したキーは、画面全体のショートカット（t や r など）に渡さない
      event.stopPropagation();
    },
    [items, close],
  );

  return {
    open,
    menuId,
    anchorRef,
    triggerRef,
    menuRef,
    openMenu,
    close,
    toggle,
    triggerProps: {
      "aria-haspopup": "menu",
      "aria-expanded": open,
      "aria-controls": menuId,
      onKeyDown: onTriggerKeyDown,
    },
    menuProps: { id: menuId, role: "menu", inert: !open, onKeyDown: onMenuKeyDown },
  };
}

/** メニューの見た目（開閉の動きは styles.css の .m3-menu）。 */
export function MenuSurface({
  controller,
  label,
  className = "",
  align = "end",
  children,
}: {
  controller: MenuController;
  label: string;
  className?: string;
  /** start = 左端をそろえる / end = 右端をそろえる */
  align?: "start" | "end";
  children: ReactNode;
}) {
  return (
    <div
      ref={controller.menuRef}
      {...controller.menuProps}
      aria-label={label}
      className={`m3-menu align-${align} ${controller.open ? "is-open" : ""} ${className}`}
    >
      {children}
    </div>
  );
}

export function MenuHeading({ children }: { children: ReactNode }) {
  return (
    <span className="label-medium m3-menu-heading" role="presentation">
      {children}
    </span>
  );
}

export function MenuDivider() {
  return <hr className="m3-menu-divider" role="separator" />;
}
