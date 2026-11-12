import logging
import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import discord

from persona_bot.services.affinity_service import strip_affinity_tag
from persona_bot.core.app_state import BotState, GuildSettings
from persona_bot.config.constants import (
    ANNIVERSARY_HISTORY_LIMIT,
    ANNIVERSARY_PROFILE_KEY,
    ANNIVERSARY_PUBLIC_LABELS,
    ANNIVERSARY_TIMEOUT_SECONDS,
    ANNIVERSARY_TIMEZONE,
    AUTO_TOPIC_HISTORY_LIMIT,
    AUTO_TOPIC_TIMEOUT_SECONDS,
)
from persona_bot.services.content_moderator import ContentModerator
from persona_bot.core.background import run_in_thread
from persona_bot.core.database import Database
from persona_bot.utils.anniversaries import parse_anniversary
from persona_bot.utils.discord_helpers import (
    can_bot_send_messages,
    channel_display_name,
    fetch_channel_history_text,
    send_long_message,
)
from persona_bot.services.gemini_service import GeminiService
from persona_bot.services.memory_service import MemoryService
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.config.paths import load_persona_template
from persona_bot.services.prompting import build_anniversary_prompt, build_auto_topic_prompt
from persona_bot.utils.timeutil import resolve_timezone
from persona_bot.utils.usage_graph import UsageTracker

logger = logging.getLogger(__name__)


class AutoTopicService:
    """自発的な会話（無会話チャンネルへの話題提供）と記念日の自動お祝いを管理するサービス。

    どちらもサーバー（ギルド）ごとの GuildSettings（ペルソナ・モデル・監視チャンネル
    など）を使い、同期の観測は BotState.get_guild_if_ready() で復元済み設定を参照する。
    iter_all_guild_settings() を介して全サーバーを横断して見回る。

    自発的な会話: ボットが会話に参加したチャンネルを監視対象として登録し、設定した
    時間会話が無ければ、過去の話題をもとに Gemini で話しかけを生成して投稿する。
    一度話題提供したら、その後に人間の発言があるまでは再投稿しない
    （誰も反応しないまま同じチャンネルへ延々と投稿し続けるのを防ぐ）。

    記念日の自動お祝い: ユーザーの長期記憶（profile）の "anniversaries" キーに
    構造化された誕生日・記念日があれば、今日が該当日のときにそのユーザーが
    所属するサーバーの監視チャンネルへお祝いメッセージを投稿する。

    監視状態と話題提供の投稿記録はインメモリのみ。記念日の投稿記録はDBに保存する。
    """

    def __init__(
        self,
        client: discord.Client,
        state: BotState,
        memory: MemoryService,
        gemini: GeminiService,
        moderator: ContentModerator,
        ng_filter: NgWordFilter,
        usage: UsageTracker,
        db: Database,
        *,
        is_blocked: Callable[[int], bool] | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._memory = memory
        self._gemini = gemini
        self._moderator = moderator
        self._ng_filter = ng_filter
        self._usage = usage
        self._db = db
        self._is_blocked = is_blocked
        self._tz = resolve_timezone(ANNIVERSARY_TIMEZONE, purpose="記念日の判定")

    # ==========================================================================
    # 自発的な会話（無会話チャンネルへの話題提供）
    # ==========================================================================

    def watch_channel(self, channel_id: int, guild_id: int | None) -> None:
        """チャンネルを監視対象に登録し、最終活動時刻を今に更新する。

        ボットが会話に参加した（＝話しかけられた）チャンネルだけを対象にする。
        guild_id が None（DM）の場合は監視しない。
        """
        if guild_id is None:
            return
        guild_settings = self._state.get_guild_if_ready(guild_id)
        if guild_settings is not None:
            guild_settings.auto_topic_channels[channel_id] = datetime.now(timezone.utc)

    def mark_activity(self, channel_id: int, guild_id: int | None) -> None:
        """監視対象チャンネルの最終活動時刻を更新する。未登録なら何もしない。

        ボット宛てかどうかに関係なく「チャンネルで会話が行われている」ことを
        記録するため、on_message の早い段階（メンション判定より前）から呼ばれる。
        """
        if guild_id is None:
            return
        guild_settings = self._state.get_guild_if_ready(guild_id)
        if guild_settings is not None and channel_id in guild_settings.auto_topic_channels:
            guild_settings.auto_topic_channels[channel_id] = datetime.now(timezone.utc)

    async def run_pending_checks(self) -> None:
        """全サーバー（+DM）を見回り、無会話しきい値を超えたチャンネルへ話題提供する。"""
        for _guild_id, guild_settings in list(self._state.iter_all_guild_settings()):
            if not guild_settings.auto_topic_enabled:
                continue
            await self._run_pending_checks_for_guild(guild_settings)

    async def _run_pending_checks_for_guild(self, guild_settings: GuildSettings) -> None:
        # 自律行動（autonomous_service）で配信中のサーバーには話題提供しない。
        # 両方が有効なサーバーでは、しきい値（auto_topic_interval_hours）を共用する
        # 都合で「無会話が続いた瞬間」に両方が同時に発火しうる。ボットが同じ時刻に
        # ボイスで独り言を始めつつテキストにも話しかけると、どちらに応えればよいのか
        # 分からない上に、うるさいだけになる。既に自分から喋っている間は、
        # 話題提供の側を黙らせる（/functions のパネルで2つのトグルを1枚にまとめて
        # いるのと同じ「競合させない」という方針）。
        if guild_settings.autonomous_session is not None:
            return

        # ループ中に辞書を更新する可能性があるためスナップショットを走査する
        for channel_id in list(guild_settings.auto_topic_channels):
            if not guild_settings.auto_topic_enabled or guild_settings.autonomous_session is not None:
                return
            if not self._topic_is_due(guild_settings, channel_id):
                continue

            channel = self._client.get_channel(channel_id)
            if channel is None:
                # チャンネルが削除された・アクセスできなくなった場合は監視から外す
                logger.warning("自発的な会話: チャンネルが見つからないため監視を解除します。")
                guild_settings.auto_topic_channels.pop(channel_id, None)
                guild_settings.auto_topic_posted_at.pop(channel_id, None)
                continue

            try:
                done = await self._post_topic(channel, guild_settings)
            except discord.Forbidden:
                # 生成中に権限が変わった場合も、同じ沈黙期間にAPIを呼び続けない。
                logger.warning("自発的な会話: 送信権限がなくなったため話題提供を見送ります。", exc_info=True)
                done = True
            except Exception as error:
                logger.error("自発的な会話: 話題提供に失敗しました。", exc_info=error)
                continue

            if done:
                # 完了時のみ記録する。一時的な失敗（False）は次回の見回りで自然にリトライされる。
                guild_settings.auto_topic_posted_at[channel_id] = datetime.now(timezone.utc)

    @staticmethod
    def _topic_is_due(
        guild_settings: GuildSettings, channel_id: int, *, last_activity: datetime | None = None,
    ) -> bool:
        """現在の投稿条件を確認し、生成開始後に活動が更新された話題も見送る。"""
        if not guild_settings.auto_topic_enabled or guild_settings.autonomous_session is not None:
            return False
        current_activity = guild_settings.auto_topic_channels.get(channel_id)
        if current_activity is None or (last_activity is not None and current_activity != last_activity):
            return False
        if datetime.now(timezone.utc) - current_activity < timedelta(hours=guild_settings.auto_topic_interval_hours):
            return False
        posted_at = guild_settings.auto_topic_posted_at.get(channel_id)
        return posted_at is None or posted_at < current_activity

    async def _post_topic(self, channel: Any, guild_settings: GuildSettings) -> bool:
        """過去の話題をもとに話しかけを生成し、チャンネルへ投稿する。

        戻り値は「この沈黙期間の処理を完了したか」。True なら次に人間の発言が
        あるまで再実行されない（投稿できた場合と、モデレーション見送りのように
        リトライしても同じ結果になる場合）。False は一時的な失敗で、次回の
        見回りでリトライされる。設定・活動の変更による中止も False を返して記録しない。
        生成前の権限不足は False、生成後の権限不足は True として再生成を抑える。
        """
        last_activity = guild_settings.auto_topic_channels.get(channel.id)
        return await self._post_auto(
            channel, guild_settings,
            eligible=lambda: self._topic_is_due(guild_settings, channel.id, last_activity=last_activity),
            member=None, anniversary_label=None,
        )

    async def _post_auto(
        self, channel: Any, guild_settings: GuildSettings, *, eligible: Callable[[], bool],
        member: discord.Member | None, anniversary_label: str | None,
    ) -> bool:
        """投稿前後の条件確認と生成・検査・送信を共通化する。"""
        anniversary = member is not None
        log_label = "記念日のお祝い" if anniversary else "自発的な会話"
        short_label = "記念日" if anniversary else "自発的な会話"
        if not eligible() or not self._can_send(channel) or (anniversary and not channel.permissions_for(member).view_channel):
            return False
        response_settings = guild_settings.snapshot_response()
        try:
            persona_prompt = await load_persona_template(response_settings.prompt_file)
        except FileNotFoundError:
            logger.warning("%s: プロンプトファイルが見つかりません。", short_label, exc_info=True)
            return False
        history_text = await fetch_channel_history_text(
            channel, guild_settings.channel_reset_points.get(channel.id), self._client.user,
            ANNIVERSARY_HISTORY_LIMIT if anniversary else AUTO_TOPIC_HISTORY_LIMIT,
            is_blocked=self._is_blocked,
        )
        if anniversary:
            full_prompt = build_anniversary_prompt(persona_prompt, member.display_name, anniversary_label, history_text)
        else:
            # 個人の長期記憶は共有チャンネルの話題提供には使わない。
            full_prompt = build_auto_topic_prompt(persona_prompt, "", history_text)
        if not eligible() or not self._can_send(channel) or (anniversary and not channel.permissions_for(member).view_channel):
            return False
        response_text, usage_info, _sources = await self._gemini.generate(
            full_prompt, response_settings.model_name, images=None,
            timeout=ANNIVERSARY_TIMEOUT_SECONDS if anniversary else AUTO_TOPIC_TIMEOUT_SECONDS,
            grounding=False,
        )
        if not response_text:
            if anniversary:
                logger.error("記念日: お祝いメッセージの生成に失敗しました。")
            else:
                logger.warning("自発的な会話: 話題の生成に失敗しました。次回の見回りで再試行します。")
            return False
        # 生成に成功した時点で消費したトークンを記録する。後段のモデレーション・権限の再確認で
        # 投稿を見送っても、Gemini を呼んだ分が統計から欠けないようにする（#432）。
        channel_label = channel_display_name(channel)
        if anniversary:
            self._usage.log_snapshot(log_label, f"{member.display_name} さんの{anniversary_label}",
                                     channel_label, full_prompt, response_text, usage_info)
        else:
            self._usage.log_snapshot(log_label, f"{channel_label} への話題提供", "自動実行",
                                     full_prompt, response_text, usage_info)
        screened, done = await self._screen_auto_post(response_text, log_label)
        if not eligible() or (anniversary and not channel.permissions_for(member).view_channel):
            return False
        if not self._can_send(channel):
            logger.warning("%s: 生成後に送信権限がなくなったため投稿を見送ります。", short_label)
            return True
        if screened is None:
            return done
        if anniversary:
            await send_long_message(
                channel, f"{member.mention} {screened}",
                allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False, replied_user=False),
            )
        else:
            await send_long_message(channel, screened, allowed_mentions=discord.AllowedMentions.none())
        return True

    # ==========================================================================
    # 自動投稿の共通処理
    # ==========================================================================

    @staticmethod
    def _can_send(channel: Any) -> bool:
        """生成前にチャンネル単位の閲覧・送信権限を確認する。DMは対象外。"""
        guild = getattr(channel, "guild", None)
        bot_member = guild.me if guild is not None else None
        if bot_member is None:
            return False
        return can_bot_send_messages(channel, bot_member)

    async def _screen_auto_post(self, response_text: str, label: str) -> tuple[str | None, bool]:
        """ボットが自分から投稿するテキストへの共通の後処理。

        感情タグの除去 → NG ワードのマスク → AI モデレーション、の順に掛ける。
        自発的な会話と記念日のお祝いで手順が同じため、ここにまとめている。

        Returns:
            (投稿するテキスト, 完了扱いにするか) のタプル。テキストが None のときは
            投稿しない。2つ目の値は呼び出し元の戻り値にそのまま使う想定で、
            True = 完了扱い（再試行しない） / False = 次の見回りで再試行する。
        """
        # 感情タグが混じった場合の保険（誰への返信でもないため親密度は更新しない）
        text = strip_affinity_tag(response_text)
        if not text:
            # 一時的な生成失敗の可能性があるため、次の見回りで再試行する
            logger.warning("自動投稿: 生成結果が空でした。")
            return None, False

        # NG ワードは既存パイプラインと同様に ● でマスクする
        text = self._ng_filter.mask_with_log(text, label=label)

        # 不適切な内容は、チャット応答と違い謝罪文へ差し替えて投稿する意味が無い
        # （誰も話していないのに唐突な謝罪になる）ため、投稿自体を見送る。
        # 同じ材料からの再生成はほぼ同じ結果になるため、リトライせず完了扱いにする。
        is_safe, reason = await self._moderator.check(text, direction="Bot応答")
        if not is_safe:
            logger.warning("自動投稿: モデレーションにより投稿を見送りました。")
            return None, True

        return text, True

    # ==========================================================================
    # 記念日・誕生日の自動お祝い
    # ==========================================================================

    async def run_anniversary_checks(self) -> None:
        """全ユーザーの記念日（profile の "anniversaries" キー）を確認し、
        今日が該当日のユーザーがいれば、そのユーザーが所属するサーバーの
        監視チャンネルへお祝いメッセージを投稿する。

        記念日は自然な会話の中で記憶圧縮（memory_service._compact_memory）が
        LLM の判断で profile へ書き込む自由記述寄りの情報のため、形式が
        期待通りでないエントリはすべて静かにスキップする（フェイルセーフ）。
        私的な記念日の露出を防ぐため、投稿対象は
        _is_public_anniversary_label() を通ったラベルだけに絞る。
        """
        try:
            all_anniversaries = await run_in_thread(
                self._db.profile_load_all_for_key, ANNIVERSARY_PROFILE_KEY
            )
        except Exception as error:
            logger.error("記念日チェック: profile の読み込みに失敗しました。", exc_info=error)
            return

        if not all_anniversaries:
            return

        today = datetime.now(self._tz).date()
        today_str = today.isoformat()

        due: list[tuple[str, str]] = []  # (user_id, label)
        for user_id, entries in all_anniversaries.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                anniversary = parse_anniversary(entry)
                if anniversary is None:
                    continue
                label, month, day = anniversary
                # 私的な記念日を共有チャンネルへ出さないための許可リスト照合。
                # 対象外は「記憶としては持つがお祝いはしない」だけの正常な状態で、
                # 毎時の見回りで何度も通るためログは出さず静かにスキップする。
                if not self._is_public_anniversary_label(label):
                    continue
                if self._is_today(today, month, day):
                    due.append((str(user_id), label))

        for user_id, label in due:
            try:
                await self._celebrate_user(user_id, label, today_str)
            except Exception as error:
                logger.error("記念日チェック: お祝い投稿に失敗しました。", exc_info=error)

    @staticmethod
    def _is_public_anniversary_label(label: str) -> bool:
        """公開チャンネルでお祝いしてよい記念日ラベルかどうかを判定する。

        anniversaries は記憶圧縮（memory_service._compact_memory）の LLM が
        自由記述で書き込むため、「退院記念日」「離婚記念日」のように DM でだけ
        話した私的な記念日が混ざりうる。それをそのまま共有チャンネルで祝うと、
        本人が公開するつもりのなかった事情が他の参加者へ露出してしまう。
        そのため constants.ANNIVERSARY_PUBLIC_LABELS の許可リストに載っている
        ラベルだけを自動お祝いの対象にする。

        照合は完全一致で行う。部分一致にすると「離婚記念日」「退院記念日」が
        「記念日」を含むという理由で通ってしまい、許可リストの意味が無くなる。
        """
        return label in ANNIVERSARY_PUBLIC_LABELS

    @staticmethod
    def _is_today(today: Any, month: int, day: int) -> bool:
        """記念日の月日が「今日」かどうかを判定する。

        2月29日の記念日は平年に存在しないため、そのままでは4年に3回お祝いが
        飛ばされてしまう。平年は2月28日をその代わりとして扱う
        （日本で年齢計算に用いられる慣行に合わせる）。
        """
        if month == today.month and day == today.day:
            return True
        if month == 2 and day == 29 and not calendar.isleap(today.year):
            return today.month == 2 and today.day == 28
        return False

    async def _resolve_member(self, guild: discord.Guild, user_id: str) -> discord.Member | None:
        """サーバー内のメンバーを取得する。キャッシュに無ければ API から取り直す。

        本ボットは特権インテントの members を有効にしていないため、
        guild.get_member() が参照するメンバーキャッシュはほとんど埋まらない
        （ボイス接続者などに限られる）。キャッシュだけに頼るとお祝い対象が
        ほぼ常に「メンバーではない」と判断されてしまうため、取得できなければ
        API へ問い合わせる。記念日は多くても1人年1回のため呼び出し回数は少ない。
        """
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError):
            # profile は LLM や手書きで書かれうるため、数値でない ID は静かに対象外にする。
            return None
        member = guild.get_member(numeric_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(numeric_id)
        except discord.NotFound:
            return None  # このサーバーのメンバーではない（正常なケース）
        except discord.Forbidden:
            logger.warning("記念日: メンバー取得の権限がありません。", exc_info=True)
            return None
        except discord.HTTPException as error:
            logger.error("記念日: メンバー取得に失敗しました。", exc_info=error)
            return None

    def _is_user_blocked(self, user_id: int) -> bool:
        """ブラックリスト登録者か。判定関数が注入されていなければ（テストなど）誰もブロックしない。"""
        return self._is_blocked is not None and bool(self._is_blocked(user_id))

    async def _celebrate_user(self, user_id: str, label: str, today_str: str) -> None:
        """指定ユーザーが所属する各サーバーの監視チャンネルへお祝いメッセージを投稿する。

        DM は「みんなに見える場所でお祝いする」という自発会話の枠組みに乗らないため
        対象外とする。同じサーバー・同じ記念日への同日中の二重投稿は
        anniversary_posted_dates で防ぐ。
        """
        posted_key = f"{user_id}:{label}"
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError):
            return  # 数値でない ID は対象外（毎時の見回りで通るためログは出さない）
        if self._is_user_blocked(numeric_id):
            # ブラックリスト登録者には、生成（API 呼び出し）もメンション付きの公開投稿もしない。
            # 記録は残さない（ブロックが解除されれば、同日中の見回りで祝える）。
            return

        for guild_id, guild_settings in list(self._state.guild_settings.items()):
            if not guild_settings.auto_topic_enabled or guild_settings.autonomous_session is not None:
                continue
            if guild_settings.anniversary_posted_dates.get(posted_key) == today_str:
                continue  # このサーバーでは今日すでに投稿済み
            if not guild_settings.auto_topic_channels:
                continue  # 自発会話の監視チャンネルが無いサーバーには投稿先が無い

            guild = self._client.get_guild(guild_id)
            if guild is None:
                continue
            member = await self._resolve_member(guild, user_id)
            if member is None:
                continue
            channel = self._pick_anniversary_channel(guild_settings, member)
            if channel is None:
                # 送信できる監視チャンネルが無い。API は呼んでいないため記録はせず、
                # 権限が戻れば次回の見回りで投稿される。
                continue
            try:
                done = await self._post_anniversary(channel, guild_settings, member, label)
            except discord.Forbidden:
                # 生成中に権限が変わった場合も、同じ日に毎時間 API を呼び続けない
                # （自発的な会話の Forbidden と同じ扱い）。今日の分は処理済みとして記録する。
                # 生成文はこのチャンネルの履歴から作ったため、別のチャンネルへは流用しない。
                logger.warning("記念日: 送信権限がなくなったため今日のお祝いを見送ります。", exc_info=True)
                done = True
            except Exception as error:
                logger.error("記念日: 投稿に失敗しました。", exc_info=error)
                continue

            if done:
                # メモリと DB の両方に記録する（当日に再起動しても再投稿しないため）
                await guild_settings.record_anniversary_post(user_id, label, today_str)

    def _pick_anniversary_channel(self, guild_settings: GuildSettings, member: discord.Member) -> Any | None:
        """お祝いの投稿先を、直近アクティビティが新しい監視チャンネルから順に選ぶ。

        最新のチャンネルが削除された・送信できなくなっただけで、そのサーバーでの
        お祝いが永久に止まらないよう、送信できるチャンネルが見つかるまで順に試す。
        削除済み（get_channel が None）のチャンネルは自発的な会話の見回りと同じく
        監視から外す。ここでは Gemini を呼ばないため、何度試しても API を消費しない。
        """
        candidates = sorted(
            guild_settings.auto_topic_channels,
            key=guild_settings.auto_topic_channels.__getitem__,
            reverse=True,
        )
        for channel_id in candidates:
            channel = self._client.get_channel(channel_id)
            if channel is None:
                logger.warning("記念日: チャンネルが見つからないため監視を解除します。")
                guild_settings.auto_topic_channels.pop(channel_id, None)
                guild_settings.auto_topic_posted_at.pop(channel_id, None)
                continue
            if self._can_send(channel) and channel.permissions_for(member).view_channel:
                return channel
        return None

    async def _post_anniversary(
        self,
        channel: Any,
        guild_settings: GuildSettings,
        member: discord.Member,
        label: str,
    ) -> bool:
        """お祝いメッセージを生成し、チャンネルへ投稿する。戻り値の意味は _post_topic と同様。"""
        def eligible() -> bool:
            # 生成の前後・送信の前に何度も呼ばれる。待機中にブロックされた場合も止める。
            return (
                guild_settings.auto_topic_enabled and guild_settings.autonomous_session is None
                and channel.id in guild_settings.auto_topic_channels
                and not self._is_user_blocked(member.id)
            )

        return await self._post_auto(
            channel, guild_settings, eligible=eligible, member=member, anniversary_label=label,
        )
