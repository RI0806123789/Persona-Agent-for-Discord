import { LoadingIndicator } from "../components/expressive";
import { Icon, type IconName } from "../components/Icon";
import { Card, Chip, InlineError, Notice, StatusChip } from "../components/ui";
import { formatRelative } from "../format";
import { describePersona, PERSONA_ESTIMATE_NOTE } from "../personaView";
import { usePrivacy } from "../privacy";
import type { GuildSettingsView, Insights, Profile } from "../types";
import { NotConnected } from "./shared";

export function GuildsPage({
  connected,
  connecting,
  onConnect,
  selectedProfile,
  insights,
  insightsFetchedAt,
  insightsError,
  now,
}: {
  connected: boolean;
  connecting: boolean;
  onConnect: () => void;
  selectedProfile: Profile | null;
  insights: Insights | null;
  insightsFetchedAt: number | null;
  insightsError: string | null;
  now: number;
}) {
  const privacy = usePrivacy();
  if (!connected) {
    return <NotConnected profile={selectedProfile} connecting={connecting} onConnect={onConnect} />;
  }
  if (!insights) {
    return (
      <Card variant="filled">
        {insightsError ? (
          <InlineError>{insightsError}</InlineError>
        ) : (
          <div className="loading-row">
            <LoadingIndicator size={48} />
            <span className="body-large muted">Discord API と bot.db から集めています…</span>
          </div>
        )}
      </Card>
    );
  }

  const discord = insights.discord;
  const guilds = discord.guilds ?? [];

  return (
    <div className="grid">
      {!discord.ok && (
        <div className="span-12">
          <InlineError>
            Discord API: {discord.error}
            {discord.partial ? "（サーバー一覧は一部取得です。取得済みの一覧と保存済み設定だけを表示しています）" : "（サーバー一覧を取得できませんでした）"}
          </InlineError>
        </div>
      )}

      {guilds.map((guild, index) => {
        // 空文字・空白だけの名前は「名前が無い」ものとして扱う（見出しとアバター文字が空欄にならないように）。
        const name = typeof guild.name === "string" && guild.name.trim() !== "" ? guild.name : null;
        return (
          <section key={`${guild.name}-${index}`} className="m3-card m3-card-filled guild-card span-6">
            <header className="guild-head">
              <span className="guild-avatar" aria-hidden="true">
                {privacy.enabled ? "•" : Array.from(name ?? "不明")[0]}
              </span>
              <div className="guild-title">
                <h2 className="title-large">{privacy.mask(name ?? "名前不明のサーバー")}</h2>
                <div className="chips">
                  <Chip icon="group">{guild.members ?? "—"} 人</Chip>
                  <Chip icon="person">オンライン {guild.online ?? "—"}</Chip>
                  {guild.voice_error ? (
                    <StatusChip tone="warning" label="ボイス状態は未取得" />
                  ) : guild.voice_channel ? (
                    <StatusChip tone="good" icon="voice" label={`ボイス: ${privacy.mask(guild.voice_channel)}`} />
                  ) : (
                    <StatusChip tone="neutral" label="ボイス未接続" />
                  )}
                </div>
              </div>
            </header>
            {guild.voice_error && <InlineError>{guild.voice_error}</InlineError>}
            <SettingsList settings={guild.settings} />
          </section>
        );
      })}

      <section className="m3-card m3-card-outlined guild-card span-6">
        <header className="guild-head">
          <span className="guild-avatar is-dm" aria-hidden="true">
            <Icon name="chat" size={22} />
          </span>
          <div className="guild-title">
            <h2 className="title-large">DM（全 DM 共通）</h2>
            <span className="body-small muted">ダイレクトメッセージはサーバーが無いため、1つの設定を共有します</span>
          </div>
        </header>
        <SettingsList settings={insights.dm_settings} />
      </section>

      <div className="span-12">
        <Notice>
          サーバー名・人数・ボイス接続は Discord API（サーバー上のトークンで読み取り）、設定は bot.db の guild_settings（読み取り専用）から取得しています
          （{insightsFetchedAt ? formatRelative(insightsFetchedAt * 1000, now) : "—"}）。話したいゲージや配信中のセッションはボットのメモリの中にしか無いため、ボットを変えずには表示できません。
          {insights.persona_estimated && PERSONA_ESTIMATE_NOTE}
        </Notice>
      </div>
    </div>
  );
}

function SettingsList({ settings }: { settings: GuildSettingsView }) {
  const privacy = usePrivacy();
  const persona = describePersona(settings);
  const channel = (id: string | null | undefined) =>
    id === undefined ? "この版には無い機能" : id === null ? "指定なし（自動）" : `ID: ${privacy.mask(id)}`;
  const rows: { icon: IconName; label: string; value: string; muted?: boolean }[] = [
    // ペルソナ名・注記は保存値やファイル名に由来する自由文なので、描画時に既知の秘密を伏せる。
    { icon: "sparkle", label: "ペルソナ", value: privacy.redact(persona.value) },
    { icon: "bolt", label: "モデル", value: settings.model ?? "—" },
    { icon: "chat", label: "応答", value: settings.respond_mode ?? "—" },
    { icon: "search", label: "Google 検索", value: settings.grounding ? "ON" : "OFF" },
    {
      icon: "history",
      label: "自発的な会話",
      value: settings.auto_topic_enabled ? `ON（${settings.auto_topic_interval_hours ?? "?"}時間）` : "OFF",
    },
    {
      icon: "stage",
      label: "自律行動",
      value: settings.autonomous_enabled === null ? "この版には無い機能" : settings.autonomous_enabled ? "ON" : "OFF",
      muted: settings.autonomous_enabled === null,
    },
    {
      icon: "stage",
      label: "配信先（ステージ）",
      value: channel(settings.autonomous_stage_channel_id),
      muted: settings.autonomous_stage_channel_id === undefined,
    },
    {
      icon: "voice",
      label: "配信先（ボイス）",
      value: channel(settings.autonomous_voice_channel_id),
      muted: settings.autonomous_voice_channel_id === undefined,
    },
  ];
  return (
    <>
      <ul className="settings-list">
        {rows.map((row) => (
          <li key={row.label}>
            <Icon name={row.icon} size={20} />
            <span className="body-medium settings-label">{row.label}</span>
            <span className={`body-large settings-value ${row.muted ? "muted" : ""}`}>{row.value}</span>
          </li>
        ))}
      </ul>
      {persona.notes.map((note) => (
        <span key={note} className="body-small muted">
          {privacy.redact(note)}
        </span>
      ))}
      {!settings.saved && <span className="body-small muted">保存された設定が無いため、既定値で動いています。</span>}
    </>
  );
}
