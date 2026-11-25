// M3 の基本ダイアログ（角丸 28px・上にアイコン・見出しは headline small）。
// 閉じる動きを見せるため、閉じても DOM に残して中身だけ保持する。
// 開くきっかけのボタンから広がり、閉じるとそこへ縮んで戻る（useEmergeFrom）。
// フォーカスは開いたら中へ移し、Tab は中で循環させ、閉じたら開く前の場所へ戻す（useModalFocus）。
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useEmergeFrom, useModalFocus } from "../hooks";
import { usePrivacy, type Privacy } from "../privacy";
import { Icon, type IconName } from "./Icon";
import { Button, IconButton, Kbd } from "./ui";

export interface ConfirmRequest {
  title: string | ((privacy: Privacy) => string);
  /** 保存時に表示を固定せず、描画のたびに現在の伏せ字設定で組み立てる。 */
  body: (privacy: Privacy) => ReactNode;
  confirmLabel: string;
  tone: "danger" | "primary";
  icon: IconName;
  /** これと同じ文字を入力しないと実行できない（停止など影響の大きい操作用） */
  requireText?: string;
  onConfirm: () => void;
}

export function ConfirmDialog({ request, open, onClose }: { request: ConfirmRequest | null; open: boolean; onClose: () => void }) {
  const privacy = usePrivacy();
  const [typed, setTyped] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  // 最初のフォーカス: 入力が要るなら入力欄、危険な操作ならキャンセル（Enter の押し間違いで実行しないように）
  const initialRef = request?.requireText ? inputRef : request?.tone === "danger" ? cancelRef : confirmRef;
  useModalFocus(open, dialogRef, initialRef);
  useEmergeFrom(open, dialogRef);

  useEffect(() => {
    if (open) setTyped("");
  }, [open, request]);

  const allowed = request !== null && (!request.requireText || typed === request.requireText);

  return (
    <div
      className={`m3-scrim ${open ? "is-open" : ""}`}
      inert={!open}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        className={`m3-dialog ${request?.tone === "danger" ? "is-danger" : ""}`}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-body"
      >
        <span className="m3-dialog-icon">
          <Icon name={request?.icon ?? "info"} size={24} />
        </span>
        <h2 id="confirm-title" className="headline-small">
          {privacy.redact(typeof request?.title === "function" ? request.title(privacy) : request?.title)}
        </h2>
        <div id="confirm-body" className="m3-dialog-body body-medium">
          {request?.body(privacy)}
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (allowed && request) {
              request.onConfirm();
              onClose();
            }
          }}
        >
          {request?.requireText && (
            <label className="m3-text-field">
              <input
                ref={inputRef}
                value={typed}
                onChange={(event) => setTyped(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                placeholder=" "
                aria-describedby="confirm-require"
              />
              <span className="m3-text-field-label">確認のため「{privacy.redact(request.requireText)}」と入力</span>
              <span id="confirm-require" className="m3-text-field-support body-small">
                {typed && !allowed ? "サービス名と一致していません" : "一致すると実行できるようになります"}
              </span>
            </label>
          )}
          <div className="m3-dialog-actions">
            <Button ref={cancelRef} variant="text" onClick={onClose}>
              キャンセル
            </Button>
            <Button ref={confirmRef} type="submit" variant={request?.tone === "danger" ? "danger" : "filled"} disabled={!allowed}>
              {privacy.redact(request?.confirmLabel ?? "実行")}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

// 「~」で始まる要素はキーではなく区切りの文字として表示する
const SHORTCUT_GROUPS: { title: string; items: { keys: string[]; label: string }[] }[] = [
  {
    title: "ページの移動",
    items: [
      { keys: ["1", "~〜", "6"], label: "ページを切り替える" },
      { keys: ["j", "~/", "k"], label: "次 / 前のページへ" },
      { keys: ["g", "~→", "o l s d g c"], label: "概要 / ログ / 操作 / データ / サーバー / 接続 へ移動" },
    ],
  },
  {
    title: "データ",
    items: [
      { keys: ["r"], label: "今すぐ更新（ボットの中の状態もキャッシュを使わず取り直す）" },
      { keys: ["a"], label: "自動更新の ON / OFF" },
      { keys: ["/"], label: "ログを絞り込む" },
      { keys: ["p"], label: "ログのライブ表示を一時停止 / 再開" },
    ],
  },
  {
    title: "表示",
    items: [
      { keys: ["["], label: "ナビゲーションレールを広げる / 折りたたむ（狭い画面では重ねて開く）" },
      { keys: ["h"], label: "伏せ字モード（ホスト名・ホスト鍵・ボット名・サーバー名・チャンネル名・会話を隠す）" },
      { keys: ["t"], label: "ライト / ダークの切り替え" },
      { keys: ["?"], label: "このヘルプ" },
      { keys: ["Esc"], label: "閉じる / 入力欄から抜ける" },
    ],
  },
  {
    title: "メニューの中",
    items: [
      { keys: ["↓", "~/", "↑"], label: "項目を移動（j / k も使える）" },
      { keys: ["Enter"], label: "選ぶ" },
    ],
  },
];

export function ShortcutHelp({ open, onClose }: { open: boolean; onClose: () => void }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useModalFocus(open, dialogRef);
  useEmergeFrom(open, dialogRef);

  return (
    <div
      className={`m3-scrim ${open ? "is-open" : ""}`}
      inert={!open}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div ref={dialogRef} className="m3-dialog m3-dialog-wide" role="dialog" aria-modal="true" aria-labelledby="shortcut-title">
        <header className="m3-dialog-head">
          <span className="m3-dialog-icon">
            <Icon name="keyboard" size={24} />
          </span>
          <h2 id="shortcut-title" className="headline-small">
            キーボードショートカット
          </h2>
          <IconButton icon="close" label="閉じる" onClick={onClose} />
        </header>
        {SHORTCUT_GROUPS.map((group) => (
          <section key={group.title} className="shortcut-group" aria-label={group.title}>
            <h3 className="title-small shortcut-group-title">{group.title}</h3>
            <ul className="shortcut-list">
              {group.items.map((shortcut) => (
                <li key={shortcut.label}>
                  <span className="shortcut-keys">
                    {shortcut.keys.map((key, index) =>
                      key.startsWith("~") ? (
                        <span key={index} className="shortcut-sep">
                          {key.slice(1)}
                        </span>
                      ) : (
                        <Kbd key={index}>{key}</Kbd>
                      ),
                    )}
                  </span>
                  <span className="body-medium">{shortcut.label}</span>
                </li>
              ))}
            </ul>
          </section>
        ))}
      </div>
    </div>
  );
}
