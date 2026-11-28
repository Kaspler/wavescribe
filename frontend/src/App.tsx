import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { wanted } from "./lib/auth";
import { S, loadState, running, setNotice, switchOnAndStart } from "./lib/live";
import { visible } from "./lib/prefs";
import { MAX_COLS, go, movedHash, parse, setCols, useHash } from "./lib/router";
import { useTopics } from "./lib/store";
import { V, railShown, toggleRail } from "./lib/view";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { Icon } from "./components/Icon";
import { Button, Kbd, Toasts } from "./components/ui";
import { AlertAnnouncer, AlertsRail, openAlert } from "./reading/Alerts";
import { Header } from "./reading/Header";
import { Rack } from "./reading/Rack";
import { Reading } from "./reading/Reading";
import { T } from "./reading/strings";
import { TokenGate } from "./reading/TokenGate";

const Manage = lazy(() => import("./manage/Manage"));

export function App() {
  useTopics("prefs", "narrow", "state", "auth");
  const hash = useHash();
  const route = parse(hash);
  useEffect(() => {
    const moved = movedHash(hash);
    if (moved) location.replace(moved); // an old link: the address follows to the new page
  }, [hash]);
  // A link to an alert, once the page knows its stations.
  const alertId = route.kind === "alert" ? route.id : null;
  useEffect(() => {
    if (alertId != null && S.loaded) openAlert(alertId);
  }, [alertId, S.loaded]);
  // Listen, Find stations and Settings answer only on the Wavescribe machine; elsewhere they read
  // as the start.
  const elsewhere = route.kind === "listen" || route.kind === "add" || route.kind === "settings";
  const area = elsewhere && S.manage ? route : null;
  // Back from Settings: what was changed there (a rule, a language) is in what the page loads.
  const was = useRef(area?.kind);
  useEffect(() => {
    if (was.current === "settings" && !area) loadState().catch(() => {});
    was.current = area?.kind;
  }, [area?.kind]);
  const [keys, setKeys] = useState(false);
  useShortcuts(elsewhere, setKeys);
  const rail = !area && railShown();
  const docked = rail && V.wide;
  return (
    <div className={`app${area ? " is-manage" : ""}${docked ? " has-rail" : ""}`}>
      <a className="skip" href="#main" onClick={skipToMain}>{area ? "Skip to the page" : "Skip to the text"}</a>
      {/* Each part fails on its own: a broken header, station list or alerts panel shows its own
          notice and leaves the rest of the page working. */}
      <ErrorBoundary what="header">
        <Header area={area?.kind ?? null} />
      </ErrorBoundary>
      {wanted() ? <TokenGate /> : <Notice />}
      {wanted() ? null : area ? (
        <ErrorBoundary what={area.kind === "listen" ? "Listen page" : area.kind === "add" ? "Find stations page" : "settings"}>
          <Suspense fallback={<main className="manage-loading" id="main" tabIndex={-1}>Loading…</main>}>
            <Manage area={area.kind} page={area.page} />
          </Suspense>
        </ErrorBoundary>
      ) : (
        <>
          <div className={`work${V.narrow ? "" : " has-rack"}`}>
            {!V.narrow && <ErrorBoundary what="station list"><Rack /></ErrorBoundary>}
            <ErrorBoundary what="reading view">
              <Reading route={elsewhere || route.kind === "alert" ? { kind: "live" } : route} />
            </ErrorBoundary>
            {rail && <ErrorBoundary what="alerts panel"><AlertsRail overlay={!docked} /></ErrorBoundary>}
          </div>
        </>
      )}
      {keys && <Shortcuts onClose={() => setKeys(false)} />}
      <AlertAnnouncer />
      <Toasts />
    </div>
  );
}

/** The hash is the router's: following "#main" would leave the current view. */
function skipToMain(e: React.MouseEvent) {
  e.preventDefault();
  document.getElementById("main")?.focus();
}

function Notice() {
  useTopics("notice", "conn");
  const lost = S.conn === "reconnecting" || S.conn === "blocked";
  const text = S.conn === "blocked" ? T.tooManyTabs : lost ? T.connectionLost : S.notice;
  const off = lost ? null : S.switchOn;
  if (!text) return null;
  return (
    <div className={`notice${lost ? " is-lost" : ""}`} role="status">
      <Icon name={S.conn === "reconnecting" ? "refresh" : "info"} />
      <span>{text}</span>
      {off && S.manage && <Button size="sm" variant="secondary" icon="power" onClick={() => switchOnAndStart(off)}>{T.switchOn}</Button>}
      {!lost && <Button size="sm" variant="ghost" icon="close" aria-label="Dismiss" onClick={() => setNotice("")} />}
    </div>
  );
}

/** 1–9 read a station · Shift+1–9 add or take away its column · 0 all live · / search · A alerts ·
 * Esc back · ? this list */
function useShortcuts(managing: boolean, setKeys: (v: boolean) => void) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable], [role=dialog], [role=menu]")) return;
      if (e.key === "?") { setKeys(true); return; }
      if (e.key === "Escape") {
        if (railShown() && !V.wide) return toggleRail(false);
        const r = parse(location.hash);
        if (r.kind !== "live" && r.kind !== "cols") go("#/");
        return;
      }
      if (managing) return;
      // e.code, not e.key: Shift+1 types "!" on most layouts, and some layouts need Shift for digits
      const digit = /^(?:Digit|Numpad)([0-9])$/.exec(e.code)?.[1];
      if (digit === "0") go("#/");
      else if (digit) {
        const on = S.stations.filter((s) => visible(s.id) && running(s.id));
        const st = on[Number(digit) - 1];
        if (!st) return;
        if (!e.shiftKey || V.narrow) setCols([st.id]);
        else setCols(V.shown.includes(st.id) ? V.shown.filter((x) => x !== st.id) : [...V.shown, st.id].slice(-MAX_COLS));
      } else if (e.key === "a" || e.key === "A") toggleRail();
    };
    addEventListener("keydown", onKey);
    return () => removeEventListener("keydown", onKey);
  }, [managing, setKeys]);
}

function Shortcuts({ onClose }: { onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null; // focus goes back there on close
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" || e.key === "?") { e.preventDefault(); close.current(); return; }
      if (e.key !== "Tab" || !box.current) return;
      // A modal keeps Tab inside itself.
      const f = [...box.current.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")];
      if (!f.length) return;
      const i = f.indexOf(document.activeElement as HTMLElement);
      const next = e.shiftKey ? (i <= 0 ? f.length - 1 : i - 1) : (i + 1) % f.length;
      e.preventDefault();
      f[next].focus();
    };
    addEventListener("keydown", onKey);
    return () => { removeEventListener("keydown", onKey); before?.focus?.(); };
  }, []); // once per opening: a re-render mustn't hand focus back early
  const rows: [React.ReactNode, string][] = [
    [<Kbd>/</Kbd>, "Search or ask"],
    [<><Kbd>1</Kbd>–<Kbd>9</Kbd></>, "Read a live station, in the list's order"],
    [<><Kbd>⇧</Kbd><Kbd>1</Kbd>–<Kbd>9</Kbd></>, "Add it as a further column, or take its column away"],
    [<Kbd>0</Kbd>, "All live stations"],
    [<Kbd>A</Kbd>, "Show or hide the alerts"],
    [<Kbd>Esc</Kbd>, "Close the alerts, search, Ask, Listen, Find or Settings"],
    [<Kbd>?</Kbd>, "This list"],
  ];
  // Listen's tuner takes these while a receiver is tuned by hand (manage/tuner.tsx, useTuningKeys).
  const tuning: [React.ReactNode, string][] = [
    [<><Kbd>↑</Kbd><Kbd>→</Kbd></>, "Step the frequency up"],
    [<><Kbd>↓</Kbd><Kbd>←</Kbd></>, "Step it down"],
    [<><Kbd>⇧</Kbd> with them</>, "Ten steps at once"],
    [<><Kbd>Page Up</Kbd><Kbd>Page Down</Kbd></>, "Next or previous channel, else ten steps"],
  ];
  return (
    <div className="scrim" onPointerDown={onClose}>
      <div ref={box} className="pop keys" role="dialog" aria-modal="true" aria-labelledby="keys-h" onPointerDown={(e) => e.stopPropagation()}>
        <h2 id="keys-h">{T.shortcuts}</h2>
        <dl>{rows.map(([k, v], i) => <div key={i}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
        <h3>Listen, tuning by hand</h3>
        <dl>{tuning.map(([k, v], i) => <div key={i}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
        <Button variant="secondary" size="sm" onClick={onClose} autoFocus>Close</Button>
      </div>
    </div>
  );
}
