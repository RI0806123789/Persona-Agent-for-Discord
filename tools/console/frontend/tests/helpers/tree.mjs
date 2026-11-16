// 仮想 DOM（React 要素の木）の走査（#416）。

/** 木に含まれる全要素を、親→子の順に平らな配列で返す。 */
export const nodes = (node) => Array.isArray(node) ? node.flatMap(nodes)
  : node && typeof node === "object" ? [node, ...nodes(node.props?.children)] : [];
