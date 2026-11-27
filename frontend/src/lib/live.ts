// The page's live data: /api/state once per connection, then /api/events, shared by the tabs
// open in this browser (tabs.ts).
import { takeAlert } from "./alerts";
import { fetchRead, onWanted, streamWantsToken, ticketed, wanted } from "./auth";
import { sameLang } from "./lang";
import { P, setPrefs } from "./prefs";
import { switchOnFor } from "./startRefusal";
import { emit } from "./store";
import { type Conn, type Env, type Shared, type Sink, share } from "./tabs";
import type { Alert, Live, PageState, Station, Status, Utterance } from "./types";

const MAX_ALERTS = 500;

const MAX_PER_STATION = 600; // utterances kept per station, so a page left open for days stays light
const HARD_MAX_PER_STATION = 3000; // even while someone reads old text, never more than this
const FRESH_MS = 2400;

export type { Conn };

export const S = {
  loaded: false,
  conn: "connecting" as Conn,
  stations: [] as Station[],
  byId: {} as Record<string, Station>,
  live: {} as Record<string, Live>,
  status: null as Status | null,
  canControl: false,
  audioRate: 16000,
  hasStore: false,
  manage: false,
  ask: null as string | null,
  askCode: null as string | null,
  digest: null as string | null,
  mtModel: null as string | null,
  noncommercial: [] as string[],
  node: { name: "", url: "" },
  utts: {} as Record<string, Utterance[]>,
  index: new Map<string, Utterance>(),
  alerts: new Map<number, Alert>(),
  freshAlert: null as number | null,
  /** Counts the alerts that rang (new, or more serious), so the same alert can ring again. */
  rang: 0,
  fresh: new Set<string>(), // utterance keys that just arrived, for a brief highlight
  reading: new Set<string>(), // stations whose column is scrolled back: don't trim them
  notice: "" as string, // one line of trouble shown under the header
  /** The station whose Start the notice says was refused because it is switched off on the page
   * (code switched_off): the notice offers to switch it on. */
  switchOn: null as string | null,
  /** A station whose Start was pressed in a station list while its receiver is tuned where it
   * doesn't hear it (`startThere`). */
  startThere: null as string | null,
};

export const running = (id: string) => !!S.live[id] && !S.live[id].stopping;

export function setNotice(text: string, switchOn: string | null = null) {
  S.notice = text;
  S.switchOn = switchOn;
  emit("notice");
}

/** POSTs a station action; shows the server's reason when it refuses. True when it was done. */
export async function post(url: string, body?: unknown): Promise<boolean> {
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "X-Wavescribe": "1", ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({}));
      setNotice(data.error || r.statusText, switchOnFor(url, data));
    }
    return r.ok;
  } catch {
    setNotice("Couldn't reach Wavescribe: is it still running?");
    return false;
  }
}

/** The language a first visit reads in: the reader's own, if a station is translated into it,
 * else the most common first translation target. Null while no station is translated at all
 * (before a translator is downloaded): a choice saved then would stick as "original language". */
function firstLanguage(stations: Station[]): string | null {
  const targets = stations.flatMap((s) => s.translate_to);
  if (!targets.length) return null;
  for (const want of navigator.languages || []) {
    const hit = targets.find((t) => sameLang(t, want));
    if (hit) return hit;
  }
  const firsts = stations.map((s) => s.translate_to[0]).filter(Boolean);
  const count = (x: string) => firsts.filter((y) => y === x).length;
  return [...firsts].sort((a, b) => count(b) - count(a))[0] || null;
}

export async function loadState() {
  const r = await fetchRead("/api/state");
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  const state: PageState = await r.json();
  S.stations = state.stations;
  S.byId = Object.fromEntries(state.stations.map((s) => [s.id, s]));
  S.canControl = state.can_control;
  S.audioRate = state.audio_rate;
  S.hasStore = !!state.store;
  S.manage = !!state.manage;
  S.ask = state.ask;
  S.askCode = state.ask_code ?? null;
  S.digest = state.digest ?? null;
  S.mtModel = state.mt_model;
  S.noncommercial = state.noncommercial || [];
  S.node = state.node;
  if (P.lang === null) {
    const lang = firstLanguage(state.stations);
    if (lang) setPrefs({ lang });
  }
  // Stored alerts don't say what changed lately; what this page heard of that is kept.
  S.alerts = new Map((state.alerts || []).map((a) => [a.id, { ...a, lately: S.alerts.get(a.id)?.lately }]));
  S.utts = {};
  S.index.clear();
  for (const u of state.recent) {
    S.index.set(u.key, u);
    (S.utts[u.station] ||= []).push(u);
  }
  S.loaded = true;
  heldBack = false;
  showConn();
  applyStatus(state.status);
  emit("state", "alerts", ...state.stations.map((s) => `utt:${s.id}`));
}

const LOADING = "Loading models… stations start once they're ready.";
let lastReady: boolean | null = null;
let seenRev: unknown = undefined;
const lookedUp = new Set<string>();
function applyStatus(status: Status) {
  S.status = status;
  S.live = status.stations || {};
  // Stations added or removed elsewhere (another tab, a phone, the command line) reach the page
  // without a reload: by the server's count of station changes where it sends one, else by a
  // running station this page doesn't know, looked up once.
  if (S.loaded) {
    const rev = status.stations_rev;
    const unknown = Object.keys(S.live).filter((id) => !Object.hasOwn(S.byId, id) && !lookedUp.has(id));
    if ((rev !== undefined && seenRev !== undefined && rev !== seenRev) || unknown.length) {
      unknown.forEach((id) => lookedUp.add(id));
      loadState().catch(() => {});
    }
    seenRev = rev;
  }
  if (status.ready !== lastReady) {
    lastReady = status.ready;
    if (!status.ready) setNotice(LOADING);
    else if (S.notice === LOADING) setNotice("");
  }
  emit("status");
}

function upsert(u: Utterance) {
  const prev = S.index.get(u.key);
  S.index.set(u.key, u);
  const list = (S.utts[u.station] ||= []);
  if (prev) list[list.indexOf(prev)] = u;
  else list.push(u);
  // New text, and a translation that just replaced its placeholder, fade in once.
  if (!prev || prev.pending.length > u.pending.length) {
    S.fresh.add(u.key);
    setTimeout(() => S.fresh.delete(u.key), FRESH_MS);
  }
  // Trim old text only while nobody is reading it (or past a hard cap).
  if (list.length > HARD_MAX_PER_STATION || (list.length > MAX_PER_STATION && !S.reading.has(u.station))) {
    list.splice(0, list.length - MAX_PER_STATION + 50).forEach((old) => S.index.delete(old.key));
  }
  emit(`utt:${u.station}`, "utt");
}

function upsertAlert(a: Alert) {
  const { alert, rings } = takeAlert(S.alerts.get(a.id), a);
  S.alerts.set(a.id, alert);
  if (rings) {
    S.freshAlert = a.id;
    S.rang++;
  }
  if (S.alerts.size > MAX_ALERTS) {
    // a page left open for weeks: keep the newest (ids grow in arrival order)
    const ids = [...S.alerts.keys()].sort((x, y) => x - y);
    ids.slice(0, ids.length - MAX_ALERTS).forEach((id) => S.alerts.delete(id));
  }
  emit("alerts");
}

/** Older text for one station, from the store; returns how many arrived. A failed request throws,
 * so it isn't mistaken for the start of the history. */
export async function loadEarlier(id: string): Promise<number> {
  const list = S.utts[id] || [];
  const oldest = list.find((u) => u.id != null);
  if (!oldest) return 0;
  const r = await fetchRead(`/api/history?station=${encodeURIComponent(id)}&before_id=${oldest.id}`);
  if (!r.ok) throw new Error(`the server answered ${r.status}`);
  const older: Utterance[] = await r.json();
  if (!older.length) return 0;
  older.forEach((u) => S.index.set(u.key, u));
  S.utts[id] = [...older, ...list];
  emit(`utt:${id}`);
  return older.length;
}

// A browser holds back a request it has no free connection for, without an error; this long
// without an answer means that is what happened.
const STUCK_MS = 10000;

const RETRY_MS = 3000;

/** The events stream, reopened after the browser gives up on it. */
function openStream(sink: Sink) {
  ticketed("/api/events").then((url) => stream(sink, url), () => {
    sink.conn("reconnecting");
    setTimeout(() => openStream(sink), RETRY_MS);
  });
}

function stream(sink: Sink, url: string) {
  const es = new EventSource(url);
  // A ticket works once: the browser reconnecting with the same address would be refused.
  const spent = url.includes("ticket=");
  let stuck = 0;
  const arm = () => {
    clearTimeout(stuck);
    stuck = window.setTimeout(() => { if (es.readyState === EventSource.CONNECTING) sink.conn("blocked"); }, STUCK_MS);
  };
  arm();
  es.onopen = () => {
    clearTimeout(stuck);
    sink.conn("live");
    sink.opened();
  };
  es.onerror = () => {
    sink.conn("reconnecting");
    if (es.readyState === EventSource.CLOSED || spent) {
      // the browser gave up (the server answered with an error), or can't reconnect with a spent
      // ticket: try again ourselves, unless reading now needs a token
      clearTimeout(stuck);
      es.close();
      streamWantsToken().then((wants) => { if (!wants) setTimeout(() => openStream(sink), RETRY_MS); });
    } else arm();
  };
  for (const name of ["utterance", "status", "alert"])
    es.addEventListener(name, (e) => sink.event(name, (e as MessageEvent).data));
}

const LOCK = "wavescribe-events";

/** Locks exist only where the page is served securely (localhost counts); elsewhere, every tab
 * keeps its own stream. */
function browserEnv(): Env {
  const locks = navigator.locks;
  let bc: BroadcastChannel | null = null;
  try {
    bc = new BroadcastChannel(LOCK);
  } catch {
    bc = null;
  }
  const holdForGood = () => new Promise<void>(() => {});
  return {
    acquire: locks
      ? (lead, follow) => {
          locks.request(LOCK, { ifAvailable: true }, (lock) => {
            if (lock) {
              lead();
              return holdForGood();
            }
            follow();
            locks.request(LOCK, () => { lead(); return holdForGood(); });
            return undefined;
          });
        }
      : null,
    channel: bc && {
      post: (m) => bc.postMessage(m),
      listen: (f) => { bc.onmessage = (e) => f(e.data); },
    },
    open: openStream,
  };
}

// What the header shows: the stream's state, unless this tab's own requests are held back.
let streamConn: Conn = "connecting";
let heldBack = false;
function showConn() {
  const c = heldBack ? "blocked" : streamConn;
  if (c !== S.conn) {
    S.conn = c;
    emit("conn");
  }
}

let shared: Shared | null = null;

/** The lines around one stored segment, for showing a line that is no longer loaded; null where
 * the server doesn't serve them (an older one ignores around_id and sends the newest instead). */
export async function linesAround(id: string, seg: number, limit = 60): Promise<Utterance[] | null> {
  const r = await fetchRead(`/api/history?station=${encodeURIComponent(id)}&around_id=${seg}&limit=${limit}`);
  if (!r.ok) return null;
  const list: Utterance[] = await r.json();
  return Array.isArray(list) && list.some((u) => u.id === seg) ? list : null;
}

export function connect() {
  onWanted(() => emit("auth"));
  const reload = () => loadState().catch((e) => { if (!wanted()) setNotice(`Couldn't load the stations: ${e.message}`); });
  shared = share(browserEnv(), {
    event(name, data) {
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        return; // a malformed event: skip it, keep the stream
      }
      if (name === "utterance") upsert(parsed);
      else if (name === "status") applyStatus(parsed);
      else if (name === "alert") upsertAlert(parsed);
    },
    conn(c) {
      streamConn = c;
      showConn();
    },
    opened: reload,
    stations: reload,
  });
  addEventListener("pagehide", () => shared?.leave());
  setTimeout(() => {
    if (!S.loaded && !wanted() && streamConn !== "reconnecting") {
      heldBack = true;
      showConn();
    }
  }, STUCK_MS);
}

/** Starts a station in its own mode: one started only to play is written down from now on. */
export async function startStation(id: string) {
  if (await post(`/api/stations/${id}/start`)) await stationsChanged().catch(() => {});
}

/** "Switch it on", where Start was refused because the station is switched off on the page: on,
 * then started, as Start was pressed for. */
export async function switchOnAndStart(id: string) {
  setNotice("");
  if (await post(`/api/manage/stations/${encodeURIComponent(id)}`, { disabled: null })) await startStation(id);
}

/** Start, pressed in a station list on a station its receiver doesn't hear where it is tuned:
 * the move that brings it back (MoveThere, in its column, which the caller opens) is pressed
 * there, where the server's question fits when the move leaves other stations out. Only this
 * machine's page moves a receiver: the reading route refuses such a start. */
export function startThere(id: string) {
  S.startThere = id;
  emit("startThere");
}

/** After this tab changed the stations: reloads them here and in the other tabs. */
export function stationsChanged() {
  shared?.changed();
  return loadState();
}
