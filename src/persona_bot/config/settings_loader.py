"""APIキー（Gemini / Discord）の読み込み。

キーはプロジェクトルート直下の `.env` から読む。
以前は `config_private.py`（実キー・Git 管理外）と `config.py`（テンプレート）の
2ファイルを import で切り替えていたが、次の理由で `.env` 方式へ移行した:

- 実キーが .py ファイルだと「import できてしまう」ため、うっかり別モジュールから
  参照したり、__pycache__ に残った .pyc から古い値で動いたりする事故が起きうる。
- サーバー運用（systemd の EnvironmentFile= や Docker の --env-file）では
  環境変数でキーを渡すのが定石で、.env 方式ならコードを変えずにそのまま移行できる。

`load_settings()` の戻り値は従来どおり `.API_KEY_GEMINI` / `.TOKEN_DISCORD` の
属性を持つオブジェクトなので、呼び出し側（bot.py）の書き方は変わらない。
"""

import logging

import os
from dataclasses import dataclass
from pathlib import Path

from persona_bot.config.constants import ENV_FILE_PATH


logger = logging.getLogger(__name__)

# python-dotenv が未インストールでも「起動すらできない」状態にはしない。
# systemd の EnvironmentFile= や Docker の --env-file のように OS 側から
# 環境変数を渡す運用では .env の読み込み自体が不要なため、import に失敗しても
# os.environ だけで動作を継続できるようにしておく（それでもキーが得られなければ、
# あとの未設定チェックで止まる）。
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]


# .env.example に書いてあるプレースホルダ。コピーしたまま書き換え忘れた値を
# 「未設定」として弾くために使う。これを通してしまうと、起動自体は成功して
# 最初の API 呼び出しで認証エラーになり、原因の切り分けが難しくなる。
# .env.example の右辺を変更したら、ここも必ず合わせて直すこと。
_PLACEHOLDER_VALUES = {
    "YOUR_GEMINI_API_KEY",
    "YOUR_DISCORD_BOT_TOKEN",
}

# 必須キーの名前 → 未設定時に出す案内文（どこで取得できるか）。
_REQUIRED_KEYS = {
    "API_KEY_GEMINI": "Google Gemini の APIキー（https://aistudio.google.com/app/apikey で取得）",
    "TOKEN_DISCORD": "Discord ボットのトークン（https://discord.com/developers/applications で取得）",
}

# `.env` の読み込み結果。案内メッセージの出し分けにだけ使う。
_ENV_LOADED = "loaded"        # .env を読み込んだ
_ENV_MISSING = "missing"      # .env が存在しない
_ENV_NO_DOTENV = "no_dotenv"  # python-dotenv が未インストールで読み込めない


class SettingsError(RuntimeError):
    """APIキーが揃っていないときに送出する例外。

    `load_settings()` は bot.py のトップレベルで呼ばれるため、ここで例外を投げると
    ボットは起動しない。キーが無いまま起動すると Discord へは接続できても
    Gemini の呼び出しが全滅する、といった中途半端な状態になるのを防ぐ。
    """


@dataclass(frozen=True)
class Settings:
    """読み込んだAPIキーの入れ物。

    属性名は旧 config_private.py の変数名と同一にしてある。おかげで
    `settings.API_KEY_GEMINI` / `settings.TOKEN_DISCORD` という bot.py 側の
    参照はそのまま動く（移行のために bot.py を書き換えなくてよい）。
    frozen=True は、読み込み後にキーを書き換えられないようにするため。
    """

    API_KEY_GEMINI: str
    TOKEN_DISCORD: str


def _load_env_file(env_path: Path) -> str:
    """`.env` を環境変数へ読み込む。戻り値は上の `_ENV_*` のいずれか。

    見つからなくてもここでは止めない。OS 側から環境変数が渡されていれば
    `.env` は不要なので、「実際に値が無い」と分かった時点（`load_settings`）で
    まとめて案内する。
    """
    if load_dotenv is None:
        logger.warning('警告: python-dotenv が見つかりません。.env は読み込まず、OSの環境変数だけで続行します。')
        logger.warning('      .env からキーを読み込むには `pip install -r requirements.txt` を実行してください。')
        return _ENV_NO_DOTENV

    if not env_path.exists():
        return _ENV_MISSING

    # override=False（python-dotenv の既定値）をあえて明示している。
    # OS の環境変数が既にある場合はそちらを優先する ＝ 将来 systemd や Docker から
    # キーを渡す運用へ移したときに、サーバーに残った古い .env が黙って優先されて
    # 「直したはずのキーが反映されない」事故が起きないようにするため。
    # encoding も明示する（Windows の既定エンコーディングは cp932 で、
    # 日本語コメント入りの UTF-8 な .env を読むと文字化けしうる）。
    load_dotenv(dotenv_path=env_path, override=False, encoding="utf-8-sig")
    return _ENV_LOADED


def _read_key(name: str) -> str:
    """環境変数から1つのキーを読む。「未設定」とみなす場合は空文字を返す。

    未設定の判定条件（`.env` が無いケースは呼び出し側で吸収される）:
      - 変数そのものが存在しない
      - 値が空文字、または空白のみ
      - 値が .env.example のプレースホルダのまま
    """
    value = os.environ.get(name, "").strip()
    if not value or value in _PLACEHOLDER_VALUES:
        return ""
    return value


def _report_missing(missing: list[str], env_status: str) -> None:
    """キーが足りないことを日本語で案内する（例外の送出は呼び出し側で行う）。"""
    # 移行スクリプト等から単独で呼んでも、既定のWARNING閾値で対処方法まで表示する。
    # この1行は「キーが無いときの合図」として仕様で決まっている文言のため、
    # 変更・削除しないこと。詳しい案内はこの後ろに続ける。
    logger.error('キー未設定')
    logger.error('APIキーが設定されていないため、ボットを起動できません。')
    logger.error('以下のキーが見つかりませんでした:')
    for name in missing:
        logger.error('  - %s: %s', name, _REQUIRED_KEYS[name])

    if env_status == _ENV_MISSING:
        logger.error('.env が見つかりません: <PATH>')
        logger.error('  .env.example をコピーして .env を作り、上記のキーを書き込んでください。')
        logger.error('    Windows (PowerShell): Copy-Item .env.example .env')
        logger.error('    Linux / macOS:        cp .env.example .env')
    elif env_status == _ENV_NO_DOTENV:
        logger.error('python-dotenv が未インストールのため .env を読み込めていません。')
        logger.error('  `pip install -r requirements.txt` を実行するか、')
        logger.error('  OS の環境変数（systemd なら EnvironmentFile=）でキーを渡してください。')
    else:
        logger.error('.env は読み込みましたが、上記のキーが空か、.env.example のままです: <PATH>')
        logger.error('  `API_KEY_GEMINI=（実際の値）` のように、= の右側を自分のキーへ書き換えてください。')
        logger.error('  ※ = の前後に空白を入れないこと。')

    logger.error('設定後、src/ の中から `python bot.py` で起動し直してください。')


def load_settings() -> Settings:
    """APIキーを読み込んで返す。1つでも欠けていれば `SettingsError` を送出する。"""
    env_status = _load_env_file(ENV_FILE_PATH)

    values: dict[str, str] = {}
    missing: list[str] = []
    for name in _REQUIRED_KEYS:
        value = _read_key(name)
        if value:
            values[name] = value
        else:
            missing.append(name)

    if missing:
        _report_missing(missing, env_status)
        # bot.py のトップレベルから呼ばれるので、ここで投げればボットは起動しない。
        raise SettingsError(f"APIキーが未設定です: {', '.join(missing)}")

    return Settings(**values)
