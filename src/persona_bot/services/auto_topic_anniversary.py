"""記念日・誕生日の自動お祝い（#469。`AutoTopicService` から分離）。

無会話チャンネルへの話題提供と、記念日のお祝いは、投稿の生成・検査・送信（`_post_auto`）を共有する
一方で、対象の決め方（沈黙時間か、profile の記念日か）が別機能のため、記念日側をミックスインに分ける。
`AutoTopicService` が継承するので、公開メソッドの名前・呼び出し方は変わらない。

★ ログは分割前と同じロガー名 `persona_bot.services.auto_topic_service` へ出す。ミックスインは
`_client` / `_state` / `_db` / `_tz` / `_is_blocked` と `_can_send` / `_post_auto` / `_unwatch_channel`
を `AutoTopicService` から受け取る前提で、単独では使わない。
"""

import calendar
import logging
from datetime import datetime
from typing import Any

import discord

from persona_bot.config.constants import ANNIVERSARY_PROFILE_KEY, ANNIVERSARY_PUBLIC_LABELS
from persona_bot.core.app_state import GuildSettings
from persona_bot.core.background import run_in_thread
from persona_bot.utils.anniversaries import parse_anniversary

logger = logging.getLogger("persona_bot.services.auto_topic_service")


class AnniversaryMixin:
    """記念日・誕生日の自動お祝い。投稿の共通処理は `AutoTopicService` 側にある。"""

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
            # 前のサーバーの投稿を待つ間に退出した設定（is_active が偽）は飛ばす（#466）。
            if (not guild_settings.auto_topic_enabled or guild_settings.autonomous_session is not None
                    or not guild_settings.is_active):
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
                self._unwatch_channel(guild_settings, channel_id, "記念日")
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
            # 待機中にサーバーから退出した設定（is_active が偽）でも止める（#466。古い設定のまま投稿すると、
            # 記録は保存されず、再参加後の新しい設定が同じ日にもう一度祝ってしまう）。
            return (
                guild_settings.is_active
                and guild_settings.auto_topic_enabled and guild_settings.autonomous_session is None
                and channel.id in guild_settings.auto_topic_channels
                and not self._is_user_blocked(member.id)
            )

        return await self._post_auto(
            channel, guild_settings, eligible=eligible, member=member, anniversary_label=label,
        )
