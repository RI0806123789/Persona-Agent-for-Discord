// 画面側テスト共通の TypeScript ローダー（#416）。
// ソースを CommonJS へ変換して実行し、require は渡した模擬の表だけで解決する
// （表にない import は空のオブジェクトになる）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import ts from "typescript";

/** `src/` 配下のソースを文字列で読む（Windows の CRLF は LF にそろえる）。 */
export const readSource = async (name) =>
  (await readFile(new URL(`../../src/${name}`, import.meta.url), "utf8")).replaceAll("\r\n", "\n");

function evaluate(text, modules, transpileOptions) {
  const exports = {};
  const output = ts.transpileModule(text, transpileOptions).outputText;
  const resolve = typeof modules === "function" ? modules : (name) => modules[name] ?? {};
  new Function("require", "exports", output)(resolve, exports);
  return exports;
}

/**
 * TSX を CommonJS として評価し、exports を返す。
 * @param {string} text ソース本文
 * @param {Record<string, unknown> | ((name: string) => unknown) | undefined} modules require の戻り値の表または解決関数
 * @param {string | undefined} fileName 拡張子で TSX / TS を切り替えたいときだけ指定する
 */
export function compile(text, modules = {}, fileName) {
  return evaluate(text, modules, {
    ...(fileName ? { fileName } : {}),
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  });
}

/** 模擬の表に無い require を即座に失敗させる厳格な TSX ローダー。 */
export function compileStrict(text, modules = {}, fileName) {
  return compile(text, (name) => {
    assert.ok(name in modules, `未定義の依存: ${name}`);
    return modules[name];
  }, fileName);
}

/** JSX を使わない .ts ソース用（山括弧の型アサーションを JSX と取り違えない）。 */
export function compileTs(text, modules = {}) {
  return evaluate(text, modules, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
}
