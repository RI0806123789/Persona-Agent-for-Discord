"""README を最小限にして詳細を紹介サイトの説明ページへ移した（#528）後も、文書のリンクと内容が食い違わないことを確かめる。

- 紹介サイトの「使い方」に載せた /functions のボタンの一覧が、ボットの機能パネルのボタンと一致する。
- README などの文書のリポジトリ内のリンク（画像を含む）と、紹介サイトの説明ページ・節へのリンクが実在する。
- キャラ設定パネルの案内が、README の消えた節を指さない。

外部への通信はしない（サイトの URL は、Site/ の HTML と説明ページの文言のファイルと突き合わせる）。
"""

import ast
from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from persona_bot.config.constants import PROMPT_TEMPLATE_FILE_NAME
from persona_bot.config.personas import PersonaCatalog
from persona_bot.views import settings_view

SITE_URL = "https://ri0806123789.github.io/Persona-Agent-for-Discord/"
FUNCTIONS_VIEW = PROJECT_ROOT / "src" / "persona_bot" / "views" / "functions_view.py"
USAGE_GUIDE = PROJECT_ROOT / "Site" / "src" / "guides" / "usage.ts"

# リンクを確かめる文書。README から節を移した先・移した節を指していた文書を含める。
DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "Site/README.md",
    "tools/console/README.md",
    "docs/release-notes/README.md",
    "tests/README.md",
)

_MARKDOWN_LINK = re.compile(r"!?\[[^\]\n]*\]\(([^)\s]+)\)")
_HTML_SRC = re.compile(r"<img [^>]*src=\"([^\"]+)\"")
# 文中のコード（`…`・``…``）。書式の説明に書いた [文字](行き先) をリンクとして読まないために除く
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_SITE_LINK = re.compile(re.escape(SITE_URL) + r"([a-z-]*\.html)?(?:#([a-z0-9-]+))?")


def _github_slug(heading: str) -> str:
    """GitHub が見出しに付けるアンカー（英字は小文字にし、記号・絵文字を除き、空白をハイフンにする）。"""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _anchors(markdown: Path) -> set[str]:
    text = markdown.read_text(encoding="utf-8")
    # コードの例の中の # は見出しではない
    text = re.sub(r"^```.*?^```", "", text, flags=re.S | re.M)
    anchors = {_github_slug(match) for match in re.findall(r"^#{1,6} (.+)$", text, flags=re.M)}
    anchors |= set(re.findall(r"<a id=\"([^\"]+)\"", text))
    return anchors


def _hub_buttons() -> list[str]:
    """機能パネル（FunctionsView）に並ぶボタンの「絵文字 名前」を、画面の上から順に取り出す。"""
    tree = ast.parse(FUNCTIONS_VIEW.read_text(encoding="utf-8"))
    labels: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "__init__"):
                continue
            keywords = {keyword.arg: keyword.value for keyword in call.keywords}
            label, emoji = keywords.get("label"), keywords.get("emoji")
            if isinstance(label, ast.Constant) and isinstance(emoji, ast.Constant):
                labels[node.name] = f"{emoji.value} {label.value}"
        # 🔍 Google検索 は ON/OFF で名前が変わる（"🔍 Google検索: ON" のような文字列を組み立てる）
        for constant in ast.walk(node):
            if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                match = re.fullmatch(r"(\S+ .+): ON", constant.value)
                if match:
                    labels[node.name] = match.group(1)
    view = next(node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "FunctionsView")
    # ast.walk は幅優先なので、行番号で並べ直して画面の順（add_item を呼んだ順）にする
    calls = sorted(
        (call for call in ast.walk(view) if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "add_item"),
        key=lambda call: call.lineno,
    )
    order = [
        call.args[0].func.id
        for call in calls
        if call.args and isinstance(call.args[0], ast.Call) and isinstance(call.args[0].func, ast.Name)
    ]
    return [labels[name] for name in order]


class FunctionsButtonsTests(unittest.TestCase):
    def test_usage_guide_lists_every_hub_button_in_order(self) -> None:
        # サイトの「使い方」の表と、ボットの機能パネルの食い違いを防ぐ（ボタンを足したらサイトも直す）
        guide = re.findall(r'\bbutton: "([^"]+)"', USAGE_GUIDE.read_text(encoding="utf-8"))
        hub = _hub_buttons()
        self.assertEqual(len(hub), 11)
        self.assertEqual(guide, hub)


class DocumentLinkTests(unittest.TestCase):
    def test_repository_links_and_images_exist(self) -> None:
        for name in DOCUMENTS:
            document = PROJECT_ROOT / name
            text = document.read_text(encoding="utf-8")
            text = _CODE_SPAN.sub("", re.sub(r"^```.*?^```", "", text, flags=re.S | re.M))
            for target in [*_MARKDOWN_LINK.findall(text), *_HTML_SRC.findall(text)]:
                if re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                path_part, _, anchor = target.partition("#")
                path = (document.parent / unquote(path_part)).resolve()
                with self.subTest(document=name, target=target):
                    self.assertTrue(path.exists(), f"{name} のリンク先がありません: {target}")
                    if anchor and path.suffix == ".md":
                        self.assertIn(unquote(anchor), _anchors(path), f"{name} のリンク先に節がありません: {target}")

    def test_readme_shows_banner_image(self) -> None:
        # バナー画像は制作者が用意する（#528）。README の冒頭に置き、代替テキストにボット名とキャッチコピーを書く
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        match = re.search(r'<img src="([^"]+)" alt="([^"]+)"', readme)
        self.assertIsNotNone(match)
        title = re.search(r"^# .*Persona Agent for Discord$", readme, flags=re.M)
        self.assertIsNotNone(title)
        self.assertLess(match.start(), title.start())
        self.assertIn("Persona Agent for Discord", match.group(2))
        self.assertIn("呼べば答える。呼ばなくても、話しかけてくる。", match.group(2))
        self.assertTrue((PROJECT_ROOT / match.group(1)).is_file(), f"バナー画像を置いてください: {match.group(1)}")

    def test_readme_title_icon_is_site_favicon(self) -> None:
        # 見出しのアイコンは、紹介サイトのファビコンをそのまま使う（画像を 2 か所に持たない）。
        # 見出しの文字がボット名を読み上げるので、アイコンは飾りとして alt を空にする
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        title = re.search(r"^# (<img [^>]+>) Persona Agent for Discord$", readme, flags=re.M)
        self.assertIsNotNone(title, "README の見出しの先頭にアイコンを置いてください")
        icon = title.group(1)
        self.assertIn('src="Site/public/favicon.svg"', icon)
        self.assertIn('alt=""', icon)
        self.assertTrue((PROJECT_ROOT / "Site" / "public" / "favicon.svg").is_file())
        index = (PROJECT_ROOT / "Site" / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="./favicon.svg"', index)

    def test_site_links_point_to_existing_pages_and_sections(self) -> None:
        found = 0
        for name in DOCUMENTS:
            for page, anchor in _SITE_LINK.findall((PROJECT_ROOT / name).read_text(encoding="utf-8")):
                found += 1
                page = page or "index.html"
                with self.subTest(document=name, page=page, anchor=anchor):
                    self.assertTrue((PROJECT_ROOT / "Site" / page).is_file(), f"{name}: サイトにページがありません: {page}")
                    if anchor:
                        guide = PROJECT_ROOT / "Site" / "src" / "guides" / f"{Path(page).stem}.ts"
                        self.assertIn(f'id: "{anchor}"', guide.read_text(encoding="utf-8"), f"{name}: {page} に #{anchor} がありません")
        self.assertGreater(found, 0)

    def test_readme_links_every_guide_page_and_keeps_no_setup_steps(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        for page in ("setup.html", "persona.html", "usage.html", "operations.html"):
            self.assertIn(SITE_URL + page, readme)
        # セットアップ・運用の手順はサイトにだけ置く（README を最小限に保つ。#528 の決定）
        for command in ("pip install -r requirements.txt", "systemctl", "python bot.py", "cp .env.example"):
            self.assertNotIn(command, readme)


class PersonaNoticeTests(unittest.TestCase):
    def test_notice_points_to_template_instead_of_removed_readme_section(self) -> None:
        notice = settings_view._persona_notice(PersonaCatalog())
        self.assertIn(PROMPT_TEMPLATE_FILE_NAME, notice)
        self.assertNotIn("README", notice)


if __name__ == "__main__":
    unittest.main()
