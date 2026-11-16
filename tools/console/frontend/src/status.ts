import type { ServiceAction, ServiceStatus } from "./types";

export type Tone = "good" | "warning" | "serious" | "critical" | "neutral" | "info";

/** サービス操作（起動・停止・再起動）の表示名。確認ダイアログ・通知・操作履歴で共通に使う。 */
export const ACTION_LABELS: Record<ServiceAction, string> = { start: "起動", stop: "停止", restart: "再起動" };

export interface ToneLabel {
  tone: Tone;
  label: string;
  hint?: string;
}

/** 起動途中・自動再起動待ち・再読み込み中も、明示的な停止を許可する。 */
export function canStopService(service: ServiceStatus | null): boolean {
  return service !== null
    && (service.load_state === "loaded" || service.load_state === "masked")
    && ["active", "activating", "reloading"].includes(service.active_state);
}

/** systemd の状態を、色と日本語のラベルにする。色だけで意味を伝えないよう、必ずラベルと組で使う。 */
export function serviceTone(service: ServiceStatus | null): ToneLabel {
  if (!service) return { tone: "neutral", label: "不明" };
  if (service.load_state === "not-found") return { tone: "critical", label: "未登録" };
  switch (service.active_state) {
    case "active":
      return { tone: "good", label: service.sub_state === "running" ? "稼働中" : `稼働中（${service.sub_state}）` };
    case "activating":
      if (service.sub_state === "auto-restart") {
        return {
          tone: "critical",
          label: "再起動待ち",
          hint: "この状態が続くなら、落ちては再起動を繰り返しています（StartLimitIntervalSec=0 のため止まりません）。設定ミスを疑ってください。",
        };
      }
      return { tone: "warning", label: "起動中" };
    case "deactivating":
      return { tone: "warning", label: "停止処理中" };
    case "reloading":
      return { tone: "warning", label: "再読み込み中" };
    case "failed":
      return { tone: "critical", label: "異常終了" };
    case "inactive":
      return { tone: "serious", label: "停止中" };
    default:
      return { tone: "neutral", label: service.active_state };
  }
}

/** 使用率（0〜1）を3段階で評価する。 */
export function usageTone(ratio: number | null): ToneLabel {
  if (ratio === null) return { tone: "neutral", label: "—" };
  if (ratio >= 0.9) return { tone: "critical", label: "逼迫" };
  if (ratio >= 0.75) return { tone: "warning", label: "注意" };
  return { tone: "good", label: "正常" };
}

export function ratioOf(used: number | undefined, total: number | undefined): number | null {
  if (used === undefined || total === undefined || total <= 0) return null;
  return Math.min(1, Math.max(0, used / total));
}
