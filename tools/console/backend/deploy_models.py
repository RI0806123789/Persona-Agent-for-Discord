"""デプロイの記録（データモデル）と、画面に出す手順の定義。

`deploy.py` の `DeployRunner` が、この記録を書き換えながら進める。画面は記録をそのまま表示する。
モデルだけを切り出してあるのは、手順の定義・記録の形・生成を、実行（SSH・待機・巻き戻し）から
独立して読めるようにするため（#416）。外からは従来どおり `deploy` から import できる（再公開）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from profiles import Profile

DEPLOY_STEPS = (
    ("precheck", "事前確認", "ブランチ・未コミットの変更・GitHub の最新を確かめる"),
    ("pull", "git pull", "{branch} の最新を早送りで取り込む"),
    ("deps", "pip install -r requirements.txt", "requirements.txt か constraints.txt が変わったときだけ、依存関係を入れ直す"),
    ("restart", "sudo systemctl restart {service}", "新しいコードで起動し、PID が変わったことを確かめる"),
    ("health", "ヘルスチェック", "「ログイン成功」のログが出て、落ちずに動いていることを確かめる"),
)
ROLLBACK_STEPS = (
    (
        "rollback_code", "コードを戻す",
        "ブランチ・コミット・未コミットの変更が想定どおりか確かめ、git reset --keep でデプロイ前のコミットへ戻す（取り込んだコミットは GitHub に残る）",
    ),
    ("rollback_deps", "依存関係を戻す", "依存関係を入れ直していたら、前の requirements.txt・constraints.txt で入れ直す"),
    ("rollback_restart", "再起動", "前の版で起動し直し、PID が変わったことを確かめる"),
    ("rollback_health", "ヘルスチェック", "前の版で「ログイン成功」まで戻ったことを確かめる"),
)


@dataclass
class Check:
    """ステップの中で確かめた事実1つ（画面に ✓ / ✗ で出す）。"""

    label: str
    ok: bool
    detail: str = ""


@dataclass
class Step:
    key: str
    title: str
    description: str
    phase: str = "deploy"  # deploy / rollback
    status: str = "pending"  # pending / running / succeeded / failed / skipped
    started_at: float | None = None
    finished_at: float | None = None
    commands: list[str] = field(default_factory=list)
    output: str = ""
    # 再起動ログは本文の境界が不明。切り詰めや追記後も画面で隠すために保持する。
    output_contains_logs: bool = False
    checks: list[Check] = field(default_factory=list)
    message: str = ""


@dataclass
class DeployRecord:
    id: int
    profile_id: str
    environment: str
    service: str
    branch: str
    started_at: float
    steps: list[Step]
    # running / succeeded / up_to_date / failed / rolled_back / rollback_failed / unknown
    status: str = "running"
    finished_at: float | None = None
    before_commit: str | None = None
    target_commit: str | None = None
    after_commit: str | None = None
    commits: list[dict] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class StepFailed(Exception):
    """ステップの確認が通らなかった。メッセージは画面にそのまま出す日本語。"""


def new_record(record_id: int, profile: Profile) -> DeployRecord:
    steps = [
        Step(key, title.format(branch=profile.deploy_branch, service=profile.service), description.format(branch=profile.deploy_branch))
        for key, title, description in DEPLOY_STEPS
    ]
    return DeployRecord(
        id=record_id,
        profile_id=profile.id,
        environment=profile.environment,
        service=profile.service,
        branch=profile.deploy_branch,
        started_at=time.time(),
        steps=steps,
    )
