import { useEffect, useRef, useState } from "react";
import { GUIDE_PAGE } from "../../content";
import { splitShellComment } from "../../guides/markup";
import type { CodeLanguage } from "../../guides/types";

const LANGUAGE_LABELS: Record<CodeLanguage, string> = {
  powershell: "PowerShell",
  bash: "bash",
  json: "JSON",
  text: "テキスト",
};

/** 「コピーしました」を出しておく長さ */
const COPY_FEEDBACK_MS = 2000;

type CopyState = "idle" | "done" | "failed";

function canCopy(): boolean {
  // クリップボードは安全な接続（https・localhost）でしか使えない。使えない環境ではボタンを出さない
  return typeof navigator !== "undefined" && Boolean(navigator.clipboard) && window.isSecureContext;
}

/** シェルの例は、行末の # から後ろ（コメント）を薄い色で見せる。それ以外の色分けはしない（ライブラリを足さないため） */
function CodeLines({ code, language }: { code: string; language: CodeLanguage }) {
  if (language !== "bash" && language !== "powershell") return <>{code}</>;
  const lines = code.split("\n");
  return (
    <>
      {lines.map((line, index) => {
        const [command, comment] = splitShellComment(line);
        return (
          <span key={index}>
            {command}
            {comment && <span className="code-comment">{comment}</span>}
            {index < lines.length - 1 && "\n"}
          </span>
        );
      })}
    </>
  );
}

/** コマンドや設定の例。札（PowerShell・bash など）とコピーのボタンを付ける */
export function CodeBlock({ code, language, title }: { code: string; language: CodeLanguage; title?: string }) {
  const [state, setState] = useState<CopyState>("idle");
  const [copyable] = useState(canCopy);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = async () => {
    window.clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(code);
      setState("done");
    } catch {
      setState("failed");
    }
    timer.current = window.setTimeout(() => setState("idle"), COPY_FEEDBACK_MS);
  };

  return (
    <figure className="code-block">
      <figcaption className="code-block__bar">
        <span className={`code-block__label code-block__label--${language}`}>{title ?? LANGUAGE_LABELS[language]}</span>
        {copyable && (
          <button type="button" className={`code-block__copy is-${state}`} onClick={copy}>
            {/* 読み上げには、押した結果を知らせる（文字が変わったことだけでは伝わらない） */}
            <span aria-live="polite">{GUIDE_PAGE.copy[state]}</span>
          </button>
        )}
      </figcaption>
      {/* 横に長い行は折り返さずにスクロールさせる。キーボードでも送れるよう tabIndex を付ける */}
      <pre className="code-block__body" tabIndex={0}>
        <code>
          <CodeLines code={code} language={language} />
        </code>
      </pre>
    </figure>
  );
}
