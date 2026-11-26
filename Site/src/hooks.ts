import { useCallback, useEffect, useRef, useState } from "react";

export type Theme = "light" | "dark";

const THEME_STORAGE_KEY = "site-theme";

function readInitialTheme(): Theme {
  // index.html の先頭のスクリプトが、最初の描画より前に data-theme を決めている
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** 配色の切り替え。選んだ配色はこの端末にだけ覚える（外部へは送らない） */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(readInitialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const next: Theme = current === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(THEME_STORAGE_KEY, next);
      } catch {
        // プライベートウィンドウなどで保存できなくても、表示の切り替えは続ける
      }
      return next;
    });
  }, []);

  return [theme, toggle];
}

/** 端末の「視差効果を減らす」設定に従う */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduced(query.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  return reduced;
}

/** ページを少しでも下へ送ったかどうか（ヘッダーに影を付ける合図） */
export function useScrolled(threshold = 8): boolean {
  const [scrolled, setScrolled] = useState(() => window.scrollY > threshold);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > threshold);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [threshold]);

  return scrolled;
}

/** 要素が画面に入ったかどうか。once が真なら、一度入ったあとは真のままにする */
export function useInView<T extends Element>(options: { once: boolean; threshold?: number }) {
  const ref = useRef<T | null>(null);
  // IntersectionObserver が無い環境では、最初から見えている扱いにして内容を隠さない
  const [inView, setInView] = useState(() => typeof IntersectionObserver === "undefined");
  const { once, threshold = 0.15 } = options;

  useEffect(() => {
    const element = ref.current;
    if (!element || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setInView(true);
            if (once) observer.disconnect();
          } else if (!once) {
            setInView(false);
          }
        }
      },
      { threshold },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [once, threshold]);

  return [ref, inView] as const;
}

/**
 * 読み込んだ時点の URL の #… の位置へ移動する。
 * 本文は JavaScript で描画するので、ブラウザが最初に探す時点ではまだ行き先が無い（他のページや README から
 * ./operations.html#data-migration のように直接開かれたとき、ページの先頭に留まってしまう）。描画の後に探し直す。
 */
export function useScrollToHash(): void {
  useEffect(() => {
    const id = decodeURIComponent(window.location.hash.slice(1));
    if (!id) return;
    const target = document.getElementById(id);
    // 読み込み直後なので、なめらかに動かさずにすぐ移す（scroll-behavior: smooth を上書きする）
    target?.scrollIntoView({ block: "start", behavior: "instant" });
  }, []);
}
