"""依存の更新提案と脆弱性検査の設定（Issue #285）が約束を守っていることを検証する。

PyYAML はボットの依存に無い（足さない）ため、YAML は解析せず、守るべき約束を文字列として確かめる。
GitHub 上での実際の動作（Dependabot の提案・Actions の実行）はここでは確かめられない。
"""

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
GITHUB_DIR = ROOT / ".github"
DEPENDABOT_PATH = GITHUB_DIR / "dependabot.yml"
WORKFLOWS_DIR = GITHUB_DIR / "workflows"
AUDIT_WORKFLOW_PATH = WORKFLOWS_DIR / "dependency-audit.yml"
AUDIT_TOOL_REQUIREMENTS = GITHUB_DIR / "audit" / "requirements.txt"
CONSTRAINTS_WORKFLOW_PATH = WORKFLOWS_DIR / "update-constraints.yml"
CONSTRAINTS_TOOL_REQUIREMENTS = GITHUB_DIR / "constraints" / "requirements.txt"
SECURITY_EXCEPTIONS_PATH = GITHUB_DIR / "constraints" / "security-exceptions.txt"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
CONSTRAINTS_PATH = ROOT / "constraints.txt"

sys.path.insert(0, str(ROOT / "deploy"))

import merge_constraints  # noqa: E402  constraints.txt を作る合成スクリプト（名前の正規化を共用する）

# 「公開から2週間以上たった版に固定する」方針（tools/console/backend/requirements.txt・.npmrc と同じ）
MIN_COOLDOWN_DAYS = 14

# 更新を提案させる対象: (エコシステム, ディレクトリ) → そこに無ければならない依存の定義ファイル
EXPECTED_UPDATE_TARGETS = {
    ("pip", "/"): ("requirements.txt",),
    ("pip", "/tools/console/backend"): ("requirements.txt",),
    ("pip", "/.github/audit"): ("requirements.txt",),
    ("pip", "/.github/constraints"): ("requirements.txt",),
    ("npm", "/tools/console/frontend"): ("package.json", "package-lock.json"),
    ("npm", "/Site"): ("package.json", "package-lock.json"),
    ("github-actions", "/"): (".github/workflows",),
}

# 変更されたら検査を走らせる依存の定義ファイル（push と pull_request の両方）
EXPECTED_AUDIT_PATHS = {
    "requirements.txt",
    "constraints.txt",
    "tools/console/backend/requirements.txt",
    "tools/console/frontend/package.json",
    "tools/console/frontend/package-lock.json",
    "Site/package.json",
    "Site/package-lock.json",
    ".github/audit/requirements.txt",
    ".github/workflows/dependency-audit.yml",
}

# pip-audit で照合する requirements（ボット本体と管理コンソールのバックエンド）
EXPECTED_PIP_AUDIT_TARGETS = ("requirements.txt", "tools/console/backend/requirements.txt")

# 影響が無いと確かめて pip-audit で除外している脆弱性 → 除外の前提になっている requirements.txt の行。
# この行が変わったら（Dependabot の更新提案など）テストが失敗し、除外を外すか見直すよう促す。
EXPECTED_IGNORED_VULNS = {
    # CVE-2025-69277（PyNaCl 1.6.2 未満）。discord.py 2.7.1 の [voice] が PyNaCl<1.6 に制約しているあいだだけ除外する
    "GHSA-mrfv-m5wm-5w6w": "discord.py[voice]==2.7.1",
}

_SHA_PINNED_USES = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def _read(path: Path) -> str:
    # read_text は改行を \n にそろえるので、Windows の CRLF のチェックアウトでも同じに読める
    return path.read_text(encoding="utf-8")


def _dependabot_entries() -> list[str]:
    """dependabot.yml の updates を、`- package-ecosystem:` ごとの塊に分ける。"""
    text = _read(DEPENDABOT_PATH)
    starts = [match.start() for match in re.finditer(r"^  - package-ecosystem:", text, re.MULTILINE)]
    return [text[start:end] for start, end in zip(starts, starts[1:] + [len(text)])]


def _entry_ecosystem(entry: str) -> str:
    return re.match(r"  - package-ecosystem:\s*(\S+)", entry).group(1)


def _entry_directories(entry: str) -> list[str]:
    single = re.search(r'^    directory:\s*"([^"]+)"', entry, re.MULTILINE)
    if single:
        return [single.group(1)]
    listed = re.search(r'^    directories:\n((?:      - "[^"]+"\n)+)', entry, re.MULTILINE)
    if listed:
        return re.findall(r'"([^"]+)"', listed.group(1))
    return []


def _uses_lines(text: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.MULTILINE)]


class DependabotConfigTests(unittest.TestCase):
    def test_every_manifest_gets_update_proposals(self) -> None:
        """ボット・管理コンソール（バックエンドと画面）・検査ツール・actions のすべてが対象になる。"""
        text = _read(DEPENDABOT_PATH)
        self.assertRegex(text, r"(?m)^version: 2$")
        targets = {
            (_entry_ecosystem(entry), directory)
            for entry in _dependabot_entries()
            for directory in _entry_directories(entry)
        }
        self.assertEqual(targets, set(EXPECTED_UPDATE_TARGETS))
        # ディレクトリを移したのに設定を直し忘れると、Dependabot は黙って何も提案しなくなる
        for (ecosystem, directory), manifests in EXPECTED_UPDATE_TARGETS.items():
            base = ROOT / directory.strip("/")
            for manifest in manifests:
                path = (ROOT / manifest) if ecosystem == "github-actions" else (base / manifest)
                with self.subTest(ecosystem=ecosystem, directory=directory, manifest=manifest):
                    self.assertTrue(path.exists(), f"{path.relative_to(ROOT)} がありません")

    def test_every_update_waits_two_weeks_after_release(self) -> None:
        """公開から2週間未満の版を提案させない（cooldown が抜けた更新対象を作らない）。"""
        entries = _dependabot_entries()
        self.assertTrue(entries)
        for entry in entries:
            ecosystem = _entry_ecosystem(entry)
            with self.subTest(ecosystem=ecosystem):
                self.assertIn("    cooldown:\n", entry)
                days = re.search(r"^      default-days:\s*(\d+)\s*$", entry, re.MULTILINE)
                self.assertIsNotNone(days, "cooldown に default-days がありません")
                self.assertGreaterEqual(int(days.group(1)), MIN_COOLDOWN_DAYS)
                if ecosystem == "github-actions":
                    # github-actions は semver-*-days に対応していない（書くと設定エラーになる）
                    self.assertNotIn("semver-", entry)

    def test_every_update_runs_weekly(self) -> None:
        for entry in _dependabot_entries():
            with self.subTest(ecosystem=_entry_ecosystem(entry)):
                self.assertRegex(entry, r"(?m)^      interval:\s*weekly\s*$")


class AuditWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(AUDIT_WORKFLOW_PATH)

    def test_runs_read_only_without_secrets(self) -> None:
        """フォークからの PR でもシークレットなしで動く（読み取りだけの権限）。"""
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r":\s*write\b")
        self.assertNotIn("secrets.", self.text)
        # pull_request_target はフォークの PR に書き込み権限とシークレットを渡してしまう
        self.assertNotIn("pull_request_target", self.text)
        checkouts = self.text.count("uses: actions/checkout@")
        self.assertGreater(checkouts, 0)
        self.assertEqual(self.text.count("persist-credentials: false"), checkouts)

    def test_runs_when_dependencies_change_and_every_week(self) -> None:
        path_lists = re.findall(r"^    paths:\n((?:      - .+\n)+)", self.text, re.MULTILINE)
        self.assertEqual(len(path_lists), 2, "push と pull_request の両方に paths が必要です")
        for path_list in path_lists:
            self.assertEqual(set(re.findall(r"      - (.+)", path_list)), EXPECTED_AUDIT_PATHS)
        for path in EXPECTED_AUDIT_PATHS:
            with self.subTest(path=path):
                self.assertTrue((ROOT / path).exists(), f"{path} がありません")
        # 依存を変えなくても新しい脆弱性は公開されるので、定期実行も必要
        self.assertRegex(self.text, r"(?m)^  schedule:\n    - cron: '[^']+'$")

    def test_audits_bot_console_and_frontend(self) -> None:
        loop = re.search(r"for target in ([^;]+); do", self.text)
        self.assertIsNotNone(loop)
        self.assertEqual(tuple(loop.group(1).split()), EXPECTED_PIP_AUDIT_TARGETS)
        self.assertIn('python -m pip_audit -r "$target"', self.text)
        # 推移依存が変わる OS と Python の版を、回帰テストと同じ組み合わせで照合する
        self.assertIn("os: [ubuntu-latest, windows-latest]", self.text)
        self.assertIn("python: ['3.11', '3.13']", self.text)
        # 画面（管理コンソール）と紹介サイトの両方の package-lock.json を、同じ手順で照合する
        self.assertIn("directory: [tools/console/frontend, Site]", self.text)
        self.assertIn("working-directory: ${{ matrix.directory }}", self.text)
        # 概要の見出しではなく、実行するコマンドそのものを確かめる
        commands = re.findall(r"^\s*if (npm audit\b[^>]*)>", self.text, re.MULTILINE)
        self.assertEqual([command.strip() for command in commands], ["npm audit --omit=dev"])

    def test_ignored_vulnerabilities_are_documented(self) -> None:
        """除外は影響が無いと確かめたものだけにし、理由と外す条件をコメントに残す。"""
        listed = re.search(r"^\s*ignored_vulns=\(([^)]*)\)$", self.text, re.MULTILINE)
        self.assertIsNotNone(listed, "除外の一覧（ignored_vulns）がありません")
        self.assertEqual(set(listed.group(1).split()), set(EXPECTED_IGNORED_VULNS))
        self.assertIn('python -m pip_audit -r "$target" "${ignore_args[@]}"', self.text)
        # 除外を忘れたまま残さないよう、毎回の概要に出す
        self.assertIn("${ignored_vulns[*]}", self.text)
        for vuln in EXPECTED_IGNORED_VULNS:
            with self.subTest(vuln=vuln):
                comment = re.search(
                    rf"^\s*# - {re.escape(vuln)}\b.*\n((?:\s*#   .*\n)+)", self.text, re.MULTILINE
                )
                self.assertIsNotNone(comment, f"{vuln} の除外の説明がありません")
                self.assertIn("理由:", comment.group(1))
                self.assertIn("外す条件:", comment.group(1))

    def test_ignores_are_revisited_when_their_premise_changes(self) -> None:
        """除外の前提にしている依存の版が変わったら、除外を外すか見直す。"""
        requirements = {line.strip() for line in _read(ROOT / "requirements.txt").splitlines()}
        for vuln, premise in EXPECTED_IGNORED_VULNS.items():
            with self.subTest(vuln=vuln):
                self.assertIn(
                    premise,
                    requirements,
                    f"requirements.txt の {premise} が変わりました。{vuln} がまだ検出されるかを確かめ、"
                    "検出されなければ dependency-audit.yml の除外と説明を外し、このテストの期待値も直してください",
                )

    def test_starts_as_warning_and_can_be_made_required(self) -> None:
        """誤検知を確かめるまでは警告だけにする。必須にするときは AUDIT_REQUIRED とこのテストを直す。"""
        self.assertRegex(self.text, r"(?m)^  AUDIT_REQUIRED: 'false'$")
        # 両方のジョブが、検出時の警告の注釈と、切り替え後だけの失敗を持つ
        self.assertEqual(self.text.count("::warning title=依存の脆弱性検査"), 2)
        self.assertEqual(self.text.count('[ "$failed" -ne 0 ] && [ "$AUDIT_REQUIRED" = "true" ]'), 2)
        self.assertNotIn("continue-on-error", self.text)


class AuditToolPinTests(unittest.TestCase):
    def test_pip_audit_is_pinned_for_ci_only(self) -> None:
        """pip-audit は CI でだけ使う。版を固定し、ボット・管理コンソールの依存には入れない。"""
        requirements = [
            line.strip()
            for line in _read(AUDIT_TOOL_REQUIREMENTS).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(requirements), 1)
        self.assertRegex(requirements[0], r"^pip-audit==\d+(\.\d+)+$")
        self.assertIn(
            "python -m pip install -r .github/audit/requirements.txt", _read(AUDIT_WORKFLOW_PATH)
        )
        for runtime_requirements in EXPECTED_PIP_AUDIT_TARGETS:
            with self.subTest(requirements=runtime_requirements):
                self.assertNotRegex(_read(ROOT / runtime_requirements).lower(), r"pip[-_]audit")


def _constraint_lines() -> list[tuple[str, str, str]]:
    """constraints.txt の行を (正規化した名前, 版, 環境マーカー) にする。コメントと空行は飛ばす。"""
    rows = []
    for raw in _read(CONSTRAINTS_PATH).splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = line.partition(";")
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s]+)", requirement.strip())
        if match is None:
            raise AssertionError(f"constraints.txt に「名前==版」でない行があります: {line}")
        rows.append((merge_constraints.normalize_name(match.group(1)), match.group(2), marker.strip()))
    return rows


def _root_pip_entry() -> str:
    entries = [
        entry
        for entry in _dependabot_entries()
        if _entry_ecosystem(entry) == "pip" and _entry_directories(entry) == ["/"]
    ]
    if len(entries) != 1:
        raise AssertionError("ルート（/）の pip の更新対象がちょうど1つではありません")
    return entries[0]


def _yaml_list_after(entry: str, key: str) -> list[str]:
    block = re.search(rf"^(\s+){re.escape(key)}:\n((?:\1  - .+\n)+)", entry, re.MULTILINE)
    return re.findall(r"- \"?([^\"\n]+?)\"?\s*$", block.group(2), re.MULTILINE) if block else []


class ConstraintsTests(unittest.TestCase):
    """推移依存を constraints.txt で固定する仕組み（Issue #285 の提案3）。"""

    def setUp(self) -> None:
        self.direct = merge_constraints.direct_dependency_names(_read(REQUIREMENTS_PATH))

    def test_requirements_reads_constraints(self) -> None:
        # pip install -r requirements.txt（README・CI・デプロイ）だけで推移依存の固定版が効く
        self.assertRegex(_read(REQUIREMENTS_PATH), r"(?m)^-c constraints\.txt$")
        self.assertTrue(CONSTRAINTS_PATH.exists())

    def test_constraints_hold_only_transitive_pins(self) -> None:
        rows = _constraint_lines()
        self.assertTrue(rows)
        names = {name for name, _, _ in rows}
        # 直接依存は requirements.txt だけで固定する（2か所に書くと更新のたびに食い違う）
        self.assertFalse(names & self.direct)
        self.assertFalse(names & merge_constraints.BOOTSTRAP_PACKAGES)
        # 同じ依存が複数行あるのは、環境マーカーで書き分けたときだけ
        for name in names:
            markers = [marker for row_name, _, marker in rows if row_name == name]
            with self.subTest(name=name):
                if len(markers) > 1:
                    self.assertTrue(all(markers), "複数行の依存には環境マーカーが要ります")

    def test_marker_split_pins_are_not_updated_by_dependabot(self) -> None:
        """書き分けた依存は Dependabot が両方の行を同じ版にしかねないので、提案から外して作り直しで更新する。"""
        split = {name for name, _, marker in _constraint_lines() if marker}
        ignored = {
            merge_constraints.normalize_name(name)
            for name in re.findall(r"- dependency-name:\s*\"?([^\"\n]+?)\"?\s*$", _root_pip_entry(), re.MULTILINE)
        }
        self.assertTrue(split, "今の constraints.txt には書き分けた依存（contourpy）があるはずです")
        self.assertLessEqual(split, ignored)

    def test_security_exceptions_are_documented_and_applied(self) -> None:
        """公開日の上限の例外は、推移依存のセキュリティ修正だけ。理由と外す条件を残し、constraints.txt に反映されている。"""
        pinned: dict[str, set[str]] = {}
        for name, version, _ in _constraint_lines():
            pinned.setdefault(name, set()).add(version)
        comments: list[str] = []
        for raw in _read(SECURITY_EXCEPTIONS_PATH).splitlines():
            line = raw.strip()
            if not line:
                comments = []
                continue
            if line.startswith("#"):
                comments.append(line)
                continue
            match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line)
            self.assertIsNotNone(match, f"「名前==版」でない行があります: {line}")
            name = merge_constraints.normalize_name(match.group(1))
            note = "\n".join(comments)
            with self.subTest(name=name):
                self.assertNotIn(name, self.direct)  # 直接依存は requirements.txt で人が選ぶ
                self.assertRegex(note, r"(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}|PYSEC-\d{4}-\d+|CVE-\d{4}-\d+)")
                self.assertIn("理由", note)
                self.assertIn("外す条件", note)
                # 作り直しで例外が効き、constraints.txt がその版になっている
                self.assertEqual(pinned.get(name), {match.group(2)})
            comments = []

    def test_dependabot_groups_only_transitive_pins(self) -> None:
        """推移依存は1つの PR にまとめ、直接依存は1つずつ提案させる（除外の一覧を requirements.txt とそろえる）。"""
        entry = _root_pip_entry()
        self.assertEqual(_yaml_list_after(entry, "patterns"), ["*"])
        excluded = _yaml_list_after(entry, "exclude-patterns")
        # Dependabot が使う名前は正規化した形なので、その形のまま書く（discord.py ではなく discord-py）
        self.assertEqual(excluded, [merge_constraints.normalize_name(name) for name in excluded])
        self.assertEqual(set(excluded), self.direct)


class ConstraintsWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _read(CONSTRAINTS_WORKFLOW_PATH)

    def test_runs_only_by_hand_and_read_only(self) -> None:
        # 作り直しは差分を人が見て取り込むもの。push・PR・定期では動かさず、コミットもしない
        on_block = re.search(r"(?ms)^on:\n(.*?)^\S", self.text).group(1)
        self.assertEqual(re.findall(r"(?m)^  (\w+):", on_block), ["workflow_dispatch"])
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: read\n")
        self.assertNotRegex(self.text, r":\s*write\b")
        self.assertNotIn("secrets.", self.text)
        self.assertNotRegex(self.text, r"git (commit|push)")
        self.assertEqual(self.text.count("persist-credentials: false"), self.text.count("uses: actions/checkout@"))

    def test_input_reaches_scripts_only_through_env(self) -> None:
        self.assertRegex(self.text, r"(?m)^        default: P14D$")
        # 入力をスクリプトへ直接埋め込むと、コマンドの差し込みになりうる
        self.assertEqual(self.text.count("${{ inputs.uploaded_prior_to }}"), 2)
        self.assertEqual(self.text.count("UPLOADED_PRIOR_TO: ${{ inputs.uploaded_prior_to }}"), 2)
        self.assertIn('--uploaded-prior-to "$UPLOADED_PRIOR_TO"', self.text)

    def test_resolves_production_python_and_keeps_direct_pins(self) -> None:
        self.assertIn("os: [ubuntu-latest, windows-latest]", self.text)
        self.assertIn("python: ['3.11', '3.12', '3.13']", self.text)
        # 直接依存は find-links に置いて公開日の上限を外し、-c の行を除いた requirements.txt で解決する
        self.assertIn(
            "--no-deps -d direct-dists -r requirements-unconstrained.txt -r .github/constraints/security-exceptions.txt",
            self.text,
        )
        self.assertIn(
            "--find-links direct-dists -r requirements-unconstrained.txt -c .github/constraints/security-exceptions.txt",
            self.text,
        )
        self.assertIn("grep -v -E '^[[:space:]]*(-c|--constraint)([[:space:]=]|$)' requirements.txt", self.text)
        self.assertIn("python deploy/merge_constraints.py --requirements requirements.txt", self.text)

    def test_generator_pip_is_pinned_to_a_version_with_upload_time_filter(self) -> None:
        requirements = [
            line.strip()
            for line in _read(CONSTRAINTS_TOOL_REQUIREMENTS).splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(len(requirements), 1)
        match = re.fullmatch(r"pip==(\d+)\.(\d+)(?:\.(\d+))?", requirements[0])
        self.assertIsNotNone(match)
        # --uploaded-prior-to は pip 26.0.1 から
        self.assertGreaterEqual(tuple(int(part or 0) for part in match.groups()), (26, 0, 1))
        self.assertIn("-r .github/constraints/requirements.txt", self.text)

    def test_dependabot_updates_only_pip_next_to_security_exceptions(self) -> None:
        # 例外は人が脆弱性ごとに決める版。同じフォルダの pip の固定版だけを Dependabot に更新させる
        entries = [
            entry for entry in _dependabot_entries() if "/.github/constraints" in _entry_directories(entry)
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(_entry_directories(entries[0]), ["/.github/constraints"])
        self.assertEqual(re.findall(r"- dependency-name:\s*(\S+)", entries[0]), ["pip"])
        self.assertIn("    allow:\n", entries[0])


class WorkflowPinningTests(unittest.TestCase):
    def test_every_action_is_pinned_by_commit_sha(self) -> None:
        """タグは付け替えられるので、actions はコミットSHAで固定する（Dependabot も SHA で更新する）。"""
        workflows = sorted(WORKFLOWS_DIR.glob("*.yml"))
        self.assertIn(AUDIT_WORKFLOW_PATH, workflows)
        for workflow in workflows:
            uses = _uses_lines(_read(workflow))
            self.assertTrue(uses, f"{workflow.name} に uses がありません")
            for action in uses:
                with self.subTest(workflow=workflow.name, action=action):
                    self.assertRegex(action, _SHA_PINNED_USES)


if __name__ == "__main__":
    unittest.main()
