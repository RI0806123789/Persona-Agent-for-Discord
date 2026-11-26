import type { CSSProperties } from "react";
import { GUIDE_PAGE, GUIDES } from "../../content";
import type { Guide, GuideSection } from "../../guides/types";
import { useScrollToHash } from "../../hooks";
import { SiteFooter, SiteHeader } from "../SiteChrome";
import { ArrowIcon } from "../icons";
import { GuideBlocks } from "./GuideBlocks";
import { InlineText } from "./InlineText";

/** ページの見出しを上から順に少しずつ遅らせて出す（トップページのファーストビューと同じ動き） */
function heroOrder(order: number): CSSProperties {
  return { "--hero-order": order } as CSSProperties;
}

/** ガイドの一覧（いま開いているページに印を付ける） */
function GuideList({ current }: { current: Guide["id"] }) {
  return (
    <nav className="guide-list" aria-label={GUIDE_PAGE.listLabel}>
      <ul>
        {GUIDES.map((guide) => (
          <li key={guide.id}>
            <a href={guide.href} className={guide.id === current ? "is-current" : undefined} aria-current={guide.id === current ? "page" : undefined}>
              <span aria-hidden>{guide.icon}</span>
              {guide.title}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function TocLinks({ sections }: { sections: readonly GuideSection[] }) {
  return (
    <ol className="guide-toc__list">
      {sections.map((section) => (
        <li key={section.id}>
          <a href={`#${section.id}`}>
            <InlineText text={section.title} />
          </a>
        </li>
      ))}
    </ol>
  );
}

/** 前後のページへの案内 */
function Pager({ current }: { current: Guide["id"] }) {
  const index = GUIDES.findIndex((guide) => guide.id === current);
  const previous = index > 0 ? GUIDES[index - 1] : undefined;
  const next = index < GUIDES.length - 1 ? GUIDES[index + 1] : undefined;
  return (
    <nav className="guide-pager" aria-label="前後のページ">
      {previous ? (
        <a className="card guide-pager__link guide-pager__link--previous" href={previous.href}>
          <span className="guide-pager__label">{GUIDE_PAGE.previousLabel}</span>
          <span className="guide-pager__title">{previous.title}</span>
        </a>
      ) : (
        <a className="card guide-pager__link guide-pager__link--previous" href={GUIDE_PAGE.backToTop.href}>
          <span className="guide-pager__label">{GUIDE_PAGE.previousLabel}</span>
          <span className="guide-pager__title">{GUIDE_PAGE.backToTop.label}</span>
        </a>
      )}
      {next && (
        <a className="card guide-pager__link guide-pager__link--next" href={next.href}>
          <span className="guide-pager__label">{GUIDE_PAGE.nextLabel}</span>
          <span className="guide-pager__title">
            {next.title}
            <ArrowIcon />
          </span>
        </a>
      )}
    </nav>
  );
}

/** 説明ページ（ガイド）1 枚分 */
export function GuidePage({ guide }: { guide: Guide }) {
  useScrollToHash();
  return (
    <>
      <a className="skip-link" href="#guide-content">
        本文へ移動
      </a>
      <SiteHeader page={guide.id} />
      <main>
        <header className="guide-head">
          <div className="hero__glow guide-head__glow" aria-hidden />
          <div className="container guide-head__inner">
            <p className="eyebrow hero-in" style={heroOrder(0)}>
              {GUIDE_PAGE.eyebrow}
            </p>
            <h1 className="guide-head__title hero-in" style={heroOrder(1)}>
              {guide.title}
            </h1>
            <p className="guide-head__lead hero-in" style={heroOrder(2)}>
              <InlineText text={guide.lead} />
            </p>
          </div>
        </header>
        <div className="container guide-layout">
          <aside className="guide-side">
            <GuideList current={guide.id} />
            {/* 広い画面では目次を常に出し、狭い画面では開閉できる形にする（片方は CSS で隠す） */}
            <nav className="guide-toc guide-toc--wide" aria-label={GUIDE_PAGE.tocLabel}>
              <p className="guide-toc__title">{GUIDE_PAGE.tocLabel}</p>
              <TocLinks sections={guide.sections} />
            </nav>
            <details className="guide-toc guide-toc--narrow">
              <summary>{GUIDE_PAGE.tocLabel}</summary>
              <nav aria-label={GUIDE_PAGE.tocLabel}>
                <TocLinks sections={guide.sections} />
              </nav>
            </details>
          </aside>
          <article id="guide-content" className="guide-body" tabIndex={-1}>
            {guide.sections.map((section) => (
              <section key={section.id} id={section.id} className="guide-section" aria-labelledby={`${section.id}-title`}>
                <h2 id={`${section.id}-title`}>
                  <a className="guide-section__anchor" href={`#${section.id}`}>
                    <InlineText text={section.title} />
                  </a>
                </h2>
                <GuideBlocks blocks={section.blocks} />
              </section>
            ))}
            <Pager current={guide.id} />
          </article>
        </div>
      </main>
      <SiteFooter page={guide.id} />
    </>
  );
}
