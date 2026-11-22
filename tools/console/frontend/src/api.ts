import type {
  CommandRecord,
  ConnectionState,
  DbSummary,
  DbTablePage,
  DeployResponse,
  InsightsResponse,
  LogEntry,
  Operation,
  ProfilesResponse,
  ServiceAction,
  StatusResponse,
} from "./types";

// バックエンドは全APIでこのヘッダーを必須にしている（ほかのサイトからの勝手な呼び出しを防ぐため）
const HEADERS = {
  "Content-Type": "application/json",
  "X-Requested-By": "persona-bot-console",
};

export class ApiError extends Error {
  readonly status: number;
  readonly safeMessage?: string;

  constructor(message: string, status: number, safeMessage?: string) {
    super(message);
    this.status = status;
    this.safeMessage = safeMessage;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers: { ...HEADERS, ...init?.headers } });
  } catch {
    throw new ApiError("バックエンドに接続できません。python app.py が起動しているか確認してください。", 0);
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let safeDetail: string | undefined;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
        detail = body.detail;
        if ("detail_safe" in body && typeof body.detail_safe === "string") safeDetail = body.detail_safe;
      }
    } catch {
      // JSON でない応答（プロキシのエラーページなど）はステータスだけ見せる
    }
    throw new ApiError(detail, response.status, safeDetail);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  profiles: () => request<ProfilesResponse>("/api/profiles"),
  connection: () => request<ConnectionState>("/api/connection"),
  connect: (profileId: string) => post<ConnectionState>("/api/connect", { profile_id: profileId }),
  disconnect: () => post<ConnectionState>("/api/disconnect"),
  status: () => request<StatusResponse>("/api/status"),
  logs: (lines: number) => request<{ entries: LogEntry[] }>(`/api/logs?lines=${lines}`),
  service: (action: ServiceAction, target: { profile_id: string; connected_at: number }) =>
    post<Operation>(`/api/service/${action}`, target),
  operations: () => request<{ operations: Operation[] }>("/api/operations"),
  history: () => request<{ commands: CommandRecord[] }>("/api/history"),
  db: () => request<DbSummary>("/api/db"),
  dbTable: (table: string, offset: number, limit: number) =>
    request<DbTablePage>(`/api/db/tables/${encodeURIComponent(table)}?offset=${offset}&limit=${limit}`),
  deploy: () => request<DeployResponse>("/api/deploy"),
  startDeploy: (target: { profile_id: string; connected_at: number }) => post<DeployResponse>("/api/deploy", target),
  insights: (refresh = false) => request<InsightsResponse>(`/api/insights${refresh ? "?refresh=true" : ""}`),
};

export function logStreamUrl(lines: number): string {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/api/logs/stream?lines=${lines}`;
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** API が返した伏せ字用の安全な文面（`detail_safe`）。無ければ null（#494）。 */
export function errorSafeMessage(error: unknown): string | null {
  return error instanceof ApiError && error.safeMessage ? error.safeMessage : null;
}
