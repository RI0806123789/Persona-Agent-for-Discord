// 画面全体の切り替え（ページ・テーマ・伏せ字）を、ブラウザのビュー遷移（View Transitions）でなめらかにつなぐ。
// 前の画面の写しを残したまま新しい画面を描き、styles.css の「ビュー遷移」の動きで入れ替える。
// 動きを減らす設定・ビュー遷移に対応していないブラウザ・裏に回ったタブでは、すぐに切り替える（従来どおり）。
import { flushSync } from "react-dom";
import { prefersReducedMotion } from "./motionPreference";

export type ViewTransitionKind = "page" | "theme" | "privacy";

let latest = 0;

export function withViewTransition(kind: ViewTransitionKind, update: () => void): void {
  if (typeof document.startViewTransition !== "function" || prefersReducedMotion() || document.visibilityState !== "visible") {
    update();
    return;
  }
  // どの切り替えかを <html> に書き、CSS がそれに合った動きを選ぶ。
  // 前の遷移の途中で次が始まると前のものは飛ばされるので、印は最後の遷移が終わったときだけ消す
  const root = document.documentElement;
  const id = String(++latest);
  root.dataset.viewTransition = kind;
  root.dataset.viewTransitionId = id;
  // 写しを撮る前に React の描画を終わらせる（新しい画面の状態を確実に写すため）
  const transition = document.startViewTransition(() => flushSync(update));
  // 飛ばされた遷移の ready は失敗になる。画面の切り替え自体は済んでいるので、エラーとして扱わない
  transition.ready.catch(() => undefined);
  transition.finished
    .catch(() => undefined)
    .finally(() => {
      if (root.dataset.viewTransitionId !== id) return;
      delete root.dataset.viewTransition;
      delete root.dataset.viewTransitionId;
    });
}
