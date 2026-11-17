"""停止中のボットの memory/・ng/ を、バックアップして data/ 配下へ移す。

リポジトリルートで python deploy/migrate_data_directory.py を実行する。
--check は移行可否の確認だけを行い、ファイルを作成・変更しない。
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import stat
from uuid import uuid4

# 旧配置（ルート直下）のデータフォルダ名。src/persona_bot/config/data_paths.py の LEGACY_DIRECTORY_NAMES と
# 同じ値にすること（ボットの起動時の残留確認と、この移行の対象を揃える。一致はテストが確かめる）。
LEGACY_DIRECTORY_NAMES = ("memory", "ng")


class MigrationError(RuntimeError):
    """既存データを上書きせずに移行できない状態。"""


@dataclass(frozen=True)
class MigrationResult:
    files: int
    backup: Path | None = None


def _check_path(path: Path, *, directory: bool = False) -> None:
    # ジャンクションも拒否し、想定外の場所をコピー・削除しない。
    if path.is_symlink() or (
        path.exists()
        and getattr(path.lstat(), "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise MigrationError("リンクを含む保存先は移行できません。通常のディレクトリへ整理してください。")
    if path.exists() and not (path.is_dir() if directory else path.is_file()):
        raise MigrationError("保存先のファイルとディレクトリが衝突しています。配置を確認してください。")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migrate_data_directory(project_root: Path, *, check_only: bool = False) -> MigrationResult:
    """全件を事前確認し、原本のバックアップとコピー検証後に旧配置を片付ける。"""
    project_root = project_root.resolve()
    data_root = project_root / "data"
    _check_path(data_root, directory=True)
    sources = []
    files = []
    directories = []
    for name in LEGACY_DIRECTORY_NAMES:
        source = project_root / name
        target = data_root / name
        _check_path(source, directory=True)
        _check_path(target, directory=True)
        if not source.exists():
            continue
        sources.append(source)
        directories.append((source, target))
        for entry in sorted(source.rglob("*")):
            destination = target / entry.relative_to(source)
            if entry.is_dir():
                _check_path(entry, directory=True)
                _check_path(destination, directory=True)
                directories.append((entry, destination))
                continue
            _check_path(entry)
            _check_path(destination)
            digest = _digest(entry)
            if destination.exists() and _digest(destination) != digest:
                raise MigrationError(
                    "移行先に内容が異なる同名ファイルがあります。"
                    "旧配置と data/ 配下をバックアップし、採用する内容を確認してください。"
                )
            files.append((entry, destination, digest))

    backup_parent = data_root / ".migration-backups"
    if sources:
        _check_path(backup_parent, directory=True)
    if check_only or not sources:
        return MigrationResult(len(files))

    backup_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_parent / f"{stamp}-{uuid4().hex[:8]}"
    backup.mkdir(mode=0o700)
    for source in sources:
        shutil.copytree(source, backup / source.name)
    for source, _, digest in files:
        if _digest(backup / source.relative_to(project_root)) != digest or _digest(source) != digest:
            raise MigrationError("バックアップ中にデータが変化しました。ボットを停止して再実行してください。")

    # 新配置へ全件をコピーしてから原本を片付ける。既存ファイルは上書きしない。
    for _, destination in directories:
        destination.mkdir(parents=True, exist_ok=True)
    for source, destination, digest in files:
        if not destination.exists():
            with source.open("rb") as original, destination.open("xb") as copied:
                shutil.copyfileobj(original, copied)
            shutil.copystat(source, destination)
        if _digest(destination) != digest:
            raise MigrationError("移行先の検証に失敗しました。旧配置とバックアップを保持して確認してください。")
    # コピー中に書き込まれた原本も消さない。
    if any(_digest(source) != digest for source, _, digest in files):
        raise MigrationError("移行中にデータが変化しました。ボットを停止し、バックアップと比較してください。")
    for source, _, _ in files:
        if os.name == "nt":
            source.chmod(source.stat().st_mode | stat.S_IWRITE)
        source.unlink()
    for source, _ in sorted(directories, key=lambda pair: len(pair[0].parts), reverse=True):
        if os.name == "nt":
            source.chmod(source.stat().st_mode | stat.S_IWRITE)
        source.rmdir()
    return MigrationResult(len(files), backup)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="移行可否だけを確認し、変更しない")
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    try:
        result = migrate_data_directory(project_root, check_only=args.check)
    except MigrationError as error:
        print(f"移行を停止しました: {error}")
        return 1
    except OSError as error:
        print(
            f"移行を停止しました（{type(error).__name__}）。"
            "ボットの停止・空き容量・読み書き権限を確認し、旧配置と data/.migration-backups/ を保持してください。"
        )
        return 1
    if args.check:
        print(f"移行前確認: OK（対象 {result.files} ファイル）。ボット停止後に --check を外して実行してください。")
    elif result.backup is not None:
        print(f"移行: OK（{result.files} ファイル）。バックアップ: {result.backup.relative_to(project_root)}")
    else:
        print("移行対象はありません。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
