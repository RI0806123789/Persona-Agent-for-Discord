/**
 * デプロイが進める手順の予定。実行前の確認ダイアログと、まだ一度もデプロイしていないときのカードが
 * 同じ定義を使う（バックエンドの `deploy.py` の手順と合わせること）。
 */
export interface PlannedDeployStep {
  /** カードの見出し。 */
  title: string;
  /** カードの説明。 */
  detail: string;
  /** 確認ダイアログの1行。ブランチ名・サービス名は呼び出し側が伏せ字を通してから渡す。 */
  confirmText: (target: { branch: string; service: string }) => string;
}

export const DEPLOY_PLAN: readonly PlannedDeployStep[] = [
  {
    title: "事前確認",
    detail: "ブランチ・未コミットの変更・GitHub の最新を確かめる",
    confirmText: () => "事前確認（ブランチ・未コミットの変更・GitHub の最新）",
  },
  {
    title: "git pull",
    detail: "最新を早送りで取り込む",
    confirmText: ({ branch }) => `git pull（origin/${branch} を早送りで取り込む）`,
  },
  {
    title: "pip install -r requirements.txt",
    detail: "requirements.txt か constraints.txt が変わったときだけ、依存関係を入れ直す",
    confirmText: () => "pip install -r requirements.txt（requirements.txt か constraints.txt が変わったときだけ）",
  },
  {
    title: "sudo systemctl restart",
    detail: "新しいコードで起動し、PID が変わったことを確かめる",
    confirmText: ({ service }) => `sudo systemctl restart ${service}`,
  },
  {
    title: "ヘルスチェック",
    detail: "「ログイン成功」のログが出て、落ちずに動いていることを確かめる",
    confirmText: () => "ヘルスチェック（「ログイン成功」のログが出るまで）",
  },
];
