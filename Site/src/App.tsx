import type { CSSProperties, ReactNode } from "react";
import {
  FEATURES_INTRO,
  FEATURE_GROUPS,
  GUIDES,
  HERO,
  PURPOSE,
  STACK,
  START,
  STORY,
  TOOLS,
  UPDATES,
  type StackItem,
} from "./content";
import { DiscordDemo } from "./components/DiscordDemo";
import { Reveal } from "./components/Reveal";
import { SiteFooter, SiteHeader, externalProps } from "./components/SiteChrome";
import { ArrowIcon, GitHubIcon } from "./components/icons";
import { useScrollToHash } from "./hooks";

/** ファーストビューの要素を、上から順に少しずつ遅らせて出す（遅れは CSS が --hero-order から決める） */
function heroOrder(order: number): CSSProperties {
  return { "--hero-order": order } as CSSProperties;
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
      <div className="guide-cards">
        <Reveal className="guide-cards__head">
          <h3>{START.guidesTitle}</h3>
          <p>{START.guidesLead}</p>
        </Reveal>
        <ul className="guide-cards__list">
          {GUIDES.map((guide, index) => (
            <li key={guide.id}>
              <Reveal order={index} className="guide-card-wrap">
                <a className="card guide-card" href={guide.href}>
                  <span className="guide-card__icon" aria-hidden>
                    {guide.icon}
                  </span>
                  <span className="guide-card__title">
                    {guide.title}
                    <ArrowIcon />
                  </span>
                  <span className="guide-card__summary">{guide.summary}</span>
                </a>
              </Reveal>
            </li>
          ))}
        </ul>
      </div>
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

export function App() {
  useScrollToHash();
  return (
    <>
      <a className="skip-link" href="#features">
        本文へ移動
      </a>
      <SiteHeader page="home" />
      <main>
        <Hero />
        <Features />
        <Story />
        <Start />
        <Stack />
        <Updates />
      </main>
      <SiteFooter page="home" />
    </>
  );
}
