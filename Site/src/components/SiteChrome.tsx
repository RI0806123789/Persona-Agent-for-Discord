// トップページと説明ページで共通のヘッダー・フッター（Issue #528 で App.tsx から切り出した）。
import { DISCLAIMER, FOOTER, GUIDE_PAGE, GUIDES, LINKS, NAV_ITEMS, SITE_NAME } from "../content";
import type { GuideId } from "../guides/types";
import { useScrolled, useTheme } from "../hooks";
import { GitHubIcon, LogoMark, MoonIcon, SunIcon } from "./icons";

/** 今どのページを表示しているか。トップページは "home" */
export type PageId = "home" | GuideId;

/** ページの外（GitHub）へ出るリンクかどうか。外へ出るものだけ新しいタブで開く */
export function isExternal(href: string): boolean {
  return href.startsWith("http");
}

export function externalProps(href: string) {
  return isExternal(href) ? { target: "_blank", rel: "noreferrer" } : {};
}

/** トップページの節へのリンク。説明ページからは ./index.html#… で戻る */
function homeHref(page: PageId, hash: string): string {
  return page === "home" ? hash : `./index.html${hash}`;
}

export function SiteHeader({ page }: { page: PageId }) {
  const [theme, toggleTheme] = useTheme();
  const scrolled = useScrolled();
  const nextLabel = theme === "dark" ? "明るい配色に切り替える" : "暗い配色に切り替える";
  const onGuide = page !== "home";
  return (
    <header className={`header${scrolled ? " is-scrolled" : ""}`}>
      <div className="container header__inner">
        <a className="brand" href={homeHref(page, "#top")}>
          <LogoMark />
          <span>{SITE_NAME}</span>
        </a>
        <nav className="nav" aria-label={onGuide ? "サイト内の移動" : "ページ内の移動"}>
          {NAV_ITEMS.map((item) => (
            <a key={item.href} href={homeHref(page, item.href)}>
              {item.label}
            </a>
          ))}
          <a href={GUIDES[0].href} className={onGuide ? "is-current" : undefined} aria-current={onGuide ? "true" : undefined}>
            {GUIDE_PAGE.navLabel}
          </a>
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

export function SiteFooter({ page }: { page: PageId }) {
  return (
    <footer className="footer">
      <div className="container">
        <aside className="disclaimer" aria-labelledby="disclaimer-title">
          <h2 id="disclaimer-title">{DISCLAIMER.title}</h2>
          <p>{DISCLAIMER.body}</p>
          <p>{DISCLAIMER.voiceNote}</p>
        </aside>
        <div className="footer__row">
          <a className="brand" href={homeHref(page, "#top")}>
            <LogoMark size={24} />
            <span>{SITE_NAME}</span>
          </a>
          <div className="footer__navs">
            <nav className="footer__group" aria-labelledby="footer-guides">
              <p id="footer-guides" className="footer__label">
                {FOOTER.guidesLabel}
              </p>
              <div className="footer__links">
                {GUIDES.map((guide) => (
                  <a key={guide.id} href={guide.href} aria-current={guide.id === page ? "page" : undefined}>
                    {guide.title}
                  </a>
                ))}
              </div>
            </nav>
            <nav className="footer__group" aria-labelledby="footer-related">
              <p id="footer-related" className="footer__label">
                {FOOTER.relatedLabel}
              </p>
              <div className="footer__links">
                {FOOTER.links.map((link) => (
                  <a key={link.label} href={link.href} {...externalProps(link.href)}>
                    {link.label}
                  </a>
                ))}
              </div>
            </nav>
          </div>
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
