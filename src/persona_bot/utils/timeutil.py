"""タイムゾーンの解決を1か所に集めたヘルパー。

会話統計・記念日・リマインダーはいずれも「日本時間で何時か / 今日はいつか」を
基準に動くため、それぞれが ZoneInfo を組み立てていた。ZoneInfo はタイムゾーン
データベースが無い環境（tzdata を含まないスリムな Linux イメージなど）で
例外を投げるため、各所で try/except を書く必要があり重複していた。

解決に失敗した場合は UTC へフォールバックする（集計時刻や表示がずれるだけで、
ボット自体は動かせるようにするフェイルオープン設計）。
"""

import logging

from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)

# 日本語の曜日名。datetime.weekday() は月曜=0・日曜=6 を返すため、この並び順と一致する。
#
# content_moderator.py（リマインダー登録用プロンプト）と reminder_service.py
# （リマインダーの表示）は、format_weekday() を通してこのテーブルを参照する。
# 登録時と表示時の表記を揃えるため、曜日名を変えるときはこのテーブルを直すこと。
WEEKDAY_NAMES_JA = ("月", "火", "水", "木", "金", "土", "日")


def utc_now() -> datetime:
    """現在の UTC 時刻を秒精度（マイクロ秒を落とす）で返す。保存する時刻の既定の精度。

    リマインダーの期限判定のように ISO 8601 文字列の比較で扱う値があるため、
    保存する時刻の桁数は秒に揃える（桁数が混ざると文字列の比較が壊れる）。
    """
    return datetime.now(timezone.utc).replace(microsecond=0)


def utc_now_iso(*, timespec: str = "seconds") -> str:
    """現在の UTC 時刻を、保存用の ISO 8601 文字列（例: 2026-01-01T00:00:00+00:00）で返す。

    既定は秒精度。各サービスで datetime.now(...).isoformat() を直書きすると精度が
    混ざるため、保存する時刻文字列はこの関数で作る。同じ1秒に作られた記録の順序を
    時刻で決める列（文書キューの created_at）だけは timespec="microseconds" を明示する。
    """
    return datetime.now(timezone.utc).isoformat(timespec=timespec)


def parse_iso_datetime(text: str) -> datetime:
    """Python 3.10でもUTC末尾のZを解釈する。その他のISO形式は標準に従う。"""
    return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)


def as_utc(value: datetime) -> datetime:
    """日時をUTCへ揃える。タイムゾーンのない旧値もUTCとして扱う。"""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def resolve_timezone(name: str, *, purpose: str = "") -> tzinfo:
    """タイムゾーン名を解決する。読み込めない環境では UTC を返す。

    Args:
        name: "Asia/Tokyo" のような IANA タイムゾーン名。
        purpose: 呼出元の用途を示す互換用の引数。用途名や設定値は非公開情報を
            含みうるため、失敗時にもログへ出さない。
    """
    try:
        return ZoneInfo(name)
    except Exception as error:
        logger.warning('タイムゾーンの読み込みに失敗しました。UTC で代用します。', exc_info=error)
        return timezone.utc


def timezone_label(tz: tzinfo) -> str:
    """解決済みタイムゾーンの表示名を返す（"Asia/Tokyo" / UTC 代用時は "UTC"）。

    **画面表示やプロンプトに載せる基準タイムゾーン名は、定数
    （REMINDER_TIMEZONE 等）ではなく必ずこの関数で作ること。**
    resolve_timezone は tzdata の無い環境で UTC へフォールバックするため、
    定数をそのまま表示すると「実際は UTC の時刻なのに Asia/Tokyo と名乗る」
    という食い違いが起きる。リマインダーでは、この食い違いがそのまま
    9時間ずれた時刻での登録につながる。
    """
    return str(tz)


def format_weekday(dt: datetime) -> str:
    """datetime から日本語の曜日1文字（"月"〜"日"）を返す。

    プロンプトへ埋める現在日時と、ユーザーへ見せるリマインダーの日時表示の両方から
    使うことを想定している（WEEKDAY_NAMES_JA のコメント参照）。

    Args:
        dt: 曜日を取り出す日時。tz 有無は問わない（weekday() はどちらでも使える）。
    """
    return WEEKDAY_NAMES_JA[dt.weekday()]


def format_prompt_now(tz: tzinfo, *, include_zone: bool = False) -> str:
    """プロンプトへ埋める「現在日時（曜日つき）」の文字列を返す。

    例: `2026-11-19 09:30（木曜日）`。`include_zone=True` なら `（木曜日、Asia/Tokyo）` のように
    実際に解決できたタイムゾーン名（timezone_label）を添える。相対表現の解釈に使う基準日時は、
    リマインダー判定（content_moderator）と委任の子（agent_service）で表記を揃えるため、
    ここに1つだけ置く。
    """
    now = datetime.now(tz)
    suffix = f"、{timezone_label(tz)}" if include_zone else ""
    return f"{now:%Y-%m-%d %H:%M}（{format_weekday(now)}曜日{suffix}）"
