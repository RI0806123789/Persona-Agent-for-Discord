"""Gemini Live による音声会話（#268）と、エコー除去・割り込み（#409）の調整値。

`persona_bot.config.constants` が `import *` で再公開する。利用側は従来どおり
`from persona_bot.config.constants import 名前` と書くこと（このモジュールを直接 import しない）。
テストの `patch.multiple("persona_bot.config.constants", ...)` がその入口を差し替えるため。

★ このモジュールは何も import しない（constants.py や他の領域モジュールを import すると、
再公開との間で循環参照になる）。他の領域の値から決まる定数は constants.py に置く。
★ 名前は constants.py・他の領域モジュールと重複させないこと（後から読み込まれた方が黙って勝つ）。
"""

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
# 返答の生成を待っている間（まだ声が出ていない間）に話し始めた人を、割り込みとみなす有声の長さ（ミリ秒）。
# 声が出ている最中の割り込みは、下の LIVE_BARGE_IN_HOLD_SECONDS と文字起こしで判定する（#409）。
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
# フェードアウト中の音源が鳴り終わるのを待つ上限（秒）。LIVE_BARGE_IN_FADE_MS の絞り切りに
# 再生スレッドの終了が追いつくのを見て、それでも終わらなければ諦めて切り替える。
LIVE_PLAYBACK_FADE_WAIT_SECONDS = 0.5
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

# --- 音声会話のエコー除去（AEC）と、ペルソナの発話中の割り込み（#409） ---
# ペルソナの声が参加者のスピーカーからマイクへ回り込むと、自分の声を割り込みと取り違えたり、
# 自分の声に返答し続ける音声ループが起きる。再生した音声を参照信号にして受信した音声から取り除く
# （audio/echo_canceller.py。依存は numpy だけ）。★ 以下の値は合成信号での確認で決めた初期値で、
# 実際の Discord（スピーカー環境）で実験を繰り返して調整する（#409 の決定事項）。
# False にするとエコー除去を行わない（受信した音声をそのまま使う。割り込みの判定は下の規則のまま）。
LIVE_AEC_ENABLED = True
# 参照信号（再生した音声）を保持する長さ（秒）。遅延の上限＋推定の窓より長くする。メモリ上だけに持つ。
LIVE_AEC_REFERENCE_SECONDS = 3.0
# 再生スレッドの読み出しがこれ以上遅れたら、参照信号の時間軸を張り直す（ミリ秒）。
LIVE_AEC_REFERENCE_GAP_MS = 60
# 回り込みの遅延を探す範囲の上限（ミリ秒）。ボット→Discord→参加者のスピーカー→マイク→Discord→ボットの往復。
LIVE_AEC_MAX_DELAY_MS = 1000
# 遅延の推定に使う受信音声の長さ（ミリ秒）と、推定を始めるのに要る最短の長さ、推定し直す間隔。
LIVE_AEC_DELAY_WINDOW_MS = 600
LIVE_AEC_DELAY_MIN_WINDOW_MS = 300
LIVE_AEC_DELAY_UPDATE_MS = 300
# 相互相関のピークが平均のこの倍以上なら、推定した遅延を採用する（低いと雑音・発話を回り込みと誤る）。
LIVE_AEC_DELAY_MIN_PEAK_RATIO = 12.0
# その遅延で受信音声と参照信号の波形が似ている度合い（相関係数）の下限。偽のピークを採用しない。
# 合成信号では、反響のある回り込みの正しい遅延で 0.08 以上、偽のピークで 0.03〜0.06 程度だった。
# さらに、2回続けて同じ遅延が出るまで採用しない（LIVE_AEC_DELAY_TOLERANCE_MS の範囲で一致）。
LIVE_AEC_DELAY_MIN_CORRELATION = 0.06
# この差（ミリ秒）までは同じ遅延とみなす。別の遅延の推定が2回続いたら切り替え、フィルタを学び直す。
LIVE_AEC_DELAY_TOLERANCE_MS = 4
# 受信が途切れたとみなす間隔（ミリ秒）。話し始めごとに受信音声の時間軸を張り直す。
LIVE_AEC_SPURT_GAP_MS = 200
# 適応フィルタが学習する反響の長さ（ミリ秒）と、推定した遅延より手前から見る余裕（ミリ秒）。
LIVE_AEC_FILTER_MS = 80
LIVE_AEC_FILTER_PRE_MS = 10
# 適応フィルタの学習の速さ（NLMS のステップサイズ。大きいと速く追従するが不安定になる）。
LIVE_AEC_STEP_SIZE = 0.3
# 参照信号が鳴っているとみなす音量（16bit の実効値）。これ未満の区間では回り込みを疑わない。
LIVE_AEC_REFERENCE_FLOOR_RMS = 50.0
# 受信音声と参照信号のコヒーレンス（0〜1）がこれ以上なら、回り込みが主とみなす（この間だけ学習する。
# 学習が進む前は、発話として数えない）。
LIVE_AEC_ECHO_COHERENCE = 0.5
# 除去量（ERLE。デシベル）がこれ以上になったら、学習が進んだとみなして下の残差の規則で発話を判定する。
LIVE_AEC_CONVERGED_ERLE_DB = 6.0
# 除去後の残差が、除去量から見込まれる残りのエコー（推定したエコー ÷ ERLE）のこの倍（振幅）未満なら、
# 残響・歪みで残ったエコーとみなす（発話として数えない）。大きいほど誤検出は減るが、小声の割り込みを逃す。
# 合成信号（遅延 120〜600ms・反響・歪み・雑音の6通り）では、2.0 でエコーを発話と誤るフレームが平均 1.9%
# （歪みが強いとき最大 9.6%）、重ねて話した声の検出が平均 82%。3.0 では 0.2% / 63%。割り込みは
# LIVE_BARGE_IN_HOLD_SECONDS の継続と文字起こしでも確かめるため、取りこぼしの少ない側にしている。
LIVE_AEC_RESIDUAL_MARGIN = 2.0
# ペルソナの声が出ている間の割り込み。話し始めを検知しても Live へは送らず手元に留め、
# この長さ（秒）話し続けたら Live へ送る（短い相槌・言いよどみは送らない＝返答を止めない）。
LIVE_BARGE_IN_HOLD_SECONDS = 2.0
# 留めている間に、この長さ（ミリ秒）声が途切れたら、相槌とみなして捨てる。
LIVE_BARGE_IN_GAP_MS = 500
# 送った後、入力の文字起こしがフィラー・相槌を除いてこの語数以上になったら、割り込みと確定する
# （日本語は区切りが無いため、かな・漢字は LIVE_BARGE_IN_CJK_CHARS_PER_WORD 文字を1語と数える）。
LIVE_BARGE_IN_MIN_WORDS = 2
LIVE_BARGE_IN_CJK_CHARS_PER_WORD = 2
# 割り込みとして数えない言いよどみ・相槌（ひらがな・小文字で書く。伸ばし棒の重なりは1つにまとめて照合する）。
# 「ー」「っ」を含むものは、語の頭に付いていても取り除く（「えーと明日」→「明日」）。
LIVE_BARGE_IN_FILLERS = (
    "あ", "あー", "あっ", "あの", "あのー", "あのね", "え", "えー", "えっ", "えと", "えーと", "えっと", "えーっと",
    "う", "うー", "うーん", "うん", "ううん", "ん", "んー", "んっ", "ふーん", "へー", "ほー", "おー", "おお", "わー",
    "まあ", "まー", "そのー", "なんか", "なんだろ", "ええ", "はい", "そう", "そうそう", "そうだね", "そうなんだ",
    "なるほど", "たしかに", "ね", "ねー", "ほう",
    "uh", "um", "uhm", "er", "erm", "ah", "oh", "hmm", "mm", "mhm", "yeah", "yes", "ok", "okay", "right",
)
# 割り込みを確定したときに、ペルソナの声を絞り切るまでの長さ（ミリ秒）と、絞り方の強さ（指数。大きいほど早く絞る）。
# 会話の終了・違反の検出・一時停止では、フェードアウトせずに即座に止める。
LIVE_BARGE_IN_FADE_MS = 120
LIVE_BARGE_IN_FADE_EXPONENT = 2.0
# 入力の文字起こしが、ボット自身の直近の発話とこの割合以上（文字の2つ組の一致率）重なったら、
# 自分の声の回り込みとみなす（割り込みにしない・その発言への返答を再生しない）。短い発言は判定しない。
LIVE_ECHO_TEXT_OVERLAP = 0.6
LIVE_ECHO_TEXT_MIN_CHARS = 6
# 回り込みの照合に使う、ボット自身の発話（出力の文字起こし）を覚えておく時間（秒）。
LIVE_ECHO_TEXT_WINDOW_SECONDS = 20.0
# ペルソナの声が止まってから、この時間（秒）以内に話し始めた発言も、回り込みの可能性を疑う。
LIVE_ECHO_GUARD_SECONDS = 1.5
