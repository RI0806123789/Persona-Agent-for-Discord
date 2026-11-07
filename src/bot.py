import logging

from persona_bot.utils.logging_setup import configure_logging

# 全モジュールを読み込む前に、UTF-8 の標準出力とログ形式を1か所で設定する。
configure_logging()
logger = logging.getLogger(__name__)

import discord
from discord import app_commands

from persona_bot.core.app_state import BotState
from persona_bot.services.affinity_service import AffinityService
from persona_bot.services.agent_service import AgentService
from persona_bot.services.auto_topic_service import AutoTopicService
from persona_bot.services.autonomous_service import AutonomousService
from persona_bot.core.background import make_guarded_loop, run_in_thread, spawn
from persona_bot.services.blacklist_service import BlacklistService
from persona_bot.core.bot_client import PersonaClient
from persona_bot.core.commands import register_commands
from persona_bot.core.guild_lifecycle import prepare_guild_join, remove_guild_state
from persona_bot.config.constants import (
    ANNIVERSARY_CHECK_MINUTES,
    AUTO_TOPIC_CHECK_MINUTES,
    AUTONOMOUS_CHECK_MINUTES,
    DATABASE_PATH,
    FEEDBACK_PRUNE_MINUTES,
    LEGACY_MEMORY_OWNER_ID,
    MEMORY_MAINTENANCE_MINUTES,
    MODERATION_MODEL_NAME,
    MODERATION_TIMEOUT_SECONDS,
    REMINDER_CHECK_SECONDS,
)
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.core.database import Database
from persona_bot.utils.discord_helpers import post_welcome_message
from persona_bot.services.document_service import DocumentService
from persona_bot.services.feedback_service import FeedbackService, set_default_service
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.genai_client import create_genai_client
from persona_bot.views.help_view import build_welcome_text, resolve_bot_name
from persona_bot.services.memory_service import MemoryService
from persona_bot.core.message_handler import MessageHandler
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.services.reminder_service import ReminderService
from persona_bot.config.personas import reload_persona_catalog
from persona_bot.config.data_paths import ensure_data_directory_ready
from persona_bot.config.settings_loader import load_settings
from persona_bot.services.stats_service import StatsService
from persona_bot.services.voice_conversation import VoiceConversationService
from persona_bot.services.voice_guard import VoiceGuard
from persona_bot.utils.usage_graph import UsageTracker


# 旧保存先を見落として空の DB・違反記録で再開しないよう、接続や初期化より先に確認する。
ensure_data_directory_ready()
settings = load_settings()
genai_client = create_genai_client(settings.API_KEY_GEMINI)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

database = Database(DATABASE_PATH, legacy_owner_id=LEGACY_MEMORY_OWNER_ID)
database.initialize()

# 終了時に Discord・Gemini の接続とあわせて DB も閉じられるよう、DB を渡して生成する。
client = PersonaClient(genai_client=genai_client, database=database, intents=intents)
tree = app_commands.CommandTree(client)

state = BotState()
usage = UsageTracker()
# prompts/ のペルソナ一覧を読む。保存済みの設定の復元が「ファイルが見つかるか」を
# この一覧で確かめるため、復元より先に読む。
reload_persona_catalog()
# 保存済みのサーバーごとの設定（ペルソナ・モデル・応答モード・Google検索・自発会話）と
# 記念日の投稿記録をメモリへ復元する。以降の設定変更は各 GuildSettings.persist() で保存される。
state.restore_from_database(database)
gemini = GeminiService(genai_client)
memory = MemoryService(state, genai_client, database)
affinity = AffinityService(database)
ng_filter = NgWordFilter()
moderator = ContentModerator(
    genai_client,
    ng_filter,
    model_name=MODERATION_MODEL_NAME,
    timeout=MODERATION_TIMEOUT_SECONDS,
)
blacklist = BlacklistService()
documents = DocumentService(
    client, state, memory, gemini, usage, database, ng_filter, moderator,
    is_blocked=blacklist.is_blocked,
)
client.add_shutdown_callback(documents.stop_worker)
stats = StatsService(database, gemini)
auto_topic = AutoTopicService(
    client, state, memory, gemini, moderator, ng_filter, usage, database,
    is_blocked=blacklist.is_blocked,
)
autonomous = AutonomousService(
    client, state, gemini, moderator, ng_filter, usage, is_blocked=blacklist.is_blocked,
)
client.add_shutdown_callback(autonomous.stop_all_sessions)
# 管理者などにボットを別のボイスチャンネルへ移されたとき、接続先を守る（#171）。
voice_guard = VoiceGuard(client, autonomous)
reminders = ReminderService(client, database, ng_filter)
feedback = FeedbackService(client, database)
# 集計パネル（会話統計 → 👍 フィードバック）は機能ハブ経由で生成されるため、
# UI 層がこのインスタンスを引けるよう既定として登録しておく。
set_default_service(feedback)
# 会話の中でサブエージェントの使用を明記された依頼を、子に委任する（#170）。
agent = AgentService(gemini, usage)
# /join のボイスチャンネルで、Gemini Live と声で会話する（#268）。
voice_conversation = VoiceConversationService(
    client, genai_client,
    moderator=moderator, ng_filter=ng_filter, blacklist=blacklist,
    memory=memory, affinity=affinity, usage=usage, voice_guard=voice_guard,
)
client.add_shutdown_callback(voice_conversation.stop_all)

message_handler = MessageHandler(
    client=client, state=state, auto_topic=auto_topic, autonomous=autonomous,
    blacklist=blacklist, ng_filter=ng_filter, moderator=moderator, stats=stats,
    reminders=reminders, documents=documents, memory=memory, affinity=affinity,
    gemini=gemini, usage=usage, feedback=feedback, agent=agent,
)

register_commands(
    tree, state, usage, ng_filter, blacklist, gemini, moderator, stats, reminders, autonomous,
    voice_guard=voice_guard,
    task_owner=client.background_tasks,
    voice_conversation=voice_conversation,
)


# --- 定期実行の見回りループ ---
# どれも「間隔を指定 / 接続完了を待つ / 本体を try-except で包んでログを出す」の
# 3点セットなので、background.make_guarded_loop にまとめて作らせる
# （生成するだけで開始はしないため、start() は下の on_ready で行う）。
memory_maintenance_loop = make_guarded_loop(
    memory.flush_if_needed,
    label="バックグラウンド記憶処理エラー",
    client=client,
    owner=client.background_tasks,
    minutes=MEMORY_MAINTENANCE_MINUTES,
)

auto_topic_loop = make_guarded_loop(
    auto_topic.run_pending_checks,
    label="自発的な会話の見回りエラー",
    client=client,
    owner=client.background_tasks,
    minutes=AUTO_TOPIC_CHECK_MINUTES,
)

anniversary_loop = make_guarded_loop(
    auto_topic.run_anniversary_checks,
    label="記念日チェックエラー",
    client=client,
    owner=client.background_tasks,
    minutes=ANNIVERSARY_CHECK_MINUTES,
)

# 自律行動（ボイス/ステージでの配信）の見回り。配信の開始判定だけでなく、
# 独り言の投下・無応答での退出まで面倒を見るため、他の見回りより短い間隔で回す
# （生成そのものは裏のタスクへ逃がすので、このループ自体はすぐ返る）。
autonomous_loop = make_guarded_loop(
    autonomous.run_pending_checks,
    label="自律行動の見回りエラー",
    client=client,
    owner=client.background_tasks,
    minutes=AUTONOMOUS_CHECK_MINUTES,
)

# リマインダーは分単位の指定に応えるため、記念日（時間単位）よりずっと短い間隔で見回る。
reminder_loop = make_guarded_loop(
    reminders.run_due_checks,
    label="リマインダー確認エラー",
    client=client,
    owner=client.background_tasks,
    seconds=REMINDER_CHECK_SECONDS,
)

# フィードバック記録の間引き。以前は評価が押されるたびに実行していたが、
# DB のロックを握って主経路の返信まで遅らせていたためここへ移した。
# tasks.loop は start() 直後に1回目を実行するので、起動時の間引きも兼ねる。
feedback_prune_loop = make_guarded_loop(
    feedback.prune_stored,
    label="フィードバック記録の整理エラー",
    client=client,
    owner=client.background_tasks,
    minutes=FEEDBACK_PRUNE_MINUTES,
)


@client.event
async def on_ready() -> None:
    if client.background_tasks.stopping:
        return
    # 起動確認と管理コンソールのボット名表示に必要なため、接続したボット名だけを記録する。
    logger.info("ログイン成功: %s", client.user)
    async with memory.io_lock:
        await run_in_thread(memory.ensure_storage)

    if not state.memory_background_started:
        memory_maintenance_loop.start()
        state.memory_background_started = True

    if not state.document_worker_started:
        # 文書ワーカーはボットの生存期間ずっと走り続ける常駐コルーチン。
        # 参照を保持しないと GC で静かに止まり、添付ファイルが永久に処理されなくなる。
        documents.start_worker()
        state.document_worker_started = True

    if not state.auto_topic_background_started:
        auto_topic_loop.start()
        state.auto_topic_background_started = True

    if not state.anniversary_background_started:
        anniversary_loop.start()
        state.anniversary_background_started = True

    if not state.autonomous_background_started:
        autonomous_loop.start()
        state.autonomous_background_started = True

    if not state.reminder_background_started:
        reminder_loop.start()
        state.reminder_background_started = True

    if not state.feedback_prune_background_started:
        feedback_prune_loop.start()
        state.feedback_prune_background_started = True

    if not state.commands_synced:
        await tree.sync()
        state.commands_synced = True


@client.event
async def on_guild_join(guild: discord.Guild) -> None:
    """新しいサーバーへ招待されたときに、使い方の案内を1回だけ投稿する。

    「メンションしないと反応しない」ことも「機能が /functions に集約されている」
    ことも、招待しただけでは誰も気づけない。最初の一歩をここで案内する。

    投稿先の選定・連投防止・送信は discord_helpers.post_welcome_message が
    まとめて担い、本文は help_view が組み立てる（ヘルプと同じ文言を使うため）。
    ここでの try/except は、本文の組み立てなど想定外の失敗まで含めて
    案内の失敗でイベント処理を落とさないための最後の受け皿
    （案内が出せなくてもボット自体は普通に使えるのでフェイルオープンにする）。
    必要な guilds インテントは Intents.default() に含まれるため設定変更は不要。
    """
    prepare_guild_join(guild.id, state, voice_guard)
    try:
        welcome_text = build_welcome_text(resolve_bot_name(client.user))
        await post_welcome_message(guild, welcome_text, state.welcomed_guild_ids)
    except Exception as error:
        logger.error("ウェルカムメッセージの処理エラー。", exc_info=True)


@client.event
async def on_guild_remove(guild: discord.Guild) -> None:
    await remove_guild_state(guild.id, state, autonomous, voice_guard, voice_conversation)


@client.event
async def on_message(message: discord.Message) -> None:
    await message_handler.on_message(message)


@client.event
async def on_voice_state_update(
    member: discord.Member, before: discord.VoiceState, after: discord.VoiceState,
) -> None:
    """ボット自身が別のボイスチャンネルへ移されたら、接続先を保護する（#171）。

    判断と保護動作は voice_guard が担う（ボット以外の利用者の更新はそこで無視する）。
    参加者の出入りは音声会話（#268）へ伝え、同意の確認と発話権を更新させる。
    想定外の失敗でイベント処理を落とさないよう、ここで受け止めてログだけ残す。
    必要な voice_states インテントは上で有効にしている。
    """
    try:
        voice_guard.handle_voice_state_update(member, before, after)
    except Exception as error:
        logger.error("ボイス接続の保護の処理エラー。", exc_info=True)
    try:
        voice_conversation.handle_voice_state_update(member, before, after)
    except Exception as error:
        logger.error("音声会話の参加者の更新エラー。", exc_info=True)


@client.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """ボット返信への 👍/👎 を評価として記録する。

    on_reaction_add ではなく raw 版を使う（前者はメッセージがキャッシュに
    載っている間しか発火せず、少し前の返信への評価を取りこぼすため）。
    このイベントはボット自身が付けた初期リアクションでも発火するが、
    その除外はサービス側（_resolve_vote_target）が行う。
    必要な reactions インテントは Intents.default() に含まれるため設定変更は不要。
    """
    await feedback.handle_reaction_add(payload)


@client.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent) -> None:
    """👍/👎 が外されたときに、その評価の記録を取り消す。"""
    await feedback.handle_reaction_remove(payload)


@client.event
async def on_raw_reaction_clear(payload: discord.RawReactionClearEvent) -> None:
    """全リアクションの一括削除を、フィードバックの評価状態へ反映する。"""
    await feedback.handle_reaction_clear(payload)


@client.event
async def on_raw_reaction_clear_emoji(payload: discord.RawReactionClearEmojiEvent) -> None:
    """1種類のリアクションの一括削除を、フィードバックへ反映する。"""
    await feedback.handle_reaction_clear_emoji(payload)


def main() -> None:
    # discord.py 側で別の stderr ハンドラを追加せず、上の共通設定へ揃える。
    client.run(settings.TOKEN_DISCORD, log_handler=None)


if __name__ == "__main__":
    main()
