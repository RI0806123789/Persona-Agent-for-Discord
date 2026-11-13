import type { CSSProperties, ReactNode } from "react";
import {
  DISCLAIMER,
  FEATURES_INTRO,
  FEATURE_GROUPS,
  FOOTER,
  HERO,
  LINKS,
  NAV_ITEMS,
  PURPOSE,
  SITE_NAME,
  STACK,
  START,
  STORY,
  TOOLS,
  UPDATES,
  type StackItem,
} from "./content";
import { DiscordDemo } from "./components/DiscordDemo";
import { Reveal } from "./components/Reveal";
import { ArrowIcon, GitHubIcon, LogoMark, MoonIcon, SunIcon } from "./components/icons";
import { useScrolled, useTheme } from "./hooks";

/** ページの外（GitHub）へ出るリンクかどうか。外へ出るものだけ新しいタブで開く */
function isExternal(href: string): boolean {
  return href.startsWith("http");
}

function externalProps(href: string) {
  return isExternal(href) ? { target: "_blank", rel: "noreferrer" } : {};
}

/** ファーストビューの要素を、上から順に少しずつ遅らせて出す（遅れは CSS が --hero-order から決める） */
function heroOrder(order: number): CSSProperties {
  return { "--hero-order": order } as CSSProperties;
}

function Header() {
  const [theme, toggleTheme] = useTheme();
  const scrolled = useScrolled();
  const nextLabel = theme === "dark" ? "明るい配色に切り替える" : "暗い配色に切り替える";
  return (
    <header className={`header${scrolled ? " is-scrolled" : ""}`}>
      <div className="container header__inner">
        <a className="brand" href="#top">
          <LogoMark />
          <span>{SITE_NAME}</span>
        </a>
        <nav className="nav" aria-label="ページ内の移動">
          {NAV_ITEMS.map((item) => (
            <a key={item.href} href={item.href}>
              {item.label}
            </a>
          ))}
        </nav>
        <div className="header__actions">
          <button type="button" className="icon-button" onClick={toggleTheme} aria-label={nextLabel} title={nextLabel}>
            {/* key を変えて作り直し、切り替えのたびにアイコンを少し回しながら出す */}
            <span key={theme} className="icon-swap">
              {theme === "dark" ? <SunIcon /> : <MoonIcon />}
            </span>
          </button>
          <a className="icon-button" href={LINKS.repository} aria-label="GitHub のリポジトリを開く" title="GitHub" {...externalProps(LINKS.repository)}>
            <GitHubIcon />
          </a>
        </div>
      </div>
    </header>
  );
}

function Hero() {
  const titleCount = HERO.titleLines.length;
  return (
    <section className="hero" id="top">
      <div className="hero__glow" aria-hidden />
      <div className="container hero__inner">
        <div className="hero__copy">
          <p className="eyebrow hero-in" style={heroOrder(0)}>
            {HERO.eyebrow}
          </p>
          <h1 className="hero__title">
            {HERO.titleLines.map((line, index) => (
              <span
                key={line}
                className={`hero-in${index === HERO.titleLines.length - 1 ? " is-accent" : ""}`}
                style={heroOrder(index + 1)}
              >
                {line}
              </span>
            ))}
          </h1>
          <p className="hero__lead hero-in" style={heroOrder(titleCount + 1)}>
            {HERO.lead}
          </p>
          <div className="hero__actions hero-in" style={heroOrder(titleCount + 2)}>
            <a className="button button--primary" href={HERO.primaryAction.href}>
              {HERO.primaryAction.label}
              <ArrowIcon />
            </a>
            <a className="button button--ghost" href={HERO.secondaryAction.href} {...externalProps(HERO.secondaryAction.href)}>
              <GitHubIcon />
              {HERO.secondaryAction.label}
            </a>
          </div>
          <ul className="hero__notes hero-in" style={heroOrder(titleCount + 3)}>
            {HERO.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
        <div className="hero__demo hero-in hero-in--demo" style={heroOrder(2)}>
          <DiscordDemo />
        </div>
      </div>
    </section>
  );
}

type SectionProps = {
  id?: string;
  eyebrow: string;
  title: string;
  lead?: string;
  tone?: "plain" | "tinted";
  children: ReactNode;
};

function Section({ id, eyebrow, title, lead, tone = "plain", children }: SectionProps) {
  return (
    <section id={id} className={`section section--${tone}`}>
      <div className="container">
        <Reveal className="section__head">
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
          {lead && <p className="section__lead">{lead}</p>}
        </Reveal>
        {children}
      </div>
    </section>
  );
}

function Features() {
  return (
    <Section id="features" {...FEATURES_INTRO}>
      <div className="feature-groups">
        {FEATURE_GROUPS.map((group) => (
          <div key={group.id} className="feature-group">
            <Reveal className="feature-group__head">
              <h3>{group.title}</h3>
              <p>{group.lead}</p>
            </Reveal>
            <div className="feature-grid">
              {group.features.map((feature, index) => (
                <Reveal key={feature.title} order={index} className="card feature">
                  <span className="feature__icon" aria-hidden>
                    {feature.icon}
                  </span>
                  <h4>{feature.title}</h4>
                  <p>{feature.body}</p>
                  {feature.note && <p className="feature__note">{feature.note}</p>}
                </Reveal>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Story() {
  return (
    <Section id="story" tone="tinted" eyebrow={STORY.eyebrow} title={STORY.title}>
      <div className="story">
        <Reveal className="story__text">
          {STORY.paragraphs.map((paragraph) => (
            <p key={paragraph}>{paragraph}</p>
          ))}
        </Reveal>
        <div className="story__purpose">
          <Reveal>
            <p className="eyebrow">{PURPOSE.eyebrow}</p>
            <h3>{PURPOSE.title}</h3>
          </Reveal>
          <ol className="purpose-list">
            {PURPOSE.items.map((item, index) => (
              <li key={item.title}>
                <Reveal order={index} className="card purpose">
                  <span className="purpose__number" aria-hidden>
                    {index + 1}
                  </span>
                  <div>
                    <h4>{item.title}</h4>
                    <p>{item.body}</p>
                  </div>
                </Reveal>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </Section>
  );
}

function Start() {
  return (
    <Section id="start" eyebrow={START.eyebrow} title={START.title} lead={START.lead}>
      <ol className="steps">
        {START.steps.map((step, index) => (
          <li key={step.title}>
            <Reveal order={index} className="card step">
              <span className="step__number" aria-hidden>
                {index + 1}
              </span>
              <h3>{step.title}</h3>
              <p>{step.body}</p>
              {"action" in step && (
                <a className="text-link" href={step.action.href} {...externalProps(step.action.href)}>
                  {step.action.label}
                  <ArrowIcon />
                </a>
              )}
            </Reveal>
          </li>
        ))}
      </ol>
    </Section>
  );
}

function StackList({ items }: { items: readonly StackItem[] }) {
  return (
    <ul className="stack-list">
      {items.map((item, index) => (
        <li key={item.name}>
          <Reveal order={index} className="stack-item">
            <strong>{item.name}</strong>
            <span>{item.role}</span>
          </Reveal>
        </li>
      ))}
    </ul>
  );
}

function Stack() {
  return (
    <section id="stack" className="section section--tinted">
      <div className="container stack">
        <div>
          <Reveal className="section__head section__head--left">
            <p className="eyebrow">{STACK.eyebrow}</p>
            <h2>{STACK.title}</h2>
            <p className="section__lead">{STACK.lead}</p>
          </Reveal>
          <StackList items={STACK.items} />
        </div>
        <div>
          <Reveal className="section__head section__head--left">
            <p className="eyebrow">{TOOLS.eyebrow}</p>
            <h2>{TOOLS.title}</h2>
            <p className="section__lead">{TOOLS.lead}</p>
          </Reveal>
          <StackList items={TOOLS.items} />
        </div>
      </div>
    </section>
  );
}

function Updates() {
  return (
    <section className="section">
      <div className="container">
        <Reveal className="card updates">
          <div>
            <p className="eyebrow">{UPDATES.eyebrow}</p>
            <h2>{UPDATES.title}</h2>
            <p>{UPDATES.lead}</p>
          </div>
          <div className="updates__actions">
            {UPDATES.actions.map((action, index) => (
              <a
                key={action.href}
                className={`button ${index === 0 ? "button--primary" : "button--ghost"}`}
                href={action.href}
                {...externalProps(action.href)}
              >
                {action.label}
                <ArrowIcon />
              </a>
            ))}
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="footer">
      <div className="container">
        <aside className="disclaimer" aria-labelledby="disclaimer-title">
          <h2 id="disclaimer-title">{DISCLAIMER.title}</h2>
          <p>{DISCLAIMER.body}</p>
          <p>{DISCLAIMER.voiceNote}</p>
        </aside>
        <div className="footer__row">
          <a className="brand" href="#top">
            <LogoMark size={24} />
            <span>{SITE_NAME}</span>
          </a>
          <nav className="footer__links" aria-label="関連するページ">
            {FOOTER.links.map((link) => (
              <a key={link.label} href={link.href} {...externalProps(link.href)}>
                {link.label}
              </a>
            ))}
          </nav>
        </div>
        <p className="footer__meta">
          <a href={FOOTER.license.href} {...externalProps(FOOTER.license.href)}>
            {FOOTER.license.label}
          </a>
          {FOOTER.notes.map((note) => (
            <span key={note}>{note}</span>
          ))}
        </p>
      </div>
    </footer>
  );
}

export function App() {
  return (
    <>
      <a className="skip-link" href="#features">
        本文へ移動
      </a>
      <Header />
      <main>
        <Hero />
        <Features />
        <Story />
        <Start />
        <Stack />
        <Updates />
      </main>
      <Footer />
    </>
  );
}
