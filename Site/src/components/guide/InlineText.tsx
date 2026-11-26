import { Fragment } from "react";
import { parseInline, type InlineToken } from "../../guides/markup";
import { externalProps } from "../SiteChrome";

function renderTokens(tokens: InlineToken[]) {
  return tokens.map((token, index) => {
    switch (token.type) {
      case "code":
        return <code key={index}>{token.text}</code>;
      case "strong":
        return <strong key={index}>{renderTokens(token.children)}</strong>;
      case "link":
        return (
          <a key={index} className="inline-link" href={token.href} {...externalProps(token.href)}>
            {renderTokens(token.children)}
          </a>
        );
      default:
        return <Fragment key={index}>{token.text}</Fragment>;
    }
  });
}

/** 説明ページの文中の書式（`コード`・**強調**・[文字](行き先)）を描画する。HTML としては解釈しない */
export function InlineText({ text }: { text: string }) {
  return <>{renderTokens(parseInline(text))}</>;
}
