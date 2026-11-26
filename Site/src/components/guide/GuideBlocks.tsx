import type { GuideBlock } from "../../guides/types";
import { CodeBlock } from "./CodeBlock";
import { InlineText } from "./InlineText";

function Block({ block }: { block: GuideBlock }) {
  switch (block.kind) {
    case "paragraph":
      return (
        <p>
          <InlineText text={block.text} />
        </p>
      );
    case "list": {
      const items = block.items.map((item) => (
        <li key={item}>
          <InlineText text={item} />
        </li>
      ));
      return block.ordered ? <ol className="prose-list prose-list--ordered">{items}</ol> : <ul className="prose-list">{items}</ul>;
    }
    case "heading":
      return (
        <h3 id={block.id} className="prose-heading">
          <InlineText text={block.text} />
        </h3>
      );
    case "code":
      return <CodeBlock code={block.code} language={block.language} title={block.title} />;
    case "note":
      return (
        <aside className={`prose-note prose-note--${block.tone}`}>
          {block.title && (
            <p className="prose-note__title">
              <span aria-hidden>{block.tone === "warning" ? "⚠️ " : "ℹ️ "}</span>
              <InlineText text={block.title} />
            </p>
          )}
          <p>
            <InlineText text={block.text} />
          </p>
        </aside>
      );
    case "table":
      return (
        // 狭い画面では表ごと横にスクロールさせる（列を詰めるとコマンドが読めなくなる）
        <div className="prose-table" tabIndex={0}>
          <table>
            <thead>
              <tr>
                {block.columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {block.rows.map((row) => (
                <tr key={row.join("|")}>
                  {row.map((cell, index) => (
                    <td key={index}>
                      <InlineText text={cell} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
  }
}

/** 説明ページの節の中身を、書いた順に描画する */
export function GuideBlocks({ blocks }: { blocks: readonly GuideBlock[] }) {
  return (
    <>
      {blocks.map((block, index) => (
        <Block key={index} block={block} />
      ))}
    </>
  );
}
