// Listen (#/listen), Find stations (#/add/<page>) and Settings (#/settings/<page>): one shell, a
// list of pages on the left (wrapping rows of tabs on phones) when an area has more than one.
// Offered only on the Wavescribe machine itself.
import { useEffect, useRef } from "react";
import { Icon, type IconName } from "../components/Icon";
import { Advanced, cx } from "../components/ui";
import { langName } from "../lib/lang";
import type { Area } from "../lib/router";
import { T } from "../reading/strings";
import { knownLangs } from "./api";
import { AlertRules } from "./AlertRules";
import { FeedApi } from "./FeedApi";
import { OnTheInternet } from "./Internet";
import { ListenPage } from "./ListenPage";
import { Models } from "./Models";
import { Outputs } from "./Outputs";
import { OnTheAir, RadioKept, ReceiversPage, Responsibility } from "./Radio";
import { Stations, TranslationLanguages } from "./Stations";
import { Status, Storage } from "./Status";
import "./manage.css";

type Page = [id: string, label: string, icon: IconName, body: () => React.ReactNode];

/** Each area's pages, the most used first; an area opens on its first. */
const AREAS: Record<Area, { label: string; pages: Page[] }> = {
  listen: {
    label: "Listen",
    pages: [["", "Listen", "speaker", () => <ListenPage />]],
  },
  add: {
    label: "Find stations",
    pages: [
      ["air", "On the air", "antenna", () => <OnTheAir />],
      ["internet", "On the internet", "globe", () => <OnTheInternet />],
    ],
  },
  settings: {
    label: "Settings",
    pages: [
      ["stations", "Stations", "live", () => <Stations />],
      ["alerts", "Alerts", "bell", () => <AlertRules />],
      ["languages", "Languages and models", "languages", () => <Models after={<TranslationLanguages />} />],
      ["receivers", "Receivers", "antenna", () => <ReceiversPage />],
      ["storage", "Storage and privacy", "storage", () => <><Storage /><RadioKept /></>],
      ["connections", "Connections", "send", () => <><Outputs /><Advanced id="connections" label="For other programs"><FeedApi /></Advanced></>],
      ["health", "Health", "pulse", () => <Status />],
    ],
  },
};

/** Pages about tuning a receiver say first that the law where you are is yours to follow (Listen
 * says it above its tuner). */
const RADIO_PAGES = new Set(["add/air"]);

export default function Manage({ area, page }: { area: Area; page: string }) {
  const a = AREAS[area];
  const current = a.pages.find(([id]) => id === page) || a.pages[0];
  const single = a.pages.length === 1;
  const body = useRef<HTMLElement>(null);
  const nav = useRef<HTMLElement>(null);
  useEffect(() => {
    body.current?.scrollTo(0, 0);
    // A new page starts at its top for the keyboard and screen readers too, not on the link left behind.
    body.current?.focus({ preventScroll: true });
    // A short window at a large text size scrolls the list: keep the current page in view.
    nav.current?.querySelector("[aria-current]")?.scrollIntoView({ block: "nearest", inline: "nearest" });
    document.title = single ? `${a.label} · Wavescribe` : `${current[1]} · ${a.label} · Wavescribe`;
    return () => { document.title = "Wavescribe"; };
  }, [current, a, single]);
  return (
    <div className={cx("manage", single && "is-single")}>
      {!single && (
        <nav ref={nav} className="m-nav" aria-labelledby="m-nav-h">
          <h2 id="m-nav-h" className="m-nav-h">{a.label}</h2>
          <ul>
            {a.pages.map(([id, label, icon]) => (
              <li key={id}>
                <a href={`#/${area}/${id}`} aria-current={id === current[0] ? "page" : undefined} className={cx(id === current[0] && "is-current")}>
                  <Icon name={icon} />
                  {label}
                </a>
              </li>
            ))}
          </ul>
          {area === "settings" && (
            <>
              <p className="m-nav-note">{T.byMachine}</p>
              <a className="m-nav-foot" href="/licenses.txt" target="_blank" rel="noopener">Licences of the page's parts</a>
            </>
          )}
        </nav>
      )}
      <main ref={body} className="m-body" id="main" tabIndex={-1} aria-labelledby="m-title">
        <div className="m-page">
          <h2 className="m-title" id="m-title">{current[1]}</h2>
          {RADIO_PAGES.has(`${area}/${current[0]}`) && <Responsibility />}
          {current[3]()}
          {area === "settings" && <p className="m-page-foot">{T.byMachine}</p>}
        </div>
        <datalist id="m-langs">{knownLangs().map((t) => <option key={t} value={t}>{langName(t)}</option>)}</datalist>
      </main>
    </div>
  );
}
