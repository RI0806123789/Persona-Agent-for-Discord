// bot.db のテーブルの見せ方（列の名前・種類・一覧に出すかどうか）と、値の読み取り。
// 列はボットの src/persona_bot/core/db_migrations.py（テーブルの定義と移行。スキーマ バージョン10）に合わせている。
// 表示の部品に依存しない（tests/dbTables.test.mjs が、このファイルだけを変換して確かめる）。
import type { IconName } from "./components/Icon";
import type { Tone } from "./status";
import type { DbRow, DbTablePage, DbValue } from "./types";

/** リマインダー・文書処理キューの状態（集計のカードとテーブルの中身で共通） */
export const STATUS_LABELS: Record<string, string> = {
  pending: "待機",
  processing: "処理中",
  sending: "送信中",
  done: "完了",
  completed: "完了",
  failed: "失敗",
  canceled: "取り消し",
};

export const STATUS_TONES: Record<string, Tone> = {
  pending: "info",
  processing: "info",
  sending: "info",
  done: "good",
  completed: "good",
  failed: "critical",
  canceled: "neutral",
};

export function statusTone(status: DbValue): Tone {
  return typeof status === "string" ? (STATUS_TONES[status] ?? "neutral") : "neutral";
}

/**
 * 列の種類。表示のしかたと、伏せ字モードで隠すかどうかを決める。
 * - user / guild / id / name / text / label / json / samples / analysis / error … 伏せ字モードで隠す
 * - それ以外（日時・数値・状態・ペルソナ・モデルなど）は、個人を特定しないので隠さない
 */
export type ColumnKind =
  | "user" // 利用者の ID（表示名を添える）
  | "guild" // サーバーの ID（0 は DM）
  | "id" // チャンネル・メッセージ・タスクの ID
  | "name" // 利用者の表示名
  | "text" // 本文（会話・リマインダー・文書への質問）
  | "label" // 短い本文（記憶の項目名・記念日の名前）
  | "isoTime" // UTC の ISO 8601
  | "logTime" // pending_log の「YYYY-MM-DD HH:MM:SS UTC」
  | "unixTime" // UNIX 秒
  | "date" // YYYY-MM-DD
  | "level" // 0〜100 の値（親密度・機嫌・テンション）
  | "count"
  | "flag" // 0 / 1
  | "rating" // 1 = 👍 / -1 = 👎
  | "status"
  | "persona"
  | "model"
  | "respondMode"
  | "recurrence"
  | "hours"
  | "json" // 記憶の値（JSON）
  | "hourly" // 時間帯別の発言回数（24 個）
  | "samples" // 直近の発言サンプル
  | "analysis" // 話題・性格の解析結果
  | "error"
  | "raw"; // この画面が知らない列（ボットの新しい版で増えた列など）

export interface ColumnSpec {
  key: string;
  label: string;
  kind: ColumnKind;
  /** 一覧の表にも出す（出さない列は、行を開いたときだけ見える） */
  list?: boolean;
}

export interface TableSpec {
  label: string;
  icon: IconName;
  description: string;
  /** 並び順の説明（並び順そのものは db_table.py の TABLES が決める） */
  order: string;
  columns: ColumnSpec[];
}

/** 伏せ字モードで隠す列の種類 */
const SECRET_KINDS = new Set<ColumnKind>(["user", "guild", "id", "name", "text", "label", "json", "samples", "analysis", "error", "raw"]);

export function isSecretKind(kind: ColumnKind): boolean {
  return SECRET_KINDS.has(kind);
}

export const DB_TABLE_SPECS: Record<string, TableSpec> = {
  affinity: {
    label: "親密度・感情",
    icon: "person",
    description:
      "利用者ごとの親密度と、機嫌・テンション（2次元の感情）。値は最後に話した時点のもので、ボットは使うときに時間の経過に合わせて機嫌とテンションを 50 へ戻します。",
    order: "最後に話したのが新しい順",
    columns: [
      { key: "user_id", label: "利用者", kind: "user", list: true },
      { key: "affinity", label: "親密度", kind: "level", list: true },
      { key: "valence", label: "機嫌", kind: "level", list: true },
      { key: "arousal", label: "テンション", kind: "level", list: true },
      { key: "last_interaction", label: "最後に話した日時", kind: "unixTime", list: true },
    ],
  },
  user_stats: {
    label: "会話統計",
    icon: "overview",
    description:
      "ボットへの発言の回数・時間帯・直近の発言と、話題・性格の解析結果。統計の収集を止めている人の発言サンプルと解析結果は、サーバーから持ち帰りません。",
    order: "最後の発言が新しい順",
    columns: [
      { key: "user_id", label: "利用者", kind: "user", list: true },
      { key: "total_count", label: "発言回数", kind: "count", list: true },
      { key: "last_seen", label: "最後の発言", kind: "isoTime", list: true },
      { key: "first_seen", label: "最初の発言", kind: "isoTime", list: true },
      { key: "stats_opt_out", label: "収集の停止", kind: "flag", list: true },
      { key: "user_name", label: "表示名（記録時）", kind: "name" },
      { key: "hourly", label: "時間帯別の発言回数", kind: "hourly" },
      { key: "recent_samples", label: "直近の発言", kind: "samples" },
      { key: "analysis", label: "話題・性格の解析", kind: "analysis" },
    ],
  },
  queue_tasks: {
    label: "文書処理キュー",
    icon: "description",
    description: "添付された文書（PDF・テキストなど）の処理待ちと処理結果。完了・失敗したものは新しい 50 件まで残ります。",
    order: "受け付けたのが新しい順",
    columns: [
      { key: "created_at", label: "受付", kind: "isoTime", list: true },
      { key: "status", label: "状態", kind: "status", list: true },
      { key: "author_id", label: "依頼した人", kind: "user", list: true },
      { key: "question", label: "質問", kind: "text", list: true },
      { key: "attachment_count", label: "添付", kind: "count", list: true },
      { key: "updated_at", label: "更新", kind: "isoTime" },
      { key: "finished_at", label: "終了", kind: "isoTime" },
      { key: "error", label: "失敗の理由", kind: "error" },
      { key: "task_id", label: "タスク ID", kind: "id" },
      { key: "channel_id", label: "チャンネル", kind: "id" },
      { key: "message_id", label: "メッセージ", kind: "id" },
    ],
  },
  pending_log: {
    label: "記憶圧縮前のログ",
    icon: "chat",
    description: "長期記憶へまとめる前の会話（発言と応答）。記憶の圧縮が済むと消えます。",
    order: "新しい順",
    columns: [
      { key: "created_at", label: "記録", kind: "logTime", list: true },
      { key: "user_id", label: "利用者", kind: "user", list: true },
      { key: "question", label: "発言", kind: "text", list: true },
      { key: "response", label: "応答", kind: "text", list: true },
      { key: "id", label: "番号", kind: "count" },
    ],
  },
  legacy_imports: {
    label: "旧ログの取り込み記録",
    icon: "history",
    description: "旧ファイルの二重取り込みを防ぐ記録。会話本文は含みません。",
    order: "ファイル名・内容のハッシュ順",
    columns: [
      { key: "source_name", label: "ファイル名", kind: "label", list: true },
      { key: "content_hash", label: "内容のハッシュ", kind: "id", list: true },
    ],
  },
  profile: {
    label: "プロフィール記憶",
    icon: "person",
    description: "会話から覚えた、利用者ごとのプロフィール（項目と値）。記念日は anniversaries に入ります。",
    order: "利用者・項目の順",
    columns: [
      { key: "user_id", label: "利用者", kind: "user", list: true },
      { key: "key", label: "項目", kind: "label", list: true },
      { key: "value", label: "値", kind: "json", list: true },
    ],
  },
  guild_settings: {
    label: "サーバー設定",
    icon: "guilds",
    description: "サーバーごとのペルソナ・モデル・応答のしかたと、自発会話・自律行動（配信先を含む）の設定。DM は全員で1つの設定を共有します。",
    order: "サーバー ID の順（DM が先頭）",
    columns: [
      { key: "guild_id", label: "サーバー", kind: "guild", list: true },
      { key: "prompt_file", label: "ペルソナ", kind: "persona", list: true },
      { key: "model_name", label: "モデル", kind: "model", list: true },
      { key: "respond_mode", label: "応答", kind: "respondMode", list: true },
      { key: "grounding_enabled", label: "Google 検索", kind: "flag", list: true },
      { key: "auto_topic_enabled", label: "自発会話", kind: "flag", list: true },
      { key: "auto_topic_interval_hours", label: "しきい値", kind: "hours", list: true },
      { key: "autonomous_enabled", label: "自律行動", kind: "flag", list: true },
      // 0 は「指定なし（自動）」
      { key: "autonomous_stage_channel_id", label: "配信先のステージ", kind: "id" },
      { key: "autonomous_voice_channel_id", label: "配信先のボイス", kind: "id" },
    ],
  },
  anniversary_posts: {
    label: "記念日の投稿記録",
    icon: "sparkle",
    description: "記念日・誕生日のお祝いを投稿した記録。同じ日に二度お祝いしないために使います。",
    order: "投稿した日が新しい順",
    columns: [
      { key: "posted_date", label: "最後に投稿した日", kind: "date", list: true },
      { key: "user_id", label: "お祝いした人", kind: "user", list: true },
      { key: "label", label: "記念日", kind: "label", list: true },
      { key: "guild_id", label: "サーバー", kind: "guild", list: true },
    ],
  },
  reminders: {
    label: "リマインダー",
    icon: "alarm",
    description: "会話から登録したリマインダー。繰り返しのものは、通知のたびに通知予定が次の回へ進みます。",
    order: "登録が新しい順",
    columns: [
      { key: "remind_at", label: "通知予定", kind: "isoTime", list: true },
      { key: "status", label: "状態", kind: "status", list: true },
      { key: "recurrence", label: "繰り返し", kind: "recurrence", list: true },
      { key: "user_id", label: "依頼した人", kind: "user", list: true },
      { key: "message", label: "本文", kind: "text", list: true },
      { key: "created_at", label: "登録", kind: "isoTime" },
      { key: "recurrence_anchor", label: "繰り返しの基準", kind: "isoTime" },
      { key: "reminder_id", label: "番号", kind: "count" },
      { key: "user_name", label: "表示名（登録時）", kind: "name" },
      { key: "guild_id", label: "サーバー", kind: "guild" },
      { key: "channel_id", label: "チャンネル", kind: "id" },
    ],
  },
  feedback: {
    label: "👍👎 の評価",
    icon: "thumbUp",
    description: "ボットの返信に付いた 👍👎。どのモデル・どのペルソナの返信だったかも一緒に残しています。",
    order: "記録が新しい順",
    columns: [
      { key: "created_at", label: "記録", kind: "isoTime", list: true },
      { key: "rating", label: "評価", kind: "rating", list: true },
      { key: "model_name", label: "モデル", kind: "model", list: true },
      { key: "prompt_file", label: "ペルソナ", kind: "persona", list: true },
      { key: "user_id", label: "評価した人", kind: "user", list: true },
      { key: "target_user_id", label: "返信の宛先", kind: "user" },
      { key: "guild_id", label: "サーバー", kind: "guild" },
      { key: "channel_id", label: "チャンネル", kind: "id" },
      { key: "message_id", label: "メッセージ", kind: "id" },
    ],
  },
};

/** 中身を表示できるテーブルか（バックエンドの DB_TABLES と同じ。ほかのテーブルは件数だけを出す） */
export function isViewableTable(name: string): boolean {
  return Object.hasOwn(DB_TABLE_SPECS, name);
}

/**
 * 実際に届いた列に合わせて、見せる列を決める。DB に無い列（古い版）は外し、
 * この画面が知らない列（新しい版で増えた列）は「raw」として最後に足す。
 */
export function resolveColumns(table: string, columns: string[]): ColumnSpec[] {
  const known = DB_TABLE_SPECS[table]?.columns ?? [];
  const present = new Set(columns);
  const resolved = known.filter((column) => present.has(column.key));
  for (const column of columns) {
    if (!known.some((spec) => spec.key === column)) resolved.push({ key: column, label: column, kind: "raw" });
  }
  return resolved;
}

// ---------------------------------------------------------------- 値の読み取り

/**
 * JavaScript の Date で表せるミリ秒だけを通す（±8.64e15。#394）。
 * 有限の数でも範囲外なら Invalid Date になり、`toISOString()` が RangeError を投げて
 * テーブル全体の描画が止まるため、範囲外は「読めない」として null にする。
 */
function validDateMs(ms: number): number | null {
  return Number.isFinite(ms) && !Number.isNaN(new Date(ms).getTime()) ? ms : null;
}

/**
 * 日時の列を、ミリ秒（UNIX 時刻）にする。読めない・Date で表せない値は null
 * （画面は元の値をそのまま見せる）。★ 返した値は Date として必ず有効であること。
 * 描画側（DbTableDetail の CellValue）はこれを前提に `toISOString()` を呼ぶ。
 */
export function parseDbTime(value: DbValue, kind: "isoTime" | "logTime" | "unixTime"): number | null {
  if (kind === "unixTime") {
    const seconds = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
    // 秒が有限でも、ミリ秒への変換で Infinity になったり Date の範囲を超えたりする（手で直した・移行した値など）
    return Number.isFinite(seconds) && seconds > 0 ? validDateMs(seconds * 1000) : null;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  let text = value.trim();
  if (kind === "logTime") {
    // 「2026-10-18 03:12:45 UTC」→「2026-10-18T03:12:45Z」
    const match = /^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2}) UTC$/.exec(text);
    if (!match) return null;
    text = `${match[1]}T${match[2]}Z`;
  } else if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(text)) {
    // タイムゾーンの無い ISO 8601 は、ボットの保存のしかた（UTC）に合わせて UTC として読む
    text = `${text.replace(" ", "T")}Z`;
  }
  return validDateMs(Date.parse(text));
}

/** 「../prompts/prompt_tsun.txt」→「prompt_tsun」 */
export function personaName(value: DbValue): string {
  if (typeof value !== "string" || !value) return "—";
  const file = value.split(/[\\/]/).pop() ?? value;
  return file.replace(/\.txt$/i, "") || value;
}

const RESPOND_MODES: Record<string, string> = { all: "すべての発言", mention: "メンションのみ" };
const RECURRENCES: Record<string, string> = { none: "単発", daily: "毎日", weekly: "毎週", monthly: "毎月" };

export function respondModeLabel(value: DbValue): string {
  return typeof value === "string" ? (RESPOND_MODES[value] ?? (value || "—")) : "—";
}

export function recurrenceLabel(value: DbValue): string {
  return typeof value === "string" ? (RECURRENCES[value] ?? (value || "—")) : "—";
}

/** 0 / 1 の列（SQLite には真偽値の型が無い） */
export function isOn(value: DbValue): boolean {
  return value === true || value === 1 || value === "1";
}

/** JSON の値を1行の文にする（一覧の表で使う）。文字列の配列は「、」でつなぐ。 */
export function inlineText(value: DbValue): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value.replace(/\s+/g, " ").trim() || "—";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value) && value.every((item) => typeof item === "string")) return value.join("、") || "—";
  return JSON.stringify(value);
}

/** 時間帯別の発言回数を 24 個の数にそろえる（読めない値は 0）。 */
export function hourlyCounts(value: DbValue): number[] {
  const values = Array.isArray(value) ? value : [];
  return Array.from({ length: 24 }, (_, hour) => {
    const count = values[hour];
    return typeof count === "number" && Number.isFinite(count) && count > 0 ? count : 0;
  });
}

export interface StatsAnalysis {
  topics: { label: string; weight: number }[];
  bigFive: { key: string; label: string; value: number }[];
  personality: string;
  computedAt: string | null;
  sampleCount: number | null;
}

const BIG_FIVE: [string, string][] = [
  ["openness", "開放性"],
  ["conscientiousness", "誠実性"],
  ["extraversion", "外向性"],
  ["agreeableness", "協調性"],
  ["neuroticism", "神経症傾向"],
];

/** 会話統計の解析結果（LLM の出力を保存したもの）を、型をそろえて読む。形が違えば null。 */
export function readAnalysis(value: DbValue): StatsAnalysis | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const topics = Array.isArray(value.topics)
    ? value.topics.flatMap((topic) => {
        if (typeof topic === "string") return [{ label: topic, weight: 1 }];
        if (topic && typeof topic === "object" && !Array.isArray(topic) && typeof topic.label === "string") {
          return [{ label: topic.label, weight: typeof topic.weight === "number" ? topic.weight : 0 }];
        }
        return [];
      })
    : [];
  const bigFiveSource = value.big_five && typeof value.big_five === "object" && !Array.isArray(value.big_five) ? value.big_five : {};
  const bigFive = BIG_FIVE.flatMap(([key, label]) => {
    const number = bigFiveSource[key];
    return typeof number === "number" && Number.isFinite(number) ? [{ key, label, value: Math.min(100, Math.max(0, number)) }] : [];
  });
  return {
    topics,
    bigFive,
    personality: typeof value.personality === "string" ? value.personality : "",
    computedAt: typeof value.computed_at === "string" ? value.computed_at : null,
    sampleCount: typeof value.sample_count === "number" ? value.sample_count : null,
  };
}

/** 利用者の ID から表示名を引く（無ければ null）。 */
export function userName(page: Pick<DbTablePage, "users">, value: DbValue): string | null {
  return typeof value === "string" || typeof value === "number" ? (page.users[String(value)] ?? null) : null;
}

/**
 * 伏せ字モードで文の中からも隠すため、ページに出てくる利用者の表示名を集める
 * （ログや通知の文に名前が出ることがあるため。privacy.ts の collectSecrets へ渡す）。
 */
export function namesInPage(page: Pick<DbTablePage, "users"> & { rows: DbRow[] }): string[] {
  const names = new Set(Object.values(page.users));
  for (const row of page.rows) {
    const name = row.cells.user_name;
    if (typeof name === "string" && name) names.add(name);
  }
  return [...names];
}
