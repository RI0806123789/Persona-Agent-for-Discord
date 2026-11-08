"""旧データファイルの読み取りと解釈（data/memory/ の旧 JSON・テキスト）。

`Database`（`database.py`）の旧データ取り込みから、「ファイルを読んで、取り込む中身へ解釈する」部分だけを
切り出したモジュール。5形式を扱う。

  - affinity.json     … {ユーザーID: 親密度・感情の辞書}
  - user_stats.json   … {ユーザーID: 会話統計の辞書}
  - queue.json        … {"tasks": [文書処理タスクの辞書, ...]}
  - pending_log.txt   … 「[時刻] User: …」「[時刻] Bot: …」の行形式
  - profile.json      … {ユーザーID: {キー: 値}}（旧・全体共有形式の手書きも許容）

★ ここは DB にも、ロックにも、ログにも触れない。読み取り・解釈・検証だけを行い、結果を返す。
  DB への書き込み（SQL・ロック・トランザクション）、取り込み後の改名、ログは `Database` が担当する。
  ログの出力元を `persona_bot.core.database` のまま保つ（運用ログの出元・テストの捕捉先が変わらない）ためと、
  取り込みで DB に書く値の変換（`_affinity_row_params` など。通常の保存と共有する）を `database.py`
  に一か所だけ置くため。
★ 取り込めない形式の旧ファイルは例外にする。`{}` 扱いに戻すと、取り込めなかったファイルが
  「取り込み済み」として改名され、データを取り込めなかったことに誰も気づけない
  （例外にすれば `Database._import_legacy_files` が元のファイルを残して警告し、次回起動時に再試行する）。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_legacy_json_object(path: Path) -> dict[str, Any]:
    """旧 JSON ファイルを読み、最上位が object でなければ例外にする。

    壊れたファイルを {} として扱うと、取り込みが「成功」して .imported.bak へ改名され、
    データを取り込めなかったことに誰も気づけない。例外にすれば呼び出し側が
    元のファイルを残して警告し、次回起動時に再試行する。
    """
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"最上位が object ではありません（{type(data).__name__}）")
    return data


def free_backup_path(path: Path, max_suffix: int) -> Path:
    """旧ファイルの退避先を、既存のファイルと衝突しない名前で返す（#379）。

    通常は「<元の名前>.imported.bak」。すでに同名のファイルがあるときは、
    「<元の名前>.imported.<連番>.bak」の空いている最小の番号にする（連番は max_suffix まで）。
    Path.replace は既存のファイルを黙って置き換えるため、名前を決めずに改名すると、
    前回の退避ファイルが消える。特に引き継ぎ先が無いときの pending_log.txt は
    DB へ取り込まれず、退避ファイルが唯一のコピーになる。
    空きが見つからなければ FileExistsError にする（呼び出し側が取り込みごと見送り、
    元のファイルを残して警告する）。
    max_suffix は呼び出し側から受け取る（`Database` は自分のモジュールの LEGACY_BACKUP_MAX_SUFFIX を渡す。
    テストがその値を差し替えて「空きなし」を再現するため）。
    """
    candidates = [path.with_name(path.name + ".imported.bak")]
    candidates += [
        path.with_name(f"{path.name}.imported.{number}.bak")
        for number in range(1, max_suffix + 1)
    ]
    for candidate in candidates:
        # 壊れたシンボリックリンクは exists() が偽になるため、is_symlink() も見る
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise FileExistsError(f"退避先の名前が空いていません（{path.name}）")


def load_legacy_user_entries(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """affinity.json・user_stats.json のように {ユーザーID: 辞書} の形のファイルから、辞書の項目だけを返す。

    値が辞書でない項目は読み飛ばす（手で直したファイルの誤りで取り込み全体を止めない）。
    """
    data = load_legacy_json_object(path)
    return [(user_id, entry) for user_id, entry in data.items() if isinstance(entry, dict)]


def load_legacy_queue_tasks(path: Path) -> list[dict[str, Any]]:
    """queue.json の "tasks" から、辞書で task_id を持つタスクだけを返す。

    "tasks" が配列でなければ例外にする（読み飛ばすと、キューが空として取り込み済みに見える）。
    """
    tasks = load_legacy_json_object(path).get("tasks", [])
    if not isinstance(tasks, list):
        raise ValueError("tasks が配列ではありません")
    return [task for task in tasks if isinstance(task, dict) and task.get("task_id")]


@dataclass(frozen=True)
class PendingLogFile:
    """旧 pending_log.txt の解釈結果。

    content_hash は読み取った元のバイト列の SHA-256。entries は {"created_at", "question", "response"}
    の辞書を、ファイルに書かれた順に並べたもの。
    """

    content_hash: str
    entries: list[dict[str, str]]


def parse_pending_log(source: bytes) -> PendingLogFile | None:
    """旧 pending_log.txt（[時刻] User: … / [時刻] Bot: … 形式）を行単位で復元する。

    読み取った同じバイト列からハッシュと会話を作り、別内容の取り違えを防ぐ。
    本文が空白だけなら None（取り込むものが無い。取り込み済みの記録も作らない）。
    発言が複数行だった場合の続きの行は、直前に書いていた側（User / Bot）へ連結する。
    """
    content_hash = hashlib.sha256(source).hexdigest()
    text = source.decode("utf-8-sig")
    if not text.strip():
        return None
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    bot_seen = False
    for line in text.splitlines():
        user_match = re.match(r"^\[(.+?)\] User: (.*)$", line)
        bot_match = re.match(r"^\[(.+?)\] Bot: (.*)$", line)
        if user_match:
            if current is not None:
                entries.append(current)
            current = {"created_at": user_match[1], "question": user_match[2], "response": ""}
            bot_seen = False
        elif bot_match and current is not None:
            current["response"] = bot_match[2]
            bot_seen = True
        elif current is not None:
            # 発言が複数行だった場合の続きの行。直前に書いていた側へ連結する。
            key = "response" if bot_seen else "question"
            current[key] += "\n" + line
    if current is not None:
        entries.append(current)
    return PendingLogFile(content_hash=content_hash, entries=entries)


def load_profile_json(path: Path) -> dict[str, Any] | None:
    """手書き追加用の profile.json を読む。取り込む中身が無ければ None。

    空のファイル・"{}"・最上位が空の object や object 以外は None（取り込むものが無い）。
    JSON として読めない場合は json.JSONDecodeError を、読み取りの失敗は OSError などを、そのまま呼び出し側へ
    伝える（手書きミスでデータを失わないよう、呼び出し側がファイルを壊さず残して警告する）。
    """
    raw_text = path.read_text(encoding="utf-8-sig").strip()
    if not raw_text or raw_text == "{}":
        return None
    data = json.loads(raw_text)
    if not isinstance(data, dict) or not data:
        return None
    return data


def split_profile_by_user(data: dict[str, Any], legacy_owner_id: str) -> dict[str, dict[str, Any]]:
    """profile.json の中身を、取り込み先のユーザーID → そのユーザーのプロフィールに振り分ける。

    全キーが ASCII 数字のユーザーID、全値が辞書のときだけユーザー別形式とみなす。旧形式では
    値がすべて辞書のこともあるため、名前や全角数字をIDにしない。ユーザー別形式では空の辞書を読み飛ばす。
    それ以外（旧・全体共有形式の手書き）は legacy_owner_id のプロフィールとして取り込む（後方互換）。
    引き継ぎ先が無い（空文字）旧形式は、取り込めないものとして空を返す。
    """
    looks_per_user = all(
        user_id.isascii() and user_id.isdigit() and isinstance(value, dict)
        for user_id, value in data.items()
    )
    if looks_per_user:
        return {
            str(user_id): profile
            for user_id, profile in data.items()
            if isinstance(profile, dict) and profile
        }
    if legacy_owner_id:
        return {legacy_owner_id: data}
    return {}
