import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from persona_bot.config.constants import BASE_DIR, BUILTIN_PERSONA_PROMPT


def resolve_prompt_path(relative_path: str) -> Path:
    return (BASE_DIR / relative_path).resolve()


# ペルソナのファイルを読めなかったときの例外。見つからない（FileNotFoundError）だけでなく、権限がない・
# UTF-8 でない（メモ帳の ANSI＝Shift_JIS 保存など。UnicodeDecodeError）場合も同じ扱いにする（#496）。
PERSONA_READ_ERRORS = (OSError, UnicodeDecodeError)


def load_prompt_template(file_path: str | Path) -> str:
    # メモ帳などで「UTF-8 (BOM 付き)」保存されたペルソナも、先頭に U+FEFF を残さず読む
    return Path(file_path).read_text(encoding="utf-8-sig")



async def load_persona_template(prompt_file: str) -> str:
    """src基準のペルソナを別スレッドで読み、読込例外は呼出元へ返す。

    空の値はペルソナを1つも作っていない状態（personas.py）なので、ファイルを読まずに
    組み込みの標準プロンプトを返す。
    """
    if not prompt_file:
        return BUILTIN_PERSONA_PROMPT
    from persona_bot.core.background import run_in_thread

    return await run_in_thread(_load_persona_template, prompt_file)


def _load_persona_template(prompt_file: str) -> str:
    return load_prompt_template(resolve_prompt_path(prompt_file))


async def load_persona_or_default(
    prompt_file: str,
    *,
    logger: logging.Logger,
    message: str,
    loader: Callable[[str], Awaitable[str]] = load_persona_template,
    default: str | None = None,
) -> str | None:
    """ペルソナを読み、ファイルを読めないときは警告ログを出して `default` を返す（#434）。

    「読めない」は、見つからない・権限がない・UTF-8 で保存されていない（`PERSONA_READ_ERRORS`）を指す（#496）。

    自発会話・自律行動・会話要約・文書など、ペルソナが読めなければ処理を見送る（None）か、口調なしで続ける
    （空文字）か、標準のプロンプトで続けるサービスの定型を共通化する。警告は呼び出し側のロガーに出し、
    運用ログの出元を変えない。

    ペルソナのファイルが見つからないときの扱い（#480。呼び出し元ごとに意図して違う）:

    - 自発会話・自律行動（`default=None`）: 生成を見送る。次の見回りで再試行できる。
    - 会話要約（`default=""`）: 口調なしの要約で続ける。
    - 文書（`default=BUILTIN_PERSONA_PROMPT`）: 受付済みの依頼なので、標準のプロンプトで回答まで進める。
    - 通常会話（`message_response`）・音声会話の開始: このヘルパーを使わず、利用者へ設定の確認を案内して
      止める（黙って別のキャラクターで答えない）。`load_persona_template` の例外を `PERSONA_READ_ERRORS` で受ける
      （通常会話。音声会話の開始は例外をそのまま呼び出し元の失敗の案内へ渡す）。

    Args:
        logger: 警告の出力先（呼び出し元モジュールのロガー）。
        message: 警告の文面。ファイル名・パスを含めないこと。
        loader: 読込関数。呼び出し元モジュールで差し替えられる名前（テストの差し替え口）を渡す。
        default: ファイルが無いときの戻り値。
    """
    try:
        return await loader(prompt_file)
    except PERSONA_READ_ERRORS:
        logger.warning(message, exc_info=True)
        return default
