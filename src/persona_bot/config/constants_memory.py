"""長期記憶・親密度・記念日の調整値。

`persona_bot.config.constants` が `import *` で再公開する。利用側は従来どおり
`from persona_bot.config.constants import 名前` と書くこと（このモジュールを直接 import しない）。
テストの `patch.multiple("persona_bot.config.constants", ...)` がその入口を差し替えるため。

★ このモジュールは何も import しない（constants.py や他の領域モジュールを import すると、
再公開との間で循環参照になる）。他の領域の値から決まる定数は constants.py に置く。
★ 名前は constants.py・他の領域モジュールと重複させないこと（後から読み込まれた方が黙って勝つ）。
"""

ROUTER_MODEL_NAME = "gemini-3.1-flash-lite"
ROUTER_TIMEOUT_SECONDS = 15.0
MEMORY_COMPACT_TIMEOUT_SECONDS = 30.0
# 記憶圧縮がこの回数連続で失敗したら pending_log を破棄して無限リトライを防ぐ
MEMORY_COMPACT_MAX_FAILURES = 3
# pending_log の Q/A 件数がこれ以上になったら記憶圧縮を実行する（ユーザー単位で判定）
MEMORY_FLUSH_THRESHOLD_ENTRIES = 25
MEMORY_FLUSH_IDLE_SECONDS = 60 * 60
MEMORY_MAINTENANCE_MINUTES = 10
# 1 回のフラッシュ処理で圧縮するユーザー数の上限。記憶はユーザー単位で個別に
# 圧縮する（他人の会話ログを同じ Gemini 呼び出しに混ぜない＝プライバシー保護）ため、
# アクティブ人数分の呼び出しが一度に走らないよう上限で区切る（レート制限対策）。
# 上限を超えた分は次回の見回りで順次処理される。
MEMORY_FLUSH_MAX_USERS_PER_RUN = 5
# 共有ストレージの障害が疑われる場合、同じ巡回での失敗を繰り返しすぎない。
MEMORY_FLUSH_MAX_CONSECUTIVE_ERRORS = 3

# 記憶ルーティングの分類カテゴリ（route_category とモデレーション統合判定の共通定義）。
MEMORY_ROUTE_CATEGORIES = ("profile", "projects", "none")

# ── ユーザー別記憶への移行用 ──
# 本ボットはユーザー別記憶の導入前、単一ユーザーによるテスト運用のため
# profile / projects を「全体共有の1スペース」で持っていた。その既存データを
# 失わないよう、移行時にここで指定したユーザーIDの記憶として引き継ぐ。
#
# 移行は既に完了しているため空文字にしてある（引き継ぎ元がもう存在しない）。
# 空文字のときは引き継ぎ処理自体が行われない。旧・全体共有形式のデータが
# 残っている環境から移行する場合のみ、その所有者の Discord ユーザーIDを入れること。
LEGACY_MEMORY_OWNER_ID = ""
# 旧ファイルの退避名（<元の名前>.imported.bak）が既にあるときに試す連番の上限（#379）。
# 既存の退避ファイルを上書きしないための歯止めで、通常は連番まで進まない。
LEGACY_BACKUP_MAX_SUFFIX = 1000

# ── 記憶の可視化（#484。memory_graph.py）──
# /functions → 🔒 統計プライバシー → 🧠 記憶を見る で、本人の記憶をノードグラフの HTML にして渡す。
# 図に載せるノードの上限（本人・分類のノードを含む）。超えた分は省略し、ファイルの冒頭で知らせる。
# 記憶圧縮が「重要な事実だけ」に絞るため通常は届かない。ブラウザでの配置計算（全ノード対の反発）が
# ノード数の2乗で重くなるので、上限なしにはしない。
MEMORY_GRAPH_MAX_NODES = 400
# 本人を 0 とした枝の深さの上限。これより深い入れ子は枝を伸ばさず、中身をまとめて詳細に載せる。
MEMORY_GRAPH_MAX_DEPTH = 6
# 図のノードに表示する文字数の上限（超えた分は「…」。全文はクリックした詳細に出す）。
MEMORY_GRAPH_LABEL_MAX_CHARS = 32
# 詳細に表示する1ノード分の文字数の上限（ファイルが際限なく大きくならないための歯止め）。
MEMORY_GRAPH_DETAIL_MAX_CHARS = 2000
# 添付するファイル名。利用者 ID などの識別子は入れない（ファイルを他人へ渡したときに漏れないため）。
MEMORY_GRAPH_FILENAME = "memory_graph.html"
# 作成日時・文書の保存日時の表示に使うタイムゾーン。
MEMORY_GRAPH_TIMEZONE = "Asia/Tokyo"

# 親密度 / 感情（affinity_service.py）用
# valence（機嫌）/ arousal（テンション）が中立値へ戻るまでの半減期（秒）。
# 30 分で「今の感情の揺れ」が半分に薄まる。
AFFINITY_HALF_LIFE_SECONDS = 1800.0
# 親密度・機嫌・テンションの中立値（初期値であり、減衰の収束先でもある）。
AFFINITY_NEUTRAL = 50
# 機嫌がこの値より高い返答では親密度を 1 上げる。
AFFINITY_RAISE_THRESHOLD = 70
# 機嫌がこの値より低い返答では親密度を 1 下げる。
AFFINITY_LOWER_THRESHOLD = 30
# 親密度・機嫌・テンションが取りうる範囲（この外へは丸める）。
AFFINITY_MIN = 0
AFFINITY_MAX = 100

# 記念日・誕生日の自動お祝い用
# profile 記憶の特別キー名。値は [{"label": "誕生日", "month": 1〜12, "day": 1〜31}, ...] の形式。
ANNIVERSARY_PROFILE_KEY = "anniversaries"
# 「今日が記念日かどうか」の見回り間隔（分）。記念日はその日1回届けば十分なため、
# 自発的な会話(AUTO_TOPIC_CHECK_MINUTES)ほど高頻度にする必要はない。
ANNIVERSARY_CHECK_MINUTES = 60
# お祝いメッセージ生成の Gemini 呼び出しタイムアウト（秒）。
ANNIVERSARY_TIMEOUT_SECONDS = 60.0
# お祝いメッセージ生成プロンプトに含めるチャンネル履歴の件数上限。
ANNIVERSARY_HISTORY_LIMIT = 30
# 記念日の判定に使うタイムゾーン（STATS_TIMEZONE と同じ地域基準で「今日」を判定する）。
ANNIVERSARY_TIMEZONE = "Asia/Tokyo"
# 公開チャンネルへお祝いを投稿してよい記念日ラベルの許可リスト。
# 長期記憶の ANNIVERSARY_PROFILE_KEY（"anniversaries"）は記憶圧縮の LLM が
# 自由記述で書き込むため、「退院記念日」「離婚記念日」のように DM でだけ話した
# 私的な記念日が入りうる。それらをそのまま共有チャンネルで祝うと、本人が
# 公開するつもりのなかった事情が他の参加者へ露出してしまう。
# そのため、一般に祝われることが明らかなラベルだけを公開投稿の対象にする
# （ここに無いラベルは記憶としては保持するが、自動お祝いはしない）。
ANNIVERSARY_PUBLIC_LABELS = ("誕生日", "記念日")
