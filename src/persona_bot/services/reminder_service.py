"""リマインダーサービス。

「30分後にゴミ出しって知らせて」のような依頼を会話から受け取り、指定時刻に
依頼されたチャンネルへ通知する。

日時の解釈（相対表現→絶対日時）は content_moderator の統合判定が
モデレーションと同じ1回の Gemini 呼び出しの中で済ませており、このサービスは
その結果の検証・保存・通知だけを担う。通知文は Gemini を呼ばずテンプレートで
組み立てる — リマインダーは文面の魅力より「正確に届くこと」が重要なため。

繰り返し（毎日/毎週/毎月）にも対応する。繰り返しは新しい行を作らず、
**同じ行の remind_at を次回時刻へ進めて pending のまま残す**方式にしている
（reminder_id が変わらないので、パネルからの取り消しが直感どおりに効く）。
次回時刻の計算は next_occurrence()（暦だけを扱う純粋関数）に切り出してあり、
DB のクレームと同一トランザクションの中から呼ばれる。
"""

import logging
import asyncio
import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from weakref import WeakValueDictionary

import discord

from persona_bot.config.constants import (
    DM_SETTINGS_GUILD_KEY,
    REMINDER_MAX_DAYS_AHEAD,
    REMINDER_MAX_FINISHED,
    REMINDER_MAX_MESSAGE_LENGTH,
    REMINDER_MAX_PENDING_PER_USER,
    REMINDER_RECURRENCE_LABELS,
    REMINDER_RECURRENCE_MAX_CATCHUP_STEPS,
    REMINDER_STALE_HOURS,
    REMINDER_TIMEZONE,
)
from persona_bot.core.database import Database
from persona_bot.core.background import run_in_thread
from persona_bot.utils.discord_helpers import can_bot_send_messages, send_long_message
from persona_bot.services.ng_word_service import NgWordFilter
from persona_bot.utils.timeutil import parse_iso_datetime, format_weekday, resolve_timezone, utc_now
from persona_bot.utils.normalization import normalize_recurrence

logger = logging.getLogger(__name__)


class ReminderStorageError(Exception):
    """リマインダーのDB操作が失敗した。正常な空一覧・対象なしとは区別する。"""


# LLM が返す日時のゆらぎを吸収するため、複数の形式を順に試す。
_AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def _utc_now() -> datetime:
    """秒精度の現在時刻（UTC）。保存する時刻はすべてこの精度に揃える。

    remind_at の期限判定は ISO 8601 文字列の比較で行うため、桁数が混ざらないよう
    マイクロ秒を落として形式を統一する（精度は timeutil.utc_now に集約）。
    テストが時計を固定するための差し替え口として、この名前を残している。
    """
    return utc_now()


def _format_local(iso_utc: str, tz: Any) -> str:
    """保存されている UTC の ISO 8601 文字列を、表示用のローカル日時表記に直す。"""
    try:
        dt = parse_iso_datetime(iso_utc)
    except ValueError:
        return iso_utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    # strftime の %-m / %#m はプラットフォーム依存のため、数値は自前で埋め込む。
    # 曜日名は timeutil の唯一のテーブルから取る（登録時にプロンプトへ埋める
    # 曜日表記と同じ情報源にして、表記が食い違わないようにするため）。
    return (
        f"{local.year}年{local.month}月{local.day}日"
        f"（{format_weekday(local)}）{local:%H:%M}"
    )


def recurrence_label(value: Any) -> str:
    """繰り返し種別の日本語表示名を返す（単発・未知は空文字）。"""
    return REMINDER_RECURRENCE_LABELS.get(normalize_recurrence(value), "")


# ---- 次回時刻の計算（暦だけを扱う純粋関数。DB もネットワークも触らない） --------


def _to_local_naive(iso_utc: str, tz: Any) -> datetime | None:
    """UTC の ISO 8601 文字列を、tz における「素朴な（tzinfo 無しの）ローカル日時」にする。

    日付の加算をローカルの壁時計で行うため、いったん tzinfo を外す。UTC のまま
    timedelta を足すと、DST のあるタイムゾーンへ設定を変えた瞬間に時刻がずれる。
    """
    try:
        dt = parse_iso_datetime(str(iso_utc))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).replace(tzinfo=None)


def _local_to_utc(local_naive: datetime, tz: Any) -> datetime:
    """ローカルの素朴な日時を、保存用の UTC・秒精度の日時（tz 付き）へ戻す。

    夏時間の切り替えで壁時計の時刻が二度現れる（曖昧な）場合は**1回目**を、
    時計が進んで存在しない時刻は**進める前の UTC オフセット**で解釈して
    進めた分だけ後ろへずらした時刻（例: America/New_York の 02:30 → 03:30）を使う。
    どちらも PEP 495 の fold=0 の規則で、元の日時が持っていた fold に左右されないよう
    ここで明示的に固定する。
    期限判定は ISO 文字列の比較で行うため、_utc_now() と同じくマイクロ秒を落として
    桁数を揃える（桁数が混ざると比較が壊れる）。
    """
    local = local_naive.replace(tzinfo=tz, fold=0)
    return local.astimezone(timezone.utc).replace(microsecond=0)


def _monthly_occurrence(anchor_local: datetime, months: int) -> datetime:
    """アンカーから months か月後の予定日時（ローカル）を返す。

    対象日が存在しない月（例: 1/31 の翌月）はその月の末日へ丸める。
    **アンカーは常に初回時刻のまま**で、丸めた日付から次を計算しないため、
    1/31 → 2/28 → 3/31 と元の日付へ戻る（timedelta(days=30) では実現できない）。
    """
    total = (anchor_local.year * 12 + (anchor_local.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    # calendar.monthrange(年, 月)[1] はその月の日数（閏年の2月は29）。
    day = min(anchor_local.day, calendar.monthrange(year, month)[1])
    return anchor_local.replace(year=year, month=month, day=day)


def _month_distance(anchor_local: datetime, current_local: datetime) -> int:
    """アンカーから見て current が何か月後かを返す（日付の丸めの影響を受けない）。"""
    return (current_local.year - anchor_local.year) * 12 + (current_local.month - anchor_local.month)


def next_occurrence(
    *,
    remind_at: str,
    recurrence: str,
    anchor: str,
    after: datetime,
    tz: Any,
    max_steps: int = REMINDER_RECURRENCE_MAX_CATCHUP_STEPS,
) -> str | None:
    """繰り返しリマインダーの次回通知時刻（UTC・秒精度の ISO 8601）を返す。

    ボット停止などで複数回分を過ぎていた場合は、**after より後になるまで繰り返し
    進める**（1回分だけ進めると、追い付くまで見回りのたびに通知が飛んでしまう）。
    過ぎた回をまとめて通知することはしない — 通知は最大1回、時刻は未来へ。

    候補の日時はローカルの暦で作り、**UTC へ戻してから after と比べる**。
    壁時計の時刻どうしで比べると、夏時間の終了で同じ時刻が二度現れる時間帯に
    「壁時計では後だが UTC では前」の候補を採用し、過去の時刻を pending のまま
    保存して次の見回りで同じ系列を取り直してしまう（#222）。
    曖昧な時刻・存在しない時刻の解釈は _local_to_utc を参照。

    Args:
        remind_at: 今回通知した（＝進める前の）予定時刻。UTC の ISO 8601。
        recurrence: 'daily' / 'weekly' / 'monthly'。'none' や未知の値は None を返す。
        anchor: 初回の予定時刻（UTC の ISO 8601）。毎月の日付を元へ戻すために使う。
                空なら remind_at をアンカーとみなす（移行前の行などへの保険）。
        after: この日時より後になるまで進める（通常は現在時刻。tz 付きでも無しでもよい）。
        tz: 日付の加算を行う基準タイムゾーン（REMINDER_TIMEZONE を解決したもの）。
        max_steps: 追い付きの最大反復回数。到達したら None を返して系列を止める。

    Returns:
        次回の通知時刻。繰り返さない場合・計算できない場合は None。
    """
    kind = normalize_recurrence(recurrence)
    if kind == "none":
        return None

    current_local = _to_local_naive(remind_at, tz)
    if current_local is None:
        logger.warning("リマインダー: 繰り返しの基準日時を読めませんでした。")
        return None

    # アンカーが読めない行は今回の予定時刻を基準にする（繰り返しを止めない）。
    anchor_local = _to_local_naive(anchor, tz) or current_local

    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)

    if kind == "monthly":
        # 反復の起点はアンカーではなく「今回の予定時刻の月」にする。
        # アンカーから数え直すと、何年も続いた繰り返しで反復回数が上限に届いてしまう。
        months = _month_distance(anchor_local, current_local)
        for _ in range(max_steps):
            months += 1
            candidate = _local_to_utc(_monthly_occurrence(anchor_local, months), tz)
            if candidate > after:
                return candidate.isoformat()
    else:
        delta = timedelta(days=1) if kind == "daily" else timedelta(days=7)
        # 今回が何回目かをアンカーからの経過で数え、次の回は**アンカーの壁時計の時刻**で
        # 作る。前回の予定時刻に delta を足すと、夏時間の開始で存在しない時刻を
        # ずらした回（02:30 → 03:30）から先がずっと 03:30 のままになるため。
        # 四捨五入するのは、ずらした分（delta より十分短い）で回数を数え違えないため。
        steps = round((current_local - anchor_local) / delta)
        for _ in range(max_steps):
            steps += 1
            candidate = _local_to_utc(anchor_local + delta * steps, tz)
            if candidate > after:
                return candidate.isoformat()

    # ここに来るのは「進めても未来に届かない」＝計算が前進していない不具合のとき。
    # 無限ループになるより、系列を止めて気づけるようにする。
    logger.error("リマインダー: 次回時刻の計算が %d 回で追い付きませんでした。", max_steps)
    return None


class ReminderService:
    """リマインダーの登録・一覧・取り消しと、期限到来分の通知を行うサービス。

    保存先は DB の reminders テーブル（再起動しても消えない）。通知先は
    「依頼されたチャンネル」で、そこに依頼者をメンションして投稿する。
    """

    def __init__(self, client: discord.Client, db: Database, ng_filter: NgWordFilter) -> None:
        self._client = client
        self._db = db
        self._ng_filter = ng_filter
        self._tz = resolve_timezone(REMINDER_TIMEZONE, purpose="リマインダーの表示・日時解釈")
        # 実行中・待機中だけ保持し、使い終わった系列のロックを溜めない。
        self._delivery_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()

    def _delivery_lock(self, reminder_id: int) -> asyncio.Lock:
        """系列ごとに取消と送信を順序付ける（DBの同期ロックとは別）。"""
        lock = self._delivery_locks.get(reminder_id)
        if lock is None:
            lock = asyncio.Lock()
            self._delivery_locks[reminder_id] = lock
        return lock

    # ---- 登録 -----------------------------------------------------------------

    async def create_from_request(
        self, message: discord.Message, request: dict[str, str]
    ) -> str | None:
        """会話から抽出されたリマインダー依頼を登録し、ユーザーへ返す確認文を返す。

        request は {"at": 日時, "message": 本文, "repeat": 繰り返し種別} の形
        （すべて文字列。"repeat" は欠けていてもよく、その場合は単発として扱う）。
        繰り返しであっても検証は**初回の日時**に対して行う（過去・遠すぎる日時の
        拒否も、本文の切り詰めも、単発とまったく同じ扱い）。

        Returns:
            登録できた場合は確認メッセージ、上限に達している場合はその案内。
            日時が解釈できない・過去・遠すぎるなど「リマインダーとして扱えない」
            場合は None を返す — 呼び出し側は通常のチャット応答へ流すこと
            （依頼ではない発言を LLM が誤って拾った可能性があるため、
            エラーを返すより普通に会話を続ける方が自然）。
        """
        remind_at = self._parse_at(request.get("at", ""))
        if remind_at is None:
            return None

        now = _utc_now()
        if remind_at <= now:
            logger.warning("リマインダー: 過去の日時のため登録しません。")
            return None
        if remind_at > now + timedelta(days=REMINDER_MAX_DAYS_AHEAD):
            logger.warning("リマインダー: 遠すぎる日時のため登録しません。")
            return None

        body = request.get("message", "").strip()
        if not body:
            return None
        if len(body) > REMINDER_MAX_MESSAGE_LENGTH:
            body = body[:REMINDER_MAX_MESSAGE_LENGTH] + "…"

        user_id = str(message.author.id)
        # 抽出できない・未知の繰り返し種別は none（単発）に倒す。
        recurrence = normalize_recurrence(request.get("repeat"))

        guild_id = message.guild.id if message.guild is not None else DM_SETTINGS_GUILD_KEY
        try:
            reminder_id = await run_in_thread(
                self._db.reminder_add,
                user_id=user_id,
                user_name=message.author.display_name,
                guild_id=guild_id,
                channel_id=message.channel.id,
                message=body,
                remind_at=remind_at.isoformat(),
                created_at=now.isoformat(),
                recurrence=recurrence,
                # アンカーは初回時刻で固定する（毎月の日付を元へ戻すための基準）。
                # 単発は使わないので空文字にしておく。
                recurrence_anchor=remind_at.isoformat() if recurrence != "none" else "",
                max_pending=REMINDER_MAX_PENDING_PER_USER,
            )
        except Exception as error:
            logger.error("リマインダー: 登録に失敗しました。", exc_info=error)
            return "⏰ リマインダーの登録に失敗しました。もう一度お試しください。"

        if reminder_id is None:
            return (
                f"⏰ リマインダーは1人あたり {REMINDER_MAX_PENDING_PER_USER} 件までです。"
                "`/functions` → ⏰ リマインダー から不要なものを取り消してください。"
            )

        when = _format_local(remind_at.isoformat(), self._tz)
        label = recurrence_label(recurrence)
        repeat_note = f"（以後 {label} くり返します）" if label else ""
        return f"⏰ **{when}** に「{body}」をお知らせします。{repeat_note}"

    def _parse_at(self, at_text: str) -> datetime | None:
        """LLM が返した日時文字列を UTC の datetime に変換する。解釈できなければ None。

        文字列にタイムゾーンが無い場合は REMINDER_TIMEZONE のローカル時刻とみなす
        （プロンプトでその基準の現在日時を渡しているため）。夏時間の境目の曖昧・存在しない時刻は、
        繰り返しの次回時刻と同じ `_local_to_utc`（fold=0 に固定）で解釈する（#398）。
        """
        text = (at_text or "").strip()
        if not text:
            return None

        for fmt in _AT_FORMATS:
            try:
                return _local_to_utc(datetime.strptime(text, fmt), self._tz)
            except (ValueError, OverflowError):
                continue

        # 上の形式で読めない場合、タイムゾーン付き ISO 8601 の可能性を試す。
        try:
            parsed = parse_iso_datetime(text)
            if parsed.tzinfo is None:
                return _local_to_utc(parsed, self._tz)
            return parsed.astimezone(timezone.utc).replace(microsecond=0)
        except (ValueError, OverflowError):
            logger.warning("リマインダー: 日時を解釈できませんでした。", exc_info=True)
            return None

    # ---- 一覧・取り消し（/functions の ⏰ リマインダーパネルから使う） ----------

    async def list_pending(self, user_id: str) -> list[dict[str, Any]]:
        """未通知一覧を返す。DB障害はReminderStorageErrorで呼出元へ伝える。"""
        try:
            return await run_in_thread(self._db.reminder_list_pending, str(user_id))
        except Exception as error:
            logger.error("リマインダー: 一覧の取得に失敗しました。", exc_info=error)
            raise ReminderStorageError("リマインダーの一覧を取得できませんでした。") from error

    async def cancel(self, reminder_id: int, user_id: str) -> dict[str, Any] | None:
        """本人の系列を取り消す。対象なしはNone、DB障害はReminderStorageError。

        送信開始後なら今回の送信終了を待って次回分を止める。
        """
        try:
            reminder_id = int(reminder_id)
            async with self._delivery_lock(reminder_id):
                return await run_in_thread(self._db.reminder_cancel, reminder_id, str(user_id))
        except Exception as error:
            logger.error("リマインダー: 取り消しに失敗しました。", exc_info=error)
            raise ReminderStorageError("リマインダーを取り消せませんでした。") from error

    def describe(self, record: dict[str, Any]) -> str:
        """リマインダー1件を「日時 — 本文（繰り返し）」の1行表記にする（一覧表示用）。"""
        label = recurrence_label(record.get("recurrence"))
        suffix = f"（{label}）" if label else ""
        return (
            f"{_format_local(record.get('remind_at', ''), self._tz)}"
            f" — {record.get('message', '')}{suffix}"
        )

    # ---- 通知（バックグラウンドの見回りから呼ばれる） --------------------------

    def _build_next_at(self, now: datetime) -> Callable[[dict[str, Any]], str | None]:
        """reminder_claim_due へ渡す「次回時刻を返す関数」を作る。

        **必ず同期関数を返すこと。** この関数は DB のクレームと同一トランザクション
        （＝ Database の threading.Lock を保持したまま）呼ばれるため、
        async def にすると await されずコルーチンが返って壊れる。
        同じ理由で、**中から Database のメソッドを呼んではいけない**
        （再入不可のロックを同じスレッドから取り直してデッドロックする）。
        next_occurrence は暦の計算だけを行う純粋関数なのでこの制約を満たす。
        """

        def next_at(record: dict[str, Any]) -> str | None:
            return next_occurrence(
                remind_at=str(record.get("remind_at", "")),
                recurrence=str(record.get("recurrence", "none") or "none"),
                anchor=str(record.get("recurrence_anchor", "") or ""),
                after=now,
                tz=self._tz,
            )

        return next_at

    async def run_due_checks(self) -> None:
        """期限が来たリマインダーを通知する。REMINDER_CHECK_SECONDS ごとに呼ばれる。"""
        now = _utc_now()
        try:
            due = await run_in_thread(
                self._db.reminder_claim_due, now.isoformat(), self._build_next_at(now)
            )
        except Exception as error:
            logger.error("リマインダー: 期限の確認に失敗しました。", exc_info=error)
            return

        if not due:
            return

        # 停止中に溜まった古い通知は送らない（起動直後に何件も流れるのを防ぐ）。
        # claim 済みなので、送らなくても再通知はされない。
        # 繰り返しの場合も次回時刻はクレーム時に進み終えているため、ここで今回分を
        # 見送っても系列は途切れない（＝長期停止明けは翌回の定刻から自然に再開する）。
        # 判定に使う record["remind_at"] は「進める前の予定時刻」であることが前提。
        stale_before = now - timedelta(hours=REMINDER_STALE_HOURS)

        for record in due:
            if self._is_stale(record, stale_before):
                logger.warning("リマインダー: 予定時刻を大きく過ぎているため通知を見送りました。")
                continue
            try:
                await self._notify(record)
            except Exception as error:
                logger.error("リマインダー: 通知に失敗しました。", exc_info=error)

        # 通知済みレコードが際限なく溜まらないよう、古いものから捨てる。
        try:
            await run_in_thread(self._db.reminder_prune_finished, REMINDER_MAX_FINISHED)
        except Exception as error:
            logger.error("リマインダー: 古い記録の掃除に失敗しました。", exc_info=error)

    @staticmethod
    def _is_stale(record: dict[str, Any], stale_before: datetime) -> bool:
        """通知予定が古すぎる（stale_before より前）かどうかを返す。

        日時が読めない壊れたレコードは古い扱いにしない（通知して気づけるようにする）。
        """
        try:
            remind_at = parse_iso_datetime(str(record.get("remind_at", "")))
        except ValueError:
            return False
        if remind_at.tzinfo is None:
            remind_at = remind_at.replace(tzinfo=timezone.utc)
        return remind_at < stale_before

    async def _notify(self, record: dict[str, Any]) -> None:
        """1件分の通知を、依頼されたチャンネルへ投稿する。"""
        channel_id = int(record["channel_id"])
        channel = self._client.get_channel(channel_id)
        if channel is None:
            # 起動直後などキャッシュに無い場合があるため、API からの取得も試す。
            try:
                channel = await self._client.fetch_channel(channel_id)
            except Exception as error:
                logger.error("リマインダー: チャンネルを取得できませんでした。", exc_info=error)
                return

        # カテゴリなど投稿できない種類のチャンネルが返ることがあるため確認する
        if not hasattr(channel, "send"):
            logger.warning("リマインダー: チャンネルへは投稿できません。")
            return

        guild = getattr(channel, "guild", None)
        if guild is not None:
            # 判定は自発会話・記念日と同じ共通関数で行う（スレッドは「スレッドで送信」を見る）。
            if guild.me is None:
                logger.warning("リマインダー: ボットの権限を確認できないため通知を見送りました。")
                return
            if not can_bot_send_messages(channel, guild.me):
                logger.warning("リマインダー: 閲覧・送信権限が不足しているため通知を見送りました。")
                return

        if isinstance(channel, discord.Thread) and channel.archived:
            if guild is None or guild.me is None:
                logger.warning("リマインダー: スレッドの権限を確認できません。")
                return
            # 取消済みの系列は、通知だけでなくスレッドの再開も行わない。
            # 再開の通信中は従来どおり取消を受け付け、送信直前にも再確認する。
            if record.get("next_remind_at"):
                async with self._delivery_lock(int(record["reminder_id"])):
                    if not await run_in_thread(self._db.reminder_is_pending, int(record["reminder_id"])):
                        return
            # 宛先を親へ変えると、非公開スレッドの内容を別の参加者へ漏らしうる。
            # 元のスレッドだけを再開し、ロック解除や代替配送は行わない。
            if channel.locked and not channel.permissions_for(guild.me).manage_threads:
                logger.warning("リマインダー: ロックされたスレッドを再開する権限がありません。")
                return
            try:
                channel = await channel.edit(archived=False, reason="登録済みリマインダーの通知")
            except discord.HTTPException as error:
                logger.error("リマインダー: スレッドを再開できませんでした。", exc_info=error)
                return
            if not can_bot_send_messages(channel, guild.me):
                logger.warning("リマインダー: スレッド再開後の送信権限が不足しています。")
                return

        text = str(record.get("message", ""))
        # 本文はユーザーの発言そのものなので、投稿前に NG ワードをマスクする
        # （登録時に AI モデレーションは通過済みのため、ここでは再判定しない）。
        text = self._ng_filter.mask_with_log(text, label="リマインダー")

        body = f"⏰ <@{record['user_id']}> リマインダーです: **{text}**"
        # 繰り返しはクレーム時に次回時刻まで進んでいるので、その予定を添えて
        # 「まだ続くこと」と「止めたいときの目安」を伝える（単発には付けない）。
        next_at = record.get("next_remind_at")
        if next_at:
            body += f"\n🔁 次回は {_format_local(str(next_at), self._tz)} にお知らせします。"
        # 本文は利用者が書いた文なので、@everyone・@here・ロール・他人へのメンションが
        # 含まれうる（AI モデレーションは不適切な表現を見るだけで、これらは通過する）。
        # 通知を飛ばしてよいのは依頼者本人だけに限る（記念日のお祝いと同じ制御）。
        # 繰り返しのリマインダーでは同じ本文が定期的に出るため、毎回この制限を掛ける。
        allowed_mentions = discord.AllowedMentions(
            users=[discord.Object(id=int(record["user_id"]))],
            everyone=False, roles=False, replied_user=False,
        )
        if not next_at:
            # 単発はクレーム時に完了済みで、これ以降の取消は成功しない。
            await send_long_message(channel, body, allowed_mentions=allowed_mentions)
            return

        # チャンネル取得・スレッド再開を待つ間は取消を妨げず、送信直前に確認する。
        # 送信中はこの系列だけをロックし、開始済みの通知は完了してから取消を返す。
        # DBの共有同期ロックは通信中に保持しない。
        async with self._delivery_lock(int(record["reminder_id"])):
            if not await run_in_thread(self._db.reminder_is_pending, int(record["reminder_id"])):
                return
            await send_long_message(channel, body, allowed_mentions=allowed_mentions)
