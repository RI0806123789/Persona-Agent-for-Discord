import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { REDUCED_MOTION_QUERY, prefersReducedMotion } from "./motionPreference";

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
    if (width <= 0 || height <= 0) {
      // まだ大きさが決まっていない（非表示など）ダイアログは、割り算が Infinity / NaN になるので動かさない
      delete dialog.dataset.emerge;
      return;
    }
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
