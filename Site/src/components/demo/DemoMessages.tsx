// 疑似 Discord 画面の、チャンネルのアイコン・アバター・発言・入力中の表示。
// 描くだけで状態を持たない。再生の進み方は ScenarioPlayer.tsx、タブと全体は ../DiscordDemo.tsx。
import { DEMO_CAST, type DemoChannelKind } from "../../content";
import type { DemoAuthor, DemoMessage } from "../../demoTimeline";
import { HashIcon, PaperclipIcon, SpeakerIcon, StageIcon, ThreadIcon } from "../icons";

export function ChannelIcon({ kind, size }: { kind: DemoChannelKind; size?: number }) {
  if (kind === "voice") return <SpeakerIcon size={size} />;
  if (kind === "stage") return <StageIcon size={size} />;
  if (kind === "thread") return <ThreadIcon size={size} />;
  return <HashIcon size={size} />;
}

export function Avatar({ author, speaking = false }: { author: DemoAuthor; speaking?: boolean }) {
  return (
    <span className={`dc-avatar dc-avatar--${author}${speaking ? " is-speaking" : ""}`} aria-hidden>
      {DEMO_CAST[author].initial}
    </span>
  );
}

function SpokenTag() {
  return (
    <span className="dc-spoken">
      <span className="dc-wave" aria-hidden>
        <i />
        <i />
        <i />
        <i />
      </span>
      声
    </span>
  );
}

export function Message({ message }: { message: DemoMessage }) {
  const member = DEMO_CAST[message.author];
  return (
    <>
      {message.dividerBefore && (
        <div className="dc-divider">
          <span>{message.dividerBefore}</span>
        </div>
      )}
      <div className="dc-message">
        <Avatar author={message.author} />
        <div className="dc-message__body">
          <div className="dc-message__head">
            <span className={`dc-name dc-name--${message.author}`}>{member.name}</span>
            {member.badge && <span className="dc-badge">{member.badge}</span>}
            {message.spoken && <SpokenTag />}
          </div>
          <p className="dc-text">
            {message.mention && <span className="dc-mention">@{DEMO_CAST.bot.name}</span>}
            {message.mention && " "}
            {message.text}
          </p>
          {message.attachment && (
            <span className="dc-attachment">
              <PaperclipIcon />
              {message.attachment}
            </span>
          )}
          {message.links && (
            <div className="dc-chips">
              {message.links.map((link) => (
                <span key={link} className="dc-link">
                  {link}
                </span>
              ))}
            </div>
          )}
          {message.reactions && (
            <div className="dc-chips">
              {message.reactions.map((reaction) => (
                <span key={reaction} className="dc-reaction">
                  {reaction} <b>1</b>
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

export function TypingRow({ message }: { message: DemoMessage }) {
  const name = DEMO_CAST[message.author].name;
  return (
    <div className="dc-typing">
      <span className="dc-typing__dots" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      <span>
        <b>{name}</b> が{message.spoken ? "話しています" : "入力中"}…
      </span>
    </div>
  );
}
