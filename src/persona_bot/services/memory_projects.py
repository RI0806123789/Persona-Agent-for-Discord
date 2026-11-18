"""記憶サービスの純粋な補助関数（#469）。

projects.json の初期構造・ユーザー別スライスの取り出し・文書要約の統合と文脈化・
圧縮対象ログの整形・定型発言の判定を担う。DB・ファイル・Gemini に触れないので、
単体で検証できる。`memory_service` が従来の名前で再公開する。
"""

import re
from typing import Any

# projects.json のユーザー別スキーマ識別子。users 辞書がある場合は識別子欠落時も保護する。
PROJECTS_SCHEMA = "per_user_v1"

# 記憶参照が明らかに不要な短い定型発言（あいさつ・相槌など）。ルーターの LLM 呼び出しを
# 省いて category="none" を即決するための簡易判定に使う（API 呼び出し削減）。
_TRIVIAL_MESSAGE_PATTERN = re.compile(
    r"^(おはよう|おはよ|こんにちは|こんばんは|やあ|やっほー?|ハロー|hello|hi|hey"
    r"|ありがと(う|ね)?|thanks?|thx|了解|りょうかい|おk|ok|おけ|うん|はい|いいえ"
    r"|なるほど|そうだね|そっか|わかった|分かった|おやすみ|おつかれ?|お疲れ様?でした?"
    r"|バイバイ|またね|乙|w+|笑|草)[！!。、.?？~〜ー\s]*$",
    re.IGNORECASE,
)


def default_profile_memory() -> dict[str, Any]:
    return {}


def default_projects_memory() -> dict[str, Any]:
    """projects.json の空の初期構造（ユーザー別スキーマ）。"""
    return {"_schema": PROJECTS_SCHEMA, "users": {}}


def user_projects(wrapper: dict[str, Any], user_id: str) -> dict[str, Any]:
    """projects.json のラッパーから、そのユーザーのスライスだけを取り出す（型が崩れていれば空）。"""
    users = wrapper.get("users", {})
    value = users.get(str(user_id), {})
    return value if isinstance(value, dict) else {}


def looks_memory_irrelevant(question: str) -> bool:
    """長期記憶の参照が明らかに不要そうな発言か（あいさつ・相槌・短文）を簡易判定する。

    True の場合、ルーターの LLM 呼び出しを省いて category="none" とする。
    取りこぼしても route_category は失敗時 none にフォールバックする設計なので、
    安全側（少し多めに none 判定）に倒しても実害は小さい。
    """
    text = question.strip()
    if not text:
        return True
    return bool(_TRIVIAL_MESSAGE_PATTERN.match(text))


def merge_document_summaries(summaries: Any, incoming: list[dict[str, Any]]) -> list:
    """保存前の文脈と保存時で、同名・同内容の置換規則を揃える。"""
    if not isinstance(summaries, list):
        summaries = []
    else:
        summaries = list(summaries)
    for summary_data in incoming:
        # 再試行で同じファイルを再解析しても、同名・同内容の要約を重複させない。
        digest = summary_data.get("document_hash")
        if digest:
            summaries = [
                old for old in summaries
                if not isinstance(old, dict) or (old.get("document_hash"), old.get("document_name"))
                != (digest, summary_data.get("document_name"))
            ]
        summaries.append(summary_data)
    return summaries


def format_document_summary_lines(summaries: Any, limit: int) -> str:
    """文書要約の末尾 `limit` 件を、プロンプトへ差し込む箇条書きへ整える（不正な要素は読み飛ばす）。"""
    lines = []
    if isinstance(summaries, list) and limit > 0:
        for summary in summaries[-limit:]:
            if not isinstance(summary, dict):
                continue
            raw_name = summary.get("document_name")
            name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else "unknown"
            raw_text = summary.get("summary")
            text = raw_text.strip() if isinstance(raw_text, str) else ""
            if text:
                lines.append(f"- {name}: {text}")
    return "\n".join(lines)


def max_entry_id(entries: list[dict[str, Any]]) -> int:
    """圧縮対象の pending_log 行の最大 id を返す（削除範囲の上限に使う）。

    `pending_fetch_entries` は `SELECT *` なので各行に AUTOINCREMENT の id が入る。
    万一 id を読めない行しか無い場合は 0 を返す。呼出元は生成・保存・削除を
    進めずに読込エラーとして扱い、原データを保護する。
    """
    ids: list[int] = []
    for entry in entries:
        try:
            ids.append(int(entry["id"]))
        except (KeyError, TypeError, ValueError):
            continue
    return max(ids) if ids else 0


def format_pending_entries(entries: list[dict[str, Any]]) -> str:
    """DB の pending_log 行を、記憶圧縮プロンプトへ渡すテキスト形式に整形する。"""
    lines: list[str] = []
    for entry in entries:
        lines.append(f"[{entry['created_at']}] User: {entry['question']}")
        lines.append(f"[{entry['created_at']}] Bot: {entry['response']}")
    return "\n".join(lines)
