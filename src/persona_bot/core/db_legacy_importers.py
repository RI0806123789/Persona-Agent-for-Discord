"""旧データファイルの読み取り・解釈と、DB への取り込み（data/memory/ の旧 JSON・テキスト）。

`Database`（`database.py`）の旧データ取り込みを担当するモジュール。5形式を扱う。

  - affinity.json     … {ユーザーID: 親密度・感情の辞書}
  - user_stats.json   … {ユーザーID: 会話統計の辞書}
  - queue.json        … {"tasks": [文書処理タスクの辞書, ...]}
  - pending_log.txt   … 「[時刻] User: …」「[時刻] Bot: …」の行形式
  - profile.json      … {ユーザーID: {キー: 値}}（旧・全体共有形式の手書きも許容）

前半の関数（`load_*`・`parse_*`・`split_*`・`free_backup_path`）は、DB にも、ロックにも、ログにも触れない。
読み取り・解釈・検証だけを行い、結果を返す。後半の `LegacyDataImporter` が、それらを使って DB へ書き込み、
取り込み後の改名とログを担当する（#414。以前は `Database` のメソッドだった）。

★ このモジュールは sqlite3・threading・logging・persona_bot を import しない（`ImporterPurityTests` が確認する）。
  `LegacyDataImporter` が使う接続・ロック・ロガー・JSON の書き込みは、すべて `Database` が生成時に渡す。
  ログの出力元は `persona_bot.core.database` のまま保つ（運用ログの出元・テストの捕捉先が変わらない）。
  ロガー・退避先の連番の上限・オーナーIDは、`Database` が差し替え口として持つ `database.logger`・
  `database.LEGACY_BACKUP_MAX_SUFFIX`・`Database._legacy_owner_id` を**呼出しのたびに**受け取る
  （テストがそれらを差し替えるため、生成時に固定しない）。取り込みで DB に書く値の変換
  （`_affinity_row_params` など）も、通常の保存と共有するため `database.py` に一か所だけ置いて渡す。
★ 取り込めない形式の旧ファイルは例外にする。`{}` 扱いに戻すと、取り込めなかったファイルが
  「取り込み済み」として改名され、データを取り込めなかったことに誰も気づけない
  （例外にすれば `LegacyDataImporter.import_files` が元のファイルを残して警告し、次回起動時に再試行する）。
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


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


@dataclass(frozen=True)
class LegacyRowParams:
    """取り込みで DB に書く値の変換（通常の保存と共有する関数）。`database.py` の定義を渡す。"""

    affinity: Callable[[Any, dict[str, Any]], tuple[Any, ...]]
    user_stats: Callable[[Any, dict[str, Any]], tuple[Any, ...]]
    queue: Callable[[dict[str, Any]], tuple[Any, ...]]


class LegacyDataImporter:
    """旧データの DB への取り込み（#414。以前は `Database` のメソッド）。

    `Database` の接続とロックを共有し、起動時の `initialize` から次の順に呼ばれる:
    `migrate_profile_rows` → `import_files` → `absorb_profile_json`。
    DB への書き込みは `with lock, conn` のトランザクションで行う（通常の保存と同じ）。
    """

    def __init__(
        self,
        conn: Any,
        lock: Any,
        legacy_dir: Path,
        *,
        owner_id: Callable[[], str],
        row_params: LegacyRowParams,
        profile_merge: Callable[[str, dict[str, Any]], None],
        write_json: Callable[[Path, Any], None],
        backup_max_suffix: Callable[[], int],
        get_logger: Callable[[], Any],
    ) -> None:
        # conn は sqlite3.Connection、lock は threading.Lock、get_logger は logging.Logger を返す関数
        # （型を import しないための Any。ここの import 制約は上のモジュールの説明を参照）。
        self._conn = conn
        self._lock = lock
        self._legacy_dir = legacy_dir
        # 以下は呼出しのたびに読む。Database 側の値（テストが差し替える）に追従するため。
        self._owner_id = owner_id
        self._backup_max_suffix = backup_max_suffix
        self._get_logger = get_logger
        self._row_params = row_params
        self._profile_merge = profile_merge
        self._write_json = write_json

    def migrate_profile_rows(self) -> None:
        """ユーザー別記憶への移行の後始末（冪等）。

        バージョン3の移行で退避した profile_legacy_v2（旧・全体共有プロフィール）の
        行を owner の user_id に割り当てて新 profile へ移し、退避テーブルを削除する。
        あわせて pending_log の user_id 未設定行（旧・全体共有）も owner へ振り直す。

        スキーマ移行本体（_apply_migrations）とは別トランザクションで走るため、
        途中でクラッシュしても次回起動時にこのメソッドが再実行されて完了する
        （テーブルの有無・行の user_id で判定するため二重実行されても安全）。
        owner が空文字の場合は引き継ぎ元を破棄する（旧データを捨てる）。
        """
        logger = self._get_logger()
        owner = self._owner_id()
        with self._lock, self._conn:
            legacy_exists = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='profile_legacy_v2'"
            ).fetchone()
            if legacy_exists is not None:
                if owner:
                    # 既に owner に同名キーがあれば旧値で上書きしない（新 DB 側を正とする）
                    self._conn.execute(
                        "INSERT OR IGNORE INTO profile (user_id, key, value)"
                        " SELECT ?, key, value FROM profile_legacy_v2",
                        (owner,),
                    )
                    logger.info('データベース: 旧・全体共有プロフィールを user_id=<ID> へ移行しました。')
                self._conn.execute("DROP TABLE profile_legacy_v2")
            if owner:
                cursor = self._conn.execute(
                    "UPDATE pending_log SET user_id = ? WHERE user_id = ''", (owner,)
                )
                if cursor.rowcount:
                    logger.info(f'データベース: pending_log の未割当 {cursor.rowcount} 件を user_id=<ID> へ振り直しました。')
            else:
                # 引き継ぎ先が無いときは、旧・全体共有の会話ログも破棄する（profile と同じ方針）。
                # 残すと user_id='' の「誰でもない利用者」として記憶圧縮の対象に数えられ、
                # 誰も読まない記憶を作るために Gemini を呼び続ける。
                cursor = self._conn.execute("DELETE FROM pending_log WHERE user_id = ''")
                if cursor.rowcount:
                    logger.warning(f'データベース: 引き継ぎ先が無いため、pending_log の未割当 {cursor.rowcount} 件を破棄しました。')

    # ---- 旧 JSON / テキストファイルの取り込み --------------------------------

    def import_files(self) -> None:
        importers = [
            ("affinity.json", self.import_affinity_json),
            ("user_stats.json", self.import_user_stats_json),
            ("queue.json", self.import_queue_json),
            ("pending_log.txt", self.import_pending_log_txt),
        ]
        logger = self._get_logger()
        for filename, importer in importers:
            path = self._legacy_dir / filename
            if not path.exists():
                continue
            try:
                # 退避先は取り込みの前に決める。空きが無ければ取り込まずに元のファイルを残す。
                # 起動処理は単一のスレッドで動くため、決めてから改名するまでに同名は作られない。
                backup_path = free_backup_path(path, self._backup_max_suffix())
                importer(path)
                path.replace(backup_path)
                logger.info(f'データベース: {filename} を取り込み、{backup_path.name} に改名しました。')
            except Exception as error:
                # 取り込み失敗でも起動は続ける（ファイルが残るので次回起動時に再試行される）
                logger.error(f'データベース: {filename} の取り込みに失敗しました: {type(error).__name__}', exc_info=error)

    def import_affinity_json(self, path: Path) -> None:
        entries = load_legacy_user_entries(path)
        with self._lock, self._conn:
            for user_id, entry in entries:
                # 既に DB にあるユーザーは上書きしない（DB 側を正とする）
                self._conn.execute(
                    "INSERT OR IGNORE INTO affinity"
                    " (user_id, affinity, valence, arousal, last_interaction)"
                    " VALUES (?, ?, ?, ?, ?)",
                    self._row_params.affinity(user_id, entry),
                )

    def import_user_stats_json(self, path: Path) -> None:
        entries = load_legacy_user_entries(path)
        with self._lock, self._conn:
            for user_id, entry in entries:
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_stats"
                    " (user_id, user_name, total_count, hourly, first_seen, last_seen,"
                    "  recent_samples, analysis, stats_opt_out)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._row_params.user_stats(user_id, entry),
                )

    def import_queue_json(self, path: Path) -> None:
        tasks = load_legacy_queue_tasks(path)
        with self._lock, self._conn:
            for task in tasks:
                self._conn.execute(
                    "INSERT OR IGNORE INTO queue_tasks"
                    " (task_id, status, channel_id, message_id, author_id, question,"
                    "  attachment_count, created_at, updated_at, finished_at, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    self._row_params.queue(task),
                )

    def import_pending_log_txt(self, path: Path) -> None:
        """旧 pending_log.txt（[時刻] User: … / [時刻] Bot: … 形式）を行単位で復元する。"""
        # 読み取った同じバイト列からハッシュと会話を作り、別内容の取り違えを防ぐ（解釈は parse_pending_log）。
        parsed = parse_pending_log(path.read_bytes())
        if parsed is None:
            return
        content_hash, entries = parsed.content_hash, parsed.entries

        # 旧 pending_log.txt はユーザー別記憶の導入前のもの＝全体共有。owner に紐付ける。
        owner = self._owner_id()
        if not owner:
            # 引き継ぎ先が無ければ取り込まない（migrate_profile_rows と同じく破棄する）。
            # ファイルは呼び出し元が .imported.bak へ改名して残すため、内容は失われない。
            if entries:
                self._get_logger().info(f'データベース: 引き継ぎ先が無いため、pending_log.txt の {len(entries)} 件は取り込みません。')
            return
        with self._lock, self._conn:
            imported = self._conn.execute(
                "SELECT 1 FROM legacy_imports WHERE source_name = ? AND content_hash = ?",
                (path.name, content_hash),
            ).fetchone()
            if imported:
                return
            for entry in entries:
                self._conn.execute(
                    "INSERT INTO pending_log (user_id, created_at, question, response) VALUES (?, ?, ?, ?)",
                    (owner, entry["created_at"], entry["question"], entry["response"]),
                )
            # INSERTと同じトランザクションで確定する。失敗時は両方とも再試行できる。
            self._conn.execute(
                "INSERT INTO legacy_imports (source_name, content_hash) VALUES (?, ?)",
                (path.name, content_hash),
            )

    def absorb_profile_json(self) -> None:
        """profile.json に書き足された内容を profile テーブルへ吸収する（毎起動時）。

        ユーザー別記憶に対応するため、形式は {ユーザーID: {キー: 値, ...}, ...}。
        各ユーザーごとにトップレベルキー単位でマージ（同名キーはファイル側が勝つ）し、
        ファイルを {} に戻す。数字のユーザーIDと dict の組でないもの（旧・全体共有形式の
        手書き）は owner のプロフィールとして取り込む（後方互換）。
        JSON として読めない場合はファイルを壊さず残し、警告だけ出してスキップする
        （手書きミスでデータを失わないため）。
        """
        path = self._legacy_dir / "profile.json"
        if not path.exists():
            return
        logger = self._get_logger()
        try:
            data = load_profile_json(path)
            if data is None:
                return

            # 形式の判定（ユーザー別か、旧・全体共有か）と振り分けは split_profile_by_user が行う。
            absorbed: dict[str, list[str]] = {}
            for user_id, profile in split_profile_by_user(data, self._owner_id()).items():
                self._profile_merge(user_id, profile)
                absorbed[user_id] = list(profile.keys())

            if not absorbed:
                logger.warning('データベース: profile.json に取り込めるプロフィールがありません。ユーザー別形式と旧形式の引継ぎ先を確認してください。ファイルを保持します。')
                return

            self._write_json(path, {})
            logger.info(f'データベース: profile.json から {len(absorbed)} 人分・{sum((len(keys) for keys in absorbed.values()))} 項目を profile テーブルへ追加しました。')
        except json.JSONDecodeError as error:
            logger.error(f'データベース: profile.json が JSON として読めないため取り込みをスキップします: {type(error).__name__}', exc_info=error)
        except Exception as error:
            logger.error(f'データベース: profile.json の取り込みに失敗しました: {type(error).__name__}', exc_info=error)
