from pathlib import Path

# モジュールの配置に関係なく、既存のペルソナ相対パスは src/ を基準に保つ。
BASE_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BASE_DIR.parent

# 運用ログは標準出力へ揃え、journaldと管理コンソールが同じ形式を扱う。
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# サービス終了通知と残留タスクの回収に使う共通の待機上限。
# 超過時は失敗を返し、使用中の共有クライアントやDBを先に閉じない。
BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS = 60.0

# 別接続の短い書き込みと競合した場合の待機上限。超過したロックは例外として扱う。
DATABASE_BUSY_TIMEOUT_SECONDS = 30.0

# ペルソナ（キャラクター設定）のプロンプトを置くフォルダ。Git が管理するのは書式見本の
# テンプレートだけで、キャラ別のファイルは利用者が各自のパソコン・サーバーで作る
# （.gitignore で除外済み）。ここにある .txt がキャラ設定の選択肢になる（personas.py）。
PROMPTS_DIR = PROJECT_ROOT / "prompts"
# プロンプトの書式見本（Git 管理対象）。コピーしてキャラを作る元で、選択肢には出さない。
PROMPT_TEMPLATE_FILE_NAME = "00_prompt_format.txt"
# 表示名・読み上げの声・既定のペルソナを決める任意の設定ファイル（利用者がローカルで作る）。
# 無くてもよく、その場合はファイル名（拡張子なし）を表示名にし、先頭のファイルを既定にする。
PERSONA_SETTINGS_PATH = PROMPTS_DIR / "personas.json"
# personas.json の書式見本（Git 管理対象）。利用者が personas.json へコピーして書き換える元で、
# ボットはこのファイルを読まない（.env.example と同じ扱い）。
PERSONA_SETTINGS_TEMPLATE_FILE_NAME = "00_prompt_format.json"
# 保存・選択に使うペルソナの値は「src/ 基準の相対パス」= この接頭辞 + ファイル名。
# 旧版の固定の選択肢と同じ形なので、ファイル名を変えなければ保存済みの設定を引き継げる。
PERSONA_VALUE_PREFIX = "../prompts/"
# ペルソナを1つも作っていない間に使う、キャラ付けのない標準のプロンプト。
# テンプレートだけを取得した直後でも、存在しないファイルを参照せずに会話できるようにする。
BUILTIN_PERSONA_PROMPT = (
    "あなたは Discord サーバーの会話に参加している、親しみやすいアシスタントです。\n"
    "日本語で、相手の質問や話題に自然な話し言葉で簡潔に答えてください。\n"
    "分からないことは分からないと伝え、事実を作り上げないでください。"
)
# 選択肢（choices.MODEL_CHOICES）から外したモデルを保存していたサーバーは、起動時にこの既定値へ戻る。
DEFAULT_MODEL_NAME = "gemini-3.5-flash-lite"
DEFAULT_RESPOND_MODE = "mention"

# 設定はサーバー（ギルド）ごとに分離されるが、DM にはギルドIDが存在しない。
# DB（guild_settings / anniversary_posts テーブル）に DM の行を保存する際は、
# 実在しないギルドIDとしてこの値を使う（Discord のIDは正の整数のため 0 は衝突しない）。
DM_SETTINGS_GUILD_KEY = 0

# APIキー（Gemini / Discord）を書いた .env ファイル。プロジェクトルート直下に置く。
# ボットは src/ の中から起動するため、カレントディレクトリ基準の相対探索では
# 見つけられない。settings_loader.py はこの絶対パスを load_dotenv() へ明示的に渡す。
# .gitignore で除外済み（見本は Git 管理された .env.example）。
ENV_FILE_PATH = PROJECT_ROOT / ".env"

# プログラムと利用者のデータを分け、保存先を data/ 配下へまとめる。
DATA_DIR = PROJECT_ROOT / "data"
MEMORY_DIR = DATA_DIR / "memory"
# SQLite データベース本体。親密度・統計・文書キュー・一時ログ・プロフィール・
# サーバー設定・記念日の投稿記録・リマインダー・フィードバックの9テーブルを格納する。
# profile.json は起動時の手書き追加用、projects.json は自由形式JSONとして保持する。
DATABASE_PATH = MEMORY_DIR / "bot.db"
PROFILE_MEMORY_PATH = MEMORY_DIR / "profile.json"
PROJECTS_MEMORY_PATH = MEMORY_DIR / "projects.json"

NG_DIR = DATA_DIR / "ng"
NG_WORD_PATH = NG_DIR / "NG_WORD.txt"
NG_WORD_PRIVATE_PATH = NG_DIR / "NG_WORD_private.txt"
BLACKLIST_PATH = NG_DIR / "blacklist.json"
BLACKLIST_MAX_VIOLATIONS = 3

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

HISTORY_LIMIT = 100
GEMINI_RESPONSE_TIMEOUT_SECONDS = 60.0
MODERATION_MODEL_NAME = "gemini-3.1-flash-lite"
MODERATION_TIMEOUT_SECONDS = 10.0
# モデレーションプロンプトに「参考」として載せる NG ワードの最大件数。
# 全件を載せるとプロンプトが肥大化してトークンを浪費するため先頭からこの件数だけ渡す。
# UI 側の DISCORD_SELECT_LABEL_MAX_LENGTH と数値は同じだが意味がまったく違うので別名にする。
MODERATION_NG_WORD_SAMPLE_LIMIT = 100

# 通常会話は入力AI判定・画像読込より前に確保し、満杯なら待たずに見送る。
CHAT_RATE_WINDOW_SECONDS = 60.0
CHAT_MAX_REQUESTS_PER_USER = 5
CHAT_MAX_CONCURRENT_PER_GUILD = 2
CHAT_MAX_CONCURRENT_GLOBAL = 4
# 期間内の受付・見送り案内の記録をそれぞれ持つ人数。生きた記録は追い出さない。
CHAT_RATE_MAX_USERS = 4096
CHAT_RATE_LIMIT_NOTICE = "短時間に多くの発言を受け付けたため、少し待ってからもう一度話しかけてください。"
CHAT_BUSY_NOTICE = "ただいま混み合っています。少し待ってからもう一度話しかけてください。"

# 会話要約 (/functions → 📋 会話要約) 用
# 対象メッセージがこの件数を超えたら要約せず自動停止する（トークン超過・
# API 取得時間・レート制限の暴発を防ぐ安全弁）。
SUMMARY_MAX_MESSAGES = 1000
# 大量メッセージの要約は通常チャットより時間がかかるため長めに確保する。
SUMMARY_TIMEOUT_SECONDS = 120.0
# 要約へ渡す会話ログの時刻表記に使うタイムゾーン。
# 実行環境のローカル時刻（astimezone() の既定）に頼ると、海外リージョンの VPS では
# 議事録の時刻が日本の利用者の感覚とずれるため、用途ごとに明示して固定する
# （STATS_TIMEZONE / ANNIVERSARY_TIMEZONE / REMINDER_TIMEZONE と同じ方針）。
SUMMARY_TIMEZONE = "Asia/Tokyo"

MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024
# 画像も取得前と取得後に容量を確認し、大きな添付の取得・デコードを見送る。
MAX_IMAGE_SIZE_BYTES = 5 * 1024 * 1024
# 1GBのVPS向け。縮小デコードできない形式は1200万画素（RGBA換算48MB）まで。
# 1080×8000のPNGを許容しつつ、通常会話4件でも元画像だけで約192MBに抑える。
MAX_IMAGE_PIXELS = 12_000_000
# JPEGはdraftで縮小デコードし、8064×6048の写真を含む5000万画素まで許容する。
# draftは2/4/8分の1の縮小なので、2048pxへ近づけて元のRGB領域の確保を抑える。
# デコーダー固有の一時領域もあるため、JPEGでも元画像の上限自体は設ける。
MAX_IMAGE_JPEG_PIXELS = 50_000_000
# 細長すぎる入力もヘッダーだけで拒否。保持画像は長辺2048px以下へ縮小する。
MAX_IMAGE_SOURCE_DIMENSION = 16384
MAX_IMAGE_DIMENSION = 2048
MAX_IMAGE_ATTACHMENTS = 4
IMAGE_RGB_BYTES_PER_PIXEL = 3
# 縮小後のRGB画素データだけを合計する。Pillow内部や一時領域を含む総メモリではない。
MAX_IMAGE_TOTAL_RGB_BYTES = 24 * 1024 * 1024
SUPPORTED_DOCUMENT_EXTENSIONS = {".pdf", ".txt", ".csv", ".md"}
# マルチモーダル入力として受け付ける画像の拡張子（Content-Type が空の添付の判定用）。
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# 本文が無く画像だけが送られたときに、質問文の代わりにモデルへ渡す文
# （自律配信のコメントでは AUTONOMOUS_IMAGE_ONLY_COMMENT を使う）。
IMAGE_ONLY_QUESTION = "この画像について説明・反応してください。"
# 本文が無く文書だけが送られたときに、質問文の代わりにモデルへ渡す文（文書ワーカーが使う）。
DOCUMENT_ONLY_QUESTION = "このドキュメントの要点を整理してください。"
# Discord の1メッセージあたりの文字数上限。これを超える返信は split_message で分割する。
DISCORD_MESSAGE_MAX_LENGTH = 2000
DOCUMENT_UPLOAD_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_TIMEOUT_SECONDS = 60.0
DOCUMENT_SUMMARY_LIMIT = 12
# プロンプトへ差し込む文書要約の件数（新しいものから何件を文脈に載せるか）。
DOCUMENT_MEMORY_CONTEXT_LIMIT = 3
DOCUMENT_WORKER_IDLE_SECONDS = 5
# テキスト系文書（txt/csv/md）の妥当性検証で先頭から読むバイト数。
# 全体をデコードすると大きなファイルで無駄が大きいため、先頭だけを見て
# 「UTF-8 として読めるか」を確認する。
TEXT_VALIDATION_PREVIEW_BYTES = 4096
QUEUE_MAX_FINISHED_TASKS = 50
# 文書キューの未処理（pending / processing / sending）の上限（#202）。全サーバーで1つの
# ワーカーが到着順に処理するため、1人の大量投稿が他の利用者の文書を待たせ続けないよう、
# 利用者別と全体の両方で受付時に制限する。キューは永続なので再起動しても解消しない。
DOCUMENT_QUEUE_MAX_ACTIVE_PER_USER = 3
DOCUMENT_QUEUE_MAX_ACTIVE_TOTAL = 20
# 上限に達して文書を受け付けなかったときの案内（利用者別・全体のどちらでも同じ文言にする）。
DOCUMENT_QUEUE_FULL_NOTICE = "処理待ちの文書が多いため、今は受け付けられませんでした。前の文書の回答が届いてから、もう一度送ってください。"
# 文書処理に失敗したとき元の投稿へ返す定型文（#220）。返信は公開チャンネルに出るため、
# 例外の本文（内部のパスや接続先を含みうる）は載せず、利用者が対処できる理由だけを後ろに添える。
DOCUMENT_FAILURE_NOTICE = "エラーが発生しました。（文書処理に失敗しました）"
MAX_GROUNDING_SOURCES = 5
# 検索用のサブモデル。Gemini 3 以降の検索は無料枠に含まれないため、無料枠では
# 選択モデルの検索が 429 で断られ、このモデル（Gemini 2.5 の検索枠）で検索する。
GROUNDING_MODEL_NAME = "gemini-2.5-flash"
# timeout未指定時の検索付き生成全体の期限（秒）。SDKの生成全段で共有する。
GROUNDING_REQUEST_TIMEOUT_SECONDS = 60.0
# 検索ツール非対応を示す4xx、または検索付きの試行の利用枠超過（429）の場合に、GROUNDING_MODEL_NAMEで再試行するか。
# True: 選択モデル検索 → サブモデル検索 → 選択モデル検索なしの順で試す。
# False: サブモデルを省いて検索なしへ進む。期限切れ・5xx・認証と、検索なしの生成の利用枠エラーは再試行しない。
GROUNDING_SUBMODEL_RETRY_ENABLED = True
# 検索ツールの呼び出しや思考（tool_code・thought など）が通常の本文へ混入した応答を破棄し、
# 同じモデル・検索設定で生成し直す回数の上限（1回の生成全体で共有）。再生成も生成全体の期限内で行う。
# 0 にすると再生成せず、生成の失敗として扱う。
GEMINI_INTERNAL_TEXT_MAX_REGENERATIONS = 1

# ── サブエージェント（委任型。#170） ──
# 会話の中で「サブエージェントを使って」と明記された依頼だけを、人格役（ペルソナ）から
# 子へ委任する。False にすると明記があっても通常の会話として答える。
AGENT_ENABLED = True
# 委任の判定を check_and_route に足す発言の条件（本文にこの表記があること）。明記の無い
# 普段の会話ではルーターのプロンプトを変えず、トークンと既存判定の精度に影響させない。
# 明記があっても、ルーターが「親だけで答えられる」と判断した依頼は委任しない。
AGENT_TRIGGER_PATTERN = r"サブ\s*エージェント|sub[\s_-]?agents?"
# 子の作業手順の上限。1手順につき検索付きの生成を1回行う（＝子のツール実行の上限）。
AGENT_MAX_STEPS = 3
# 子の1手順の生成期限（秒）。検索のサブモデルへの切り替えもこの時間内で行う。
AGENT_STEP_TIMEOUT_SECONDS = 90.0
# 子の作業全体の期限（秒）。超えたら中止し、ペルソナの定型文で失敗を伝える。
AGENT_TOTAL_TIMEOUT_SECONDS = 300.0
# 同時に動かす子の上限（ボット全体）。同じ利用者の依頼は常に1件ずつにする。
AGENT_MAX_CONCURRENT_TASKS = 2
# 委任内容を整理するためにルーターへ渡す、チャンネルの直近の発言数
# （「さっきの件を調べて」のような依頼を、独立した文脈で動く子へ渡せる形にするため）。
AGENT_ROUTER_HISTORY_LIMIT = 10
# ルーターが返す委任内容の長さの上限（ロール・依頼内容・出力形式・手順名）。
# 長すぎる値は切り詰める（子のプロンプトと進捗表示を肥大させないため）。
AGENT_ROLE_MAX_CHARS = 60
AGENT_TASK_MAX_CHARS = 600
AGENT_EXPECTED_OUTPUT_MAX_CHARS = 300
AGENT_STEP_NAME_MAX_CHARS = 30
# 子の各手順のメモを次の手順へ引き継ぐときの上限（文字数。1手順あたり）。
AGENT_STEP_NOTE_MAX_CHARS = 3000
# 子へ伝える「現在日時」のタイムゾーン（「来週の天気」などを正しく扱わせるため）。
AGENT_TIMEZONE = "Asia/Tokyo"
# 進捗メッセージを編集する最短の間隔（秒）。Discord のレート制限に掛からないよう間引く。
AGENT_PROGRESS_MIN_INTERVAL_SECONDS = 5.0
# 進捗表示の定型文（案A。LLM を呼ばない）。ペルソナごとの文面は prompts/personas.json の
# "agent" に書き、書かなかった項目はここを使う。特定のキャラの口調はここに書かないこと。
#   {role}    : ルーターが決めた子のロール（例: 「地域のカフェのリサーチ担当」）
#   {step}    : いま進めている手順の名前
#   {current} : いま進めている手順の番号 / {total}: 手順の数
AGENT_LINE_KEYS = (
    "start", "progress", "done", "failed", "canceled", "busy",
    "name", "handoff", "child_start", "child_progress", "child_report",
    "archive_failed", "reopened",
)
AGENT_DEFAULT_LINES = {
    # 委任を始めたとき（進捗メッセージの1行目に残る。作業スレッドでは使わない）
    "start": "サブエージェント（{role}）に作業を任せました。少し待っていてください。",
    # 手順ごとの進捗（2行目を書き換える。作業スレッドでは使わない）
    "progress": "{step}を進めています…（{current}/{total}）",
    # 子の作業が終わったとき（このあと人格役が返信する）
    "done": "サブエージェントの作業が終わりました。",
    # 子の作業が失敗・時間切れになったとき
    "failed": "ごめんなさい、サブエージェントの作業がうまくいきませんでした。少し時間をおいて、もう一度お願いします。",
    # 依頼した本人が中止したとき
    "canceled": "サブエージェントの作業を中止しました。",
    # 同じ人の依頼が進行中、または同時に動かせる上限に達しているとき
    "busy": "いまは別のサブエージェントの作業を進めています。終わってから、もう一度お願いします。",
    # ── ここから下は作業スレッド（#267）だけで使う ──
    # 子の表示名（Webhook で投稿するときの名前）
    "name": "サブエージェント（{role}）",
    # 親（ボット）がスレッドで子へ作業を頼むときの1行目（このあとに依頼内容・期待する出力・手順が続く）
    "handoff": "{role}さん、次の作業をお願いします。",
    # 子が作業を引き受けたとき
    "child_start": "承知しました。{total}つの手順で進めます。",
    # 子が手順を始めるとき（書き換えずに1通ずつ残す）
    "child_progress": "手順{current}/{total}「{step}」を進めています…",
    # 子の報告の1行目（このあとに報告の本文と参考リンクが続く）
    "child_report": "作業が終わりました。報告します。",
    # 完了した作業スレッドを閉じられなかったとき（依頼したチャンネルへ出す。このあとに理由が続く。#408）
    "archive_failed": "作業は終わりましたが、作業スレッドを閉じられませんでした。スレッドはそのまま残しています。",
    # 閉じた作業スレッドに書き込みがあり、再開したとき（追加の依頼があるかを確かめる。#408）
    "reopened": (
        "作業スレッドが再開されました。追加のご依頼はありますか？"
        "続けて任せたい作業があれば、私にメンションして「サブエージェントを使って、〇〇して」のように書いてください。"
    ),
}
# ── サブエージェントの作業スレッド（#267） ──
# 委任するとき、依頼メッセージに作業用のスレッドを作り、親（ボット）が委託内容を伝え、
# 子が Webhook で名前とアイコンを変えて進捗と報告を投稿する（同じボット・同じトークンのまま）。
# スレッドを使えない場面（DM・スレッドの中・ボイスのチャット・権限不足など）では、
# 依頼への返信1通を書き換える進捗表示へ切り替える。False にすると常にその表示にする。
AGENT_THREAD_ENABLED = True
# 作業スレッドの名前。{role} に子のロールを差し込み、Discord の上限で切る。
AGENT_THREAD_NAME = "🤖 {role}"
AGENT_THREAD_NAME_MAX_CHARS = 100
# 作業スレッドのクローズ（#408）。自動アーカイブの時間は指定しない（チャンネルの既定に任せる）。
# 作業が完了し、人格役が元のチャンネルへ返信できたら、ボットがスレッドをアーカイブする（ロックはしない）。
# 失敗・中止・期限切れのスレッドは閉じずに残す。
# ボットが閉じた作業スレッドを覚えておく上限（インメモリ。書き込みで再開したときに追加の依頼を確かめるため。
# 超えたら古いものから忘れる）。
AGENT_WORK_THREADS_MAX = 500
# 作業スレッドを閉じられなかった理由。依頼したチャンネルへ出すので、例外の本文は載せずにこの定型文だけを使う。
AGENT_THREAD_ARCHIVE_FAILURE_REASONS = {
    "forbidden": "ボットにスレッドを閉じる権限がありません（「スレッドの管理」の権限を確認してください）。",
    "not_found": "作業スレッドが見つかりませんでした（削除された可能性があります）。",
    "error": "Discord との通信に失敗しました。",
}
# 子の投稿に使う Webhook の名前。チャンネルごとに1つ作り、ボット自身が作ったものを使い回す
# （依頼のたびに作ると、チャンネルあたりの Webhook の上限に達する）。
# Discord の制約で "discord" / "clyde" を含む名前は付けられない。
AGENT_WEBHOOK_NAME = "サブエージェント"
# 子の表示名の上限（Discord の Webhook 名の上限）と、整えた結果が空になったときの名前。
AGENT_CHILD_NAME_MAX_CHARS = 80
AGENT_CHILD_FALLBACK_NAME = "サブエージェント"
# personas.json の agent.avatar_url（子のアイコンの画像 URL）の長さの上限。
AGENT_AVATAR_URL_MAX_CHARS = 2048

# UsageTracker が保持するトークン履歴の上限（リングバッファ）
MAX_USAGE_HISTORY = 500

# matplotlib で日本語ラベルを描くためのフォント候補（先頭から見つかったものを使う）。
# 利用トークングラフと会話統計グラフで同じ見た目にするため、ここを唯一の情報源にする。
GRAPH_FONT_FAMILY = [
    "Yu Gothic", "Meiryo", "Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "sans-serif",
]

# ── UI（View / セレクトメニュー）共通 ──
# 各パネル（View）の操作待ち時間（秒）。これを過ぎると UI を取り除いて時間切れ表示にする。
VIEW_TIMEOUT_SECONDS = 120.0
# Discord のセレクトメニューに載せられる選択肢の最大数（プラットフォームの制限）。
DISCORD_SELECT_MAX_OPTIONS = 25
# Discord のセレクト選択肢の label / description の最大文字数（プラットフォームの制限）。
DISCORD_SELECT_LABEL_MAX_LENGTH = 100
# Discord のセレクト選択肢の value の最大文字数（プラットフォームの制限）。
# ペルソナの値（"../prompts/<ファイル名>"）がこれを超えるファイルは選択肢に出せない。
DISCORD_SELECT_VALUE_MAX_LENGTH = 100
# リマインダーパネルの本文をここで打ち切る。Discord の 2000 文字制限に対し、
# 打ち切り案内の追記分を見込んで余裕を持たせた長さ。
REMINDER_PANEL_MAX_LENGTH = 1900

# 自発的な会話 (/functions → 💬 自発会話) 用
# 機能を有効化した直後のデフォルト間隔（時間）。選択肢は choices.py の
# AUTO_TOPIC_INTERVAL_CHOICES（1/3/6/12/24時間）を唯一の情報源とする。
DEFAULT_AUTO_TOPIC_INTERVAL_HOURS = 6
# 無会話チェックを行うバックグラウンドループの周期（分）。
# 間隔そのものではなく「間隔を超えたかどうかの見回り」の頻度。
AUTO_TOPIC_CHECK_MINUTES = 10
# 話題生成の Gemini 呼び出しタイムアウト（秒）。
AUTO_TOPIC_TIMEOUT_SECONDS = 60.0
# 話題生成プロンプトに含めるチャンネル履歴の件数上限。
AUTO_TOPIC_HISTORY_LIMIT = 50

# --- 自律行動（ボイス/ステージへ自分から出て「配信」する） ---
# ボットが自分からボイス/ステージチャンネルへ入り、独り言を続けながら、
# そのチャンネル付属のチャットに届いたコメントへ配信者のように応答する機能。
# 有効/無効は /functions → 💬 自発会話 パネルの 🎙️ 自律行動 トグル（サーバーごと）。
#
# 「話したいゲージ」の満点までの時間は自発会話と同じ auto_topic_interval_hours を
# 共用する（設定項目を増やさず、パネル1枚で完結させるため）。

# 進行中の配信の面倒を見る見回りの間隔（分）。独り言の間隔・無応答での停止判定を
# この周期で確認するため、AUTONOMOUS_TALK_INTERVAL_MINUTES より細かくすること。
AUTONOMOUS_CHECK_MINUTES = 1.0
# 深夜の抑制と「話したいゲージ」の判定に使うタイムゾーン。
AUTONOMOUS_TIMEZONE = "Asia/Tokyo"
# 自律行動を禁止する時間帯（この時刻以上・未満。0〜6 なら 00:00〜05:59 が禁止）。
# ゲージは無会話時間で増えるため、誰も居ない深夜ほど早く満タンになってしまう。
# 「寝ている間にボイスチャンネルで喋り続ける」のを防ぐための歯止め。
AUTONOMOUS_QUIET_HOURS_START = 0
AUTONOMOUS_QUIET_HOURS_END = 6
# 「話したいゲージ」がこの値に達したら配信を始める（ゲージは 0〜100）。
AUTONOMOUS_GAUGE_THRESHOLD = 100.0
# ゲージの加点（直近の会話の活発さ）を数える時間窓（分）。
AUTONOMOUS_ACTIVITY_WINDOW_MINUTES = 180
# 時間窓内の人間の発言1件あたりの加点。人が話しているサーバーほど早く出たくなる。
AUTONOMOUS_ACTIVITY_BONUS_PER_MESSAGE = 2.0
# 加点の上限。基礎値（無会話時間）を加点だけで押し切らないための頭打ち。
AUTONOMOUS_ACTIVITY_BONUS_MAX = 30.0
# 加点のために覚えておく発言時刻の最大件数（サーバーごと。メモリの歯止め）。
AUTONOMOUS_ACTIVITY_SAMPLES_MAX = 100
# 独り言の間隔（分）。配信中はこの間隔でボットが自分から喋る。
AUTONOMOUS_TALK_INTERVAL_MINUTES = 3.0
# コメントを受け付けてから、次のコメントを受け付けるまでの待ち時間（秒）。
# 配信中のチャンネルではメンション不要でコメントを拾うため、何もしないと
# 「そのチャンネルの全メッセージ × (モデレーション + 応答生成)」で Gemini 呼び出しが
# 発散する（通常のメンション必須モードでは、メンションが自然な流量制限になっていた）。
# 入力AIの前に配信単位で枠を確保し、同じユーザーの連投にも適用する。
# 入力AIで拒否された場合も受付時刻は保持する。ローカルNG判定は枠の確保より前に行う。
# 0 以下にすると制限なし（全コメントに応答する）。
AUTONOMOUS_COMMENT_COOLDOWN_SECONDS = 10.0
# 本文が無く画像だけのコメントを受け取ったときに、本文の代わりとしてモデルへ渡す文。
# 通常のチャット経路が空の質問を「この画像について説明・反応してください。」へ
# 置き換えているのと同じ役割（置き換えないと、空のコメントへの返事を生成してしまう）。
AUTONOMOUS_IMAGE_ONLY_COMMENT = "（本文なしで画像が投稿されました。この画像に反応してください）"
# 配信の開始に失敗したあと、次に試すまでの基本待ち時間（分）。
# 失敗しても「話したいゲージ」は満タンのままなので、この歯止めが無いと
# 見回りのたび（＝毎分）に接続→失敗→切断を永久に繰り返す。
# 連続失敗のたびに「この値 × 連続回数」へ延びる（上限は下の倍率）。
AUTONOMOUS_START_RETRY_COOLDOWN_MINUTES = 30.0
# 上の待ち時間の倍率の上限。既定では 30分 × 8 = 最長4時間まで延びる。
# 権限設定を直したあと待たされすぎないよう、/functions のパネルで自律行動を
# 確定すると、この待ち時間はその場で解除される。
AUTONOMOUS_START_RETRY_MAX_MULTIPLIER = 8
# コメントがこの時間（分）届かなければ配信を自動終了する（idea.md の「30分」）。
AUTONOMOUS_IDLE_STOP_MINUTES = 30.0
# 1回の配信の最長時間（分）。コメントが続く限り無応答タイマーは伸び続けるため、
# 何時間もボイス接続と API 呼び出しを続けないための安全弁。
AUTONOMOUS_SESSION_MAX_MINUTES = 180.0
# 独り言・コメント応答のプロンプトに含めるチャンネル履歴の件数上限。
AUTONOMOUS_HISTORY_LIMIT = 30
# 独り言・コメント応答の Gemini 呼び出しタイムアウト（秒）。
AUTONOMOUS_TIMEOUT_SECONDS = 60.0
# ボイス接続の待ち時間の上限（秒）。接続できないチャンネルで見回りを止めないための歯止め。
AUTONOMOUS_CONNECT_TIMEOUT_SECONDS = 30.0
# ステージ参加後に「自分が発言者になれたか」を待つ回数と間隔（秒）。
# connect() 直後は自分のボイス状態がまだキャッシュに無く、
# Member.edit(suppress=False) が channel_id を載せられずに失敗することがある。
AUTONOMOUS_VOICE_STATE_RETRIES = 5
AUTONOMOUS_VOICE_STATE_WAIT_SECONDS = 0.5
# ボットが自分で開始したステージインスタンスのトピック（チャンネル一覧に表示される）。
# 「人間を誘う」ための看板なので、募集中であることが一目で分かる文言にする。
AUTONOMOUS_STAGE_TOPIC = "会話相手募集中…"
# 配信開始をチャンネルのチャットへ知らせる見出し。この下に生成した第一声を続ける。
AUTONOMOUS_OPENING_NOTICE = "🎙️ **会話相手募集中…**（ボットの自律行動）このチャットに書き込むと反応します。"
# 無応答で自動終了したときにチャットへ残す文言（Gemini は呼ばない）。
AUTONOMOUS_CLOSING_NOTICE = "🎙️ 反応が無くなったので配信を終了します。またね。"
# 最長時間に達して終了したときの文言。
AUTONOMOUS_CLOSING_NOTICE_TIMEUP = "🎙️ 長くなってきたので、いったん配信を終了します。またね。"
# 時間帯制限（深夜）に入って終了したときの文言。
AUTONOMOUS_CLOSING_NOTICE_QUIET = "🎙️ 夜も遅いので、今日はここまでにします。おやすみなさい。"
# パネルで自律行動を無効化されて終了したときの文言。
# 何も言わずに退出すると、聞いていた人からは落ちたのか止められたのか分からない。
AUTONOMOUS_CLOSING_NOTICE_DISABLED = "🎙️ 設定が変わったので、この配信はここまでにします。またね。"
# 別のチャンネルへ移動させられて終了したときの文言（元のチャンネルのチャットへ残す）。
AUTONOMOUS_CLOSING_NOTICE_MOVED = "🎙️ 別のチャンネルへ移動になったので、この配信はここで終わりにします。"
# 権限・発言者状態を失って終了するとき、チャットへ投稿できる場合だけ残す文言。
AUTONOMOUS_CLOSING_NOTICE_PERMISSIONS = "🎙️ このチャンネルで発言できなくなったので、配信を終了します。"

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

# ユーザー別会話統計 (/functions → 📈 会話統計) 用
# 集計データは DATABASE_PATH (bot.db) の user_stats テーブルに永続化する。
# ボットへの発言のみを集計する。
# 話題／性格推定のため各ユーザーごとに保持する直近発言サンプルの上限件数。
# プライバシー配慮のため上限を設け、超過した古い発言から捨てる。
STATS_RECENT_SAMPLES_MAX = 50
# 話題／性格推定の LLM 解析結果をキャッシュする時間（時間）。
# これを超えると次回表示時に再計算する（毎回の課金を避ける）。
STATS_ANALYSIS_TTL_HOURS = 24
# 抽出する話題（トピック）の最大件数。
STATS_TOPIC_COUNT = 8
# 性格推定に必要な最小サンプル数。これ未満は「データ不足」として推定しない
# （少数サンプルへの過剰一般化を防ぐ）。
STATS_PERSONALITY_MIN_SAMPLES = 10
# ユーザー選択セレクトに載せる最大人数（Discord のセレクトの上限がそのまま効く）。
STATS_MAX_USERS_IN_SELECT = DISCORD_SELECT_MAX_OPTIONS
# 統計解析（話題／性格推定）の LLM 呼び出しタイムアウト（秒）。
STATS_ANALYSIS_TIMEOUT_SECONDS = 30.0
# 時間帯ヒストグラムの集計に使うタイムゾーン（発言時刻はこの地域時刻に変換して集計）。
STATS_TIMEZONE = "Asia/Tokyo"
# 発言回数ランキング（管理者用の全体比較）に載せる人数の上限。
STATS_RANKING_TOP_N = 15
# 時間帯ヒストグラムの区切り数（0時〜23時の24区分）。
# 1時間単位の保存形式なので、区間数変更には記録処理と既存データの移行も必要。
STATS_HOURLY_BUCKETS = 24
# 新しく統計に載るユーザーの既定値。False = 収集する（従来どおり）。
# 「原則収集しない（オプトイン）」運用に切り替えたい場合はここを True にする。
STATS_OPT_OUT_DEFAULT = False

# --- リアクションによるフィードバック収集 ---
# 機能全体の有効/無効。False にするとリアクション付与も記録も行わない
# （返信のたびに API 呼び出しが2回増えるため、レート制限が厳しい環境では切れるようにする）。
FEEDBACK_ENABLED = True
# 良い/悪いを表す絵文字。ここ以外の絵文字が付いても無視する。
FEEDBACK_REACTION_GOOD = "👍"
FEEDBACK_REACTION_BAD = "👎"
# 評価対象として覚えておくボット返信の件数（インメモリのリングバッファ）。
# ここから溢れた古い返信へのリアクションは無視される（再起動でも消える）。
# 送信のたびに DB へ書くとチャットの主経路が重くなるため、あえて永続化しない。
FEEDBACK_TRACKED_MESSAGES_MAX = 500
# フィードバック記録の保持件数。古いものから捨てる（queue_tasks / reminders と同じ方針）。
FEEDBACK_MAX_STORED_ROWS = 2000
# 集計パネルに表示する内訳（モデル×ペルソナ）の最大行数。
FEEDBACK_SUMMARY_MAX_ROWS = 10
# 👍👎 の自動付与を間引くクールダウン（秒）。同じチャンネルで直近この秒数以内に
# 付与していれば今回は見送る（評価対象としての登録は全返信に対して行うため、
# 付与を見送った返信でもユーザーが自分で押せば記録される）。
# Discord のリアクション追加はチャンネル単位のレート制限が厳しく、しかも
# 「連投で詰まったときだけ」痛む。一律に N 回に1回へ間引くと痛くない場面の
# データまで等しく捨てるため、バーストのときだけ効くこの方式を採る。
# 0 以下にすると従来どおり全返信へ付与する。
FEEDBACK_REACTION_COOLDOWN_SECONDS = 60.0
# フィードバック記録の間引き（prune）を行う間隔（分）。
# 以前は評価が押されるたびに実行していたが、prune は全行走査のうえ
# database.py のプロセス唯一のロックを握るため、その間チャットの主経路
# （記憶・親密度・統計の読み書き）まで待たされていた。件数の上限は
# 「多少超過してもよい」性質なので、主経路から切り離して定期実行にする。
FEEDBACK_PRUNE_MINUTES = 60.0

# --- オンボーディング（サーバー参加時の案内・使い方パネル） ---
# サーバーに招待されたときにウェルカムメッセージを投稿するか。
# 静かに導入したい運用者（既に使い方を知っているメンバーだけのサーバーなど）向けに
# 切れるようにしておく。False にしてもヘルプ自体は失われず、
# /functions → ❓ 使い方 からいつでも読める。
WELCOME_MESSAGE_ENABLED = True

# ボイスチャンネルの読み上げ（/join・自律行動での接続中）用
# /join でボイスチャンネルへ接続するときの待ち時間の上限（秒）。discord.py の既定値と
# 同じ値だが、調整できるよう明示している（UDP が遮断された環境では、ここで打ち切られて
# タイムアウトの案内を返す）。自律行動の接続は AUTONOMOUS_CONNECT_TIMEOUT_SECONDS を使う。
VOICE_CONNECT_TIMEOUT_SECONDS = 30.0
# ボイス接続先の保護（voice_guard.py）。管理者などがボットを別のボイスチャンネルへ
# 移したとき、/join の接続は元のチャンネルへ戻し、自律行動の配信は畳んで退出する。
# Discord の権限では管理者による移動そのものを拒否できないため、移動を検知した後に戻す。
# False にすると従来どおり移動先に留まる（自律行動の配信は毎分の見回りで終了する）。
VOICE_MOVE_GUARD_ENABLED = True
# 移動を検知してから接続の状態を確かめるまでの間隔（秒）。移動直後は discord.py が
# 音声の接続を張り直していることがあり、すぐに移動し直すと処理がぶつかるため少し待つ。
VOICE_MOVE_GUARD_SETTLE_SECONDS = 1.0
# 張り直しの完了を確かめる回数の上限。この回数待っても接続が戻らなければ退出する。
VOICE_MOVE_GUARD_SETTLE_RETRIES = 10
# VOICE_MOVE_GUARD_WINDOW_SECONDS の間に、この回数戻しても移され続けたら戻るのをやめて
# 移動先から退出する（戻す→移されるの往復を無限に続けないための歯止め）。
VOICE_MOVE_GUARD_MAX_RETURNS = 3
VOICE_MOVE_GUARD_WINDOW_SECONDS = 300
# 退避先の gTTS に渡す言語コード（主エンジンの edge-tts は話者名で言語が決まる）。
TTS_LANG = "ja"
# 読み上げる最大文字数。超過分は切り捨てて末尾に TTS_TRUNCATION_NOTICE を付ける。
# 長文の返信をすべて読み上げると数分間ボイスチャンネルを占有してしまうため、
# 「声で聞くのに適した長さ」で区切る（テキスト側には全文が投稿済み）。
TTS_MAX_CHARS = 200
# 文字数上限で切り詰めたときに末尾へ添える案内。
TTS_TRUNCATION_NOTICE = "以下省略します。"
# 再生待ちキューに積める最大件数。再生中に届いた返信はここに積まれ、順番に読み上げる。
# 上限を超えた分は捨てる（古い返信をいつまでも喋り続けないための歯止め）。
TTS_QUEUE_MAX = 5
# Discord のボイス送信が要求する PCM フォーマット。discord.PCMAudio は
# 「48kHz / 16bit / ステレオ」の生 PCM しか受け付けないため、
# miniaudio でのデコード時にこの形式へ変換する（ffmpeg は使わない）。
TTS_SAMPLE_RATE = 48000
TTS_CHANNELS = 2
# 音声合成（音声エンジンのネットワーク呼び出し＋デコード＋EQ）のタイムアウト（秒）。
# 読み上げはチャット本体の付随機能なので、詰まったら諦めて次へ進む。
# gTTS への退避も対象。期限超過・キャンセル時も同期処理の回収を待つため、
# 複数のHTTP要求を含む退避処理では、この秒数を超えて終了を待つ場合がある。
TTS_TIMEOUT_SECONDS = 30.0
# 主エンジン単独の待機上限。全体期限までに gTTS へ退避する時間を残す。
TTS_EDGE_TIMEOUT_SECONDS = 10.0
# gTTS の各HTTP要求に渡す接続・読み取り待ちの上限（秒）。
# 外側の合成期限だけでは同期通信を中断できないため、通信自体にも期限を設ける。
TTS_GTTS_CONNECT_TIMEOUT_SECONDS = 5.0
TTS_GTTS_READ_TIMEOUT_SECONDS = 10.0
# 読み上げの再生完了待ちにかける上限。`voice_client.play()` の `after` コールバックは、
# ボイス接続が正常な `disconnect()` を経ずに切れると呼ばれないことがあり、無期限に
# 待つとそのサーバーの読み上げワーカーが永久に返らなくなる。
# 上限は「音声の実再生秒数 + この余裕秒数」とする。
TTS_PLAY_TIMEOUT_MARGIN_SECONDS = 15.0

# --- TTS の音声処理（デコード・EQ）用 ---
# バイクアッドフィルタを一度に処理するサンプル数。
# IIR フィルタは1つ前の「出力」を使う再帰計算のため numpy でベクトル化できず、
# Python のループで回すしかない。全サンプルを一気に list 化すると 30 秒の音声で
# 数十 MB の一時オブジェクトができてしまうため、メモリ 1GB の環境を想定して
# 固定長のブロックに区切る（フィルタの状態は次のブロックへ持ち越すので結果は同じ）。
TTS_BIQUAD_BLOCK_SIZE = 65536
# EQ プロファイルに frequency が無い / 数値でないときに使う既定の周波数（Hz）。
TTS_EQ_DEFAULT_FREQUENCY_HZ = 1000.0
# EQ の周波数の下限（Hz）。これより低いと係数が不安定になるため切り上げる。
TTS_EQ_MIN_FREQUENCY_HZ = 10.0
# EQ の周波数の上限を決める係数。上限は「サンプリング周波数 × この値」。
# 折り返し（エイリアシング）を避けるため、ナイキスト周波数（0.5倍）の手前に抑える。
TTS_EQ_NYQUIST_MARGIN = 0.45
# EQ プロファイルに gain_db が無い / 数値でないときに使う既定の増減量（dB）。0 は無変化。
TTS_EQ_DEFAULT_GAIN_DB = 0.0
# EQ プロファイルに q が無い / 数値でないときに使う既定の Q 値（クセのない標準的な鋭さ）。
TTS_EQ_DEFAULT_Q = 0.707
# Q 値の下限。0 に近づくと係数が発散するため、これ以下は切り上げる。
TTS_EQ_MIN_Q = 0.1
# ペルソナに eq が指定されていない / 未知の EQ 名だったときに使う既定の音色名
# （VOICE_EQ_PROFILES のキー。既定は加工なし）。
DEFAULT_VOICE_EQ_NAME = "flat"
# int16 で表せる範囲。EQ のブーストでこれを超えたまま変換すると音が割れるため、
# 必ずこの範囲へクリップしてから int16 に丸める。
PCM_INT16_MIN = -32768.0
PCM_INT16_MAX = 32767.0

# --- キャラクター別ボイスプロファイル ---
# 読み上げの声をペルソナごとに変える。声の高さ（pitch）と話す速さ（rate）は
# edge-tts のサーバー側パラメータが担当し、音色（eq）だけをこちらの DSP で作る。

# edge-tts が公開している日本語話者。無料エンドポイントではこの2人しか使えない
# （Azure 本家の Aoi / Daichi / Mayu などは含まれない）。
# 素の基本周波数の実測値: Nanami 240.6Hz / Keita 110.5Hz。
EDGE_TTS_VOICE_FEMALE = "ja-JP-NanamiNeural"
EDGE_TTS_VOICE_MALE = "ja-JP-KeitaNeural"

# pitch を振ったときに実際に何半音動くかの実測値（調整の目安。話者ごとに効き方が違う）。
#
#   Nanami : +20Hz -> +1.97半音   +40Hz -> +3.93半音   +60Hz -> +5.64半音
#            -20Hz -> -2.39半音   -40Hz -> -5.33半音
#            => おおよそ 10Hz ≒ 1半音。±40Hz までは半音に対してほぼ線形。
#
#   Keita  : +30Hz -> +2.41半音   +60Hz -> +5.19半音   +90Hz -> +6.35半音
#            -20Hz -> -0.49半音
#            => 上は +6半音付近で頭打ち。下方向はほとんど効かない。
#
# 「半音 → Hz」を計算で求めようとしないこと。Nanami は理論式の約1.5倍効き、
# Keita は約0.55倍しか効かず、しかも飽和するため換算式が成立しない。
# 値を変えるときは上の実測値を目安に Hz を直接書く。

# 音色（EQ）プロファイル。値は Audio EQ Cookbook のバイクアッド係数を組み立てる引数。
#   kind       : "high_shelf"（高域を上下させる）/ "low_pass"（高域を削る）/
#                "notch"（特定の帯域だけ深く削る）/ None（加工なし）
#   frequency  : 効き始める周波数（Hz）。出力（48kHz）基準でそのまま指定してよい。
#   gain_db    : シェルフの増減量（dB）。マイナスなら減衰として働く。
#   q          : 効き方の鋭さ。0.707 前後がクセのない標準的な値。
#   bit_crush  : 量子化ビット数（省略可）。小さいほどザラついた機械的な質感になる。
#
# 当初は warm にローパス 3.5kHz を使っていたが、実測で 8kHz 以上を
# 26.6dB も削っており、息づかいと子音が消えて電話・無線機の音質になっていた。
# 「そこから上を全部落とす」ローパスをやめ、「一定量だけ下げる」シェルフに変更。
# 同様に bright も +6dB では合成由来のノイズまで強調していたため半分に抑えた。
VOICE_EQ_PROFILES = {
    # 高域を軽く持ち上げて、若々しく張りのある明るい声にする。
    "bright": {"kind": "high_shelf", "frequency": 4000.0, "gain_db": 3.0, "q": 0.707},
    # 高域を軽く落として、落ち着きのある大人びた声にする。
    "warm": {"kind": "high_shelf", "frequency": 3000.0, "gain_db": -4.0, "q": 0.707},
    # 特定の帯域を深く削り、さらに量子化を粗くして無機質な響きにする。
    # ロボット系のキャラを作るときの受け皿（personas.json の eq に "robotic" と書く）。
    "robotic": {"kind": "notch", "frequency": 1200.0, "gain_db": 0.0, "q": 1.0, "bit_crush": 6},
    # 加工なし（話者の素の声）。
    "flat": {"kind": None},
}

# ペルソナごとの声は、利用者がローカルで作る prompts/personas.json の "voice" に書く
# （キャラ別のファイルを公開しないため、ここには特定のキャラの設定を置かない。personas.py）。
#   voice : 話者。EDGE_TTS_VOICE_FEMALE / _MALE のどちらか。
#   pitch : 声の高さ。edge-tts のネイティブ形式（"+10Hz" のような符号つき整数）。
#   rate  : 話す速さ。同じくネイティブ形式（"+5%"）。長さは 1/(1+rate/100) 倍になる。
#   eq    : 音色。VOICE_EQ_PROFILES のキー。
#   live_voice : /join の音声会話（Gemini Live）で使う事前定義ボイスの名前（例: "Kore"）。
#                空文字はモデルの既定の声。上の4項目は読み上げ（edge-tts）専用。
# 書かなかった項目・形式の誤った項目は、下の既定値を使う。
VOICE_PROFILE_FALLBACK = {
    "voice": EDGE_TTS_VOICE_FEMALE, "pitch": "+0Hz", "rate": "+0%", "eq": "flat",
    "live_voice": "",
}

# --- Gemini Live による音声会話（/join。#268） ---
# /join でボイスチャンネルへ入ったボットと、声で会話する。参加者の声を受信して
# Gemini Live へ流し、返ってきた音声をそのまま再生する（テキスト返信の読み上げは行わない）。
# 自律行動の配信は従来どおり TTS（上の TTS_* / edge-tts）で読み上げる。
# False にすると /join は接続するだけになる（声の会話もテキスト返信の読み上げも行わない）。
LIVE_ENABLED = True
# 使うモデル。Google AI Studio の表示名は「Gemini 3.8 Live」（Issue #268 の実験で接続を確認）。
LIVE_MODEL_NAME = "gemini-3.8-live"
# 同時に Live 会話を行える VC の上限（試験運用は最大2VC。#268 の決定事項）。
# TPM などの割当は VC ごとではなく Google Cloud のプロジェクト全体で共有される。
LIVE_MAX_CONCURRENT_SESSIONS = 2
# Live へ送る音声（16kHz・モノラル・16bit）と、Live が返す音声（24kHz・モノラル・16bit）。
LIVE_INPUT_SAMPLE_RATE = 16000
LIVE_OUTPUT_SAMPLE_RATE = 24000
LIVE_INPUT_MIME_TYPE = "audio/pcm;rate=16000"
# 48kHz → 16kHz の間引きの前に掛けるローパスフィルタ（タップ数と遮断周波数）。
# 16kHz の上限（8kHz）の手前で切り、折り返し雑音で聞き取りの精度を落とさない。
LIVE_DOWNSAMPLE_TAPS = 47
LIVE_DOWNSAMPLE_CUTOFF_HZ = 7200.0
# Live へ音声を送るときのまとまり（ミリ秒）。小さいほど遅延が減り、送信回数が増える。
LIVE_INPUT_CHUNK_MS = 60
# 送信待ちの音声の上限（まとまりの数）。Live が詰まったときに古い音声から捨てる。
LIVE_INPUT_QUEUE_MAX_CHUNKS = 50
# ローカルの発話検出（VAD）。Discord のクライアントは話している間だけ音声を送るため、
# 届いたフレームの音量（実効値）がしきい値以上なら「話している」とみなす。
LIVE_VAD_ENERGY_THRESHOLD = 400.0
# 発話とみなすまでに続く必要がある有声の長さ（ミリ秒）。咳払いや雑音で話者を取らない。
LIVE_VAD_START_MS = 200
# 有声のフレームがこの間途切れたら、続きの発話とはみなさない（START_MS の数え直し）。
LIVE_VAD_START_GAP_MS = 200
# 話し終わりとみなす無音の長さ（ミリ秒）。ここで Live へ発話の終わりを伝える。
LIVE_VAD_SILENCE_MS = 800
# 発話の開始と判定する前の音声も送る長さ（ミリ秒）。語頭が欠けないようにする。
LIVE_VAD_PREROLL_MS = 400
# ボットが話している最中の割り込みとみなす有声の長さ（ミリ秒）。相槌程度では止めない。
LIVE_BARGE_IN_MS = 500
# 1回の発話の上限（秒）。話し続けても、ここで区切って Live に応答させる。
LIVE_MAX_UTTERANCE_SECONDS = 30.0
# 話者を待っている人（発話希望）を覚えておく人数と時間（秒）。音声そのものは溜めない。
LIVE_SPEAKER_QUEUE_MAX = 3
LIVE_SPEAKER_REQUEST_TTL_SECONDS = 3.0
# 話し終わりの判定・接続の確認などを行う見回りの間隔（秒）。
LIVE_TICK_SECONDS = 0.1
# 応答音声を再生し始めるまでに溜める長さ（ミリ秒）。出力の文字起こしの NG ワード判定に
# 少しだけ先行させるための短いバッファ（判定の前に一部が再生されうることは仕様上許容する）。
LIVE_OUTPUT_BUFFER_MS = 400
# 応答の途中で音声が届かなくなったとき、再生を打ち切るまでの時間（秒）。
LIVE_PLAYBACK_STALL_SECONDS = 5.0
# 話し終わってから、入力の文字起こしが出そろうのを待つ時間（秒）。
# 文字起こしがこの時間増えなければ、確定とみなして入力の AI モデレーションへ回す。
LIVE_INPUT_TRANSCRIPT_SETTLE_SECONDS = 0.8
# 文字起こしを待つ上限（秒）。届かなければ、あるところまでで判定する。
LIVE_INPUT_TRANSCRIPT_MAX_WAIT_SECONDS = 4.0
# 1ターン分の文字起こしとして保持する最大文字数（記憶・判定に渡す長さの上限）。
LIVE_TRANSCRIPT_MAX_CHARS = 2000
# Live への接続の待ち時間の上限（秒）。
LIVE_CONNECT_TIMEOUT_SECONDS = 15.0
# Live の接続が切れたときに、続けて再接続を試す回数の上限と、各回の待ち時間（秒）。
# 利用枠の超過（429 など）では再接続しない（#268 の決定事項）。
LIVE_RECONNECT_MAX_ATTEMPTS = 3
LIVE_RECONNECT_BACKOFF_SECONDS = (1.0, 3.0, 10.0)
# 文脈の圧縮（Context Window Compression）。文脈がトリガーのトークン数を超えたら、
# 古い部分を目標のトークン数まで削る。無料枠の TPM（65,000）を1ターンで使い切らないよう、
# 既定（文脈上限の80%）よりかなり小さくしている。値は実機での計測で調整する。
LIVE_COMPRESSION_TRIGGER_TOKENS = 16000
LIVE_COMPRESSION_TARGET_TOKENS = 8000
# 出力判定に相乗りさせる感情の変化量の上限（機嫌・テンションそれぞれ ±この値）。
LIVE_EMOTION_DELTA_MAX = 20
# 同意パネルの表示を更新する最短の間隔（秒）。参加者の出入りが続いても編集を連打しない。
LIVE_CONSENT_PANEL_UPDATE_SECONDS = 2.0

