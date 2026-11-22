// バックエンド（backend/app.py）が返す JSON の型。キー名はバックエンドに合わせて snake_case。

export type Environment = "development" | "production";
export type ServiceAction = "start" | "stop" | "restart";

export interface Profile {
  id: string;
  label: string;
  environment: Environment;
  host: string;
  port: number | null;
  username: string | null;
  service: string;
  bot_dir: string;
  note: string;
  /** デプロイで取り込むブランチ（origin/<これ>）。古いバックエンドは返さない */
  deploy_branch?: string;
  /** 見本の値（YOUR_...）のまま書き換えられていない項目。空でなければ、バックエンドは接続を断る。
   *  古いバックエンドは返さないので、読むときは profiles.ts の unsetFieldsOf を通す */
  unset_fields?: string[];
}

export interface ProfilesResponse {
  profiles: Profile[];
  source: string;
}

export interface ServerInfo {
  hostname: string;
  kernel: string;
  os: string;
}

export interface ConnectionState {
  connected: boolean;
  /** 接続先を変更しても保持する、セッション全体の遠隔処理の終了未確認 */
  state_unknown: boolean;
  profile: Profile | null;
  connected_at: number | null;
  fingerprint: string | null;
  server: ServerInfo | null;
  last_error: string | null;
  /** ローカル設定の場所を含まない診断文。旧バックエンドは返さない。 */
  last_error_safe?: string | null;
}

export interface ServiceStatus {
  name: string;
  description: string;
  load_state: string;
  active_state: string;
  sub_state: string;
  result: string;
  unit_file_state: string;
  main_pid: number | null;
  restarts: number | null;
  memory_bytes: number | null;
  cpu_seconds: number | null;
  uptime_seconds: number | null;
}

export interface Usage {
  total: number;
  used: number;
  available: number;
}

export interface Resources {
  uptime_seconds: number | null;
  load: [number, number, number] | null;
  cpu_count: number | null;
  memory: Usage | null;
  swap: Usage | null;
  disk: Usage | null;
}

export interface GitInfo {
  branch: string;
  commit: string;
  subject: string;
  committed_at: string;
  describe: string;
  dirty_files: number;
}

export interface StatusResponse {
  rtt_ms: number | null;
  fetched_at: number;
  service: ServiceStatus | null;
  service_error: string | null;
  service_error_safe?: string | null;
  resources: Resources | null;
  resources_error: string | null;
  resources_error_safe?: string | null;
  git: GitInfo | null;
  git_error: string | null;
  git_error_safe?: string | null;
}

export interface LogEntry {
  ts: number;
  message: string;
  priority: number | null;
  pid: number | null;
  source: string;
  /** フロントエンドが受信時に付ける属性。原文と分けて保持する。 */
  messagePrivacy?: "unknown" | "conversation" | "system";
}

export interface Operation {
  id: number;
  at: number;
  profile_id: string;
  environment: Environment;
  action: ServiceAction;
  ok: boolean;
  message: string;
  duration_ms: number;
}

export interface CommandRecord {
  at: number;
  label: string;
  command: string;
  exit_status: number | null;
  duration_ms: number;
  ok: boolean;
  error: string | null;
  error_safe?: string | null;
}

export interface DbSummary {
  path: string;
  exists: boolean;
  errors: string[];
  size_bytes?: number;
  wal_bytes?: number;
  schema_version?: number;
  /** 手元のリポジトリの database.py にある SCHEMA_VERSION（読めなければ null） */
  latest_schema_version?: number | null;
  tables?: Record<string, number>;
  reminders_by_status?: Record<string, number> | null;
  queue_by_status?: Record<string, number> | null;
  feedback?: { good: number; bad: number } | null;
  feedback_by_day?: { day: string; good: number; bad: number }[];
  blacklist?: { users: number; blocked: number } | null;
}

// ---- テーブルの中身（backend/remote_scripts/db_table.py の出力）

/** セルの値。ID は桁が丸まらないよう文字列で届く。JSON の列（記憶の値・発言サンプルなど）は中身ごと届く */
export type DbValue = string | number | boolean | null | DbValue[] | { [key: string]: DbValue };

export interface DbRow {
  cells: Record<string, DbValue>;
  /** 持ち帰らなかった列（会話統計の収集を止めた人の発言サンプルと解析） */
  hidden?: string[];
}

export interface DbTablePage {
  table: string;
  columns: string[];
  rows: DbRow[];
  /** テーブル全体の行数 */
  total: number;
  offset: number;
  limit: number;
  /** 利用者の ID → 表示名（会話統計・リマインダーに残っているもの） */
  users: Record<string, string>;
  schema_version?: number;
  errors: string[];
}

// ---- デプロイ（backend/deploy.py の DeployRecord）

export type DeployStepStatus = "pending" | "running" | "succeeded" | "failed" | "skipped";
export type DeployStatus = "running" | "succeeded" | "up_to_date" | "failed" | "rolled_back" | "rollback_failed" | "unknown";

export interface DeployCheck {
  label: string;
  ok: boolean;
  detail: string;
}

export interface DeployStep {
  key: string;
  title: string;
  description: string;
  phase: "deploy" | "rollback";
  status: DeployStepStatus;
  started_at: number | null;
  finished_at: number | null;
  commands: string[];
  output: string;
  /** ログ抜粋を含む場合は、文字列への切り詰め後も本文を保守的に隠す。 */
  output_contains_logs?: boolean;
  checks: DeployCheck[];
  message: string;
}

export interface DeployRecord {
  id: number;
  profile_id: string;
  environment: Environment;
  service: string;
  branch: string;
  started_at: number;
  steps: DeployStep[];
  status: DeployStatus;
  finished_at: number | null;
  before_commit: string | null;
  target_commit: string | null;
  after_commit: string | null;
  commits: { hash: string; subject: string }[];
  message: string;
}

export interface DeployResponse {
  deploy: DeployRecord | null;
  running: boolean;
}

// ---- ボットの中の状態（backend/remote_scripts/insights.py の出力）

export interface GuildSettingsView {
  /** ボットが応答に使うペルソナの表示名（現行版は prompts/ のファイル構成からの推定） */
  persona: string | null;
  /** saved = 保存した選択 / default = 未選択なので既定 / missing = 保存したファイルが見つからないので既定 */
  persona_source: "saved" | "default" | "missing";
  /** 見つからない保存済みのペルソナ（拡張子なしのファイル名）。ボットはこの値を消さずに保持する */
  persona_missing: string | null;
  /** true = ペルソナのファイルが無く、組み込みの標準プロンプトで応答している */
  persona_builtin: boolean;
  model: string | null;
  respond_mode: string | null;
  grounding: boolean;
  auto_topic_enabled: boolean;
  auto_topic_interval_hours: number | null;
  /** null = この版のボットには自律行動が無い */
  autonomous_enabled: boolean | null;
  /** undefined = 配信先指定に未対応の版 / null = 指定なし（自動）。ID は精度を保つため文字列 */
  autonomous_stage_channel_id?: string | null;
  autonomous_voice_channel_id?: string | null;
  /** false = DB に保存された設定が無く、既定値で動いている */
  saved: boolean;
}

export interface GuildInsight {
  name: string;
  members: number | null;
  online: number | null;
  voice_channel: string | null;
  voice_error?: string | null;
  settings: GuildSettingsView;
}

export interface DiscordInsight {
  ok: boolean;
  /** true = 一覧の後続ページが未取得。表示件数は参加サーバー総数ではない */
  partial?: boolean;
  error: string | null;
  api_rtt_ms?: number;
  bot_user?: string | null;
  guilds?: GuildInsight[];
}

export interface ActivityInsight {
  window_hours: number;
  gemini_calls: number;
  gemini_tokens: number;
  conversations: number;
  last_conversation_at: number | null;
  gateway_connects: number;
  gateway_resumes: number;
  gateway_invalidated: number;
  heartbeat_blocked: number;
  exceptions: number;
  autonomous_events: { at: number; text: string }[];
}

export interface Insights {
  errors: string[];
  source_dir: string | null;
  version: string | null;
  autonomous_supported: boolean;
  /** true = ペルソナを prompts/ のファイル構成からボットと同じ規則で推定した（現行版） */
  persona_estimated: boolean;
  discord: DiscordInsight;
  dm_settings: GuildSettingsView;
  activity: ActivityInsight;
  generated_at: number;
}

export interface InsightsResponse {
  data: Insights;
  fetched_at: number;
  cached: boolean;
}

// 画面側で貯める時系列（スパークライン用）
export interface Sample {
  t: number;
  rtt: number | null;
  botMemory: number | null;
  hostMemoryRatio: number | null;
  load1: number | null;
}
