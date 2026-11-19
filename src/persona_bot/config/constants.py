from pathlib import Path

# 機能別の調整値は領域ごとのモジュールに分け、ここから `import *` で再公開する（#480）。
# 利用側は従来どおり `from persona_bot.config.constants import 名前` と書く。
# ★ 管理コンソール（tools/console/backend/remote_scripts/insights.py）は、このファイルの
#   `名前 = 定数` の直接の代入だけを AST で読む（PERSONA_VALUE_PREFIX・PROMPT_TEMPLATE_FILE_NAME・
#   DISCORD_SELECT_VALUE_MAX_LENGTH・DEFAULT_MODEL_NAME・DEFAULT_RESPOND_MODE・
#   DEFAULT_AUTO_TOPIC_INTERVAL_HOURS）。それらは再公開にせず、必ずここへ直接書くこと。
from persona_bot.config.constants_agent import *  # noqa: F401,F403
from persona_bot.config.constants_autonomous import *  # noqa: F401,F403
from persona_bot.config.constants_live import *  # noqa: F401,F403
from persona_bot.config.constants_memory import *  # noqa: F401,F403
from persona_bot.config.constants_reminder import *  # noqa: F401,F403
from persona_bot.config.constants_stats import *  # noqa: F401,F403
from persona_bot.config.constants_voice import *  # noqa: F401,F403

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
# サーバー設定・記念日の投稿記録・リマインダー・フィードバック・旧データの取り込み記録の10テーブルを格納する。
# profile.json は起動時の手書き追加用、projects.json は自由形式JSONとして保持する。
DATABASE_PATH = MEMORY_DIR / "bot.db"
PROFILE_MEMORY_PATH = MEMORY_DIR / "profile.json"
PROJECTS_MEMORY_PATH = MEMORY_DIR / "projects.json"

NG_DIR = DATA_DIR / "ng"
NG_WORD_PATH = NG_DIR / "NG_WORD.txt"
NG_WORD_PRIVATE_PATH = NG_DIR / "NG_WORD_private.txt"
BLACKLIST_PATH = NG_DIR / "blacklist.json"
BLACKLIST_MAX_VIOLATIONS = 3

HISTORY_LIMIT = 100
GEMINI_RESPONSE_TIMEOUT_SECONDS = 60.0
MODERATION_MODEL_NAME = "gemini-3.1-flash-lite"
MODERATION_TIMEOUT_SECONDS = 10.0
# モデレーションプロンプトに「参考」として載せる NG ワードの最大件数。
# 全件を載せるとプロンプトが肥大化してトークンを浪費するため先頭からこの件数だけ渡す。
# UI 側の DISCORD_SELECT_LABEL_MAX_LENGTH と数値は同じだが意味がまったく違うので別名にする。
MODERATION_NG_WORD_SAMPLE_LIMIT = 100
# ボットの応答が出力モデレーションで不適切と判定されたとき、本文の代わりに返す定型の謝罪文。
# 親密度・記憶・評価の対象にしないため、呼び出し側はこの文面そのものではなく判定の成否で分岐する。
BOT_RESPONSE_REFUSAL_TEXT = "申し訳ありませんが、適切な応答を生成できませんでした。"

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

# UsageTracker が保持するトークン履歴の上限（リングバッファ）。
# 応答の生成だけでなく、モデレーション・記憶・統計の内部呼び出しも1回ずつ数える（#480）ため、
# 通常の会話1件で2〜3件（入力判定・応答・応答判定）を使う。グラフが遡れる会話数はその分だけ短い。
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
# 会話統計のユーザー選択セレクトに載せる最大人数（Discord のセレクトの上限がそのまま効く）。
# 領域モジュール（constants_stats）は constants.py を import しないため、値の元になる
# DISCORD_SELECT_MAX_OPTIONS と同じここに置く。
STATS_MAX_USERS_IN_SELECT = DISCORD_SELECT_MAX_OPTIONS
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

# --- オンボーディング（サーバー参加時の案内・使い方パネル） ---
# サーバーに招待されたときにウェルカムメッセージを投稿するか。
# 静かに導入したい運用者（既に使い方を知っているメンバーだけのサーバーなど）向けに
# 切れるようにしておく。False にしてもヘルプ自体は失われず、
# /functions → ❓ 使い方 からいつでも読める。
WELCOME_MESSAGE_ENABLED = True
