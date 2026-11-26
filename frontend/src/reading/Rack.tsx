// The station list: every station as a channel on the receiver's rack, with its lamp, its level on
// a printed scale, Start for the ones that are off and a menu for the rest. Clicking a station tunes
// its column in or out; the orange pointer marks the stations on screen. Playing them is Listen's
// job. "Edit list" chooses which stations this device shows.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { A } from "../lib/audio";
import { langNameEn, pick, shownLang } from "../lib/lang";
import { S, post, running } from "../lib/live";
import { P, setPrefs, visible } from "../lib/prefs";
import { MAX_COLS, go, parse, setCols, useHash } from "../lib/router";
import { levelOf, noText, stateOf } from "../lib/station";
import { useTopics } from "../lib/store";
import type { Station } from "../lib/types";
import { V } from "../lib/view";
import { Icon } from "../components/Icon";
import { Lamp, cx } from "../components/ui";
import { lastTextAgo } from "./Column";
import { held, hold, letGo } from "./held";
import { StationMenu } from "./StationMenu";
import { T } from "./strings";
import { withInaudible } from "./text";

// Whether the off list is open is this browser's choice, remembered.
const OFF_KEY = "wavescribe.rack.off";
function offOpen(): boolean {
  try { return localStorage.getItem(OFF_KEY) !== "closed"; } catch { return true; }
}

function toggle(id: string, solo: boolean) {
  if (solo) return setCols([id]);
  const now = V.shown;
  if (now.includes(id)) setCols(now.filter((x) => x !== id));
  else setCols([...now, id].slice(-MAX_COLS));
}

/** Shows or hides a station on this device; it keeps running either way. */
export function showHere(id: string, show: boolean) {
  setPrefs({ hidden: show ? P.hidden.filter((x) => x !== id) : [...P.hidden, id] });
}

/** What the station list says when no station is running. */
export function NoneRunning({ off }: { off: number }) {
  if (!S.stations.length) {
    return S.manage
      ? <>{T.noStationsYet} <a href="#/add">{T.findStations}</a> {T.findStationsWhere}</>
      : <>{T.noStationsReader}</>;
  }
  if (!off) return <>{T.allHidden}</>;
  return <>{S.canControl ? T.noneRunning : T.noStationsReader}</>;
}

export function Rack() {
  useTopics("state", "status", "prefs", "shown", "conn", "audio", "held");
  const route = parse(useHash());
  const [editing, setEditing] = useState(false);
  const on = S.stations.filter((s) => visible(s.id) && running(s.id) && !held.has(s.id));
  const off = S.stations.filter((s) => visible(s.id) && (!running(s.id) || held.has(s.id)));
  const stale = S.conn !== "live";
  const canEdit = S.stations.length > 1 || P.hidden.length > 0;
  if (editing) {
    return (
      <nav className="rack" aria-label={T.stations}>
        <EditList done={() => setEditing(false)} />
      </nav>
    );
  }
  return (
    <nav className={cx("rack", stale && "is-stale")} aria-label={T.stations}
         onPointerLeave={(e) => { if (e.pointerType === "mouse") letGo(); }}>
      <button type="button" className="rack-all" aria-pressed={route.kind === "live"} title={T.allLiveHint}
              onClick={() => go("#/")} aria-keyshortcuts="0" aria-label={`${T.allLive}, ${on.length} running`}>
        <Icon name="columns" />
        <span>{T.allLive}</span>
        <span className="rack-count num" aria-hidden="true">{on.length}</span>
      </button>
      <ol className="rack-list">
        {on.map((st, i) => <Channel key={st.id} st={st} n={i + 1} shown={V.shown.includes(st.id)} stale={stale} />)}
        {!on.length && S.loaded && <li className="rack-empty"><NoneRunning off={off.length} /></li>}
      </ol>
      {off.length > 0 && <OffStations off={off} />}
      {(S.manage || canEdit) && (
        <div className="rack-foot">
          {S.manage && (
            <a className="rack-foot-btn" href="#/add" title={T.findHint}>
              <Icon name="plus" />
              {T.addStation}
            </a>
          )}
          {canEdit && (
            <button type="button" className="rack-foot-btn" onClick={() => setEditing(true)} title={T.editListHint}>
              <Icon name="eye" />
              {T.editList}
            </button>
          )}
        </div>
      )}
    </nav>
  );
}

/** Which stations this device shows. Hidden ones keep running for everyone else. */
function EditList({ done }: { done: () => void }) {
  const first = useRef<HTMLButtonElement>(null);
  useEffect(() => { first.current?.focus(); }, []);
  return (
    <>
      <div className="rack-edit-head">
        <p id="rack-edit-h">{T.showHere}</p>
        <button type="button" className="rack-foot-btn" onClick={done}>
          <Icon name="check" />
          {T.done}
        </button>
      </div>
      <ul className="rack-list rack-edit" aria-labelledby="rack-edit-h">
        {S.stations.map((st, i) => {
          const on = visible(st.id);
          return (
            <li key={st.id}>
              <button ref={i === 0 ? first : undefined} type="button" className={cx("rack-edit-row", !on && "is-hidden")}
                      aria-pressed={on} onClick={() => showHere(st.id, !on)}>
                <Icon name={on ? "eye" : "eyeOff"} />
                <span className="ch-title" dir="auto">{st.name}</span>
                <span className="rack-edit-state">{on ? T.shown : T.hidden}</span>
              </button>
            </li>
          );
        })}
      </ul>
      <p className="rack-empty">{T.hiddenNote}</p>
    </>
  );
}

function Channel({ st, n, shown, stale }: { st: Station; n: number; shown: boolean; stale: boolean }) {
  const state = stateOf(st.id);
  const level = stale ? 0 : levelOf(st.id);
  const playing = A.playing === st.id;
  // A lit lamp already says "speech"; the word appears only for the states a lamp can't name.
  const word = stale ? T.reconnecting : state.lamp === "speech" ? null : state.label;
  const ref = useRef<HTMLButtonElement>(null);
  const [peek, setPeek] = useState(false);
  const timer = useRef(0);
  const show = () => { clearTimeout(timer.current); timer.current = window.setTimeout(() => setPeek(true), 380); };
  const hide = () => { clearTimeout(timer.current); setPeek(false); };
  useEffect(() => () => clearTimeout(timer.current), []);
  return (
    <li className={cx("ch", shown && "is-shown", playing && "is-playing")} onPointerEnter={show} onPointerLeave={hide}>
      <button ref={ref} type="button" className="ch-main" aria-pressed={shown} onFocus={show} onBlur={hide}
              onClick={(e) => { hide(); toggle(st.id, e.altKey || e.metaKey || e.shiftKey); }}
              onDoubleClick={() => toggle(st.id, true)}
              aria-keyshortcuts={n <= 9 ? String(n) : undefined}
              aria-label={`${st.name}, ${langNameEn(st.language)}, ${stale ? "unknown" : state.label}${shown ? ", shown" : ""}`}>
        <span className="ch-name">
          <Lamp state={stale ? "off" : state.lamp} onBand />
          <span className="ch-title" dir="auto">{st.name}</span>
        </span>
        <span className="ch-sub">
          <span>{langNameEn(st.language)}</span>
          {word && <span className={cx(state.lamp === "trouble" && "is-trouble")} title={state.detail || undefined}>{word}</span>}
        </span>
        <span className="ticks" style={{ "--lv": level.toFixed(3) } as React.CSSProperties} aria-hidden="true">
          <span className="ticks-lit" />
        </span>
      </button>
      <span className="ch-actions">
        {playing && <span className="ch-playing" title={T.playingNow}><Icon name="speaker" /><span className="sr-only">{T.playingNow}</span></span>}
        <StationMenu st={st} side />
      </span>
      {peek && !shown && <Peek st={st} anchor={ref} />}
    </li>
  );
}

/** What a station not on screen is saying now, beside its row. */
function Peek({ st, anchor }: { st: Station; anchor: React.RefObject<HTMLButtonElement | null> }) {
  useTopics(`utt:${st.id}`);
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: -9999, top: 0 });
  useLayoutEffect(() => {
    const r = anchor.current?.closest(".ch")?.getBoundingClientRect();
    const h = box.current?.offsetHeight ?? 120;
    if (r) setPos({ left: r.right + 8, top: Math.max(8, Math.min(innerHeight - h - 8, r.top)) });
  }, [anchor]);
  const shown = shownLang(st, P.lang);
  const lines = (S.utts[st.id] || []).slice(-3).map((u) => pick(u, st, shown)).filter(([, , s]) => s !== "pending");
  const last = lastTextAgo(st.id);
  return (
    <div ref={box} className="pop peek" style={{ left: pos.left, top: pos.top }} role="tooltip">
      {lines.length ? (
        <p className="peek-text" dir="auto">
          {lines.map(([text, lang], i) => <span key={i} lang={lang}>{withInaudible(text)} </span>)}
        </p>
      ) : (
        <p className="peek-text is-empty">{noText(st) ? T.noTextYet : st.transcribe === false ? T.listenOnly : T.waiting}</p>
      )}
      <p className="peek-meta">{last ? `${T.lastText(last)} · ` : ""}{T.peekHint}</p>
    </div>
  );
}

function OffStations({ off }: { off: Station[] }) {
  const [open, setOpenState] = useState(offOpen);
  const setOpen = (v: boolean) => {
    setOpenState(v);
    try { localStorage.setItem(OFF_KEY, v ? "open" : "closed"); } catch { /* private mode: not remembered */ }
  };
  return (
    <section className="rack-off" aria-label={T.notRunning}>
      <button type="button" className="rack-head" aria-expanded={open} onClick={() => setOpen(!open)}
              aria-label={`${T.notRunning}: ${off.length}`}>
        <Icon name="right" className="rack-chev" />
        {T.off}
        <span className="rack-count num" aria-hidden="true">{off.length}</span>
      </button>
      {open && (
        <ol className="rack-list">
          {off.map((st) => {
            const shown = V.shown.includes(st.id);
            const startable = S.canControl && st.available && !st.disabled;
            const started = running(st.id); // held in place after a start from here
            const state = started ? stateOf(st.id) : null;
            const word = !st.available ? T.unavailable : state ? state.label : noText(st) ? T.noText : null;
            return (
              <li key={st.id} className={cx("ch", "ch-off", shown && "is-shown")}>
                <button type="button" className="ch-main" aria-pressed={shown}
                        onClick={(e) => toggle(st.id, e.altKey || e.metaKey || e.shiftKey)}
                        title={st.available ? undefined : st.reason || T.unavailable}
                        aria-label={`${st.name}, ${langNameEn(st.language)}, ${state ? state.label : st.available ? T.off.toLowerCase() : T.unavailable}${shown ? ", shown" : ""}`}>
                  <span className="ch-name">
                    <Lamp state={state ? state.lamp : "off"} onBand />
                    <span className="ch-title" dir="auto">{st.name}</span>
                  </span>
                  <span className="ch-sub">
                    <span>{langNameEn(st.language)}</span>
                    {word && <span>{word}</span>}
                  </span>
                </button>
                {startable && !started && (
                  <span className="ch-actions">
                    <button type="button" className="btn btn-band btn-sm ch-btn ch-start" disabled={!S.status?.ready}
                            onClick={(e) => { hold(st.id, e); post(`/api/stations/${st.id}/start`); }} aria-label={T.startStation(st.name)}>
                      <Icon name="power" />
                      {T.start}
                    </button>
                  </span>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
