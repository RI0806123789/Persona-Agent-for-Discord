import type { LogEntry } from "./types";

export type LogLevel = "error" | "warn" | "info" | "system";

export const MAX_LOG_ENTRIES = 3000;

/** ライブ受信中・一時停止中のどちらも、古い行を落として保持量を制限する。 */
export function appendLogEntries(previous: LogEntry[], incoming: LogEntry[]): LogEntry[] {
  return previous.concat(incoming).slice(-MAX_LOG_ENTRIES);
}

export const LEVEL_LABELS: Record<LogLevel, string> = {
  error: "エラー",
  warn: "警告",
  info: "情報",
  system: "systemd",
};

// discord.py の旧書式と、時差を含むボット共通の logging 書式を扱う。
// 画面では journald の時刻だけを見せ、モジュール名と本文は残す。
const LOGGING_PREFIX = /^\[\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[+-]\d{4})?\] \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\s*\] /;

// print と標準出力への logging は、journald では「情報(6)」になることがある。
// そこで logging の書式があればそのレベルを、無ければ本文の言葉から重さを推定する
const ERROR_PATTERN = /Traceback|Error|ERROR|Exception|エラー|失敗|例外|^\s+File "/;
const WARN_PATTERN = /警告|Warning|WARN|タイムアウト|見送/;

export function levelOf(entry: LogEntry): LogLevel {
  if (entry.priority !== null && entry.priority <= 3) return "error";
  if (entry.priority === 4) return "warn";
  if (entry.source === "systemd") return "system";
  const prefixed = LOGGING_PREFIX.exec(entry.message);
  if (prefixed) {
    if (prefixed[1] === "ERROR" || prefixed[1] === "CRITICAL") return "error";
    if (prefixed[1] === "WARNING") return "warn";
    return "info";
  }
  if (ERROR_PATTERN.test(entry.message)) return "error";
  if (WARN_PATTERN.test(entry.message)) return "warn";
  return "info";
}

const EXCEPTION_TRACEBACK = /^Traceback \(most recent call last\)/;
// 共通Formatterは例外本文を省き、この見出しと字下げされた発生位置だけを出す。
const SAFE_EXCEPTION = /^例外: \S+$/;
// discord.py が「heartbeat blocked」の警告に続けて出す「ループが何をしていたか」のスタックなど
const OTHER_TRACEBACK = /traceback \(most recent call last\)/i;

/**
 * 並んだログをまとめて分類する。Traceback（複数行）は、字下げされた続きの行も含めて1つの塊として同じレベルにする。
 * 1行ずつ levelOf で判定すると、「File ...」はエラー、「return ...」は情報、のように塊の中で色がばらばらになるため。
 */
export function classifyEntries(entries: LogEntry[]): LogLevel[] {
  const levels: LogLevel[] = [];
  let block: LogLevel | null = null; // Traceback の途中なら、その塊のレベル
  let isException = false; // 例外の Traceback なら、最後の字下げなしの行（例外の本文）までが塊
  for (const entry of entries) {
    const message = entry.message;
    if (SAFE_EXCEPTION.test(message) && entry.source !== "systemd") {
      const previous = levels[levels.length - 1];
      block = previous === "warn" || previous === "error" ? previous : "error";
      isException = false;
    } else if (EXCEPTION_TRACEBACK.test(message)) {
      block = "error";
      isException = true;
    } else if (OTHER_TRACEBACK.test(message)) {
      // 直前の警告などの続きなので、そのレベルを引き継ぐ
      const own = levelOf(entry);
      const previous = levels[levels.length - 1];
      block = own === "error" || own === "warn" ? own : previous === "error" || previous === "warn" ? previous : "warn";
      isException = false;
    } else if (block && /^\s/.test(message)) {
      // 字下げされた行は塊の続き
    } else if (block && !message.trim()) {
      // 空行もスタックの区切りとして同じレベルで表示する
    } else if (block && isException) {
      if (LOGGING_PREFIX.test(message) || entry.source === "systemd") {
        block = null; // 途中で別のログが割り込んだ場合は、新しい行として判定する
        levels.push(levelOf(entry));
        continue;
      }
      levels.push("error"); // 例外の本文（例: ValueError: ...）で塊が終わる
      block = null;
      continue;
    } else {
      block = null;
    }
    levels.push(block ?? levelOf(entry));
  }
  return levels;
}

/** 画面に出す本文。logging の時刻とレベルの前置きを外す（レベルは別の欄に出すため）。 */
export function displayMessage(entry: LogEntry): string {
  return entry.message.replace(LOGGING_PREFIX, "");
}

/**
 * 検索語に一致する範囲 `[開始, 終了)` を、**元の文字列**での位置で返す（大文字小文字は区別しない）。
 *
 * ★ `text.toLowerCase()` の上で見つけた位置を元の文字列へそのまま使わないこと。小文字化で UTF-16 の長さが
 *   変わる文字（例: "İ" U+0130 は "i̇" の2単位になる）があると、それより後ろの一致の位置がずれる（#478）。
 *   正規表現の大文字小文字を無視する照合は、元の文字列の上で一致を探すので位置がずれない。
 */
export function findMatchRanges(text: string, query: string): Array<[number, number]> {
  const needle = query.trim();
  if (!needle) return [];
  const pattern = new RegExp(needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi");
  return Array.from(text.matchAll(pattern), (match) => [match.index, match.index + match[0].length] as [number, number]);
}
