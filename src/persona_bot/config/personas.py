"""ペルソナ（キャラクター設定）の一覧。

Git が管理するのは `prompts/` の書式見本（テンプレート）だけで、キャラ別のプロンプトは
利用者が各自のパソコン・サーバーで作る。ここでは `prompts/` にある `.txt` を選択肢として
集め、任意の `prompts/personas.json` から表示名・読み上げの声・既定のペルソナを読む。

personas.json の書式見本は `prompts/00_prompt_format.json`（`PERSONA_SETTINGS_TEMPLATE_FILE_NAME`）で、
利用者が personas.json へコピーして書き換える元。ここでは見本を読まない。personas.json の
最上位は `default` と `personas` だけを使い、それ以外（見本の説明欄 `_説明` など）は警告せずに
無視する（見本をコピーしたまま使っても警告が出ないようにするため）。

**保存・選択に使う値は旧版と同じ「src/ 基準の相対パス」（`"../prompts/<ファイル名>"`）。**
ファイル名を変えなければ、DB に保存済みのサーバー設定がそのまま引き継がれる。

**空文字の値は「既定のペルソナに従う」。** サーバーでまだ選んでいない状態を表し、
personas.json の `default` → 先頭のファイル → 組み込みの標準（`BUILTIN_PERSONA_PROMPT`）の
順に解決する。保存済みのファイルが見つからない間も値は消さずに既定で応答し、ファイルを
戻せば元のキャラに戻る（Git の更新でファイルが一時的に消えても設定を失わないため）。

一覧は起動時（`bot.py`）とキャラ設定パネルを開いたときに読み直し、スナップショットを
差し替える。読み込み途中の状態は公開しない。応答・読み上げ・表示はスナップショットだけを
参照するため、イベントループ上でファイルを読まない。
"""

from __future__ import annotations

import logging

import json
import re
import string
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from persona_bot.config import constants
from persona_bot.config.constants import (
    AGENT_AVATAR_URL_MAX_CHARS,
    AGENT_DEFAULT_LINES,
    AGENT_LINE_KEYS,
    DISCORD_SELECT_VALUE_MAX_LENGTH,
    PERSONA_SETTINGS_TEMPLATE_FILE_NAME,
    PERSONA_VALUE_PREFIX,
    PROMPT_TEMPLATE_FILE_NAME,
    VOICE_EQ_PROFILES,
    VOICE_PROFILE_FALLBACK,
)
from persona_bot.config.paths import resolve_prompt_path


logger = logging.getLogger(__name__)

# 「既定のペルソナに従う」を表す値。DB の未設定（空文字）と同じ。
FOLLOW_DEFAULT_PERSONA = ""
# ペルソナを1つも作っていない間の表示名（組み込みの標準プロンプトで応答している）。
BUILTIN_PERSONA_DISPLAY_NAME = "標準（組み込み）"

# edge-tts が受け付けるパラメータの形式（パッケージ内部の検証と同じもの）。
# 形式が違うと edge_tts.Communicate が ValueError を投げ、黙って gTTS へ退避し続ける
# （音は出るのでキャラ差が消えたことに気づきにくい）ため、読み込み時に点検する。
_EDGE_RATE_PATTERN = re.compile(r"^[+-]\d+%$")
_EDGE_PITCH_PATTERN = re.compile(r"^[+-]\d+Hz$")
# Gemini Live の事前定義ボイス名（"Kore" など。#268）。英字で始まる短い名前だけを通す
# （接続の設定にそのまま載るため、記号や改行を含む値を渡さない）。
_LIVE_VOICE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_VOICE_KEYS = ("voice", "pitch", "rate", "eq", "live_voice")
# サブエージェントの進捗の定型文（personas.json の "agent"）に書ける差し込み（#170）。
_AGENT_LINE_FIELDS = frozenset({"role", "step", "current", "total"})
# personas.json の "agent" のうち、定型文ではない項目（子のアイコン。#267）。
_AGENT_AVATAR_KEY = "avatar_url"


@dataclass(frozen=True)
class Persona:
    """選択肢に出すペルソナ1件。"""

    value: str
    name: str
    voice: Mapping[str, Any]
    # サブエージェントの進捗の定型文。書いた項目だけを持ち、残りは AGENT_DEFAULT_LINES を使う。
    agent_lines: Mapping[str, str] = field(default_factory=dict)
    # 作業スレッドで子が投稿するときのアイコンの URL（#267）。None なら Webhook の既定のアイコン。
    agent_avatar_url: str | None = None


@dataclass(frozen=True)
class PersonaCatalog:
    """読み込んだペルソナの一覧（不変のスナップショット）。"""

    personas: tuple[Persona, ...] = ()
    default_value: str = FOLLOW_DEFAULT_PERSONA
    _by_value: Mapping[str, Persona] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_by_value", MappingProxyType({persona.value: persona for persona in self.personas}))

    def get(self, value: str | None) -> Persona | None:
        return self._by_value.get(value) if isinstance(value, str) else None

    def contains(self, value: str | None) -> bool:
        return self.get(value) is not None

    def resolve(self, value: str | None) -> str:
        """応答に使う値を返す。未設定・見つからないファイルは既定のペルソナにする。"""
        return value if self.contains(value) else self.default_value

    def display_name(self, value: str | None) -> str:
        persona = self.get(value)
        if persona is not None:
            return persona.name
        if not value:
            return BUILTIN_PERSONA_DISPLAY_NAME
        # 一覧に無い値（消したファイルの評価記録など）はファイル名で見せる
        return Path(value).stem or str(value)

    def voice_profile(self, value: str | None) -> Mapping[str, Any]:
        persona = self.get(value)
        return persona.voice if persona is not None else VOICE_PROFILE_FALLBACK

    def agent_lines(self, value: str | None) -> Mapping[str, str]:
        """サブエージェントの進捗の定型文。書いていない項目は既定の文面で補う。"""
        persona = self.get(value)
        lines = dict(AGENT_DEFAULT_LINES)
        if persona is not None:
            lines.update(persona.agent_lines)
        return MappingProxyType(lines)

    def agent_avatar_url(self, value: str | None) -> str | None:
        """作業スレッドで子が使うアイコンの URL。書いていなければ None（#267）。"""
        persona = self.get(value)
        return persona.agent_avatar_url if persona is not None else None


_catalog = PersonaCatalog()


def current_persona_catalog() -> PersonaCatalog:
    """最後に読み込んだ一覧を返す（ファイルは読まない）。未読込なら空の一覧。"""
    return _catalog


def set_persona_catalog(catalog: PersonaCatalog) -> None:
    global _catalog
    _catalog = catalog


def persona_value(file_name: str) -> str:
    return PERSONA_VALUE_PREFIX + file_name


def persona_file_name(value: Any) -> str | None:
    """値がペルソナの形（"../prompts/<名前>.txt"）ならファイル名を返す。

    フォルダの区切り・テンプレート・隠しファイルを含む値は拒否する。保存値や
    セレクトの値から prompts/ の外（.env など）を読ませないための関門。
    """
    if not isinstance(value, str) or not value.startswith(PERSONA_VALUE_PREFIX):
        return None
    name = value[len(PERSONA_VALUE_PREFIX):]
    if not _is_persona_file_name(name):
        return None
    return name


def _is_persona_file_name(name: str) -> bool:
    return (
        bool(name)
        and "/" not in name
        and "\\" not in name
        and not name.startswith(".")
        and name != PROMPT_TEMPLATE_FILE_NAME
        and Path(name).suffix.lower() == ".txt"
    )


def is_selectable_persona(value: str) -> bool:
    """確定してよいペルソナか（ファイルを読むので別スレッドで呼ぶ）。"""
    if value == FOLLOW_DEFAULT_PERSONA:
        return True
    if persona_file_name(value) is None:
        return False
    try:
        return resolve_prompt_path(value).is_file()
    except OSError:
        return False


def load_persona_catalog(prompts_dir: Path | None = None, settings_path: Path | None = None) -> PersonaCatalog:
    """prompts/ と personas.json から一覧を作る（ファイルを読むので別スレッドで呼ぶ）。

    フォルダが無いときは空の一覧を返す。フォルダを読めないときは OSError を送出し、
    呼び出し側は前回の一覧を使い続ける。personas.json の誤りは警告して無視する
    （ファイルは書き換えず、そのまま残す）。
    """
    prompts_dir = constants.PROMPTS_DIR if prompts_dir is None else prompts_dir
    settings_path = constants.PERSONA_SETTINGS_PATH if settings_path is None else settings_path
    files = _scan_persona_files(prompts_dir)
    settings = _load_persona_settings(settings_path)

    entries = settings.get("personas")
    if entries is None or not _expect_object(entries, " personas ", "表示名と声の設定を使わずに続行します。"):
        entries = {}

    # personas.json に書いた順を優先し、書いていないファイルはファイル名の順に続ける
    ordered = [name for name in entries if name in files]
    ordered += sorted(name for name in files if name not in entries)
    for name in entries:
        if name not in files:
            logger.warning('警告: personas.json の「<設定値>」に対応するファイルが prompts/ にありません。')

    personas = tuple(_build_persona(name, entries.get(name)) for name in ordered)
    return PersonaCatalog(personas, _resolve_default(settings.get("default"), personas))


def reload_persona_catalog(*, announce: bool = True) -> PersonaCatalog:
    """一覧を読み直して差し替える（読めなければ前回の一覧を保つ）。ファイルを読むので別スレッドで呼べる。

    announce が真なら、読み込んだ件数（0件のときは作り方の案内）を INFO で記録する。起動時用。
    パネルを開くたびの再読込（`refresh_persona_catalog`）では出さない。
    """
    try:
        catalog = load_persona_catalog()
    except OSError as error:
        logger.warning(f'警告: prompts/ を読めませんでした（{type(error).__name__}）。前回のペルソナ一覧を使います。', exc_info=error)
        return _catalog
    set_persona_catalog(catalog)
    if not announce:
        return catalog
    if not catalog.personas:
        logger.info(f'ペルソナ: prompts/ に利用できるペルソナがありません。prompts/{PROMPT_TEMPLATE_FILE_NAME} をコピーして prompts/<名前>.txt を作ると、キャラ設定から選べます（作るまでは組み込みの標準プロンプトで応答します）。表示名と声は prompts/{PERSONA_SETTINGS_TEMPLATE_FILE_NAME} を prompts/personas.json にコピーして決められます。')
    else:
        logger.info(f'ペルソナ: {len(catalog.personas)} 件を読み込みました。')
    return catalog


async def refresh_persona_catalog() -> PersonaCatalog:
    """一覧を別スレッドで読み直して差し替える（キャラ設定パネルを開くとき用）。"""
    from persona_bot.core.background import run_in_thread

    return await run_in_thread(reload_persona_catalog, announce=False)


def _scan_persona_files(prompts_dir: Path) -> set[str]:
    if not prompts_dir.is_dir():
        return set()
    names: set[str] = set()
    for path in prompts_dir.iterdir():
        name = path.name
        if not _is_persona_file_name(name) or not path.is_file():
            continue
        if len(persona_value(name)) > DISCORD_SELECT_VALUE_MAX_LENGTH:
            logger.warning('警告: ペルソナのファイル名が長すぎるため選択肢に出せません（<設定値>）。短い名前に変えてください。')
            continue
        names.add(name)
    return names


def _load_persona_settings(settings_path: Path) -> dict[str, Any]:
    try:
        # 手書きのファイルなので、メモ帳の「UTF-8 (BOM 付き)」保存でも読めるようにする
        text = settings_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        logger.warning(f'警告: personas.json を読めませんでした（{type(error).__name__}）。表示名と声の設定を使わずに続行します。', exc_info=error)
        return {}
    try:
        settings = json.loads(text)
    except json.JSONDecodeError as error:
        logger.warning(f'警告: personas.json の {error.lineno} 行目付近が JSON として読めません。表示名と声の設定を使わずに続行します（ファイルはそのまま残します）。', exc_info=error)
        return {}
    if not isinstance(settings, dict):
        logger.warning('警告: personas.json の最上位はオブジェクト（{ }）で書いてください。表示名と声の設定を使わずに続行します。')
        return {}
    return settings


def _expect_object(value: Any, subject: str, consequence: str) -> bool:
    """値が JSON のオブジェクト（dict）なら True。違えば警告して False（呼び出し側が既定へ戻す）。

    subject は警告文の「personas.json の」と「はオブジェクト」の間に入る文字列、consequence は
    警告の最後に添える「どうなるか」の説明。設定値そのもの（利用者のファイル名など）は載せない。
    """
    if isinstance(value, dict):
        return True
    logger.warning(f'警告: personas.json の{subject}はオブジェクト（{{ }}）で書いてください。{consequence}')
    return False


def _unsupported_keys(section: str, raw: dict[str, Any], allowed: tuple[str, ...]) -> set[str]:
    """raw のうち allowed に無い項目名を返す。あれば使える項目を 1 回だけ警告する。

    項目名そのもの（利用者が書いた文字列）はログに出さない。呼び出し側は返された項目を読み飛ばす。
    """
    unknown = set(raw) - set(allowed)
    if unknown:
        logger.warning("警告: personas.json の %s に未対応の項目があります。使える項目: %s", section, ', '.join(allowed))
    return unknown


def _build_persona(file_name: str, entry: Any) -> Persona:
    if entry is not None and not _expect_object(entry, "「<設定値>」", "既定の表示名と声を使います。"):
        entry = {}
    entry = entry or {}
    name = entry.get("name")
    display = " ".join(name.split()) if isinstance(name, str) else ""
    if name is not None and not display:
        logger.warning('警告: personas.json の「<設定値>」の name が空か文字列ではないため、ファイル名を表示名にします。')
    return Persona(
        value=persona_value(file_name),
        name=display or Path(file_name).stem,
        voice=MappingProxyType(_build_voice(file_name, entry.get("voice"))),
        agent_lines=MappingProxyType(_build_agent_lines(file_name, entry.get("agent"))),
        agent_avatar_url=_build_agent_avatar_url(file_name, entry.get("agent")),
    )


def _build_voice(file_name: str, voice: Any) -> dict[str, Any]:
    profile = dict(VOICE_PROFILE_FALLBACK)
    if voice is None or not _expect_object(voice, "「<設定値>」の voice ", "既定の声を使います。"):
        return profile
    unsupported = _unsupported_keys("voice", voice, _VOICE_KEYS)
    for key, value in voice.items():
        if key in unsupported:
            continue
        if key == "live_voice" and value == "":
            # 空は「既定の声を使う」という有効な指定（既定値と同じ）。警告を出さずそのまま採用する。
            profile[key] = ""
            continue
        if not isinstance(value, str) or not value:
            logger.warning(f'警告: personas.json の「<設定値>」の voice.{key} は文字列で書いてください。既定値を使います。')
            continue
        if key == "eq" and value not in VOICE_EQ_PROFILES:
            # 実行時は未知の音色を加工なしで読み上げるだけで、気づきにくい。読み込み時に知らせる。
            logger.warning("警告: personas.json の「<設定値>」の eq が未登録の音色です（<設定値>）。加工なしの音色を使います。")
            continue
        if key == "rate" and not _EDGE_RATE_PATTERN.match(value):
            logger.warning("警告: personas.json の「<設定値>」の rate が不正です（<設定値>）。'+5%' のような形式で指定してください。既定値を使います。")
            continue
        if key == "pitch" and not _EDGE_PITCH_PATTERN.match(value):
            logger.warning("警告: personas.json の「<設定値>」の pitch が不正です（<設定値>）。'+10Hz' のような形式で指定してください。既定値を使います。")
            continue
        if key == "live_voice" and not _LIVE_VOICE_PATTERN.match(value):
            logger.warning("警告: personas.json の「<設定値>」の live_voice が不正です（<設定値>）。'Kore' のような英字の名前で指定してください。既定の声を使います。")
            continue
        profile[key] = value
    return profile


def _build_agent_lines(file_name: str, agent: Any) -> dict[str, str]:
    """サブエージェントの進捗の定型文を点検する（案A。LLM を呼ばない。#170）。

    差し込みの書き間違い（{stpe} や閉じ忘れの {）は表示のたびに例外になるため、
    読み込み時に確かめて、その項目だけ既定の文面に戻す（ファイルは書き換えない）。
    """
    if agent is None or not _expect_object(agent, "「<設定値>」の agent ", "既定の文面を使います。"):
        return {}
    unsupported = _unsupported_keys("agent", agent, (*AGENT_LINE_KEYS, _AGENT_AVATAR_KEY))
    lines: dict[str, str] = {}
    for key, value in agent.items():
        if key == _AGENT_AVATAR_KEY:
            # 文面ではないので _build_agent_avatar_url が点検する
            continue
        if key in unsupported:
            continue
        if not isinstance(value, str) or not value.strip():
            logger.warning(f'警告: personas.json の「<設定値>」の agent.{key} は文字列で書いてください。既定の文面を使います。')
            continue
        if not _is_valid_agent_line(value):
            logger.warning(f"警告: personas.json の「<設定値>」の agent.{key} の差し込みが不正です。使えるのは {{{'}, {'.join(sorted(_AGENT_LINE_FIELDS))}}} だけで、文字の {{ }} は {{{{ }}}} と書きます。既定の文面を使います。")
            continue
        lines[key] = value.strip()
    return lines


def _build_agent_avatar_url(file_name: str, agent: Any) -> str | None:
    """作業スレッドで子が使うアイコンの URL を点検する（#267）。

    Discord が画像を取りに行く URL なので https:// で始まる1行だけを受け付ける。
    誤りは警告して Webhook の既定のアイコンを使う（URL そのものはログに出さない）。
    """
    if not isinstance(agent, dict) or _AGENT_AVATAR_KEY not in agent:
        return None
    value = agent[_AGENT_AVATAR_KEY]
    url = value.strip() if isinstance(value, str) else ""
    if (
        not url.startswith("https://")
        or len(url) > AGENT_AVATAR_URL_MAX_CHARS
        or any(character.isspace() for character in url)
    ):
        logger.warning(f'警告: personas.json の「<設定値>」の agent.{_AGENT_AVATAR_KEY} は https:// で始まる{AGENT_AVATAR_URL_MAX_CHARS}文字以内の画像の URL で書いてください。既定のアイコンを使います。')
        return None
    return url


def _is_valid_agent_line(text: str) -> bool:
    try:
        # 書式の幅指定や変換は使わない。検証中の format でも巨大な文字列を作らせない。
        return all(
            name in _AGENT_LINE_FIELDS and not spec and conversion is None
            for _literal, name, spec, conversion in string.Formatter().parse(text)
            if name is not None
        )
    except ValueError:
        return False


def _resolve_default(default: Any, personas: tuple[Persona, ...]) -> str:
    values = {persona.value for persona in personas}
    if default is not None:
        if isinstance(default, str) and persona_value(default) in values:
            return persona_value(default)
        logger.warning('警告: personas.json の default に指定したファイルがありません。先頭のペルソナを既定にします。')
    return personas[0].value if personas else FOLLOW_DEFAULT_PERSONA
