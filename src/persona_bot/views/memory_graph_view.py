"""🧠 記憶を見る（#484）: 押した本人の記憶を、つながりの図の HTML ファイルにして本人だけに送る。

🔒 統計プライバシーパネル（`StatsPrivacyView`）の row 2 に置く。ハブ（`/functions`）の5行は
使い切っているため、「自分のデータの確認・管理」を扱うこのパネルへ相乗りしている（利用者の決定）。

・対象は**ボタンを押した本人の記憶だけ**。他人を指定する経路は作らない（管理者も他人の記憶は見られない）。
・閲覧のみ。記憶を書き換えない。図の線は記憶の保存構造だけで、LLM は呼ばない（`memory_graph.py`）。
・結果は本人だけに見えるエフェメラルのメッセージに添付する。ボットは Web サーバーを立てない。
・パネルは残したまま結果を別の followup で送るので、連打は `run_exclusive` で断る。
"""

import io

import discord
from discord import ui

from persona_bot.config.constants import MEMORY_GRAPH_FILENAME, MEMORY_GRAPH_MAX_NODES
from persona_bot.core.background import run_in_thread
from persona_bot.services.memory_graph import MemoryGraphDocument, build_memory_graph_document
from persona_bot.services.memory_service import MemoryService
from persona_bot.utils.timeutil import utc_now
from persona_bot.views.view_parts import defer_and_render_graph, send_graph_ephemeral

MEMORY_GRAPH_FAILURE_NOTICE = "⚠️ 記憶を読み込めませんでした。時間をおいて再度お試しください。"
MEMORY_GRAPH_SEND_FAILURE_NOTICE = "⚠️ 記憶のファイルを送れませんでした。時間をおいて再度お試しください。"
MEMORY_GRAPH_BUSY_NOTICE = "記憶のファイルを作っています。届くまでお待ちください。"


def build_memory_graph_caption(document: MemoryGraphDocument) -> str:
    """HTML ファイルに添える本文。"""
    lines = [
        "🧠 ボットが覚えているあなたの記憶を、つながりの図（HTML ファイル）にしました"
        f"（記憶の項目: {document.memory_node_count} 件）。",
        "ダウンロードしてブラウザで開くと、図と一覧で確認できます（ファイルを開いても外部へは通信しません）。",
        "※ このファイルにはあなたの記憶が入っています。他の人に渡さないでください。",
        "※ 閲覧専用です。間違いに気づいたら、会話で正しい内容を伝えてください"
        "（次に記憶をまとめるときの材料になります）。",
    ]
    if document.pending_count:
        lines.append(f"※ まとめる前の会話が {document.pending_count} 件あります（まだ図には入っていません）。")
    if document.truncated:
        lines.append(f"※ 項目が多いため、最大 {MEMORY_GRAPH_MAX_NODES} 件までを図にしています。")
    return "\n".join(lines)


def build_memory_graph_empty_notice(pending_count: int) -> str:
    """覚えている記憶が1つも無いときの案内（ファイルは送らない）。"""
    text = "🧠 まだあなたについて覚えている記憶はありません。"
    if pending_count:
        text += (
            f"\n※ まとめる前の会話が {pending_count} 件あります。一定の件数がたまるか、"
            "しばらく会話が途切れると記憶へまとめられ、ここで見られるようになります。"
        )
    return text


async def send_memory_graph(interaction: discord.Interaction, memory: MemoryService, user_id: str) -> None:
    """本人の記憶を読み、HTML を作って本人だけに送る。失敗は本人だけに案内する。"""
    async def render() -> MemoryGraphDocument:
        snapshot = await memory.load_user_snapshot(user_id)
        # テンプレートの読込と組み立てはファイル I/O と文字列処理なのでワーカーで行う。
        return await run_in_thread(build_memory_graph_document, snapshot, generated_at=utc_now())

    rendered, document = await defer_and_render_graph(
        interaction, render,
        failure_log="記憶の可視化: 記憶の読み込みまたはファイルの作成に失敗しました。",
        failure_notice=MEMORY_GRAPH_FAILURE_NOTICE,
    )
    if not rendered:
        return
    if document.html is None:
        await interaction.followup.send(build_memory_graph_empty_notice(document.pending_count), ephemeral=True)
        return
    # バッファの所有権は send_graph_ephemeral へ渡す（成否にかかわらず閉じる）。
    await send_graph_ephemeral(
        interaction, io.BytesIO(document.html), MEMORY_GRAPH_FILENAME, build_memory_graph_caption(document),
        followup=True, error_notice=MEMORY_GRAPH_SEND_FAILURE_NOTICE,
    )


class MemoryGraphButton(ui.Button):
    """押した本人の記憶を、つながりの図の HTML ファイルにして本人だけに送るボタン。"""

    def __init__(self, *, row: int = 2) -> None:
        super().__init__(label="記憶を見る", style=discord.ButtonStyle.primary, emoji="🧠", row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        memory = getattr(view, "memory", None)
        if memory is None:
            # 記憶サービスを持たないパネルにはボタンを出さない。念のため、応答だけは返す。
            await interaction.response.send_message("この機能は現在利用できません。", ephemeral=True)
            return
        # 対象は押した本人だけ。パネルはエフェメラルなので、開いた本人以外は押せない。
        user_id = str(interaction.user.id)
        await view.run_exclusive(
            interaction, "memory_graph", lambda: send_memory_graph(interaction, memory, user_id),
            busy_notice=MEMORY_GRAPH_BUSY_NOTICE,
        )
