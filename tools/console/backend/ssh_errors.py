"""SSH のエラーの型と、画面に出す診断文・伏せ字用の安全な文面の組み立て（#482）。

`ssh_session.py` から、接続やコマンドの流れと関係のない部分を分けたもの。ここは**純粋**で、接続・ファイル・
時計に触れない（`asyncssh` の例外の型を見分けるだけ）。エラーは「通常の診断文」と、ホスト名・ローカルの
パス・ユーザー名などを含まない「安全な文面（`safe_message`）」を対にして作る。伏せ字モードの画面は安全な
文面へ切り替える（#218・#399）。

`ssh_session.py` から、従来の `from ssh_session import SshError, CommandStateUnknownError, ...` で取れるよう
再公開している。

★ 新しい SSH のエラーを足したら、安全な文面も同時に作ること。診断文にローカルのパス・ホスト名を載せるなら、
  必ず `safe_message` に置き換えた文を渡す（`tests/test_ssh_error_privacy.py` が検出する）。
★ 文面は画面とテストが参照する。言い回しを変えるときはテストも直す。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh

# 接続が外から切れたときの、伏せ字モード用の文面（切断理由の原文を含めない。#399）
CONNECTION_LOST_SAFE_MESSAGE = "SSH 接続が切れました。もう一度接続してください。"
NOT_CONNECTED_MESSAGE = "SSH に接続していません。接続してからもう一度試してください。"
COMMAND_STATE_UNKNOWN_MESSAGE = (
    "遠隔コマンドの終了が未確認で、状態不明です。サーバーで処理の終了と状態を確認してからバックエンドを起動し直してください。"
)
# 終了を確認できなかった更新操作のエラーへ添える案内
STATE_UNKNOWN_SUFFIX = " 状態不明のため、サーバーで処理の終了と状態を確認してからバックエンドを起動し直してください。"
_KNOWN_HOSTS_HINT = "（profiles.json の known_hosts の場所を確かめてください。null なら ~/.ssh/known_hosts を使います）"


class SshError(Exception):
    """通常の診断文と、必要なら伏せ字用の安全な文面を持つ日本語のエラー。"""

    def __init__(self, message: str, *, safe_message: str | None = None) -> None:
        super().__init__(message)
        # 通常の診断文は維持し、設定由来のローカル情報を含まない文面を別に運ぶ。
        self.safe_message = safe_message


class NotConnectedError(SshError):
    """まだ接続していない（または切れた）ときのエラー。"""


class CommandStateUnknownError(SshError):
    """開始済みの遠隔コマンドが終了したことを確認できない。"""


def safe_config_location(path: Path, *, key: bool = False) -> str:
    """既知の配置だけ残し、任意の所有者・ディレクトリ・ファイル名を画面へ出さない。"""
    parts = [part for part in str(path).replace("\\", "/").split("/") if part]
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    if ".ssh" not in parts:
        return "<LOCAL_PATH>"
    home = len(parts) >= 3 and parts[0].lower() in {"home", "users"} and parts[2] == ".ssh"
    parent = "<LOCAL_HOME>" if home else "<LOCAL_PATH>"
    filename = "<KEY_FILE>" if key else "known_hosts" if parts[-1] == "known_hosts" else "<KNOWN_HOSTS_FILE>"
    return f"{parent}/.ssh/{filename}"


# ---------------------------------------------------------------- 接続の設定（秘密鍵・known_hosts）

def missing_key_error(key_path: Path) -> SshError:
    return SshError(
        f"秘密鍵が見つかりません: {key_path}",
        safe_message=f"秘密鍵が見つかりません: {safe_config_location(key_path, key=True)}"
        "（profiles.json の key_path の場所を確かめてください）",
    )


def missing_known_hosts_error(known_hosts: Path) -> SshError:
    return SshError(
        f"known_hosts が見つかりません: {known_hosts}" + _KNOWN_HOSTS_HINT,
        safe_message=f"known_hosts が見つかりません: {safe_config_location(known_hosts)}" + _KNOWN_HOSTS_HINT,
    )


def missing_default_known_hosts_error(default_known_hosts: Path, host: str) -> SshError:
    return SshError(
        f"{default_known_hosts} がありません。PowerShell で一度 ssh {host} を実行し、"
        "フィンガープリントを確認してからホスト鍵を登録してください。",
        safe_message="<LOCAL_HOME>/.ssh/known_hosts がありません。PowerShell で一度 ssh <SSH_HOST> を実行し、"
        "フィンガープリントを確認してからホスト鍵を登録してください。",
    )


# ---------------------------------------------------------------- 接続の失敗・切断

def describe_connect_error(error: Exception, host: str, connect_timeout_seconds: float) -> str:
    """接続エラーを「何が起きて、どうすればよいか」の日本語にする。"""
    if isinstance(error, SshError):
        return str(error)
    if isinstance(error, asyncssh.HostKeyNotVerifiable):
        return (
            "サーバーのホスト鍵を確認できませんでした（known_hosts に未登録、または鍵が変わっています）。"
            f"初回なら PowerShell で ssh {host} を実行し、フィンガープリントを確かめてから登録してください。"
            "心当たりなく鍵が変わった場合は接続しないでください（なりすましの可能性があります）。"
        )
    if isinstance(error, asyncssh.PermissionDenied):
        return "認証に失敗しました。ユーザー名と秘密鍵（key_path または ~/.ssh/config の IdentityFile）を確認してください。"
    if isinstance(error, asyncio.TimeoutError):
        return f"{connect_timeout_seconds} 秒以内にサーバーへ接続できませんでした。ホスト名・ポート・ファイアウォールを確認してください。"
    if isinstance(error, ConnectionRefusedError):
        return "接続を拒否されました。ポート番号が正しいか、サーバーで SSH が動いているか確認してください。"
    if isinstance(error, OSError):
        return "サーバーに接続できませんでした。ホスト名とネットワークを確認してください。"
    if isinstance(error, asyncssh.Error):
        return "SSH の接続処理に失敗しました。設定と接続先の状態を確認して再試行してください。"
    return "予期しないエラーで接続できませんでした。時間をおいて再試行してください。"


def connection_lost_message(exc: Exception | None) -> str:
    """外から接続が切れたときの診断文。切断理由（接続先・パスを含みうる）は画面の通常表示にだけ出す。"""
    reason = f"（{exc}）" if exc else ""
    return f"SSH 接続が切れました{reason}。もう一度接続してください。"


# ---------------------------------------------------------------- コマンドの実行

def timeout_error(label: str, timeout: float) -> SshError:
    return SshError(f"{label}: {timeout:g} 秒以内に応答がありませんでした。")


def communication_error(label: str, error: BaseException) -> SshError:
    return SshError(
        f"{label}: 通信に失敗しました（{type(error).__name__}: {error}）。",
        safe_message=f"{label}: 通信に失敗しました。",
    )


def start_error(label: str, error: BaseException) -> SshError:
    return SshError(
        f"{label}: 開始できませんでした（{error}）。",
        safe_message=f"{label}: 開始できませんでした。SSHの接続状態を確認してから再試行してください。",
    )


def exit_status_unknown_error(label: str) -> CommandStateUnknownError:
    return CommandStateUnknownError(f"{label}: 遠隔コマンドの終了コードを確認できませんでした。")


def completion_unknown_from_transport(label: str, error: BaseException) -> CommandStateUnknownError:
    return CommandStateUnknownError(
        f"{label}: 通信に失敗し、遠隔コマンドの終了を確認できません（{type(error).__name__}: {error}）。",
        safe_message=f"{label}: 通信に失敗し、遠隔コマンドの終了を確認できません。",
    )


def completion_unknown_from_unexpected(label: str, error: BaseException) -> CommandStateUnknownError:
    return CommandStateUnknownError(
        f"{label}: 遠隔コマンドの終了とチャネルの回収を確認できません（{type(error).__name__}）。"
    )


def with_unknown_state_suffix(error: CommandStateUnknownError) -> CommandStateUnknownError:
    """終了を確認できなかった更新操作のエラーへ、バックエンドを起動し直す案内を添えて作り直す。

    作り直す例外にも安全な文面を引き継ぐ（落とすと、伏せ字中に生のエラーへ戻ってしまう）。
    """
    message = str(error) + STATE_UNKNOWN_SUFFIX
    safe_message = None if error.safe_message is None else error.safe_message + STATE_UNKNOWN_SUFFIX
    return CommandStateUnknownError(message, safe_message=safe_message)
