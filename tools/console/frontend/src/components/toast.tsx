// M3 のスナックバー（画面下に短く出る通知）。
// - 操作ボタン（例: 再試行）を1つ置ける
// - マウスを乗せている間・フォーカスがある間は、消えるまでの時間を止める（読み終わる前に消えないように）
// - 通知は aria-live で読み上げ、失敗だけは role="alert" ですぐ読ませる
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { redactError, usePrivacy } from "../privacy";
import { Icon } from "./Icon";

export interface ToastInput {
  tone: "good" | "critical" | "info";
  title: string;
  detail?: string;
  /** 原文と独立した、ローカル設定の場所を含まない診断文。 */
  detailSafe?: string;
  action?: { label: string; onClick: () => void };
}

interface Toast extends ToastInput {
  id: number;
  leaving: boolean;
}

const VISIBLE_MS = 5000;
// 操作ボタンがある通知は、押す時間を見込んで長めに出す
const VISIBLE_WITH_ACTION_MS = 8000;
const LEAVE_MS = 260;
const MAX_VISIBLE = 3;

const ToastContext = createContext<(toast: ToastInput) => void>(() => undefined);
const ToastStateContext = createContext<{ toasts: Toast[]; dismiss: (id: number) => void }>({ toasts: [], dismiss: () => undefined });

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: number) => {
    setToasts((previous) => previous.map((t) => (t.id === id ? { ...t, leaving: true } : t)));
    window.setTimeout(() => setToasts((previous) => previous.filter((t) => t.id !== id)), LEAVE_MS);
  }, []);

  const notify = useCallback((toast: ToastInput) => {
    const id = nextId++;
    setToasts((previous) => [...previous.slice(-(MAX_VISIBLE - 1)), { ...toast, id, leaving: false }]);
  }, []);

  return (
    <ToastContext.Provider value={notify}>
      <ToastStateContext.Provider value={{ toasts, dismiss }}>{children}</ToastStateContext.Provider>
    </ToastContext.Provider>
  );
}

/** 通知の保存先とは分け、現在のPrivacyContextの内側で表示する。 */
export function ToastViewport() {
  const { toasts, dismiss } = useContext(ToastStateContext);
  return (
    <div className="m3-snackbars" aria-live="polite">
      {toasts.map((toast) => (
        <Snackbar key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
      ))}
    </div>
  );
}

export function Snackbar({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const privacy = usePrivacy();
  const detail = redactError(toast.detail, toast.detailSafe, privacy);
  const [held, setHeld] = useState(false);
  const remaining = useRef(toast.action ? VISIBLE_WITH_ACTION_MS : VISIBLE_MS);
  const dismissRef = useRef(onDismiss);
  dismissRef.current = onDismiss;

  // 止めていない間だけ残り時間を減らす
  useEffect(() => {
    if (held || toast.leaving) return;
    const started = Date.now();
    const timer = window.setTimeout(() => dismissRef.current(), remaining.current);
    return () => {
      window.clearTimeout(timer);
      remaining.current = Math.max(800, remaining.current - (Date.now() - started));
    };
  }, [held, toast.leaving]);

  return (
    <div
      className={`m3-snackbar tone-${toast.tone} ${toast.leaving ? "is-leaving" : ""}`}
      role={toast.tone === "critical" ? "alert" : undefined}
      onMouseEnter={() => setHeld(true)}
      onMouseLeave={() => setHeld(false)}
      onFocus={() => setHeld(true)}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setHeld(false);
      }}
    >
      <Icon name={toast.tone === "good" ? "checkCircle" : toast.tone === "critical" ? "error" : "info"} size={20} filled />
      <div className="m3-snackbar-text">
        <strong className="body-medium">{privacy.redact(toast.title)}</strong>
        {detail && <p className="body-small">{detail}</p>}
      </div>
      {toast.action && (
        <button
          type="button"
          className="m3-snackbar-action label-large"
          onClick={() => {
            toast.action?.onClick();
            onDismiss();
          }}
        >
          {privacy.redact(toast.action.label)}
        </button>
      )}
      <button type="button" className="m3-snackbar-close" onClick={onDismiss} aria-label="閉じる">
        <Icon name="close" size={20} />
      </button>
    </div>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
