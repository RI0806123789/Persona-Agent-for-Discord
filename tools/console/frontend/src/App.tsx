import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, errorMessage } from "./api";
import { Icon } from "./components/Icon";
import { ConfirmDialog, ShortcutHelp, type ConfirmRequest } from "./components/overlays";
import { CompactFab, ModalNavigationRail, NavigationBar, NavigationRail, type FabAction } from "./components/Sidebar";
import { ToastProvider, ToastViewport, useToast } from "./components/toast";
import { TopAppBar, type ThemeChoice } from "./components/TopBar";
import { Kbd } from "./components/ui";
import { useConsoleShortcuts } from "./useConsoleShortcuts";
import { deployConfirmation, serviceConfirmation } from "./confirmations";
import { useMediaQuery, useNow, useStoredState } from "./hooks";
import { useLogStream } from "./useLogStream";
import { PAGES, isPageId, type PageId } from "./navigation";
import { ConnectionPage } from "./pages/ConnectionPage";
import { DataPage } from "./pages/DataPage";
import { GuildsPage } from "./pages/GuildsPage";
import { LogsPage } from "./pages/LogsPage";
import { OperationsPage } from "./pages/OperationsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PrivacyContext, collectSecrets, createPrivacy } from "./privacy";
import { useConnectionScope, useConsolePolling, useDeployController, useServiceController } from "./controllers";
import { unsetFieldsOf, unsetFieldsText } from "./profiles";
import { SEED_OPTIONS, applyTheme } from "./theme/theme";
import { withViewTransition } from "./viewTransition";
import type { ConnectionState, Insights, Operation, Profile, Sample, ServiceAction, StatusResponse } from "./types";

// M3 のウィンドウサイズの区分（600 未満 = compact、1200 以上 = large）
const LARGE_QUERY = "(min-width: 1200px)";
const COMPACT_QUERY = "(max-width: 599px)";


export default function App() {
  return (
    <ToastProvider>
      <Console />
    </ToastProvider>
  );
}


export function Console() {
  const notify = useToast();
  const now = useNow();

  // ---- 表示の設定（ブラウザに覚えておく）
  const [storedPage, setStoredPage] = useStoredState<string>("pbc.page", "overview");
  const page: PageId = isPageId(storedPage) ? storedPage : "overview";
  const [themeChoice, setThemeChoice] = useStoredState<ThemeChoice>("pbc.theme", null);
  const systemDark = useMediaQuery("(prefers-color-scheme: dark)");
  const theme = themeChoice ?? (systemDark ? "dark" : "light");
  const [seedId, setSeedId] = useStoredState("pbc.seed", SEED_OPTIONS[0].id);
  const [railExpanded, setRailExpanded] = useStoredState("pbc.railExpanded", true);
  const [privacyOn, setPrivacyOn] = useStoredState("pbc.privacy", false);
  const [autoRefresh, setAutoRefresh] = useStoredState("pbc.autoRefresh", true);
  const [selectedProfileId, setSelectedProfileId] = useStoredState("pbc.profile", "");
  const large = useMediaQuery(LARGE_QUERY);
  const compact = useMediaQuery(COMPACT_QUERY);

  // ---- サーバーから取る状態
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [profilesSource, setProfilesSource] = useState("");
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [connection, setConnection] = useState<ConnectionState | null>(null);
  const [connecting, setConnecting] = useState(false);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [statusErrorSafe, setStatusErrorSafe] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [samples, setSamples] = useState<Sample[]>([]);
  const [insights, setInsights] = useState<Insights | null>(null);
  const [insightsFetchedAt, setInsightsFetchedAt] = useState<number | null>(null);
  const [insightsError, setInsightsError] = useState<string | null>(null);
  const [insightsErrorSafe, setInsightsErrorSafe] = useState<string | null>(null);
  const [operations, setOperations] = useState<Operation[]>([]);

  // ---- 画面の部品
  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const [modalRailOpen, setModalRailOpen] = useState(false);
  const [refreshToken, setRefreshToken] = useState(0);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const contentRef = useRef<HTMLElement>(null);
  const { requestScope, connectionChanging, wasConnected, dataConnectionKey, connectionProfileId } = useConnectionScope();

  const connected = connection?.connected ?? false;
  const connectionKey = `${connection?.profile?.id ?? ""}:${connection?.connected_at ?? ""}`;
  const stream = useLogStream(connected, 300, connectionKey);
  useEffect(() => {
    if (page !== "logs") stream.setPaused(false);
  }, [page, stream.setPaused]);
  const selectedProfile = profiles.find((p) => p.id === selectedProfileId) ?? null;
  const pageDef = PAGES.find((p) => p.id === page) ?? PAGES[0];
  const production = connected && connection?.profile?.environment === "production";

  // ---- 伏せ字モード。画面が知っているホスト名・ボット名・サーバー名などを集め、文の中からも隠す
  // データページでテーブルの中身を開くと、そこに出てきた利用者の表示名も加える（接続先が変わったら捨てる）
  const [dbUserNames, setDbUserNames] = useState<string[]>([]);
  const addDbUserNames = useCallback((names: string[]) => {
    setDbUserNames((current) => {
      const added = names.filter((name) => !current.includes(name));
      return added.length ? [...current, ...added] : current;
    });
  }, []);
  useEffect(() => {
    setDbUserNames([]);
  }, [connectionKey]);
  const secrets = useMemo(
    () => collectSecrets({ profile: connection?.profile ?? null, profiles, connection, insights, git: status?.git, userNames: dbUserNames }),
    [connection, profiles, insights, status?.git, dbUserNames],
  );
  const privacy = useMemo(() => createPrivacy(privacyOn, secrets), [privacyOn, secrets]);

  useEffect(() => {
    if (themeChoice) document.documentElement.dataset.theme = themeChoice;
    else delete document.documentElement.dataset.theme;
  }, [themeChoice]);

  useEffect(() => {
    applyTheme((SEED_OPTIONS.find((seed) => seed.id === seedId) ?? SEED_OPTIONS[0]).hex);
  }, [seedId]);

  // 広い画面になったら、重ねて開いていたレールは常設のレールに置き換わるので閉じる
  useEffect(() => {
    if (large) setModalRailOpen(false);
  }, [large]);

  const navigate = useCallback(
    (next: PageId) => {
      if (next === page) {
        contentRef.current?.scrollTo({ top: 0 });
        return;
      }
      // 前のページを薄く消しながら、新しいページのカードを順に浮かび上がらせる（styles.css の「ビュー遷移」）
      withViewTransition("page", () => {
        setStoredPage(next);
        contentRef.current?.scrollTo({ top: 0, behavior: "instant" });
      });
    },
    [page, setStoredPage],
  );

  const toggleRail = useCallback(() => {
    if (large) setRailExpanded((value) => !value);
    else setModalRailOpen((value) => !value);
  }, [large, setRailExpanded]);

  // ---- 接続状態
  const applyConnection = useCallback(
    (state: ConnectionState) => {
      const nextKey = `${state.profile?.id ?? ""}:${state.connected_at ?? ""}`;
      if (wasConnected.current && !state.connected && state.last_error) {
        notify({ tone: "critical", title: "SSH 接続が切れました", detail: state.last_error, detailSafe: state.last_error_safe ?? undefined });
      }
      if (!state.connected || dataConnectionKey.current !== nextKey) {
        requestScope.current.invalidate();
        setConfirmOpen(false);
        setDeploy(null);
        setDeployRunning(false);
        deployWasRunning.current = false;
        setStatus(null);
        setSamples([]);
        setInsights(null);
        setInsightsFetchedAt(null);
        setRefreshing(false);
        setStatusError(null);
        setStatusErrorSafe(null);
        setInsightsError(null);
        setInsightsErrorSafe(null);
      }
      connectionProfileId.current = state.profile?.id ?? null;
      dataConnectionKey.current = nextKey;
      wasConnected.current = state.connected;
      setConnection(state);
    },
    [notify],
  );

  const { refreshConnection, refreshStatus, refreshInsights } = useConsolePolling({
    requestScope, connectionChanging, wasConnected, applyConnection, connected, autoRefresh, connectionKey,
    setBackendError, setStatus, setStatusError, setStatusErrorSafe, setRefreshing, setSamples, setInsights,
    setInsightsFetchedAt, setInsightsError, setInsightsErrorSafe,
  });

  // 接続中は、選択欄を「実際に接続している接続先」に合わせる（別のタブや再読み込みで食い違わないように）
  const connectedProfileId = connected ? (connection?.profile?.id ?? null) : null;
  useEffect(() => {
    if (connectedProfileId) setSelectedProfileId(connectedProfileId);
  }, [connectedProfileId, setSelectedProfileId]);

  useEffect(() => {
    api
      .profiles()
      .then((result) => {
        setProfiles(result.profiles);
        setProfilesSource(result.source);
        setProfilesError(null);
        setSelectedProfileId((current) =>
          result.profiles.some((p) => p.id === current) ? current : (result.profiles[0]?.id ?? ""),
        );
      })
      .catch((error: unknown) => setProfilesError(errorMessage(error)));
  }, [setSelectedProfileId]);

  // ---- 状態の定期取得
  const refreshOperations = useCallback(async () => {
    try {
      setOperations((await api.operations()).operations);
    } catch {
      // 記録は補助情報なので、取れなくても画面は止めない
    }
  }, []);
  useEffect(() => {
    void refreshOperations();
  }, [refreshOperations, page]);

  const refreshAfterChange = useCallback(() => {
    void refreshStatus();
    void refreshInsights(true);
  }, [refreshStatus, refreshInsights]);
  const { deploy, deployRunning, deployWasRunning, setDeploy, setDeployRunning, refreshDeploy, runDeploy } = useDeployController(
    { requestScope, wasConnected, connectionChanging, connectionProfileId }, notify, refreshAfterChange, connected, connectionKey,
  );
  const { busyAction, runAction } = useServiceController(
    { requestScope, wasConnected, connectionChanging }, notify, refreshOperations, refreshAfterChange,
  );
  const stateUnknown = connection?.state_unknown === true || deploy?.status === "unknown";

  const requestDeploy = useCallback(() => {
    const profile = connection?.profile;
    if (!profile || deployRunning || stateUnknown || busyAction || connection?.connected_at == null) return;
    const target = { profile_id: profile.id, connected_at: connection.connected_at };
    const isCurrent = requestScope.current.current();
    setConfirm(deployConfirmation(profile, () => { if (isCurrent()) void runDeploy(target); }));
    setConfirmOpen(true);
  }, [connection, deployRunning, stateUnknown, busyAction, runDeploy]);

  // ---- 接続・切断
  const handleConnect = useCallback(async () => {
    if (!selectedProfileId) return;
    // 見本の値のままの接続先は、バックエンドも断るが、要求を送る前に直し方を案内する
    const target = profiles.find((p) => p.id === selectedProfileId);
    const unset = unsetFieldsOf(target);
    if (target && unset.length > 0) {
      notify({
        tone: "critical",
        title: `「${target.label}」はまだ設定されていません`,
        // 見本（Git に含まれる）へ実際の接続先を書かせないよう、見本を読んでいるときはコピーから案内する
        detail:
          profilesSource === "profiles.json"
            ? `backend/profiles.json の ${unsetFieldsText(unset)}を書き換えてください。手順は tools/console/README.md にあります。`
            : `backend/profiles.example.json を backend/profiles.json にコピーし、${unsetFieldsText(unset)}を書き換えてください。手順は tools/console/README.md にあります。`,
      });
      return;
    }
    if (connectionChanging.current) return;
    connectionChanging.current = true;
    requestScope.current.invalidate();
    setConnecting(true);
    try {
      const state = await api.connect(selectedProfileId);
      applyConnection(state);
      notify({ tone: "good", title: "SSH で接続しました", detail: state.profile?.label });
    } catch (error) {
      notify({
        tone: "critical",
        title: "接続できませんでした",
        detail: errorMessage(error),
        detailSafe: error instanceof ApiError ? error.safeMessage : undefined,
        action: { label: "再試行", onClick: () => void handleConnectRef.current() },
      });
    } finally {
      connectionChanging.current = false;
      setConnecting(false);
      void refreshConnection();
    }
  }, [selectedProfileId, profiles, profilesSource, applyConnection, notify, refreshConnection]);
  // 通知の「再試行」からは、その時点の最新の接続処理を呼ぶ
  const handleConnectRef = useRef(handleConnect);
  handleConnectRef.current = handleConnect;

  const handleDisconnect = useCallback(async () => {
    if (connectionChanging.current) return;
    connectionChanging.current = true;
    requestScope.current.invalidate();
    try {
      const state = await api.disconnect();
      applyConnection(state);
      notify({ tone: "info", title: "切断しました" });
    } catch (error) {
      notify({
        tone: "critical",
        title: "切断できませんでした",
        detail: errorMessage(error),
        detailSafe: error instanceof ApiError ? error.safeMessage : undefined,
      });
    } finally {
      connectionChanging.current = false;
      void refreshConnection();
    }
  }, [applyConnection, notify, refreshConnection]);

  const requestAction = useCallback(
    (action: ServiceAction) => {
      const profile = connection?.profile;
      if (!profile || busyAction || connection?.connected_at == null) return;
      const target = { profile_id: profile.id, connected_at: connection.connected_at };
      const isCurrent = requestScope.current.current();
      if (deployRunning || stateUnknown) {
        const unknown = stateUnknown;
        notify({
          tone: unknown ? "critical" : "info",
          title: unknown ? "デプロイの状態不明のためサービス操作を停止しています" : "デプロイを実行中です",
          detail: unknown ? "概要・ログ・DB閲覧と再接続は利用できます。サービス操作のページで対処手順を確認してください。" : "終わってから操作してください。",
        });
        return;
      }
      setConfirm(serviceConfirmation(profile, action, () => { if (isCurrent()) void runAction(action, target); }));
      setConfirmOpen(true);
    },
    [connection, busyAction, deployRunning, stateUnknown, notify, runAction],
  );

  const manualRefresh = useCallback(() => {
    if (!connected) return;
    void refreshStatus();
    void refreshInsights(true);
    void refreshDeploy();
    setRefreshToken((n) => n + 1);
  }, [connected, refreshStatus, refreshInsights, refreshDeploy]);

  // テーマ・配色・伏せ字の切り替えは、画面全体を写しから溶け込ませる（一瞬で色や文字が変わらないように）
  const changeTheme = useCallback(
    (choice: ThemeChoice) =>
      withViewTransition("theme", () => {
        // 新しい画面を写す前に色を変える必要があるので、data-theme は useEffect を待たずにここでも書く
        if (choice) document.documentElement.dataset.theme = choice;
        else delete document.documentElement.dataset.theme;
        setThemeChoice(choice);
      }),
    [setThemeChoice],
  );
  const toggleTheme = useCallback(() => changeTheme(theme === "dark" ? "light" : "dark"), [theme, changeTheme]);
  const changeSeed = useCallback(
    (id: string) =>
      withViewTransition("theme", () => {
        applyTheme((SEED_OPTIONS.find((seed) => seed.id === id) ?? SEED_OPTIONS[0]).hex);
        setSeedId(id);
      }),
    [setSeedId],
  );
  const togglePrivacy = useCallback(() => withViewTransition("privacy", () => setPrivacyOn((value) => !value)), [setPrivacyOn]);

  const goPending = useConsoleShortcuts({
    page, connected, paused: stream.paused, setPaused: stream.setPaused,
    confirmOpen, setConfirmOpen, helpOpen, setHelpOpen, modalRailOpen, setModalRailOpen,
    setAutoRefresh, filterInputRef, navigate, manualRefresh, toggleRail, togglePrivacy, toggleTheme,
  });

  // FAB には、その時の主な操作を置く（未接続なら接続、接続中なら更新）
  const fab: FabAction = connected
    ? { label: "今すぐ更新", icon: "refresh", onClick: manualRefresh, disabled: refreshing, busy: false }
    : { label: "接続", icon: "plug", onClick: () => void handleConnect(), disabled: connecting || !selectedProfileId, busy: connecting };

  const railProps = {
    page,
    onNavigate: navigate,
    profile: connection?.profile ?? selectedProfile,
    connected,
    fab,
    onHelp: () => setHelpOpen(true),
  };

  return (
    <PrivacyContext.Provider value={privacy}>
      <div className={`app ${production ? "is-production" : ""} ${compact ? "is-compact" : ""}`}>
        {!compact && <NavigationRail {...railProps} expanded={large && railExpanded} onMenu={toggleRail} />}
        <div className="main">
          <TopAppBar
            page={pageDef}
            connection={connection}
            profiles={profiles}
            profilesSource={profilesSource}
            selectedProfileId={selectedProfileId}
            onSelectProfile={setSelectedProfileId}
            onConnect={() => void handleConnect()}
            onDisconnect={() => void handleDisconnect()}
            connecting={connecting}
            rttMs={status?.rtt_ms ?? null}
            lastUpdated={status ? status.fetched_at * 1000 : null}
            refreshing={refreshing}
            onRefresh={manualRefresh}
            autoRefresh={autoRefresh}
            onToggleAuto={() => setAutoRefresh((value) => !value)}
            onTogglePrivacy={togglePrivacy}
            themeChoice={themeChoice}
            onSelectTheme={changeTheme}
            seedId={seedId}
            onSelectSeed={changeSeed}
            onOpenConnection={() => navigate("connection")}
            showMenuButton={compact}
            onMenu={() => setModalRailOpen(true)}
            now={now}
          />
          {backendError && (
            <div className="m3-banner is-error" role="alert">
              <Icon name="error" size={20} filled />
              <span className="body-medium">{privacy.redact(backendError)}</span>
            </div>
          )}
          {stateUnknown && (
            <div className="m3-banner is-error" role="alert">
              <Icon name="error" size={20} filled />
              <span className="body-medium">
                遠隔処理の終了が未確認で、状態不明です。このセッション全体でデプロイ・巻き戻し・サービスの起動・停止・再起動を停止しています。
                概要・ログ・DB閲覧とSSHの再接続・切断は利用できます。接続先を変えても解除されません。
                状態不明になったサーバーで残留処理の終了とコード・依存関係・サービスの状態を確認してから、バックエンドを起動し直してください。
              </span>
            </div>
          )}
          {production && (
            <div className="m3-banner is-production">
              <Icon name="shield" size={20} filled />
              <span className="body-medium">本番サーバーに接続中です。操作は Discord の利用者に影響します。</span>
            </div>
          )}
          <main className="content" ref={contentRef}>
            <div key={page} className="page">
              {page === "overview" && (
                <OverviewPage
                  connected={connected}
                  connecting={connecting}
                  status={status}
                  statusError={statusError}
                  statusErrorSafe={statusErrorSafe}
                  samples={samples}
                  connection={connection}
                  logs={stream.entries}
                  insights={insights}
                  insightsFetchedAt={insightsFetchedAt}
                  insightsError={insightsError}
                  insightsErrorSafe={insightsErrorSafe}
                  onNavigate={navigate}
                  onRequestAction={requestAction}
                  busyAction={busyAction}
                  deployRunning={deployRunning}
                  deployUnknown={stateUnknown}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  now={now}
                />
              )}
              {page === "logs" && (
                <LogsPage
                  stream={stream}
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  filterInputRef={filterInputRef}
                />
              )}
              {page === "operations" && (
                <OperationsPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  profile={connection?.profile ?? null}
                  service={status?.service ?? null}
                  busyAction={busyAction}
                  onRequestAction={requestAction}
                  operations={operations}
                  git={status?.git ?? null}
                  deploy={deploy}
                  deployRunning={deployRunning}
                  stateUnknown={stateUnknown}
                  onDeploy={requestDeploy}
                  now={now}
                />
              )}
              {page === "data" && (
                <DataPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  refreshToken={refreshToken}
                  connectionKey={connectionKey}
                  onNames={addDbUserNames}
                />
              )}
              {page === "guilds" && (
                <GuildsPage
                  connected={connected}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  selectedProfile={selectedProfile}
                  insights={insights}
                  insightsFetchedAt={insightsFetchedAt}
                  insightsError={insightsError}
                  insightsErrorSafe={insightsErrorSafe}
                  now={now}
                />
              )}
              {page === "connection" && (
                <ConnectionPage
                  profiles={profiles}
                  profilesSource={profilesSource}
                  profilesError={profilesError}
                  selectedProfileId={selectedProfileId}
                  onSelectProfile={setSelectedProfileId}
                  connection={connection}
                  connecting={connecting}
                  onConnect={() => void handleConnect()}
                  onDisconnect={() => void handleDisconnect()}
                  now={now}
                />
              )}
            </div>
          </main>
        </div>
        {compact && (
          <>
            <CompactFab fab={fab} />
            <NavigationBar page={page} onNavigate={navigate} />
          </>
        )}
        {!large && <ModalNavigationRail {...railProps} open={modalRailOpen} onClose={() => setModalRailOpen(false)} />}
        <GoKeyHint visible={goPending} />
        <ConfirmDialog request={confirm} open={confirmOpen} onClose={() => setConfirmOpen(false)} />
        <ShortcutHelp open={helpOpen} onClose={() => setHelpOpen(false)} />
      </div>
      <ToastViewport />
    </PrivacyContext.Provider>
  );
}

/** g を押して次のキーを待っている間の表示（Vim の showcmd に当たる）。 */
function GoKeyHint({ visible }: { visible: boolean }) {
  return (
    <div className={`go-key-hint ${visible ? "is-visible" : ""}`} aria-live="polite" role="status">
      {visible && (
        <>
          <Kbd>g</Kbd>
          <span className="body-medium">
            {PAGES.map((p) => (
              <span key={p.id} className="go-key-option">
                <Kbd>{p.goKey}</Kbd> {p.short}
              </span>
            ))}
          </span>
        </>
      )}
    </div>
  );
}
