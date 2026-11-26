// 説明ページの文中の書式と、コードの例のコメントを読み分ける（表示に依存しない計算だけを置く）。
// Markdown を描画するライブラリは入れない（Issue #528 の決定）ので、使う書式をここで 3 つに絞っている。
// ★ Node.js の標準テストからそのまま読み込む。値の import を足さないこと。

export type InlineToken =
  | { type: "text"; text: string }
  | { type: "code"; text: string }
  | { type: "strong"; children: InlineToken[] }
  | { type: "link"; href: string; children: InlineToken[] };

// 左から順に、最初に現れた書式を取り出す。同じ位置で始まる場合は、コード → 強調 → リンクの順に優先する
// （コードの中の ** や [ ] を書式として読まないため）。
const INLINE_PATTERN = /`([^`]+)`|\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

/** 文中の `コード`・**強調**・[文字](行き先) を読み分ける。それ以外はそのままの文字として扱う */
export function parseInline(text: string): InlineToken[] {
  const tokens: InlineToken[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE_PATTERN)) {
    const index = match.index ?? 0;
    if (index > last) tokens.push({ type: "text", text: text.slice(last, index) });
    if (match[1] !== undefined) tokens.push({ type: "code", text: match[1] });
    else if (match[2] !== undefined) tokens.push({ type: "strong", children: parseInline(match[2]) });
    else tokens.push({ type: "link", href: match[4], children: parseInline(match[3]) });
    last = index + match[0].length;
  }
  if (last < text.length) tokens.push({ type: "text", text: text.slice(last) });
  return tokens;
}

/** 文中のリンクの行き先をすべて取り出す（テストで、外部へのリンクが無いことを確かめるため） */
export function collectInlineLinks(text: string): string[] {
  const found: string[] = [];
  const walk = (tokens: InlineToken[]) => {
    for (const token of tokens) {
      if (token.type === "link") {
        found.push(token.href);
        walk(token.children);
      } else if (token.type === "strong") {
        walk(token.children);
      }
    }
  };
  walk(parseInline(text));
  return found;
}

/**
 * シェルの 1 行を、コマンドの部分と行末のコメント（# から後ろ）に分ける。
 * 行頭か空白の直後にある # だけをコメントの始まりとみなし、引用符の中の # は数えない。
 */
export function splitShellComment(line: string): [code: string, comment: string] {
  let quote: string | null = null;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (quote) {
      if (character === quote) quote = null;
      continue;
    }
    if (character === '"' || character === "'") {
      quote = character;
      continue;
    }
    if (character === "#" && (index === 0 || /\s/.test(line[index - 1]))) {
      return [line.slice(0, index), line.slice(index)];
    }
  }
  return [line, ""];
}
