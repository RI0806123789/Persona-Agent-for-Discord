import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { logStreamUrl } from "./api";
import { appendLogEntries } from "./logs";
import { createLogPrivacyTracker } from "./logPrivacy";
import { REDUCED_MOTION_QUERY, prefersReducedMotion } from "./motionPreference";
import type { LogEntry } from "./types";

/** enabled の間、callback を intervalMs ごとに呼ぶ（前の呼び出しが終わってから次を予約する）。 */
export function usePolling(callback: () => Promise<void> | void, intervalMs: number, enabled: boolean, resetKey = "") {
  const saved = useRef(callback);
  useEffect(() => {
    saved.current = callback;
  }, [callback]);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      try {
        await saved.current();
      } finally {
        if (!cancelled) timer = window.setTimeout(tick, intervalMs);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [intervalMs, enabled, resetKey]);
}

/** 「◯秒前」の表示を更新するための現在時刻。 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** localStorage に覚えておく設定。使えない環境（プライベートウィンドウ等）では普通の state として動く。 */
export function useStoredState<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // 保存できなくても動作は続ける
    }
  }, [key, value]);
  return [value, setValue] as const;
}

/** CSS のメディアクエリ（OS のダークモードなど）の現在値。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const onChange = () => setMatches(media.matches);
    media.addEventListener("change", onChange);
    onChange();
    return () => media.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** OS の「視差効果を減らす / アニメーションを減らす」設定。途中で切り替えても追従する。 */
export function useReducedMotion(): boolean {
  return useMediaQuery(REDUCED_MOTION_QUERY);
}

// 直前に「ボタンを押した」時刻（クリック、または Enter / Space）。ショートカットのキー（? など）で開いたときに、
// たまたまフォーカスがあった別のボタンから広がらないよう、押した直後に開いたときだけきっかけとして扱う
const ACTIVATION_WINDOW_MS = 800;
let lastActivation = -Infinity;
if (typeof window !== "undefined") {
  window.addEventListener("pointerdown", () => (lastActivation = performance.now()), true);
  window.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Enter" || event.key === " ") lastActivation = performance.now();
    },
    true,
  );
}

/** motion.ts が書き込んだばねのトークン（所要時間と linear()）を、Web Animations API で使える形で読む。 */
function springTiming(kind: "spatial" | "effects", speed: "fast" | "default" | "slow"): { duration: number; easing: string } {
  const style = getComputedStyle(document.documentElement);
  const duration = parseFloat(style.getPropertyValue(`--spring-${kind}-${speed}-duration`));
  const easing = style.getPropertyValue(`--spring-${kind}-${speed}-easing`).trim();
  return { duration: Number.isFinite(duration) ? duration : 300, easing: easing || "ease-out" };
}

/**
 * 閉じる動きのあいだ中身を残しておくための「表示中かどうか」。
 * open が false になっても、onExited が呼ばれる（閉じる動きが終わる）までは present のまま。
 * 動きが走らなかったときのため、最長 fallbackMs で外す。
 */
export function usePresence(open: boolean, fallbackMs = 1200): { present: boolean; onExited: () => void } {
  const [present, setPresent] = useState(open);
  useEffect(() => {
    if (open) {
      setPresent(true);
      return;
    }
    const timer = window.setTimeout(() => setPresent(false), fallbackMs);
    return () => window.clearTimeout(timer);
  }, [open, fallbackMs]);
  const onExited = useCallback(() => {
    if (!open) setPresent(false);
  }, [open]);
  return { present: open || present, onExited };
}

/**
 * ダイアログを、開くきっかけになったボタンの位置から広げ、閉じるときはそのボタンへ縮めて戻す
 * （macOS の Dock から窓が開くような、跳ねずに「ぬるっと」伸びる動き）。
 * キーボードのショートカットで開いたときなど、きっかけの要素が無ければ CSS の既定の動きのまま。
 *
 * ★ CSS の transition は Web Animations より優先されるので、この動きの間は data-emerge で
 *   ダイアログの transition を止める（styles.css の .m3-dialog[data-emerge]）。
 *   止める印は、変化を描く前（useLayoutEffect）に付けること。後から付けると、CSS の transition が先に走る。
 */
export function useEmergeFrom(open: boolean, dialogRef: RefObject<HTMLElement | null>) {
  const originRef = useRef<HTMLElement | null>(null);
  const mounted = useRef(false);
  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    if (!mounted.current) {
      mounted.current = true;
      if (!open) return; // 最初から閉じている状態は、動かさない
    }
    if (!dialog) return;
    if (open) {
      const active = document.activeElement;
      const pressed = performance.now() - lastActivation < ACTIVATION_WINDOW_MS;
      originRef.current =
        pressed && active instanceof HTMLElement && active !== document.body && !dialog.contains(active) ? active : null;
    }
    const origin = originRef.current;
    if (!origin || !origin.isConnected || prefersReducedMotion() || typeof dialog.animate !== "function") {
      delete dialog.dataset.emerge;
      return;
    }
    dialog.dataset.emerge = "";
    for (const animation of dialog.getAnimations()) {
      if (animation.id === "emerge") animation.cancel();
    }
    // ダイアログの本来の位置と大きさ（transform を掛ける前）。画面の真ん中に置かれている
    const parent = (dialog.offsetParent as HTMLElement | null)?.getBoundingClientRect();
    const width = dialog.offsetWidth;
    const height = dialog.offsetHeight;
    const centerX = (parent?.left ?? 0) + dialog.offsetLeft + width / 2;
    const centerY = (parent?.top ?? 0) + dialog.offsetTop + height / 2;
    const from = origin.getBoundingClientRect();
    const scale = Math.min(0.6, Math.max(0.08, Math.min(from.width / width, from.height / height)));
    const folded = `translate(${from.left + from.width / 2 - centerX}px, ${from.top + from.height / 2 - centerY}px) scale(${scale})`;
    // 開くときはゆっくり伸び、閉じるときは少し速く縮む。透明度は途中までに済ませ、形の動きを見せる
    const keyframes = open
      ? [{ transform: folded, opacity: 0 }, { opacity: 1, offset: 0.35 }, { transform: "none", opacity: 1 }]
      : [{ transform: "none", opacity: 1 }, { opacity: 1, offset: 0.55 }, { transform: folded, opacity: 0 }];
    const animation = dialog.animate(keyframes, { ...springTiming("spatial", open ? "slow" : "default"), id: "emerge" });
    animation.finished.then(
      () => {
        if (dialog.getAnimations().every((item) => item.id !== "emerge" || item === animation)) delete dialog.dataset.emerge;
      },
      () => undefined,
    );
  }, [open, dialogRef]);
}

const FOCUSABLE =
  'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/**
 * モーダル（ダイアログ・モーダルのレール）のフォーカス管理。
 * 開いたら中へフォーカスを移し、Tab / Shift+Tab を中で循環させ、閉じたら開く前の場所へ戻す。
 */
export function useModalFocus(open: boolean, containerRef: RefObject<HTMLElement | null>, initialFocusRef?: RefObject<HTMLElement | null>) {
  const latestInitialFocusRef = useRef(initialFocusRef);
  latestInitialFocusRef.current = initialFocusRef;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    const container = containerRef.current;
    // 開く動きが始まってからフォーカスする（スクロール位置が飛ばないよう preventScroll）
    const timer = window.setTimeout(() => {
      const target = latestInitialFocusRef.current?.current ?? container?.querySelector<HTMLElement>(FOCUSABLE);
      target?.focus({ preventScroll: true });
    }, 60);

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !container) return;
      const items = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE)).filter((item) => item.offsetParent !== null);
      if (items.length === 0) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !container.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !container.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", onKeyDown);
      if (previous && document.contains(previous)) previous.focus({ preventScroll: true });
    };
  }, [open, containerRef]);
}

const FLUSH_INTERVAL_MS = 120;

export type StreamStatus = "idle" | "connecting" | "live" | "ended" | "error";

export interface LogStream {
  entries: LogEntry[];
  status: StreamStatus;
  message: string | null;
  messageSafe: string | null;
  paused: boolean;
  pendingCount: number;
  setPaused: (paused: boolean) => void;
  clear: () => void;
  retry: () => void;
}

type StreamMessage =
  | { type: "entry"; entry: LogEntry }
  | { type: "error"; message: string; message_safe?: string | null }
  | { type: "end"; message: string; message_safe?: string | null };

/**
 * journalctl -f をバックエンド経由（WebSocket）で受け取り続ける。
 * 1件ごとに再描画すると重いので、FLUSH_INTERVAL_MS ごとにまとめて反映する。
 */
export function useLogStream(enabled: boolean, initialLines = 300, connectionKey = ""): LogStream {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [messageSafe, setMessageSafe] = useState<string | null>(null);
  const [paused, setPausedState] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [reconnectToken, setReconnectToken] = useState(0);
  const bufferRef = useRef<LogEntry[]>([]); // 次の反映で追加する分
  const heldRef = useRef<LogEntry[]>([]); // 一時停止中にたまった分
  const heldCountRef = useRef(0); // 古い行を落としても、新着件数は数え続ける
  const pausedRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return;
    }
    let disposed = false;
    let terminalMessage = false;
    const classifyPrivacy = createLogPrivacyTracker();
    bufferRef.current = [];
    heldRef.current = [];
    heldCountRef.current = 0;
    pausedRef.current = false;
    setPausedState(false);
    setEntries([]);
    setPendingCount(0);
    setStatus("connecting");
    setMessage(null);
    setMessageSafe(null);

    const fail = (reason: string) => {
      if (disposed || terminalMessage) return;
      terminalMessage = true;
      setStatus("error");
      setMessage(reason);
      setMessageSafe(null);
    };
    let socket: WebSocket;
    try {
      socket = new WebSocket(logStreamUrl(initialLines));
    } catch {
      fail("ログに接続できませんでした。接続先の設定を確認してから再試行してください。");
      return () => { disposed = true; };
    }
    socket.onopen = () => {
      if (!disposed && !terminalMessage) setStatus("live");
    };
    socket.onmessage = (event: MessageEvent<string>) => {
      if (disposed || terminalMessage) return;
      let data: StreamMessage;
      try {
        data = JSON.parse(event.data) as StreamMessage;
        if (!data || !["entry", "error", "end"].includes(data.type)) throw new Error();
        if (data.type === "entry") {
          const entry = data.entry;
          if (!entry || !Number.isFinite(entry.ts) || typeof entry.message !== "string"
              || typeof entry.source !== "string"
              || (entry.priority != null && !Number.isFinite(entry.priority))
              || (entry.pid != null && !Number.isFinite(entry.pid))) throw new Error();
        } else if (typeof data.message !== "string") throw new Error();
      } catch {
        fail("ログの応答を読み取れませんでした。再試行してください。");
        socket.close();
        return;
      }
      if (data.type === "entry") {
        // サーバーからの同名属性は使わず、保持上限・pause・clearより前に判定する。
        const entry = classifyPrivacy(data.entry);
        if (pausedRef.current) {
          heldRef.current = appendLogEntries(heldRef.current, [entry]);
          heldCountRef.current += 1;
        } else {
          bufferRef.current = appendLogEntries(bufferRef.current, [entry]);
        }
      } else {
        terminalMessage = true;
        setStatus(data.type === "error" ? "error" : "ended");
        setMessage(data.message);
        setMessageSafe(typeof data.message_safe === "string" ? data.message_safe : null);
      }
    };
    socket.onerror = () => {
      fail("ログの通信に失敗しました。接続元やサーバーの状態を確認してから再試行してください。");
    };
    socket.onclose = (event: CloseEvent) => {
      if (disposed || terminalMessage) return;
      if (event.code !== 1000) {
        fail(`ログの接続が切れました（コード ${event.code}）。接続元やサーバーの状態を確認してから再試行してください。`);
      } else {
        terminalMessage = true;
        setStatus("ended");
        setMessage("ログの受信が終了しました。再開するには再試行してください。");
        setMessageSafe(null);
      }
    };

    const flushTimer = window.setInterval(() => {
      if (bufferRef.current.length > 0) {
        const batch = bufferRef.current;
        bufferRef.current = [];
        setEntries((previous) => appendLogEntries(previous, batch));
      }
      if (pausedRef.current) setPendingCount(heldCountRef.current);
    }, FLUSH_INTERVAL_MS);

    return () => {
      disposed = true;
      window.clearInterval(flushTimer);
      const batch = bufferRef.current;
      bufferRef.current = [];
      if (batch.length > 0) setEntries((previous) => appendLogEntries(previous, batch));
      socket.close();
    };
  }, [enabled, initialLines, reconnectToken, connectionKey]);

  const setPaused = useCallback((next: boolean) => {
    if (next && !pausedRef.current) {
      heldRef.current = appendLogEntries(heldRef.current, bufferRef.current);
      heldCountRef.current += bufferRef.current.length;
      bufferRef.current = [];
    }
    pausedRef.current = next;
    setPausedState(next);
    if (!next) {
      const batch = appendLogEntries(bufferRef.current, heldRef.current);
      bufferRef.current = [];
      if (batch.length > 0) setEntries((previous) => appendLogEntries(previous, batch));
      heldRef.current = [];
      heldCountRef.current = 0;
      setPendingCount(0);
    }
  }, []);

  const clear = useCallback(() => {
    bufferRef.current = [];
    heldRef.current = [];
    heldCountRef.current = 0;
    setPendingCount(0);
    setEntries([]);
  }, []);

  const retry = useCallback(() => setReconnectToken((n) => n + 1), []);

  return { entries, status, message, messageSafe, paused, pendingCount, setPaused, clear, retry };
}
