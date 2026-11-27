// The header: the mark, the three places (reading, listening, finding stations), one field to
// search or ask, the connection, what's playing, display settings and the gear for Settings.
import { useEffect, useRef, useState } from "react";
import { A, stopAudio } from "../lib/audio";
import { headerLag } from "../lib/behind";
import { S } from "../lib/live";
import { type Area, go, isReading, parse, useHash } from "../lib/router";
import { useTopics } from "../lib/store";
import { V, railShown, toggleRail } from "../lib/view";
import { Icon, Mark } from "../components/Icon";
import { Button, Kbd, cx } from "../components/ui";
import { recentAlerts } from "./Alerts";
import { sendQuestion } from "./Ask";
import { DisplayButton } from "./Settings";
import { T } from "./strings";

/** Read goes back to what was being read: the same columns, search or question. */
let lastRead = "#/";

export function Header({ area }: { area: Area | null }) {
  useTopics("state", "conn", "narrow");
  // Only a reading view is remembered: until the page knows it is on the Wavescribe machine,
  // Listen, Find and Settings show the reading view too, and Read must not lead back to them.
  if (!area && isReading(location.hash)) lastRead = location.hash || "#/";
  const here = (a: Area | null) => (a === area ? "page" : undefined);
  return (
    <header className="hdr">
      <a className="brand" href="#/" aria-label="Wavescribe: all live stations">
        <Mark />
        <h1>Wavescribe</h1>
      </a>
      {S.manage && (
        <nav className="hdr-nav" aria-label="Wavescribe">
          <a href={area ? lastRead : "#/"} aria-current={here(null)}>{T.read}</a>
          <a href="#/listen" aria-current={here("listen")}>{T.listen}</a>
          <a href="#/add" aria-current={here("add")} title={T.findHint}>{T.find}</a>
        </nav>
      )}
      {!area && S.hasStore && <CommandField />}
      <span className="hdr-end">
        <Conn />
        <NowPlaying />
        {!area && !V.narrow && <AlertsToggle />}
        {!area && <DisplayButton />}
        {S.manage && (
          <a className={cx("btn btn-ghost btn-md btn-icon hdr-gear", area === "settings" && "is-on")} href="#/settings"
             aria-current={here("settings")} aria-label={T.settings} title={T.settingsHint}>
            <Icon name="gear" />
          </a>
        )}
      </span>
    </header>
  );
}

function Conn() {
  useTopics("conn", "state", "status");
  // "Live" only once this tab has its stations, and while the machine keeps up: text that arrives
  // minutes late isn't live.
  const conn = S.conn === "live" && !S.loaded ? "connecting" : S.conn;
  const lag = conn === "live" ? headerLag(S.status) : null;
  const behind = !!lag?.behind;
  const label = conn === "live" ? (behind ? T.behindBy(lag.minutes) : T.connected)
    : conn === "connecting" ? T.connecting : conn === "blocked" ? T.cantConnect : T.reconnecting;
  const title = conn === "live" && !behind ? "Receiving live text from this Wavescribe" : undefined;
  const tip = behind ? [T.behindWhat(lag.minutes), S.status?.load?.reason, T.behindDo].filter(Boolean) : null;
  const body = (
    <>
      <span className="conn-dot" aria-hidden="true" />
      <span className="conn-label">{label}</span>
    </>
  );
  const described = tip ? { "aria-describedby": "conn-tip" } : {};
  // On the Wavescribe machine the lamp opens Health, where each part says how it is doing.
  return (
    <span className={cx("conn", `conn-${behind ? "behind" : conn}`)} role="status">
      {S.manage
        ? <a className="conn-link" href="#/settings/health" {...described} title={tip ? undefined : `${title ? `${title}. ` : ""}${T.healthHint}`}
             aria-label={`${label}. ${T.healthHint}`}>{body}</a>
        : <span className="conn-link" title={title} tabIndex={tip ? 0 : undefined} {...described}>{body}</span>}
      {tip && (
        <span className="pop conn-tip" id="conn-tip" role="tooltip">
          {tip.map((line) => <span key={line}>{line}</span>)}
        </span>
      )}
    </span>
  );
}

function NowPlaying() {
  useTopics("audio");
  if (!A.label) return null;
  return (
    <span className="playing" role="status">
      <Icon name="speaker" size={14} />
      {/* What plays is chosen under Listen: the name leads there; Stop works from anywhere. */}
      {S.manage && !A.replay
        ? <a className="playing-label" href="#/listen" title="Open Listen" dir="auto">{A.label}</a>
        : <span className="playing-label" dir="auto">{A.replay ? `Replay · ${A.label}` : A.label}</span>}
      <Button size="sm" variant="ghost" icon="stop" aria-label={T.stopListening} title={T.stopListening} onClick={stopAudio} />
    </span>
  );
}

function AlertsToggle() {
  useTopics("alerts", "prefs", "narrow");
  const n = recentAlerts().length;
  const open = railShown();
  return (
    <Button variant="ghost" icon="bell" aria-pressed={open} onClick={() => toggleRail()}
            title={open ? "Hide the alerts" : "Show the alerts"} className="alerts-toggle"
            aria-keyshortcuts="A" aria-label={n > 0 ? T.alertsCount(n) : T.alerts}>
      {n > 0 && <span className="count num" aria-hidden="true">{n}</span>}
    </Button>
  );
}

/** One field: Enter searches everything heard; ⌘Enter (or the second option) asks. */
function CommandField() {
  useTopics("state");
  const hash = useHash();
  const route = parse(hash);
  const [q, setQ] = useState(route.kind === "find" ? route.q : "");
  const [focus, setFocus] = useState(false);
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  useEffect(() => { if (route.kind === "find") setQ(route.q); }, [hash]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement).closest("input, textarea, select, [contenteditable]");
      if ((e.key === "/" && !typing) || (e.key === "k" && (e.metaKey || e.ctrlKey))) {
        e.preventDefault();
        input.current?.focus();
        input.current?.select();
      }
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, []);
  const term = q.trim();
  const canAsk = !S.ask;
  const run = (which: number) => {
    if (!term) return;
    if (which === 1) {
      if (!canAsk) return;
      sendQuestion(term);
      setQ("");
      go("#/ask");
    } else go(`#/find/${encodeURIComponent(term)}`);
    input.current?.blur();
  };
  const open = focus && !!term;
  return (
    <div ref={wrap} className={cx("cmd", open && "is-open")}
         onBlur={(e) => { if (!wrap.current?.contains(e.relatedTarget as Node)) setFocus(false); }}>
      <Icon name="search" />
      <input ref={input} type="search" value={q} placeholder={`${T.search}…`} aria-label={T.searchHint}
             role="combobox" aria-expanded={open} aria-controls="cmd-list" aria-activedescendant={open ? `cmd-${sel}` : undefined}
             autoComplete="off" spellCheck={false} enterKeyHint="search"
             onFocus={() => setFocus(true)} onChange={(e) => { setQ(e.target.value); setSel(0); }}
             onKeyDown={(e) => {
               if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setSel(sel ? 0 : 1); }
               else if (e.key === "Enter") { e.preventDefault(); run(e.metaKey || e.ctrlKey ? 1 : sel); }
               else if (e.key === "Escape") { if (q) setQ(""); else input.current?.blur(); }
             }} />
      {!focus && !q && <Kbd>/</Kbd>}
      {open && (
        <ul className="pop cmd-list" id="cmd-list" role="listbox" aria-label={T.search}>
          <li id="cmd-0" role="option" aria-selected={sel === 0} onPointerDown={(e) => { e.preventDefault(); run(0); }} onPointerEnter={() => setSel(0)}>
            <Icon name="search" />
            <span className="cmd-text">{T.searchFor(term)}</span>
            <Kbd>↵</Kbd>
          </li>
          <li id="cmd-1" role="option" aria-selected={sel === 1} aria-disabled={!canAsk}
              onPointerDown={(e) => { e.preventDefault(); run(1); }} onPointerEnter={() => setSel(1)}>
            <Icon name="ask" />
            <span className="cmd-text">{canAsk ? T.askAbout(term) : `Ask isn't available: ${S.ask}`}</span>
            {canAsk && <><Kbd>⌘</Kbd><Kbd>↵</Kbd></>}
          </li>
        </ul>
      )}
    </div>
  );
}
