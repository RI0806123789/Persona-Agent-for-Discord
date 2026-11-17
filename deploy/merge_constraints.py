"""OS と Python の版ごとの pip freeze を合成し、推移依存の制約ファイル（constraints.txt）を作る（Issue #285）。

.github/workflows/update-constraints.yml が、requirements.txt を制約なしで解決した結果
（環境ごとの pip freeze --all）を渡して実行する。標準ライブラリだけで動く。

    python deploy/merge_constraints.py --requirements requirements.txt --output constraints.txt freeze/*.txt

入力のファイル名は「<OS>-<Python の版>.txt」（例: Linux-3.12.txt・Windows-3.13.txt）にする。
直接依存（requirements.txt に書いたもの）と pip・setuptools・wheel は書かない。版が環境で分かれた依存だけ、
環境マーカー（python_version・sys_platform）を付けて1環境に1行だけが当てはまるように書き分ける。
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Callable, Iterable

# 仮想環境を作るときに入る道具。依存ではないので固定しない
BOOTSTRAP_PACKAGES = frozenset({"pip", "setuptools", "wheel"})

# OS の名前（GitHub Actions の runner.os）→ sys_platform の値
PLATFORMS = {"Linux": "linux", "Windows": "win32", "macOS": "darwin"}

HEADER = (
    "# 推移依存（requirements.txt の直接依存が引き込む依存）の固定版。requirements.txt の「-c constraints.txt」から読まれる。",
    "# 直接依存は requirements.txt で固定するので、ここには書かない。",
    "# 作り直すときは .github/workflows/update-constraints.yml を手動で実行し、成果物の constraints.txt で置き換える。",
    "# 公開日の上限の例外（推移依存のセキュリティ修正）は .github/constraints/security-exceptions.txt に書いてから作り直す。",
    "# 1行ずつの更新は Dependabot が提案する（環境マーカー付きの依存は .github/dependabot.yml で除外し、作り直しで更新する）。",
)

_LABEL = re.compile(r"^(?P<os>" + "|".join(PLATFORMS) + r")-(?P<python>3\.\d+)$")
_FREEZE_LINE = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;#]+)$")
_REQUIREMENT_NAME = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)")


class ConstraintsError(ValueError):
    """制約ファイルを作れない入力。"""


@dataclass(frozen=True, order=True)
class Environment:
    python: tuple[int, int]
    platform: str  # sys_platform の値

    @property
    def python_text(self) -> str:
        return f"{self.python[0]}.{self.python[1]}"


@dataclass(frozen=True)
class Condition:
    """環境マーカーの文字列と、同じ判定を Python で行う関数（生成結果の自己点検に使う）。"""

    text: str
    matches: Callable[[Environment], bool]


def normalize_name(name: str) -> str:
    """PEP 503 の正規化（大文字小文字・区切り記号の違いをそろえる）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_environment_label(stem: str) -> Environment:
    match = _LABEL.match(stem)
    if not match:
        raise ConstraintsError(
            f"入力のファイル名「{stem}」から環境が分かりません（<OS>-<Python の版>、例: Linux-3.12）。"
        )
    major, minor = match.group("python").split(".")
    return Environment((int(major), int(minor)), PLATFORMS[match.group("os")])


def parse_freeze(text: str, *, source: str) -> dict[str, str]:
    """pip freeze の結果を「正規化した名前 → 版」にする。名前==版 以外の行は誤りとして止める。"""
    pins: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _FREEZE_LINE.match(line)
        if not match:
            # 編集可能インストールや URL 指定は版で固定できない。黙って落とさず止める
            raise ConstraintsError(f"{source} の {number} 行目は「名前==版」の形ではありません。")
        name = normalize_name(match.group("name"))
        if name in pins and pins[name] != match.group("version"):
            raise ConstraintsError(f"{source} に {name} が異なる版で2回あります。")
        pins[name] = match.group("version")
    return pins


def parse_constraint_line(raw: str) -> tuple[str, str, str] | None:
    """constraints.txt の1行を (正規化した名前, 版, 環境マーカー) にする。コメントと空行は None。

    マーカーが無ければ空文字。「名前==版」の形でない行は ConstraintsError にする。
    生成結果の読み戻し（テスト）と、ここで作る行の書式を同じ定義にそろえるために置いている。
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    requirement, _, marker = line.partition(";")
    match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", requirement.strip())
    if match is None:
        raise ConstraintsError(f"「名前==版」でない行があります: {line}")
    return normalize_name(match.group(1)), match.group(2), marker.strip()


def direct_dependency_names(requirements_text: str) -> set[str]:
    """requirements.txt に書いた直接依存の名前（正規化済み）。-c などの指定とコメントは飛ばす。"""
    names = set()
    for raw in requirements_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME.match(line)
        if match:
            names.add(normalize_name(match.group("name")))
    return names


def _python_condition(pythons: set[tuple[int, int]], known: list[tuple[int, int]]) -> Condition | None:
    """Python の版の条件。範囲が続いていれば不等号で書き、知らない新しい版も最も新しい側に含める。"""
    if set(known) <= pythons:
        return None

    def text_of(version: tuple[int, int]) -> str:
        return f"{version[0]}.{version[1]}"

    indexes = sorted(known.index(version) for version in pythons)
    if indexes == list(range(indexes[0], indexes[-1] + 1)):
        lower = known[indexes[0]] if indexes[0] > 0 else None
        upper = known[indexes[-1] + 1] if indexes[-1] + 1 < len(known) else None
        parts = []
        if lower is not None:
            parts.append(f'python_version >= "{text_of(lower)}"')
        if upper is not None:
            parts.append(f'python_version < "{text_of(upper)}"')
        return Condition(
            " and ".join(parts),
            lambda env: (lower is None or env.python >= lower) and (upper is None or env.python < upper),
        )
    chosen = sorted(pythons)
    return Condition(
        "(" + " or ".join(f'python_version == "{text_of(version)}"' for version in chosen) + ")",
        lambda env: env.python in pythons,
    )


def _platform_condition(platforms: set[str], known: set[str]) -> Condition | None:
    if known <= platforms:
        return None
    chosen = sorted(platforms)
    text = " or ".join(f'sys_platform == "{platform}"' for platform in chosen)
    return Condition(f"({text})" if len(chosen) > 1 else text, lambda env: env.platform in platforms)


def _by_python(subset: set[Environment], universe: set[Environment]) -> Condition | None:
    pythons = {env.python for env in subset}
    if {env for env in universe if env.python in pythons} != subset:
        return None
    return _python_condition(pythons, sorted({env.python for env in universe}))


def _by_platform(subset: set[Environment], universe: set[Environment]) -> Condition | None:
    platforms = {env.platform for env in subset}
    if {env for env in universe if env.platform in platforms} != subset:
        return None
    return _platform_condition(platforms, {env.platform for env in universe})


def _by_both(subset: set[Environment], universe: set[Environment]) -> Condition | None:
    pythons = {env.python for env in subset}
    platforms = {env.platform for env in subset}
    if {env for env in universe if env.python in pythons and env.platform in platforms} != subset:
        return None
    parts = [
        condition
        for condition in (
            _python_condition(pythons, sorted({env.python for env in universe})),
            _platform_condition(platforms, {env.platform for env in universe}),
        )
        if condition is not None
    ]
    if not parts:
        return None
    return Condition(
        " and ".join(part.text for part in parts), lambda env: all(part.matches(env) for part in parts)
    )


def _by_listing(subset: set[Environment], universe: set[Environment]) -> Condition:
    # OS と Python の版の組み合わせで表せないときは、環境を1つずつ並べる（生成した版より新しい Python には当てはまらない）
    terms = [f'sys_platform == "{env.platform}" and python_version == "{env.python_text}"' for env in sorted(subset)]
    text = terms[0] if len(terms) == 1 else " or ".join(f"({term})" for term in terms)
    return Condition(text, lambda env: env in subset)


# 簡単な書き方から順に試す。Python の版の範囲で書ければ、生成していない新しい版にも最も新しい行が当てはまる
_STRATEGIES = (_by_python, _by_platform, _by_both, _by_listing)


def _conditions_for(by_version: dict[str, set[Environment]]) -> dict[str, Condition]:
    """版ごとの条件。1つの依存の全部の版に同じ書き方を使い、どの環境でもちょうど1行だけが当てはまるものを選ぶ。"""
    universe = set().union(*by_version.values())  # その依存が入る環境（入らない環境では制約は使われない）
    for strategy in _STRATEGIES:
        conditions = {version: strategy(envs, universe) for version, envs in by_version.items()}
        if any(condition is None for condition in conditions.values()):
            continue
        # 自己点検: 入る環境ごとに、当てはまる行がちょうど1つで、それが pip freeze の版と一致すること
        if all(
            [version for version, condition in conditions.items() if condition.matches(env)]
            == [version for version, envs in by_version.items() if env in envs]
            for env in universe
        ):
            return conditions
    raise ConstraintsError("環境マーカーを1環境1行に書き分けられませんでした。")


def merge_constraints(freezes: dict[Environment, dict[str, str]], direct: Iterable[str]) -> list[str]:
    """環境ごとの固定版を合成した制約の行（名前の順）。"""
    if not freezes:
        raise ConstraintsError("pip freeze の結果が1つもありません。")
    skipped = BOOTSTRAP_PACKAGES | {normalize_name(name) for name in direct}
    names = sorted({name for pins in freezes.values() for name in pins} - skipped)
    lines = []
    for name in names:
        by_version: dict[str, set[Environment]] = {}
        for env, pins in freezes.items():
            if name in pins:
                by_version.setdefault(pins[name], set()).add(env)
        if len(by_version) == 1:
            # 入らない環境では制約は無視されるので、版が1つなら条件は要らない
            lines.append(f"{name}=={next(iter(by_version))}")
            continue
        try:
            conditions = _conditions_for(by_version)
        except ConstraintsError as error:
            raise ConstraintsError(f"{name}: {error}") from error
        for version, envs in sorted(by_version.items(), key=lambda item: min(item[1])):
            lines.append(f"{name}=={version}; {conditions[version].text}")
    return lines


def render(lines: list[str], environments: Iterable[Environment]) -> str:
    labels = ", ".join(
        f"{next(os for os, value in PLATFORMS.items() if value == env.platform)}-{env.python_text}"
        for env in sorted(environments, key=lambda env: (env.platform, env.python))
    )
    return "\n".join([*HEADER, f"# 生成した環境: {labels}", *lines]) + "\n"


def build_constraints(requirements: Path, freeze_files: Iterable[Path]) -> str:
    freezes: dict[Environment, dict[str, str]] = {}
    for path in freeze_files:
        env = parse_environment_label(path.stem)
        if env in freezes:
            raise ConstraintsError(f"同じ環境（{path.stem}）の結果が2つあります。")
        freezes[env] = parse_freeze(path.read_text(encoding="utf-8-sig"), source=path.name)
    direct = direct_dependency_names(requirements.read_text(encoding="utf-8-sig"))
    return render(merge_constraints(freezes, direct), freezes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="環境ごとの pip freeze から constraints.txt を作る。")
    parser.add_argument("--requirements", type=Path, required=True, help="直接依存を書いた requirements.txt")
    parser.add_argument("--output", type=Path, required=True, help="書き出す constraints.txt")
    parser.add_argument("freeze_files", type=Path, nargs="+", help="<OS>-<Python の版>.txt の pip freeze")
    args = parser.parse_args(argv)
    try:
        text = build_constraints(args.requirements, args.freeze_files)
    except (ConstraintsError, OSError) as error:
        print(f"constraints.txt を作れませんでした: {error}", file=sys.stderr)
        return 1
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"{args.output} に {text.count(chr(10)) - len(HEADER) - 1} 件を書きました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
