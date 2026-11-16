/** OS の「視差効果を減らす / アニメーションを減らす」設定を表すメディアクエリ。 */
export const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";

/** いまその設定が有効か（描画の外で一度だけ判断したいときに使う。追従が要るなら hooks の useReducedMotion）。 */
export function prefersReducedMotion(): boolean {
  return window.matchMedia(REDUCED_MOTION_QUERY).matches;
}
