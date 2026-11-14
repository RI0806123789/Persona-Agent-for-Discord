// 伏せ字モード（スクリーンショットを Public の issue に貼るためのもの）。
// 欄ごとに隠す（mask）だけでなく、エラー文・ログ・コマンド履歴のような自由な文の中に出てくる
// ホスト名・ホスト鍵・ボット名・サーバー名・チャンネル名・利用者の表示名も、分かっている文字列を探して隠す（redact）。
// bot.db のテーブルの中身は、列の種類ごとに隠す（dbTables.ts の isSecretKind）。
import { createContext, useContext } from "react";
import type { ConnectionState, GitInfo, Insights, Profile } from "./types";
import { conversationHeader, conversationLines } from "./logPrivacy";
export { conversationLines } from "./logPrivacy";

export const MASK = "••••••";

// これより短い文字列は、普通の単語まで巻き込んで隠してしまうので対象にしない
const MIN_SECRET_LENGTH = 3;
// 伏せ字の対象から外す、よくある値（隠しても意味が無く、文章が読めなくなるもの）
const NOT_SECRET = new Set(["localhost", "127.0.0.1", "bot", "root", "true", "false"]);
// サーバー名が取れなかったときに遠隔スクリプト（insights.py）が付ける代替の名前。
// 実際のサーバー名ではないので秘密にせず、説明文など一般の文まで巻き込まないようにする（#452）
const GUILD_NAME_PLACEHOLDER = /^(?:名前不明のサーバー|保存済みサーバー \d+)$/;

export interface Privacy {
  enabled: boolean;
  /** 欄の値をまるごと隠す */
  mask: (text: string | null | undefined) => string;
  /** 文の中に出てくる、分かっている秘密の文字列だけを隠す */
  redact: (text: string | null | undefined) => string;
}

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 画面が知っている「隠すべき文字列」を集める。 */
export function collectSecrets(parts: {
  profile: Profile | null;
  profiles: Profile[];
  connection: ConnectionState | null;
  insights: Insights | null;
  git?: GitInfo | null;
  /** データページで開いたテーブルの中身に出てきた、利用者の表示名 */
  userNames?: string[];
}): string[] {
  const values: (string | null | undefined)[] = [
    parts.connection?.fingerprint,
    parts.connection?.server?.hostname,
    parts.insights?.discord.bot_user,
    parts.git?.branch,
    ...(parts.userNames ?? []),
  ];
  // ボット名は「名前#0000」の形で出ることがあるので、名前だけでも探す
  const botUser = parts.insights?.discord.bot_user;
  if (botUser?.includes("#")) values.push(botUser.split("#")[0]);
  for (const profile of [parts.profile, ...parts.profiles]) {
    // systemd のサービス名は、既定の bot 以外は利用者が決めた値なので伏せる（概要・操作・接続ページと確認に出る）
    values.push(
      profile?.host,
      profile?.username,
      profile?.label,
      profile?.service,
      profile?.bot_dir,
      profile?.id,
      profile?.deploy_branch,
    );
    const home = profile?.bot_dir?.match(/\/(?:home|Users)\/([^/]+)(?=\/|$)/);
    values.push(home?.[0], home?.[1]);
  }
  for (const guild of parts.insights?.discord.guilds ?? []) {
    values.push(
      guild.name && !GUILD_NAME_PLACEHOLDER.test(guild.name) ? guild.name : null,
      guild.voice_channel,
    );
  }
  for (const settings of [parts.insights?.dm_settings, ...(parts.insights?.discord.guilds ?? []).map((guild) => guild.settings)]) {
    values.push(settings?.autonomous_stage_channel_id, settings?.autonomous_voice_channel_id);
  }
  const secrets = values.filter(
    (value): value is string => !!value && value.length >= MIN_SECRET_LENGTH && !NOT_SECRET.has(value.toLowerCase()),
  );
  // 長いものから置き換える（短い名前が長い名前の一部を先に壊さないように）
  return [...new Set(secrets)].sort((a, b) => b.length - a.length);
}

// ボットの通常出力と対応させる。ログ書式を追加・変更したら、この一覧と描画回帰を確認する。
// autonomous_service.py: 配信開始、接続失敗、ステージ準備失敗。
// autonomous_stage.py: 中止時のステージ枠の終了失敗。
// voice_guard.py: 元のチャンネルへの復帰。識別子は両モジュールの共通キーで処理する。
// 名前の括弧・改行を途中で切らないため、最後の対応する接尾辞まで保守的に隠す。
// 同じ書式の複数行をまとめた場合は、間の診断文も隠すことがある。
// 名前の範囲は Discord のチャンネル名の上限（100文字）に余裕を持たせた長さまでに限る。
// 上限がないと、離れた行の接尾辞まで一致してログ全体を隠し、長いログで照合も重くなる。
const NAME_SPAN = "[\\s\\S]{0,512}";
const CHANNEL_FIELDS = [
  new RegExp(`(?=(自律行動: (?:ボイス(?:チャンネル)?|ステージ)「)(${NAME_SPAN})(」で配信を開始しました))`, "g"),
  new RegExp(`(?=(自律行動: )(${NAME_SPAN})( への接続(?:を拒否されました|がタイムアウトしました|に失敗しました)))`, "g"),
  new RegExp(`(?=(自律行動: ステージ\\()(${NAME_SPAN})(\\)の準備に失敗しました))`, "g"),
  new RegExp(`(?=(自律行動: 中止時のステージ枠の終了に失敗しました（)(${NAME_SPAN})(）:))`, "g"),
  new RegExp(`(?=(ボイス接続の保護: 元のチャンネル「)(${NAME_SPAN})(」へ戻(?:ります|りました)))`, "g"),
];
// 起動確認の「ログイン成功: <ボット名>」。ボット名が未取得・短くて既知の秘密に入らなくても構造で隠す。
// 名前は行末までとし、末尾の句点・閉じ括弧は残す（デプロイの詳細は「…ログイン成功: 名前）。」の形）。
const LOGIN_SUCCESS_NAME = /(ログイン成功: )([^\r\n]+?)(?=[）)]?。?(?:\r?\n|$))/g;
const IDENTIFIER_FIELD =/(^|[^A-Za-z0-9_])(guild(?:_id)?|channel(?:_id)?|user_id|移動前|移動後)(\s*=\s*)(?:[0-9]+|<[^>\r\n]+>)/g;

/** 既知の現在名に依存せず、運用ログの名前・識別子を隠す。 */
function redactOperationalLog(text: string, mask: string): string {
  const ranges: {start: number; end: number}[] = [];
  // 原文の各開始位置を調べる。先に置換すると、重なった後続の名前の開始位置が失われる。
  // 先読みで一致を消費せず、上限付きの範囲を集めてからまとめて伏せる。
  for (const field of CHANNEL_FIELDS) {
    for (const match of text.matchAll(field)) {
      const start = match.index! + match[1].length;
      ranges.push({start, end: start + match[2].length});
    }
  }
  ranges.sort((a, b) => a.start - b.start || b.end - a.end);
  const merged: typeof ranges = [];
  for (const range of ranges) {
    const previous = merged.at(-1);
    if (previous && range.start <= previous.end) previous.end = Math.max(previous.end, range.end);
    else merged.push(range);
  }
  const parts: string[] = [];
  let offset = 0;
  for (const {start, end} of merged) {
    // 改行を残し、別エントリーへ分割された名前やデプロイ抜粋の行番号をずらさない。
    const value = text.slice(start, end);
    parts.push(text.slice(offset, start), value ? value.replace(/[^\r\n]+/g, () => mask) : mask);
    offset = end;
  }
  const result = parts.join("") + text.slice(offset);
  return result
    .replace(LOGIN_SUCCESS_NAME, (_match, label: string) => label + mask)
    .replace(IDENTIFIER_FIELD, (_match, before: string, key: string, equals: string) => before + key + equals + mask);
}

export function createPrivacy(enabled: boolean, secrets: string[]): Privacy {
  const pattern = secrets.length ? new RegExp(secrets.map(escapeRegExp).join("|"), "g") : null;
  return {
    enabled,
    mask: (text) => {
      if (!text) return "—";
      return enabled ? MASK : text;
    },
    redact: (text) => {
      if (!text) return text ?? "";
      if (!enabled) return text;
      const operational = redactOperationalLog(text, MASK);
      return pattern ? operational.replace(pattern, MASK) : operational;
    },
  };
}

export const PrivacyContext = createContext<Privacy>(createPrivacy(false, []));

export function usePrivacy(): Privacy {
  return useContext(PrivacyContext);
}

/** 診断の原文と安全な文面を切り替え、接続先由来の既知の秘密も同じように伏せる。 */
export function redactError(text: string | null | undefined, safeText: string | null | undefined, privacy: Privacy): string {
  return privacy.redact(privacy.enabled && safeText != null ? safeText : text);
}

// ---------------------------------------------------------------- ログの会話の本文

// 任意本文は運用ログと区別できないため、受信時の属性で隠す（logPrivacy.ts）。
const CHANNEL_MENTION = /#[^\s#、。，．,.:：)）」』\]]+/g;

/** 伏せ字モードで、ログ1行を見せる形にする。 */
export function redactLogLine(text: string, isConversation: boolean, privacy: Privacy): string {
  if (!privacy.enabled) return text;
  if (isConversation) {
    // 「User (場所)：」「AI：」の見出しだけ残し、誰が・どこで・何を話したかは隠す
    const header = conversationHeader(text);
    if (header === "user") return `User (${MASK})：${MASK}`;
    if (header === "ai") return `AI：${MASK}`;
    return MASK;
  }
  return privacy.redact(text).replace(CHANNEL_MENTION, `#${MASK}`);
}

/** 複数のログ行へ分割された名前も、表示範囲の抽出より前に隠す。会話の判定は呼出元のまま。 */
export function redactOperationalMessages(messages: string[], privacy: Privacy): string[] {
  if (!privacy.enabled) return messages;
  const lines = redactOperationalLog(messages.join("\n"), MASK).split("\n");
  let offset = 0;
  return messages.map((message) => {
    const count = message.split("\n").length;
    const result = lines.slice(offset, offset + count).join("\n");
    offset += count;
    return result;
  });
}

/**
 * 複数行の文章（デプロイの出力など）を伏せ字にする。ログと同じく、境界が不明な本文も隠す。
 * 再起動直後のログを出力に含めるため、そこに利用者の発言が混ざりうる。
 */
export function redactText(text: string, privacy: Privacy): string {
  if (!privacy.enabled || !text) return text;
  const lines = text.split("\n");
  const conversation = conversationLines(lines.map((message) => ({ ts: 0, message, priority: null, pid: null, source: "" })));
  return redactOperationalMessages(lines, privacy).map((line, index) => redactLogLine(line, conversation[index], privacy)).join("\n");
}
