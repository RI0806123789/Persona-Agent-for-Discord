// 確認内容は描画時の伏せ字設定を使う。
import { Icon } from "./components/Icon";
import type { ConfirmRequest } from "./components/overlays";
import { DEPLOY_PLAN } from "./deployPlan";
import { ACTION_LABELS } from "./status";
import type { Profile, ServiceAction } from "./types";

const ACTION_EFFECTS: Record<ServiceAction, string> = {
  start: "ボットを起動します。ログイン成功のログが出れば復帰です。",
  stop: "ボットを止めます。Discord では応答しなくなり、自分で起動するまで戻りません。",
  restart: "ボットを再起動します。数秒間、Discord で応答しなくなります（ボイス接続や自律配信も切れます）。",
};

export function deployConfirmation(profile: Profile, onConfirm: () => void): ConfirmRequest {
  const branch = profile.deploy_branch ?? "main";
  const isProduction = profile.environment === "production";
  return {
      title: (privacy) => `${privacy.mask(branch)} の最新をデプロイしますか？`,
      icon: "rocket",
      body: (privacy) => (
        <>
          <p>次の順に進め、ステップごとに結果を確かめてから次へ進みます。</p>
          <ol className="dialog-steps">
            {DEPLOY_PLAN.map((step) => (
              <li key={step.title}>{step.confirmText({ branch: privacy.mask(branch), service: privacy.redact(profile.service) })}</li>
            ))}
          </ol>
          <p>途中で失敗したら、自動で前の版へ戻します（bot.db は戻しません）。再起動の間、Discord で応答しなくなります。</p>
          <p className="dialog-target">
            接続先: <strong>{privacy.mask(profile.label)}</strong>
          </p>
          {isProduction && (
            <p className="dialog-warning">
              <Icon name="alert" size={20} filled />
              本番環境です。Discord の利用者全員に影響します。
            </p>
          )}
        </>
      ),
      confirmLabel: "デプロイする",
      tone: isProduction ? "danger" : "primary",
      requireText: isProduction ? profile.service : undefined,
      onConfirm,
  };
}

export function serviceConfirmation(profile: Profile, action: ServiceAction, onConfirm: () => void): ConfirmRequest {
  const isProduction = profile.environment === "production";
  return {
        title: `${profile.service} を${ACTION_LABELS[action]}しますか？`,
        icon: action === "stop" ? "stop" : action === "start" ? "play" : "restart",
        body: (privacy) => (
          <>
            <p>{ACTION_EFFECTS[action]}</p>
            <p className="dialog-target">
              接続先: <strong>{privacy.mask(profile.label)}</strong>
            </p>
            {isProduction && (
              <p className="dialog-warning">
                <Icon name="alert" size={20} filled />
                本番環境です。Discord の利用者全員に影響します。
              </p>
            )}
          </>
        ),
        confirmLabel: `${ACTION_LABELS[action]}する`,
        tone: action === "stop" || (isProduction && action === "restart") ? "danger" : "primary",
        requireText: action === "stop" ? profile.service : undefined,
        onConfirm,
  };
}
