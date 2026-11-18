"""リマインダーの日時解釈・表示・繰り返しの次回計算（#469）。

暦とタイムゾーンだけを扱う純粋ロジック。Discord・DB・通知には依存しない。
`reminder_service` が従来の名前（`next_occurrence` / `to_local_naive` / `local_to_utc` など）で再公開する。
ログは分割前と同じロガー名 `persona_bot.services.reminder_service` へ出す。
"""

import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from persona_bot.config.constants import REMINDER_RECURRENCE_LABELS, REMINDER_RECURRENCE_MAX_CATCHUP_STEPS
from persona_bot.utils.normalization import normalize_recurrence
from persona_bot.utils.timeutil import as_utc, format_weekday, parse_iso_datetime

logger = logging.getLogger("persona_bot.services.reminder_service")


# LLM が返す日時のゆらぎを吸収するため、複数の形式を順に試す。
AT_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def format_local(iso_utc: str, tz: Any) -> str:
    """保存されている UTC の ISO 8601 文字列を、表示用のローカル日時表記に直す。"""
    try:
        dt = parse_iso_datetime(iso_utc)
    except ValueError:
        return iso_utc
    local = as_utc(dt).astimezone(tz)
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


def to_local_naive(iso_utc: str, tz: Any) -> datetime | None:
    """UTC の ISO 8601 文字列を、tz における「素朴な（tzinfo 無しの）ローカル日時」にする。

    日付の加算をローカルの壁時計で行うため、いったん tzinfo を外す。UTC のまま
    timedelta を足すと、DST のあるタイムゾーンへ設定を変えた瞬間に時刻がずれる。
    """
    try:
        dt = parse_iso_datetime(str(iso_utc))
    except (TypeError, ValueError):
        return None
    return as_utc(dt).astimezone(tz).replace(tzinfo=None)


def local_to_utc(local_naive: datetime, tz: Any) -> datetime:
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
    曖昧な時刻・存在しない時刻の解釈は local_to_utc を参照。

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

    current_local = to_local_naive(remind_at, tz)
    if current_local is None:
        logger.warning("リマインダー: 繰り返しの基準日時を読めませんでした。")
        return None

    # アンカーが読めない行は今回の予定時刻を基準にする（繰り返しを止めない）。
    anchor_local = to_local_naive(anchor, tz) or current_local

    after = as_utc(after)

    if kind == "monthly":
        # 反復の起点はアンカーではなく「今回の予定時刻の月」にする。
        # アンカーから数え直すと、何年も続いた繰り返しで反復回数が上限に届いてしまう。
        months = _month_distance(anchor_local, current_local)
        for _ in range(max_steps):
            months += 1
            candidate = local_to_utc(_monthly_occurrence(anchor_local, months), tz)
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
            candidate = local_to_utc(anchor_local + delta * steps, tz)
            if candidate > after:
                return candidate.isoformat()

    # ここに来るのは「進めても未来に届かない」＝計算が前進していない不具合のとき。
    # 無限ループになるより、系列を止めて気づけるようにする。
    logger.error("リマインダー: 次回時刻の計算が %d 回で追い付きませんでした。", max_steps)
    return None
