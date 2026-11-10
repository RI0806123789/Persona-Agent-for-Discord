"""オンボーディング導線（使い方パネルと、サーバー参加時のウェルカム本文）。

このボットの機能は「メンションするだけで会話できる」ことと `/functions` の
パネル群にほぼ集約されているが、どちらも招待されただけでは誰も気づけない。
そこで最初の一歩を案内する入口を2つ用意し、その本文をここにまとめる。

  ・`/functions` → ❓ 使い方（`HelpView`）… 全機能の一覧と注意事項をその場で表示
  ・サーバー参加時のウェルカム投稿（`build_welcome_text`）… `bot.py` の
    `on_guild_join` が投稿する。投稿先の決定と送信は
    `discord_helpers.post_welcome_message` が担う。

**同じ説明を2か所に書かないこと。** ヘルプとウェルカムは説明する内容が大きく
重なるため、共通する説明（メンションでの会話・添付・リマインダー・音声会話・
サブエージェント・評価・記憶・統計）はこのモジュール内の1つの定数／関数から組み立て、両方が
それを使う。片方だけ直すと「ウェルカムには書いてあるのにヘルプには無い」
という食い違いが生まれ、案内としての信頼を失う。

本文が実装と食い違わないことが最優先。機能を足したり既定値を変えたりしたら、
必ずここの文言も合わせて直すこと。
"""

import logging

from typing import Any

from persona_bot.config.constants import BLACKLIST_MAX_VIOLATIONS, DISCORD_MESSAGE_MAX_LENGTH, VIEW_TIMEOUT_SECONDS
from persona_bot.views.view_parts import BackButton, HubFactory, PanelView


logger = logging.getLogger(__name__)

# client.user がまだ取れないとき（起動直後など）に使う表示名。
DEFAULT_BOT_NAME = "ボット"
# 本文が Discord の1メッセージ上限を超えてしまったときに末尾へ付ける案内。
_TRUNCATION_NOTICE = "\n…（以下省略）"

# --- ヘルプとウェルカムで共通の説明文（唯一の情報源） ---
_ATTACHMENT_LINE = (
    "画像を添付すると内容に反応し、pdf / txt / csv / md を添付すると要約して答えます。"
)
_REMINDER_LINE = (
    "「30分後にゴミ出しって知らせて」のように頼むとリマインダーを登録し、"
    "その時刻に頼んだチャンネルで通知します（「毎朝7時に」なら毎日くり返します）。"
)
_SUBAGENT_LINE = (
    "「サブエージェントを使って、〇〇を調べて」のように頼むと、調べ物を別の AI（サブエージェント）に"
    "任せ、検索した結果をキャラクターの言葉で伝えます（調べ物が要らない内容なら、そのまま答えます）。"
    "頼んだメッセージに作業スレッドができ、任せた内容とサブエージェントの進捗・報告が残ります"
    "（スレッドを作れない場所では返信で進捗を伝えます）。「中止」で止められます。"
    "作業が終わるとスレッドは閉じます（書き込めば再開し、追加の依頼を尋ねます）。"
)
_VOICE_LINE = (
    "`/join` で自分がいるボイスチャンネルに呼ぶと、ボットと声で会話できます"
    "（Gemini Live。`/leave` で退出）。声は Google へ送られるため、ボイスチャンネルの全員が"
    "ボットの同意パネルで「同意して参加」を押すまで一時停止します"
    "（記憶・親密度への反映は別に選べ、初期はオフ）。テキストの返信は読み上げません。"
    "ドラッグで別のチャンネルへ移しても元のチャンネルへ戻るので、"
    "移すときは移したい先で `/join` してください。"
    "接続中の通話のチャットは、メンションの無い発言も NG ワードの確認対象です。"
)
_AUTONOMOUS_LINE = (
    "🎙️ 自律行動を有効にすると、会話が途切れた頃にボットが自分から"
    "ボイス／ステージチャンネルへ入り、独り言を始めます。"
    "そのチャンネルのチャットに書き込むと、配信者のように応答します"
    "（既定は OFF。深夜は動かず、無反応・権限不足で自動退出。`/leave` でいつでも停止）。"
    "出ていく先は、サーバー管理者が `/functions` → ⚙️ キャラ設定 → 🎙️ 配信先 で指定できます"
    "（指定先を使えないときは自動で選び、手動で別のチャンネルへ移されたら配信を終えます）。"
)
_FEEDBACK_LINE = (
    "返信に付く 👍 / 👎 を押すと応答の良し悪しを記録します"
    "（誰が押したかは公開されません）。"
    "会話が続いているときは一部の返信にしか付きませんが、"
    "付いていない返信でも自分で 👍 / 👎 を押せば同じように記録されます。"
)
_MEMORY_LINE = (
    "会話の内容から長期記憶を作り、次回以降の会話に使います。"
    "記憶はユーザーごとに分かれていて、他の人の会話には出てきません。"
)
_STATS_LINE = (
    "ボットへの発言は会話統計（回数・時間帯・話題・性格推定）として集計されます。"
    "`/functions` → 🔒 統計プライバシー で収集の停止と蓄積データの削除ができます。"
)


def resolve_bot_name(bot_user: Any) -> str:
    """メンション例や自己紹介に使うボット名を `client.user` から取り出す。

    ボット名をハードコードすると、同じコードを別名のボットで動かしたときに
    案内だけが嘘になる（`@違う名前` と書かれていても誰も反応させられない）。
    そのため実行中のクライアントから毎回取る。

    `client.user` は接続前だと None になりうるため、その場合は既定名へ倒す。
    名前から `@` を落とすのは、`@everyone` / `@here` という名前のボットが
    作られた場合に本文がそのまま通知爆撃の文面になるのを防ぐため
    （送信側でも AllowedMentions.none() を付けるが、二重に防いでおく）。
    """
    name = getattr(bot_user, "display_name", None) or getattr(bot_user, "name", None)
    if not name:
        return DEFAULT_BOT_NAME
    cleaned = str(name).replace("@", "").strip()
    return cleaned or DEFAULT_BOT_NAME


def _fit_to_discord_limit(text: str) -> str:
    """本文を Discord の1メッセージ上限に収める（超過分は切り詰める）。

    ヘルプはエフェメラルメッセージの差し替え（`edit_message`）で表示するため、
    上限を超えると差し替え自体が失敗してパネルが固まる。`split_message` は
    使えない（View を持てるメッセージは1通だけ）ので、ここで切り詰める。

    通常は上限に収まる文量にしてあり、この切り詰めは「後から項目を足しすぎた」
    ときの安全弁。発動したらログに出して気づけるようにしておく。
    """
    if len(text) <= DISCORD_MESSAGE_MAX_LENGTH:
        return text
    logger.warning("案内文が Discord の上限（%d文字）を超えたため切り詰めました（%d文字）。説明文を短くしてください。", DISCORD_MESSAGE_MAX_LENGTH, len(text))
    keep = DISCORD_MESSAGE_MAX_LENGTH - len(_TRUNCATION_NOTICE)
    return text[:keep] + _TRUNCATION_NOTICE


def _chat_usage_line(bot_name: str) -> str:
    """いちばん基本の使い方（メンションで会話する）の説明。

    応答モードの既定は "mention"（`constants.DEFAULT_RESPOND_MODE`）で、
    メンションの無い発言には反応しない。ここを知らないと「反応しない壊れた
    ボット」に見えるため、ヘルプでもウェルカムでも先頭に置く。
    """
    return (
        f"`@{bot_name}` とメンションするだけで会話できます"
        "（既定は「メンションのみに反応」。DM ではメンション不要です）。"
    )


def build_help_text(bot_name: str) -> str:
    """`/functions` → ❓ 使い方 に表示する本文を組み立てる。

    Args:
        bot_name: メンション例に埋め込むボット名（`resolve_bot_name` の戻り値）。
    """
    lines = [
        f"❓ **使い方** — 「{bot_name}」でできること",
        "",
        "**1. 会話する（いちばん基本）**",
        f"・{_chat_usage_line(bot_name)}",
        f"・{_ATTACHMENT_LINE}",
        f"・{_REMINDER_LINE}",
        f"・{_SUBAGENT_LINE}",
        f"・{_FEEDBACK_LINE}",
        "",
        "**2. `/functions` … 機能パネル（本人だけに見えます）**",
        "・⚙️ キャラ設定 … ペルソナ・AIモデル・応答モード（全発言／メンションのみ）の変更と会話リセット"
        "（設定の確定はサーバー管理者のみ。DMでは本人が操作できます。管理者は 🎙️ 配信先も）",
        "・📋 会話要約 … このチャンネルの会話を、期間を選んで議事録にまとめる",
        "・📈 会話統計 … 自分の発言回数・時間帯・話題・性格推定をグラフで見る",
        "・💬 自発会話 … ボットから話しかける機能の ON/OFF（テキストへの話題提供と 🎙️ 自律行動。どちらも既定は OFF。"
        "サーバーでの確定は管理者のみ）",
        "・⏰ リマインダー … 自分の予定の一覧・取り消し",
        "・📊 グラフ / 🔄 NGワード再読込 … その場で実行",
        "・🔍 Google検索 … 検索で裏取りしてから答えるかの切り替え（サーバーでは管理者のみ）",
        "・🔒 統計プライバシー … 会話統計の収集停止と蓄積データの削除",
        "・🚫 ブロック解除 … ボット所有者にのみ表示されます（全サーバー共通のブロック）",
        "・❓ 使い方 … この画面（↩️ 戻る で機能パネルに戻れます）",
        "",
        "**3. `/join` `/leave` … 声で話す**",
        f"・{_VOICE_LINE}",
        f"・{_AUTONOMOUS_LINE}",
        "",
        "**4. 記憶とプライバシー**",
        f"・{_MEMORY_LINE}",
        f"・{_STATS_LINE}",
        f"・NGワードや不適切な発言は自動で検知され、{BLACKLIST_MAX_VIOLATIONS}回でボットが応答しなくなります。",
        "",
        f"※ 各パネルは{VIEW_TIMEOUT_SECONDS:g}秒で時間切れになります。消えたら、もう一度 `/functions` を実行してください。",
    ]
    return _fit_to_discord_limit("\n".join(lines))


def build_welcome_text(bot_name: str) -> str:
    """サーバーへ招待された直後に投稿する自己紹介・使い方の本文を組み立てる。

    公開チャンネルへ1通だけ投稿されるため、次を守ること:
      ・`@everyone` / `@here` / ロールメンションを**絶対に書かない**（通知爆撃になる）。
      ・個人情報・記憶・統計は載せない（そもそも参加時点では何のデータも無い）。
      ・長くしすぎない。詳細は ❓ 使い方 に譲り、最初の一歩だけを案内する。

    Args:
        bot_name: メンション例に埋め込むボット名（`resolve_bot_name` の戻り値）。
    """
    lines = [
        f"👋 はじめまして、**{bot_name}** です。サーバーに招待していただきありがとうございます。",
        "",
        f"・**話しかけ方**: {_chat_usage_line(bot_name)}",
        f"・**添付**: {_ATTACHMENT_LINE}",
        f"・**リマインダー**: {_REMINDER_LINE}",
        f"・**調べ物**: {_SUBAGENT_LINE}",
        "・**設定・機能**: `/functions` から、キャラ設定・会話要約・会話統計・自発会話・"
        "リマインダーなどのパネルを開けます（実行した本人だけに見えます）。",
        f"・**音声会話**: {_VOICE_LINE}",
        f"・**自律行動**: {_AUTONOMOUS_LINE}",
        f"・**評価**: {_FEEDBACK_LINE}",
        f"・**記憶**: {_MEMORY_LINE}",
        f"・**統計**: {_STATS_LINE}",
        "",
        "くわしい使い方は `/functions` → **❓ 使い方** でいつでも読めます。",
    ]
    return _fit_to_discord_limit("\n".join(lines))


class HelpView(PanelView):
    """使い方（ヘルプ）パネル。

    表示するだけで副作用が無いため、部品は ↩️ 戻る だけ。パネル自体は
    エフェメラル（本人だけに見える）で表示される想定。
    """

    def __init__(
        self,
        *,
        bot_name: str,
        make_hub: HubFactory | None = None,
    ) -> None:
        super().__init__(
            timeout_notice=(
                "⏱️ 使い方パネルは時間切れになりました。"
                "再度 /functions を実行してください。"
            )
        )
        # メンション例に使うボット名（表示のたびに解決し直さず、開いた時点の値を持つ）。
        self.bot_name = bot_name
        # 「戻る」ボタンが機能パネルを再生成するための生成関数
        self.make_hub = make_hub

        self._add_all_items()

    def _add_all_items(self) -> None:
        """UI パーツを View に追加する。"""
        self.add_item(BackButton(row=0))

    def build_preview(self) -> str:
        """パネルの表示テキスト（使い方の本文）を組み立てる。"""
        return build_help_text(self.bot_name)
