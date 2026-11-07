"""チャンネル履歴から会話要約を生成する。"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Callable

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import GuildSettings
from persona_bot.config.constants import SUMMARY_MAX_MESSAGES, SUMMARY_TIMEOUT_SECONDS
from persona_bot.services.content_moderator import ContentModerator, screen_bot_response
from persona_bot.utils.discord_helpers import build_summary_source, fetch_messages_since
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_persona_template
from persona_bot.services.prompting import build_summary_prompt
from persona_bot.utils.usage_graph import UsageTracker

logger = logging.getLogger(__name__)
_MD_HEADING_PATTERN = r"(?m)^[ \t]{0,3}#{1,6}[ \t]*"


async def generate_channel_summary(
    channel: discord.abc.Messageable,
    bot_user: object,
    period: "discord.app_commands.Choice[int]",
    *,
    guild_settings: GuildSettings,
    gemini: GeminiService,
    moderator: ContentModerator,
    ng_filter: NgWordFilter,
    usage: UsageTracker,
    is_blocked: Callable[[int], bool] | None = None,
) -> tuple[bool, str]:
    """チャンネルの会話を議事録として要約する。

    戻り値 (is_summary, text):
        is_summary=True  → text は要約本体（ヘッダー付き）。表示範囲に応じて出し分ける。
        is_summary=False → text は通知・エラーメッセージ（パネルにそのまま表示する）。
    """
    response_settings = guild_settings.snapshot_response()
    after_time = datetime.now(timezone.utc) - timedelta(hours=period.value)

    # --- 履歴取得 ---
    try:
        messages = await fetch_messages_since(channel, after_time, SUMMARY_MAX_MESSAGES)
    except discord.Forbidden:
        return False, "⚠️ メッセージ履歴を読む権限がありません。ボットの権限設定を確認してください。"
    except Exception as error:
        logger.error("要約: 履歴の取得に失敗しました。", exc_info=error)
        return False, "⚠️ 履歴の取得に失敗しました。"

    # --- 自動停止: 上限超過なら要約せず注意メッセージ ---
    if len(messages) > SUMMARY_MAX_MESSAGES:
        return False, (
            f"⚠️ 対象メッセージが多すぎます（上限 {SUMMARY_MAX_MESSAGES} 件）。"
            "より短い期間を選んで再実行してください。"
        )

    source = build_summary_source(messages, bot_user, is_blocked=is_blocked)
    if source.message_count == 0:
        return False, f"この期間（{period.name}）には要約できる会話がありませんでした。"

    # --- ペルソナテンプレート読み込み（キャラ設定パネルで選択中のペルソナ） ---
    try:
        persona_prompt = await load_persona_template(response_settings.prompt_file)
    except FileNotFoundError as error:
        # ペルソナが見つからなくても議事録自体は作れるため、口調なしで続行する
        logger.warning("要約: プロンプトファイルが見つかりません。口調なしで続行します。", exc_info=error)
        persona_prompt = ""

    full_prompt = build_summary_prompt(persona_prompt, period.name, source.text)

    # モデルはこのサーバー（ギルド）でキャラ設定パネルにより選択中のものを使う。
    # グラウンディングは既存テキストの要約に不要なため無効。
    response_text, usage_info, _sources = await gemini.generate(
        full_prompt,
        response_settings.model_name,
        images=None,
        timeout=SUMMARY_TIMEOUT_SECONDS,
        grounding=False,
    )

    if not response_text:
        return False, "⚠️ 要約の生成に失敗しました。時間をおいて再度お試しください。"

    # 感情タグが混じった場合の保険（ペルソナが誤って付けることがある）
    response_text = strip_affinity_tag(response_text)
    # Markdown 見出し記号を除去してプレーンテキスト化（#### 等が Discord で見づらいため）
    response_text = re.sub(_MD_HEADING_PATTERN, "", response_text).strip()
    if not response_text:
        return False, "⚠️ 要約の生成結果が空でした。時間をおいて再度お試しください。"

    # Bot自身の応答としてNGワードマスク＋AIモデレーションを適用する（既存パイプラインと同様）
    response_text = await screen_bot_response(
        response_text, ng_filter, moderator, log_label="会話要約"
    )

    usage.log_snapshot(
        "会話要約", f"{period.name}の要約", f"{source.message_count}件", full_prompt, response_text, usage_info
    )

    header = f"📋 **会話要約（期間: {period.name} / 対象: {source.message_count}件）**\n\n"
    return True, header + response_text
