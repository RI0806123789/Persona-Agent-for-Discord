"""保存先を変更する際、旧データを残したまま新規作成することを防ぐ。"""

from pathlib import Path

from persona_bot.config.constants import PROJECT_ROOT

# 旧配置（ルート直下）のデータフォルダ名。deploy/migrate_data_directory.py の LEGACY_DIRECTORY_NAMES と
# 同じ値にすること（一致は tests/deploy/test_shared_constants.py が確かめる）。
LEGACY_DIRECTORY_NAMES = ("memory", "ng")


def ensure_data_directory_ready(project_root: Path = PROJECT_ROOT) -> None:
    """旧配置にデータがあれば、何も変更せず移行手順を案内する。"""
    for name in LEGACY_DIRECTORY_NAMES:
        legacy = project_root / name
        if legacy.is_symlink() or (legacy.exists() and not legacy.is_dir()):
            raise RuntimeError(
                "旧データ保存先の状態を確認してください。"
                "ボットを停止し、ルートで python deploy/migrate_data_directory.py --check を実行してください。"
            )
        if legacy.is_dir() and any(path.name != ".gitkeep" for path in legacy.iterdir()):
            raise RuntimeError(
                "旧 memory/ または ng/ にデータが残っています。"
                "ボットを停止し、ルートで python deploy/migrate_data_directory.py を実行して"
                " data/ 配下へ移行してから起動してください。"
            )
