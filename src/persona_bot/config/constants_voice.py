"""ボイスチャンネルの読み上げ（TTS）・接続先の保護・キャラクター別ボイスプロファイルの調整値。

`persona_bot.config.constants` が `import *` で再公開する。利用側は従来どおり
`from persona_bot.config.constants import 名前` と書くこと（このモジュールを直接 import しない）。
テストの `patch.multiple("persona_bot.config.constants", ...)` がその入口を差し替えるため。

★ このモジュールは何も import しない（constants.py や他の領域モジュールを import すると、
再公開との間で循環参照になる）。他の領域の値から決まる定数は constants.py に置く。
★ 名前は constants.py・他の領域モジュールと重複させないこと（後から読み込まれた方が黙って勝つ）。
"""

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
