import { useCallback, useEffect, useRef, useState } from "react";
import { logStreamUrl } from "./api";
import { appendLogEntries } from "./logs";
import { createLogPrivacyTracker } from "./logPrivacy";
import type { LogEntry } from "./types";

const FLUSH_INTERVAL_MS = 120;

export type StreamStatus = "idle" | "connecting" | "live" | "ended" | "error";

export interface LogStream {
  entries: LogEntry[];
  status: StreamStatus;
  message: string | null;
  messageSafe: string | null;
  paused: boolean;
  pendingCount: number;
  setPaused: (paused: boolean) => void;
  clear: () => void;
  retry: () => void;
}

type StreamMessage =
  | { type: "entry"; entry: LogEntry }
  | { type: "error"; message: string; message_safe?: string | null }
  | { type: "end"; message: string; message_safe?: string | null };

/**
 * journalctl -f をバックエンド経由（WebSocket）で受け取り続ける。
 * 1件ごとに再描画すると重いので、FLUSH_INTERVAL_MS ごとにまとめて反映する。
 */
export function useLogStream(enabled: boolean, initialLines = 300, connectionKey = ""): LogStream {
  const [entries, setEntries] = useState<LogEntry[]>([]);
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [messageSafe, setMessageSafe] = useState<string | null>(null);
  const [paused, setPausedState] = useState(false);
  const [pendingCount, setPendingCount] = useState(0);
  const [reconnectToken, setReconnectToken] = useState(0);
  const bufferRef = useRef<LogEntry[]>([]); // 次の反映で追加する分
  const heldRef = useRef<LogEntry[]>([]); // 一時停止中にたまった分
  const heldCountRef = useRef(0); // 古い行を落としても、新着件数は数え続ける
  const pausedRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      return;
    }
    let disposed = false;
    let terminalMessage = false;
    const classifyPrivacy = createLogPrivacyTracker();
    bufferRef.current = [];
    heldRef.current = [];
    heldCountRef.current = 0;
    pausedRef.current = false;
    setPausedState(false);
    setEntries([]);
    setPendingCount(0);
    setStatus("connecting");
    setMessage(null);
    setMessageSafe(null);

    const fail = (reason: string) => {
      if (disposed || terminalMessage) return;
      terminalMessage = true;
      setStatus("error");
      setMessage(reason);
      setMessageSafe(null);
    };
    let socket: WebSocket;
    try {
      socket = new WebSocket(logStreamUrl(initialLines));
    } catch {
      fail("ログに接続できませんでした。接続先の設定を確認してから再試行してください。");
      return () => { disposed = true; };
    }
    socket.onopen = () => {
      if (!disposed && !terminalMessage) setStatus("live");
    };
    socket.onmessage = (event: MessageEvent<string>) => {
      if (disposed || terminalMessage) return;
      let data: StreamMessage;
      try {
        data = JSON.parse(event.data) as StreamMessage;
        if (!data || !["entry", "error", "end"].includes(data.type)) throw new Error();
        if (data.type === "entry") {
          const entry = data.entry;
          if (!entry || !Number.isFinite(entry.ts) || typeof entry.message !== "string"
              || typeof entry.source !== "string"
              || (entry.priority != null && !Number.isFinite(entry.priority))
              || (entry.pid != null && !Number.isFinite(entry.pid))) throw new Error();
        } else if (typeof data.message !== "string") throw new Error();
      } catch {
        fail("ログの応答を読み取れませんでした。再試行してください。");
        socket.close();
        return;
      }
      if (data.type === "entry") {
        // サーバーからの同名属性は使わず、保持上限・pause・clearより前に判定する。
        const entry = classifyPrivacy(data.entry);
        if (pausedRef.current) {
          heldRef.current = appendLogEntries(heldRef.current, [entry]);
          heldCountRef.current += 1;
        } else {
          bufferRef.current = appendLogEntries(bufferRef.current, [entry]);
        }
      } else {
        terminalMessage = true;
        setStatus(data.type === "error" ? "error" : "ended");
        setMessage(data.message);
        setMessageSafe(typeof data.message_safe === "string" ? data.message_safe : null);
      }
    };
    socket.onerror = () => {
      fail("ログの通信に失敗しました。接続元やサーバーの状態を確認してから再試行してください。");
    };
    socket.onclose = (event: CloseEvent) => {
      if (disposed || terminalMessage) return;
      if (event.code !== 1000) {
        fail(`ログの接続が切れました（コード ${event.code}）。接続元やサーバーの状態を確認してから再試行してください。`);
      } else {
        terminalMessage = true;
        setStatus("ended");
        setMessage("ログの受信が終了しました。再開するには再試行してください。");
        setMessageSafe(null);
      }
    };

    const flushTimer = window.setInterval(() => {
      if (bufferRef.current.length > 0) {
        const batch = bufferRef.current;
        bufferRef.current = [];
        setEntries((previous) => appendLogEntries(previous, batch));
      }
      if (pausedRef.current) setPendingCount(heldCountRef.current);
    }, FLUSH_INTERVAL_MS);

    return () => {
      disposed = true;
      window.clearInterval(flushTimer);
      const batch = bufferRef.current;
      bufferRef.current = [];
      if (batch.length > 0) setEntries((previous) => appendLogEntries(previous, batch));
      socket.close();
    };
  }, [enabled, initialLines, reconnectToken, connectionKey]);

  const setPaused = useCallback((next: boolean) => {
    if (next && !pausedRef.current) {
      heldRef.current = appendLogEntries(heldRef.current, bufferRef.current);
      heldCountRef.current += bufferRef.current.length;
      bufferRef.current = [];
    }
    pausedRef.current = next;
    setPausedState(next);
    if (!next) {
      const batch = appendLogEntries(bufferRef.current, heldRef.current);
      bufferRef.current = [];
      if (batch.length > 0) setEntries((previous) => appendLogEntries(previous, batch));
      heldRef.current = [];
      heldCountRef.current = 0;
      setPendingCount(0);
    }
  }, []);

  const clear = useCallback(() => {
    bufferRef.current = [];
    heldRef.current = [];
    heldCountRef.current = 0;
    setPendingCount(0);
    setEntries([]);
  }, []);

  const retry = useCallback(() => setReconnectToken((n) => n + 1), []);

  return { entries, status, message, messageSafe, paused, pendingCount, setPaused, clear, retry };
}
