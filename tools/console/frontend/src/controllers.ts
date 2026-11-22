/** 接続世代・デプロイ・サービス操作の非同期処理と後片付け。 */
import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction, type RefObject } from "react";
import { ApiError, api, errorMessage, errorSafeMessage } from "./api";
import { usePolling } from "./hooks";
import { RequestScope } from "./requestScope";
import { ACTION_LABELS } from "./status";
import type { ToastInput } from "./components/toast";
import type { ConnectionState, Insights, Sample, StatusResponse, DeployRecord, DeployStatus, ServiceAction } from "./types";

type ServiceTarget = { profile_id: string; connected_at: number };
type Scope = { requestScope: RefObject<RequestScope>; wasConnected: RefObject<boolean>; connectionChanging: RefObject<boolean> };
type Notify = (toast: ToastInput) => void;

export function useConnectionScope() {
  const requestScope = useRef(new RequestScope());
  const connectionChanging = useRef(false);
  const wasConnected = useRef(false);
  const dataConnectionKey = useRef("");
  const connectionProfileId = useRef<string | null>(null);
  useEffect(() => () => requestScope.current.invalidate(), []);
  return { requestScope, connectionChanging, wasConnected, dataConnectionKey, connectionProfileId };
}

// デプロイが終わったときの通知
const DEPLOY_DONE: Record<Exclude<DeployStatus, "running">, { tone: "good" | "critical" | "info"; title: string }> = {
  succeeded: { tone: "good", title: "デプロイしました" },
  up_to_date: { tone: "info", title: "最新でした（何も変えていません）" },
  failed: { tone: "critical", title: "デプロイを止めました" },
  rolled_back: { tone: "critical", title: "デプロイに失敗したため、前の版へ戻しました" },
  rollback_failed: { tone: "critical", title: "巻き戻しにも失敗しました。手動の対応が必要です" },
  unknown: { tone: "critical", title: "遠隔処理の終了を確認できず、状態不明です" },
};

export function useDeployController(
  { requestScope, wasConnected, connectionChanging, connectionProfileId }: Scope & { connectionProfileId: RefObject<string | null> },
  notify: Notify, refreshAfterChange: () => void, connected: boolean, connectionKey: string,
) {
  const [deploy, setDeploy] = useState<DeployRecord | null>(null);
  const [deployRunning, setDeployRunning] = useState(false);
  const deployWasRunning = useRef(false);
  const deployStarting = useRef<(() => boolean) | null>(null);
  const refreshDeploy = useCallback(async () => {
    if (!wasConnected.current || connectionChanging.current || deployStarting.current?.()) return;
    const isCurrent = requestScope.current.start("deploy");
    try {
      const result = await api.deploy();
      if (!isCurrent()) return;
      if (result.deploy && result.deploy.profile_id !== connectionProfileId.current) return;
      setDeploy(result.deploy);
      setDeployRunning(result.running);
      // 実行中 → 終わった、に変わったら結果を知らせ、状態を取り直す
      if (deployWasRunning.current && !result.running && result.deploy && result.deploy.status !== "running") {
        const done = DEPLOY_DONE[result.deploy.status];
        notify({ tone: done.tone, title: done.title, detail: result.deploy.message });
        refreshAfterChange();
      }
      deployWasRunning.current = result.running;
    } catch {
      // 記録は補助情報なので、取れなくても画面は止めない
    }
  }, [notify, refreshAfterChange]);
  // 状態不明でタスクが終わった後は、手動更新・再接続まで定期取得を止める。
  usePolling(refreshDeploy, deployRunning ? 1000 : 15000, connected && (deployRunning || deploy?.status !== "unknown"), connectionKey);

  const runDeploy = useCallback(async (target: ServiceTarget) => {
    if (!wasConnected.current || connectionChanging.current || deployStarting.current?.()) return;
    const isCurrent = requestScope.current.start("deploy");
    deployStarting.current = isCurrent;
    try {
      const result = await api.startDeploy(target);
      if (!isCurrent()) return;
      setDeploy(result.deploy);
      setDeployRunning(result.running);
      deployWasRunning.current = result.running;
      notify({ tone: "info", title: "デプロイを始めました", detail: "サービス操作のページで、1ステップずつ進み具合を確かめられます。" });
    } catch (error) {
      if (!isCurrent()) return;
      notify({
        tone: "critical",
        title: "デプロイを始められませんでした",
        detail: errorMessage(error),
        detailSafe: error instanceof ApiError ? error.safeMessage : undefined,
      });
    } finally {
      if (deployStarting.current === isCurrent) deployStarting.current = null;
    }
  }, [notify]);

  return { deploy, deployRunning, deployWasRunning, setDeploy, setDeployRunning, refreshDeploy, runDeploy };
}

export function useServiceController(
  { requestScope, wasConnected, connectionChanging }: Scope,
  notify: Notify, refreshOperations: () => Promise<void>, refreshAfterChange: () => void,
) {
  const [actionState, setActionState] = useState<{ action: ServiceAction; current: () => boolean } | null>(null);
  const busyAction = actionState?.current() ? actionState.action : null;
  const runAction = useCallback(
    async (action: ServiceAction, target: ServiceTarget) => {
      if (!wasConnected.current || connectionChanging.current) return;
      const isCurrent = requestScope.current.current();
      setActionState({ action, current: isCurrent });
      try {
        const operation = await api.service(action, target);
        if (!isCurrent()) return;
        notify(
          operation.ok
            ? { tone: "good", title: operation.message }
            : { tone: "critical", title: `${ACTION_LABELS[action]}に失敗しました`, detail: operation.message },
        );
      } catch (error) {
        if (!isCurrent()) return;
        notify({
          tone: "critical",
          title: `${ACTION_LABELS[action]}できませんでした`,
          detail: errorMessage(error),
          detailSafe: error instanceof ApiError ? error.safeMessage : undefined,
        });
      } finally {
        if (isCurrent()) {
          setActionState(null);
          void refreshOperations();
          refreshAfterChange();
          requestScope.current.schedule(() => { if (isCurrent()) refreshAfterChange(); }, 2500); // 起動直後の状態変化を拾う
        }
      }
    },
    [notify, refreshOperations, refreshAfterChange],
  );

  return { busyAction, runAction };
}


const STATUS_INTERVAL_MS = 5000;
const INSIGHTS_INTERVAL_MS = 60_000; // Discord API の利用制限に配慮して1分ごと（バックエンドも60秒キャッシュする）
const CONNECTION_INTERVAL_MS = 10000;
const MAX_SAMPLES = 60; // 5秒 × 60 = 直近5分ぶん

function toSample(status: StatusResponse): Sample {
  const memory = status.resources?.memory;
  return {
    t: status.fetched_at * 1000,
    rtt: status.rtt_ms,
    botMemory: status.service?.memory_bytes ?? null,
    hostMemoryRatio: memory && memory.total > 0 ? memory.used / memory.total : null,
    load1: status.resources?.load?.[0] ?? null,
  };
}

type Setter<T> = Dispatch<SetStateAction<T>>;

export function useConsolePolling({
  requestScope, connectionChanging, wasConnected, applyConnection, connected, autoRefresh, connectionKey,
  setBackendError, setStatus, setStatusError, setStatusErrorSafe, setRefreshing, setSamples, setInsights,
  setInsightsFetchedAt, setInsightsError, setInsightsErrorSafe,
}: Scope & {
  applyConnection: (value: ConnectionState) => void; connected: boolean; autoRefresh: boolean; connectionKey: string;
  setBackendError: Setter<string | null>; setStatus: Setter<StatusResponse | null>;
  setStatusError: Setter<string | null>; setStatusErrorSafe: Setter<string | null>; setRefreshing: Setter<boolean>; setSamples: Setter<Sample[]>;
  setInsights: Setter<Insights | null>; setInsightsFetchedAt: Setter<number | null>; setInsightsError: Setter<string | null>;
  setInsightsErrorSafe: Setter<string | null>;
}) {
  const refreshConnection = useCallback(async () => {
    if (connectionChanging.current) return;
    const isCurrent = requestScope.current.start("connection");
    try {
      const next = await api.connection();
      if (!isCurrent() || connectionChanging.current) return;
      applyConnection(next);
      setBackendError(null);
    } catch (error) {
      if (!isCurrent() || connectionChanging.current) return;
      setBackendError(errorMessage(error));
    }
  }, [applyConnection]);
  usePolling(refreshConnection, CONNECTION_INTERVAL_MS, true);

  const refreshStatus = useCallback(async () => {
    if (!wasConnected.current || connectionChanging.current) return;
    const isCurrent = requestScope.current.start("status");
    setRefreshing(true);
    try {
      const next = await api.status();
      if (!isCurrent()) return;
      setStatus(next);
      setStatusError(null);
      setStatusErrorSafe(null);
      setSamples((previous) => [...previous, toSample(next)].slice(-MAX_SAMPLES));
    } catch (error) {
      if (!isCurrent()) return;
      setStatusError(errorMessage(error));
      setStatusErrorSafe(errorSafeMessage(error));
      if (error instanceof ApiError && (error.status === 409 || error.status === 502)) void refreshConnection();
    } finally {
      if (isCurrent()) setRefreshing(false);
    }
  }, [refreshConnection]);
  usePolling(refreshStatus, STATUS_INTERVAL_MS, connected && autoRefresh, connectionKey);
  const refreshInsights = useCallback(async (force = false) => {
    if (!wasConnected.current || connectionChanging.current) return;
    const isCurrent = requestScope.current.start("insights");
    try {
      const result = await api.insights(force);
      if (!isCurrent()) return;
      setInsights(result.data);
      setInsightsFetchedAt(result.fetched_at);
      setInsightsError(null);
      setInsightsErrorSafe(null);
    } catch (error) {
      if (!isCurrent()) return;
      setInsightsError(errorMessage(error));
      setInsightsErrorSafe(errorSafeMessage(error));
    }
  }, []);
  usePolling(refreshInsights, INSIGHTS_INTERVAL_MS, connected && autoRefresh, connectionKey);

  useEffect(() => {
    // 自動更新を切っていても、接続した直後に1回は取る
    if (connected && !autoRefresh) {
      void refreshStatus();
      void refreshInsights();
    }
    // 接続が変わったときだけ動かす（自動更新の切り替えでは取り直さない）
  }, [connectionKey]);

  return { refreshConnection, refreshStatus, refreshInsights };
}
