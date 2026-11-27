// Phones: the station list with the last thing each station said, Start on the ones that are off,
// and Edit list to choose which stations this device shows.
import { useState } from "react";
import { pick, shownLang } from "../lib/lang";
import { S, post, running } from "../lib/live";
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
  const on = S.stations.filter((s) => visible(s.id) && running(s.id) && !held.has(s.id));
  const off = S.stations.filter((s) => visible(s.id) && (!running(s.id) || held.has(s.id)));
  const canEdit = S.stations.length > 1 || P.hidden.length > 0;
  return (
    <div className="phone">
      <AlertList max={3} compact />
      <section className="phone-stations" aria-labelledby="ph-st" onPointerLeave={(e) => { if (e.pointerType === "mouse") letGo(); }}>
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
              {!on.length && S.loaded && <li className="phone-empty"><NoneRunning off={off.length} /></li>}
            </ul>
            {off.length > 0 && (
              <details className="phone-off">
                <summary>{T.offCount(off.length)}</summary>
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
          {on && !stale && state.why ? ` · ${state.why}` : !on && st.out_of_reach ? ` · ${T.outOfReach}` : !on && noText(st) ? ` · ${T.noText}` : ""}
          {ago ? ` · ${T.lastText(ago)}` : ""}
        </span>
        {on && last.length > 0 && (
          <span className="phone-cap" dir="auto">
            {last.map(([text, lang], i) => <span key={i} lang={lang}>{withInaudible(text)} </span>)}
          </span>
        )}
      </button>
      {!on && S.canControl && st.available && !st.disabled && (
        <Button size="sm" variant="ghost" icon="power" className="phone-start" disabled={!S.status?.ready}
                onClick={(e) => { hold(st.id, e); post(`/api/stations/${st.id}/start`); }} aria-label={T.startStation(st.name)}>
          {T.start}
        </Button>
      )}
    </li>
  );
}

/** On a phone's transcript: the other stations, one tap away. */
export function StationStrip({ current }: { current: string }) {
  useTopics("state", "status", "conn", "alerts");
  const list = S.stations.filter((s) => visible(s.id) && (running(s.id) || s.id === current));
  const n = recentAlerts().length;
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
    </nav>
  );
}
