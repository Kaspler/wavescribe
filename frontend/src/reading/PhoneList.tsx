// Phones: the station list with the last thing each station said, Start on the ones that are off,
// and Edit list to choose which stations this device shows.
import { useState } from "react";
import { CAUGHT, caughtFeed } from "../lib/caught";
import { pick, shownLang } from "../lib/lang";
import { placeRows } from "../lib/hold";
import { S, post, running, startThere } from "../lib/live";
import { P, visible } from "../lib/prefs";
import { go, setCols } from "../lib/router";
import { noText, stateOf } from "../lib/station";
import { useTopics } from "../lib/store";
import type { Station } from "../lib/types";
import { Icon } from "../components/Icon";
import { Button, Check, Lamp, LinkButton, cx } from "../components/ui";
import { AlertList, recentAlerts } from "./Alerts";
import { lastTextAgo, spoken } from "./Column";
import { held, hold, letGo } from "./held";
import { NoneRunning, showHere } from "./Rack";
import { T } from "./strings";
import { withInaudible } from "./text";

export function PhoneList() {
  useTopics("state", "status", "prefs", "conn", "held");
  const [editing, setEditing] = useState(false);
  // A station just started keeps its row among the stopped ones for a moment (held.ts).
  const { on, off, stopped } = placeRows(S.stations.filter((s) => visible(s.id)), running,
    (id) => !!S.live[id]?.connected, (id) => held.has(id));
  const canEdit = S.stations.length > 1 || P.hidden.length > 0;
  return (
    <div className="phone">
      <AlertList max={3} compact />
      <section className="phone-stations" aria-labelledby="ph-st" onPointerMove={() => held.stir()}
               onPointerLeave={(e) => { if (e.pointerType === "mouse") letGo(); }}>
        <div className="phone-head">
          <h2 id="ph-st" className="phone-h">{editing ? T.showHere : T.stations}</h2>
          {(canEdit || editing) && (
            <Button size="sm" variant="ghost" icon={editing ? "check" : "eye"} onClick={() => setEditing(!editing)}
                    title={editing ? undefined : T.editListHint}>
              {editing ? T.done : T.editList}
            </Button>
          )}
        </div>
        {editing ? (
          <>
            <ul className="phone-edit">
              {S.stations.map((st) => (
                <li key={st.id}><Check checked={visible(st.id)} onChange={(v) => showHere(st.id, v)}><bdi>{st.name}</bdi></Check></li>
              ))}
            </ul>
            <p className="phone-empty">{T.hiddenNote}</p>
          </>
        ) : (
          <>
            <ul>
              {on.map((st) => <Row key={st.id} st={st} />)}
              {!on.length && off.length === stopped && S.loaded && <li className="phone-empty"><NoneRunning off={off.length} /></li>}
              <CaughtRow />
            </ul>
            {off.length > 0 && (
              <details className="phone-off">
                <summary>{T.offCount(stopped)}</summary>
                <ul>{off.map((st) => <Row key={st.id} st={st} />)}</ul>
              </details>
            )}
            {S.manage && S.stations.length > 0 && (
              <p className="phone-add"><LinkButton size="sm" variant="ghost" icon="plus" href="#/add">{T.addStation}</LinkButton></p>
            )}
          </>
        )}
      </section>
    </div>
  );
}

function Row({ st }: { st: Station }) {
  useTopics(`utt:${st.id}`);
  const on = running(st.id);
  const stale = S.conn !== "live";
  const state = stateOf(st.id);
  const shown = shownLang(st, P.lang);
  const last = (S.utts[st.id] || []).slice(-2).map((u) => pick(u, st, shown)).filter(([, , s]) => s !== "pending");
  const speech = on && !stale && state.lamp === "speech";
  const ago = on && !speech ? lastTextAgo(st.id) : null;
  // Out of reach, on this machine: Start opens it, where the move is pressed (as on the rail).
  const there = S.manage && !st.disabled && !!st.out_of_reach && !!st.device && st.reach_center_mhz != null;
  return (
    <li className="phone-item">
      <button type="button" className={cx("phone-row", !on && "is-off")} onClick={() => setCols([st.id])}>
        <span className="phone-row-head">
          <Lamp state={stale || !on ? "off" : state.lamp} />
          <span className="phone-name" dir="auto">{st.name}</span>
          {!speech && (
            <span className={cx("phone-state", state.lamp === "trouble" && "is-trouble")}>{stale ? T.reconnecting : on ? state.label : T.off.toLowerCase()}</span>
          )}
        </span>
        <span className="phone-sub">
          {spoken(st)}
          {!on && !st.disabled && st.out_of_reach ? ` · ${T.outOfReach}` : !on && !st.available ? ` · ${T.unavailable}` : !on && noText(st) ? ` · ${T.noText}` : ""}
          {ago ? ` · ${T.lastText(ago)}` : ""}
        </span>
        {on && last.length > 0 && (
          <span className="phone-cap" dir="auto">
            {last.map(([text, lang], i) => <span key={i} lang={lang}>{withInaudible(text)} </span>)}
          </span>
        )}
      </button>
      {!on && ((S.canControl && st.available && !st.disabled) || there) && (
        <Button size="sm" variant="ghost" icon="power" className="phone-start" disabled={!S.status?.ready}
                onClick={() => {
                  hold(st.id);
                  if (!there) post(`/api/stations/${st.id}/start`);
                  else { startThere(st.id); setCols([st.id]); }
                }} aria-label={T.startStation(st.name)}>
          {T.start}
        </Button>
      )}
    </li>
  );
}

/** Caught on the air (lib/caught.ts), while it has lines: its last two, as a station's row has. */
function CaughtRow() {
  useTopics(`utt:${CAUGHT}`, "prefs");
  const lines = S.utts[CAUGHT] || [];
  if (!lines.length) return null;
  const st = caughtFeed(T.caught, P.lang);
  const shown = shownLang(st, P.lang);
  const last = lines.slice(-2).map((u) => pick(u, st, shown)).filter(([, , s]) => s !== "pending");
  const ago = lastTextAgo(CAUGHT);
  return (
    <li className="phone-item">
      <button type="button" className="phone-row" onClick={() => setCols([CAUGHT])}>
        <span className="phone-row-head">
          <Icon name="antenna" size={12} className="col-mark" />
          <span className="phone-name">{st.name}</span>
        </span>
        <span className="phone-sub">{T.caughtSub}{ago ? ` · ${T.lastText(ago)}` : ""}</span>
        {last.length > 0 && (
          <span className="phone-cap" dir="auto">
            {last.map(([text, lang], i) => <span key={i} lang={lang}>{withInaudible(text)} </span>)}
          </span>
        )}
      </button>
    </li>
  );
}

/** On a phone's transcript: the other stations, one tap away. */
export function StationStrip({ current }: { current: string }) {
  useTopics("state", "status", "conn", "alerts", `utt:${CAUGHT}`);
  const list = S.stations.filter((s) => visible(s.id) && (running(s.id) || s.id === current));
  const n = recentAlerts().length;
  const caught = current === CAUGHT || !!S.utts[CAUGHT]?.length;
  return (
    <nav className="strip" aria-label={T.stations}>
      <button type="button" className="strip-home" onClick={() => go("#/")} aria-label={`All stations${n ? ` and ${n} recent alerts` : ""}`}>
        <Icon name="back" size={14} />
        {n > 0 && <span className="count num">{n}</span>}
      </button>
      {list.map((st) => (
        <button key={st.id} type="button" aria-current={st.id === current ? "true" : undefined} onClick={() => setCols([st.id])}>
          <Lamp state={S.conn !== "live" ? "off" : stateOf(st.id).lamp} />
          {st.name}
        </button>
      ))}
      {caught && (
        <button type="button" aria-current={current === CAUGHT ? "true" : undefined} onClick={() => setCols([CAUGHT])}>
          <Icon name="antenna" size={12} className="col-mark" />
          {T.caught}
        </button>
      )}
    </nav>
  );
}
