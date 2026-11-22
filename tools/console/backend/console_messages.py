"""画面へ返す案内文のうち、状態を持たない（引数だけで決まる）ものを組み立てる（#482）。

`app.py` とルートの両方が使う。`app.py` から従来の名前（`_unset_profile_message` など）で再公開している。
"""

from __future__ import annotations

import remote_commands as rc
from profiles import Profile
from service_jobs import last_line


def unset_profile_message(profile: Profile, source: str) -> str:
    """見本の値のままの接続先へ接続しようとしたときの案内。"""
    fields = "・".join(profile.unset_fields)
    if source == "profiles.example.json":
        how = "backend/profiles.example.json を backend/profiles.json にコピーし、YOUR_ で始まる値を書き換えてください"
    else:
        how = f"backend/{source} の YOUR_ で始まる値を書き換えてください"
    return f"接続先「{profile.label}」は見本の値のままです（{fields}）。{how}（書き換えたら接続し直すだけで反映されます）。"


def sudo_password_message(username: str | None, service: str) -> str:
    """sudo にパスワードが要る環境向けの案内。許可するのは起動・停止・再起動の3つだけにする例を示す。"""
    user = username or "<ユーザー名>"
    commands = ", ".join(f"/usr/bin/systemctl {action} {service}" for action in rc.SERVICE_ACTIONS)
    return (
        "sudo にパスワードが必要なため実行できませんでした。サーバーで sudo visudo -f /etc/sudoers.d/persona-bot-console を開き、"
        f"「{user} ALL=(root) NOPASSWD: {commands}」の1行を書くと、この3つの操作だけをパスワードなしで許可できます"
        "（systemctl の場所は which systemctl で確かめてください。見本: tools/console/examples/sudoers.example）。"
    )


_JOURNAL_PERMISSION_MESSAGE = (
    "ログを読む権限がありません。サーバーのユーザーを adm / systemd-journal グループに入れるか、"
    "profiles.json の journal_sudo を true にしてください。"
)


def _journal_permission_denied(stderr: str) -> bool:
    lowered = stderr.lower()
    return "permission" in lowered or "insufficient" in lowered


def journal_error_message(stderr: str) -> str:
    detail = last_line(stderr)
    if _journal_permission_denied(stderr):
        return _JOURNAL_PERMISSION_MESSAGE
    return f"ログを取得できませんでした: {detail or '原因不明'}"


def journal_safe_error_message(stderr: str) -> str:
    """遠隔の標準エラー（接続先のホスト名・パスを含みうる）を載せない、伏せ字モード用の文面（#494）。"""
    if _journal_permission_denied(stderr):
        return _JOURNAL_PERMISSION_MESSAGE
    return "ログを取得できませんでした。サーバーの journalctl の実行結果と権限を確認してください。"
