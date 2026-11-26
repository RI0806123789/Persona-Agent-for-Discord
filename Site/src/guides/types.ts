// 説明ページ（ガイド。Issue #528）の文言の型。文言そのものは同じフォルダの setup.ts などに置く。
//
// 文中（text・items・表のセル）で使える書式は次の 3 つだけ（Markdown のほかの書式は解釈しない）:
//   `コード`   **強調**   [リンクの文字](行き先)
// ★ リンクの行き先は、サイト内（./setup.html#xxx・#xxx）かこのリポジトリの中だけ。外部のサイトの URL は
//   `https://…` のようにコードとして文字で書き、リンクにしない（Issue #528 の決定。tests/guides.test.mjs が検出する）。
// ★ このフォルダの文言のファイルは Node.js の標準テストからそのまま読み込む。値の import を足さないこと
//   （型だけの import は実行時に消えるのでよい。content.ts と同じ規則）。

export type GuideId = "setup" | "persona" | "usage" | "operations";

/** コードの例の種類。見出しの札とコメントの色分けに使う */
export type CodeLanguage = "powershell" | "bash" | "json" | "text";

export type GuideBlock =
  /** 段落 */
  | { kind: "paragraph"; text: string }
  /** 箇条書き（ordered で番号付き） */
  | { kind: "list"; items: readonly string[]; ordered?: boolean }
  /** 節の中の小見出し（目次には出さない） */
  | { kind: "heading"; id: string; text: string }
  /** コマンドや設定の例。title は札に出す名前（無ければ言語名） */
  | { kind: "code"; language: CodeLanguage; title?: string; code: string }
  /** 注意書き */
  | { kind: "note"; tone: "info" | "warning"; title?: string; text: string }
  /** 表 */
  | { kind: "table"; columns: readonly string[]; rows: readonly (readonly string[])[] };

/** ページの節。目次に出し、id はページ内リンクの行き先になる（他のページや README からも指すので、変えるときは参照元も直す） */
export type GuideSection = { id: string; title: string; blocks: readonly GuideBlock[] };

export type Guide = {
  id: GuideId;
  title: string;
  lead: string;
  sections: readonly GuideSection[];
};
