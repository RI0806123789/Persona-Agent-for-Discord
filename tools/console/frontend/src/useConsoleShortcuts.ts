import { useEffect, useRef, useState, type Dispatch, type RefObject, type SetStateAction } from "react";
import { PAGES, type PageId } from "./navigation";

type Options = {
  page: PageId; connected: boolean; paused: boolean; setPaused: (value: boolean) => void;
  confirmOpen: boolean; setConfirmOpen: Dispatch<SetStateAction<boolean>>;
  helpOpen: boolean; setHelpOpen: Dispatch<SetStateAction<boolean>>;
  modalRailOpen: boolean; setModalRailOpen: Dispatch<SetStateAction<boolean>>;
  setAutoRefresh: Dispatch<SetStateAction<boolean>>;
  filterInputRef: RefObject<HTMLInputElement | null>;
  navigate: (page: PageId) => void; manualRefresh: () => void;
  toggleRail: () => void; togglePrivacy: () => void; toggleTheme: () => void;
};

const GO_KEY_TIMEOUT_MS = 1200; // g を押してから次のキーを待つ時間

/** 入力中は1文字のショートカットを効かせない。 */
function isTyping(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return !!element && (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.tagName === "SELECT" || element.isContentEditable);
}

export function useConsoleShortcuts({
  page, connected, paused, setPaused, confirmOpen, setConfirmOpen, helpOpen, setHelpOpen,
  modalRailOpen, setModalRailOpen, setAutoRefresh, filterInputRef,
  navigate, manualRefresh, toggleRail, togglePrivacy, toggleTheme,
}: Options) {
  const [goPending, setGoPending] = useState(false);
  // ---- キーボード操作（Vim 風）。最新の処理を ref 経由で呼ぶので、登録は1回だけ
  const keyHandler = useRef<(event: KeyboardEvent) => void>(() => undefined);
  const goTimer = useRef<number | undefined>(undefined);
  const focusTimer = useRef<number | undefined>(undefined);
  const cancelGo = () => {
    window.clearTimeout(goTimer.current);
    setGoPending(false);
  };
  keyHandler.current = (event: KeyboardEvent) => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
    const target = event.target as HTMLElement | null;
    // メニューの中のキーはメニューが扱う（↓↑ j k など）
    if (target?.closest?.('[role="menu"]')) return;
    const typing = isTyping(target);

    if (event.key === "Escape") {
      if (goPending) cancelGo();
      else if (confirmOpen) setConfirmOpen(false);
      else if (helpOpen) setHelpOpen(false);
      else if (modalRailOpen) setModalRailOpen(false);
      else if (typing) target?.blur();
      return;
    }
    if (typing || confirmOpen) return;
    if (helpOpen) {
      if (event.key === "?") setHelpOpen(false);
      return;
    }

    if (modalRailOpen && !goPending && !/^[1-6jk\[]$/.test(event.key)) return;

    if (goPending) {
      cancelGo();
      const destination = PAGES.find((p) => p.goKey === event.key);
      if (destination) {
        event.preventDefault();
        navigate(destination.id);
        setModalRailOpen(false);
      }
      return;
    }

    const index = PAGES.findIndex((p) => p.id === page);
    if (/^[1-6]$/.test(event.key)) {
      navigate(PAGES[Number(event.key) - 1].id);
      setModalRailOpen(false);
      return;
    }
    switch (event.key) {
      case "g":
        setGoPending(true);
        window.clearTimeout(goTimer.current);
        goTimer.current = window.setTimeout(() => setGoPending(false), GO_KEY_TIMEOUT_MS);
        break;
      case "j":
        setModalRailOpen(false);
        navigate(PAGES[(index + 1) % PAGES.length].id);
        break;
      case "k":
        setModalRailOpen(false);
        navigate(PAGES[(index - 1 + PAGES.length) % PAGES.length].id);
        break;
      case "r":
        manualRefresh();
        break;
      case "a":
        setAutoRefresh((value) => !value);
        break;
      case "/":
        event.preventDefault();
        setModalRailOpen(false);
        navigate("logs");
        window.clearTimeout(focusTimer.current);
        focusTimer.current = window.setTimeout(() => filterInputRef.current?.focus(), 60);
        break;
      case "p":
        if (connected) if (page === "logs") setPaused(!paused);
        break;
      case "[":
        toggleRail();
        break;
      case "h":
        togglePrivacy();
        break;
      case "t":
        toggleTheme();
        break;
      case "?":
        setModalRailOpen(false);
        setHelpOpen(true);
        break;
    }
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyHandler.current(event);
    window.addEventListener("keydown", listener);
    return () => {
      window.removeEventListener("keydown", listener);
      window.clearTimeout(goTimer.current);
      window.clearTimeout(focusTimer.current);
    };
  }, []);

  return goPending;
}
