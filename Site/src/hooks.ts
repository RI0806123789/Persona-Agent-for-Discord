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
