"""リマインダーの調整値（繰り返しを含む）。

`persona_bot.config.constants` が `import *` で再公開する。利用側は従来どおり
`from persona_bot.config.constants import 名前` と書くこと（このモジュールを直接 import しない）。
テストの `patch.multiple("persona_bot.config.constants", ...)` がその入口を差し替えるため。

★ このモジュールは何も import しない（constants.py や他の領域モジュールを import すると、
再公開との間で循環参照になる）。他の領域の値から決まる定数は constants.py に置く。
★ 名前は constants.py・他の領域モジュールと重複させないこと（後から読み込まれた方が黙って勝つ）。
"""

# リマインダー（会話から自動登録し、時刻になったら依頼されたチャンネルへ通知する）用
# 期限が来たリマインダーを探す見回りの間隔（秒）。分単位の指定に応えるため、
# 記念日（時間単位）よりずっと短い間隔で確認する。
REMINDER_CHECK_SECONDS = 60
# 「30分後」「明日の朝」などの相対表現を絶対日時に直すときの基準タイムゾーン。
# 保存は UTC で行い、ユーザーへの表示だけこのタイムゾーンに変換する。
REMINDER_TIMEZONE = "Asia/Tokyo"
# 1ユーザーが同時に持てる未通知リマインダーの上限（登録し放題にしない）。
REMINDER_MAX_PENDING_PER_USER = 20
# これより先の日時は受け付けない。日時の解釈は LLM に任せるため、
# 誤解釈で何十年も先に登録されてしまうのを防ぐ歯止め。
REMINDER_MAX_DAYS_AHEAD = 365
# リマインド本文の最大文字数（超過分は切り詰める）。
REMINDER_MAX_MESSAGE_LENGTH = 200
# 通知済み・取り消し済みレコードの保持件数。queue_tasks と同様に古い分から捨てる。
REMINDER_MAX_FINISHED = 100
# 通知予定をこの時間より大きく過ぎたリマインダーは、通知せず処理済みにする。
# ボットを長時間停止していた場合に、溜まっていた通知が起動直後へ一斉に流れるのを防ぐ
# （何時間も前の「10分後に休憩」を今さら知らされても困るため）。
REMINDER_STALE_HOURS = 24

# --- リマインダーの繰り返し ---
# 繰り返し種別の許容値。統合判定プロンプトが返す値の正規化に使う
# （MEMORY_ROUTE_CATEGORIES と同じ「許容値タプルを唯一の情報源にする」作法）。
REMINDER_RECURRENCE_VALUES = ("none", "daily", "weekly", "monthly")
# 繰り返し種別の表示名。確認文・一覧表示で使う。
# 今はユーザーが選ぶ UI が無い（会話から抽出する）ため choices.py ではなくここに置く。
# セレクトメニューを作ることになったら choices.py へ移すこと。
REMINDER_RECURRENCE_LABELS = {
    "none": "",
    "daily": "毎日",
    "weekly": "毎週",
    "monthly": "毎月",
}
# 停止中に積み残した繰り返しを「未来になるまで進める」ときの最大反復回数。
# 1分間隔の繰り返しは作れない（最短が毎日）ため、通常は数十回で足りる。
# 計算が前進しない不具合が入ったときに無限ループしないための歯止め。
REMINDER_RECURRENCE_MAX_CATCHUP_STEPS = 1000
