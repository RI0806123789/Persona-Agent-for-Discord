// デプロイ（git pull → 依存の更新 → 再起動 → ヘルスチェック）のカード。
// バックエンドの記録（DeployRecord）をそのまま見せ、1ステップずつ「状態・かかった時間・確かめた事実・
// 実行したコマンドと出力」が分かるようにする。途中で失敗したら、自動の巻き戻しのステップも続けて出る。
import { LoadingIndicator, WavyProgress } from "../components/expressive";
import { Icon, type IconName } from "../components/Icon";
import { Button, Card, Chip, StatusChip } from "../components/ui";
import { formatDateTime } from "../format";
import { redactText, usePrivacy } from "../privacy";
import type { Tone } from "../status";
import { DEPLOY_PLAN } from "../deployPlan";
import type { DeployRecord, DeployStatus, DeployStep, DeployStepStatus, GitInfo, Profile } from "../types";

const STEP_STATUS: Record<DeployStepStatus, { label: string; tone: Tone; icon: IconName }> = {
  pending: { label: "待機", tone: "neutral", icon: "radio" },
  running: { label: "実行中", tone: "info", icon: "timer" },
  succeeded: { label: "成功", tone: "good", icon: "checkCircle" },
  failed: { label: "失敗", tone: "critical", icon: "error" },
  skipped: { label: "スキップ", tone: "neutral", icon: "skip" },
};

const RESULT: Record<DeployStatus, { label: string; tone: Tone; icon: IconName }> = {
  running: { label: "実行中", tone: "info", icon: "timer" },
  succeeded: { label: "デプロイ完了", tone: "good", icon: "checkCircle" },
  up_to_date: { label: "最新でした", tone: "info", icon: "checkCircle" },
  failed: { label: "失敗", tone: "critical", icon: "error" },
  rolled_back: { label: "失敗したため前の版へ戻した", tone: "warning", icon: "undo" },
  rollback_failed: { label: "巻き戻しにも失敗", tone: "critical", icon: "error" },
  unknown: { label: "状態不明（終了未確認）", tone: "critical", icon: "error" },
};

function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
}

function stepDuration(step: DeployStep, now: number): string | null {
  if (!step.started_at) return null;
  const end = step.finished_at ?? now / 1000;
  return formatSeconds(Math.max(0, end - step.started_at));
}

export function DeployCard({
  deploy,
  running,
  stateUnknown,
  connected,
  profile,
  git,
  disabled,
  onDeploy,
  now,
}: {
  deploy: DeployRecord | null;
  running: boolean;
  stateUnknown: boolean;
  connected: boolean;
  profile: Profile | null;
  git: GitInfo | null;
  /** ほかの操作の途中などで、デプロイを始められないとき */
  disabled: boolean;
  onDeploy: () => void;
  now: number;
}) {
  const privacy = usePrivacy();
  const branch = deploy?.branch ?? profile?.deploy_branch ?? "main";
  const deploySteps = deploy?.steps.filter((step) => step.phase === "deploy") ?? [];
  const rollbackSteps = deploy?.steps.filter((step) => step.phase === "rollback") ?? [];
  const allSteps = deploy?.steps ?? [];
  const done = allSteps.filter((step) => step.status !== "pending" && step.status !== "running").length;
  const current = allSteps.find((step) => step.status === "running");
  const unknown = stateUnknown || deploy?.status === "unknown";

  return (
    <Card
      title="デプロイ"
      icon="rocket"
      source="ssh"
      className="span-12 deploy-card"
      actions={
        <Button variant="filled" icon="rocket" onClick={onDeploy} disabled={!connected || running || unknown || disabled} busy={running && !unknown}>
          {unknown ? "状態不明（デプロイを停止）" : running ? "デプロイしています…" : "デプロイする"}
        </Button>
      }
    >
      <div className="deploy-summary">
        <Chip icon="git" title="取り込むブランチ（profiles.json の deploy_branch）">
          origin/{privacy.mask(branch)}
        </Chip>
        {git && (
          <span className="body-medium deploy-current">
            デプロイ中のコード: <code>{git.commit}</code> <span className="muted">{privacy.redact(git.subject)}</span>
          </span>
        )}
        {deploy && <StatusChip tone={RESULT[deploy.status].tone} icon={RESULT[deploy.status].icon} label={RESULT[deploy.status].label} />}
      </div>

      {!deploy && (
        <>
          <ol className="deploy-steps is-planned">
            {DEPLOY_PLAN.map((step, index) => (
              <li key={step.title} className="deploy-step is-pending">
                <span className="deploy-step-icon step-number label-large" aria-hidden="true">
                  {index + 1}
                </span>
                <div className="deploy-step-body">
                  <strong className="title-small mono">{step.title}</strong>
                  <span className="body-small muted">{step.detail}</span>
                </div>
              </li>
            ))}
          </ol>
          <p className="body-small muted">
            各ステップで結果を確かめ、通らなければ次へ進みません。依存関係の更新・再起動・ヘルスチェックで失敗したら、自動で前の版へ戻します（bot.db は戻しません）。
          </p>
        </>
      )}

      {deploy && (
        <>
          {running && !unknown && (
            <div className="deploy-progress">
              <span className="body-medium">
                {current ? `「${privacy.redact(current.title)}」を実行中` : "準備中"}（{done} / {allSteps.length}）
              </span>
              <WavyProgress value={allSteps.length ? done / allSteps.length : 0} label="デプロイの進み具合" />
            </div>
          )}
          {(!running || unknown) && deploy.message && (
            <p className={`deploy-result m3-status-banner tone-${RESULT[deploy.status].tone}`} role="status">
              <Icon name={RESULT[deploy.status].icon} size={20} filled />
              <span className="body-medium">{privacy.redact(deploy.message)}</span>
            </p>
          )}
          {unknown && (
            <p className="body-small muted">
              デプロイ・巻き戻し・サービスの起動・停止・再起動を停止しています。概要・ログ・DB閲覧とSSHの再接続は利用できます。
              再接続では状態不明は解除されません。サーバーで残留処理の終了とコード・依存関係・サービスの状態を確認してから、バックエンドを起動し直してください。
            </p>
          )}

          <ol className="deploy-steps" aria-label="デプロイのステップ">
            {deploySteps.map((step, index) => (
              <StepItem key={step.key} step={step} index={index + 1} now={now} />
            ))}
          </ol>

          {rollbackSteps.length > 0 && (
            <section className="deploy-rollback" aria-label="自動の巻き戻し">
              <h3 className="title-small">
                <Icon name="undo" size={20} />
                自動の巻き戻し
              </h3>
              <ol className="deploy-steps">
                {rollbackSteps.map((step, index) => (
                  <StepItem key={step.key} step={step} index={index + 1} now={now} />
                ))}
              </ol>
            </section>
          )}

          {deploy.commits.length > 0 && (
            <details className="deploy-commits">
              <summary className="label-large">取り込んだコミット（{deploy.commits.length} 件）</summary>
              <ul>
                {deploy.commits.map((commit) => (
                  <li key={commit.hash} className="body-medium">
                    <code>{commit.hash}</code> {privacy.redact(commit.subject)}
                  </li>
                ))}
              </ul>
            </details>
          )}
          <span className="body-small muted">
            {formatDateTime(deploy.started_at * 1000)} に開始
            {deploy.finished_at ? ` · ${formatSeconds(deploy.finished_at - deploy.started_at)}で終了` : ""}
            {" · "}記録はバックエンドを再起動すると消えます
          </span>
        </>
      )}
    </Card>
  );
}

export function StepItem({ step, index, now }: { step: DeployStep; index: number; now: number }) {
  const privacy = usePrivacy();
  const status = STEP_STATUS[step.status];
  const duration = stepDuration(step, now);
  return (
    <li className={`deploy-step is-${step.status}`}>
      <span className={`deploy-step-icon tone-${status.tone}`} aria-hidden="true">
        {step.status === "running" ? (
          <LoadingIndicator size={32} label={`${privacy.redact(step.title)}を実行中`} />
        ) : step.status === "pending" ? (
          <span className="step-number label-large">{index}</span>
        ) : (
          <Icon name={status.icon} size={24} filled />
        )}
      </span>
      <div className="deploy-step-body">
        <div className="deploy-step-head">
          <strong className="title-small">{privacy.redact(step.title)}</strong>
          <StatusChip tone={status.tone} label={status.label} />
          {duration && <span className="label-medium muted mono">{duration}</span>}
        </div>
        <span className="body-small muted">{privacy.redact(step.description)}</span>
        {step.message && <p className={`body-medium deploy-step-message ${step.status === "failed" ? "is-error" : ""}`}>{privacy.redact(step.message)}</p>}
        {step.checks.length > 0 && (
          <ul className="deploy-checks" aria-label="確かめたこと">
            {step.checks.map((check, checkIndex) => (
              <li key={checkIndex} className={check.ok ? "is-ok" : "is-ng"}>
                <Icon name={check.ok ? "checkCircle" : "error"} size={18} filled />
                <span className="body-medium">{privacy.redact(check.label)}</span>
                {check.detail && <span className="body-small mono deploy-check-detail">{privacy.redact(check.detail)}</span>}
              </li>
            ))}
          </ul>
        )}
        {(step.commands.length > 0 || step.output) && (
          <details className="deploy-output">
            <summary className="label-large">実行したコマンドと出力（{step.commands.length} 件）</summary>
            <pre>{step.output_contains_logs === false
              ? privacy.redact(step.output || step.commands.map((command) => `$ ${command}`).join("\n"))
              : redactText(step.output || step.commands.map((command) => `$ ${command}`).join("\n"), privacy)}</pre>
          </details>
        )}
      </div>
    </li>
  );
}
