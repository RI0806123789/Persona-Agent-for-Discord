"""Discord まわりの共通処理の窓口（#468）。

実体は責務ごとのモジュールへ分けた。このモジュールは、従来の `from persona_bot.utils.discord_helpers import ...` が
そのまま動くように、全部を再公開するだけ。

  - discord_channels.py    … チャンネルの種別・権限・表示名、サーバー参加時の案内投稿
  - discord_attachments.py … 添付ファイルの判定、画像の縮小デコード・解放（PIL）
  - discord_history.py     … チャンネル履歴の取得・整形、会話要約の元データ
  - discord_send.py        … 分割送信、通知の抑止（`REPLY_ALLOWED_MENTIONS`・`NO_MENTIONS`）、出典リンクの整形
  - discord_permissions.py … 管理者・ボット所有者・ブロック中の利用者の判定

★ 軽い層（`core/bot_client.py` など）は、画像（PIL）や履歴（プロンプト・背景処理）まで読み込まないよう、
  必要なモジュールから直接 import すること。テストが差し替える名前（`Image`・`MAX_IMAGE_*`・`split_message`・
  `WELCOME_MESSAGE_ENABLED` など）は、実体のあるモジュールを対象にする（このモジュールを差し替えても効かない）。
"""

from persona_bot.utils.discord_channels import (  # noqa: F401
    SupportedMessageChannel,
    can_bot_send_messages,
    channel_display_name,
    is_connected_voice_chat,
    is_supported_message_channel,
    pick_announcement_channel,
    post_welcome_message,
)
from persona_bot.utils.discord_attachments import (  # noqa: F401
    _decode_image_bytes,
    attachment_is_image,
    close_image_attachments,
    describe_supported_attachments,
    is_supported_document_attachment,
    load_image_attachments,
)
from persona_bot.utils.discord_history import (  # noqa: F401
    SummarySource,
    _include_history_message,
    build_history_text,
    build_summary_source,
    build_summary_source_text,
    clean_message_content,
    fetch_channel_history_text,
    fetch_messages_since,
)
from persona_bot.utils.discord_send import (  # noqa: F401
    NO_MENTIONS,
    REPLY_ALLOWED_MENTIONS,
    _sanitize_link_uri,
    _send_chunks,
    format_grounding_sources,
    mask_grounding_sources,
    send_long_message,
    send_long_reply,
    split_message,
)
from persona_bot.utils.discord_permissions import (  # noqa: F401
    _is_application_owner,
    is_administrator,
    is_bot_owner,
    is_current_administrator,
    is_current_bot_owner,
    reject_blocked_interaction,
)
