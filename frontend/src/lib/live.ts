// The page's live data: /api/state once per connection, then /api/events.
import { sameLang } from "./lang";
import { P, setPrefs } from "./prefs";
import { emit } from "./store";
import type { Alert, Live, PageState, Station, Status, Utterance } from "./types";

const MAX_ALERTS = 500;

const MAX_PER_STATION = 600; // utterances kept per station, so a page left open for days stays light
const HARD_MAX_PER_STATION = 3000; // even while someone reads old text, never more than this
const FRESH_MS = 2400;

export type Conn = "connecting" | "live" | "reconnecting";

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
  mtModel: null as string | null,
  noncommercial: [] as string[],
  node: { name: "", url: "" },
  utts: {} as Record<string, Utterance[]>,
  index: new Map<string, Utterance>(),
  alerts: new Map<number, Alert>(),
  freshAlert: null as number | null,
  fresh: new Set<string>(), // utterance keys that just arrived, for a brief highlight
  reading: new Set<string>(), // stations whose column is scrolled back: don't trim them
  notice: "" as string, // one line of trouble shown under the header
};

export const running = (id: string) => !!S.live[id] && !S.live[id].stopping;

export function setNotice(text: string) {
  S.notice = text;
  emit("notice");
}

/** POSTs a station action; shows the server's reason when it refuses. True when it was done. */
export async function post(url: string): Promise<boolean> {
  try {
    const r = await fetch(url, { method: "POST", headers: { "X-Wavescribe": "1" } });
    if (!r.ok) setNotice((await r.json().catch(() => ({}))).error || r.statusText);
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
  const r = await fetch("/api/state");
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  const state: PageState = await r.json();
  S.stations = state.stations;
  S.byId = Object.fromEntries(state.stations.map((s) => [s.id, s]));
  S.canControl = state.can_control;
  S.audioRate = state.audio_rate;
  S.hasStore = !!state.store;
  S.manage = !!state.manage;
  S.ask = state.ask;
  S.mtModel = state.mt_model;
  S.noncommercial = state.noncommercial || [];
  S.node = state.node;
  if (P.lang === null) {
    const lang = firstLanguage(state.stations);
    if (lang) setPrefs({ lang });
  }
  S.alerts = new Map((state.alerts || []).map((a) => [a.id, a]));
  S.utts = {};
  S.index.clear();
  for (const u of state.recent) {
    S.index.set(u.key, u);
    (S.utts[u.station] ||= []).push(u);
  }
  S.loaded = true;
  applyStatus(state.status);
  emit("state", "alerts", ...state.stations.map((s) => `utt:${s.id}`));
}

const LOADING = "Loading models… stations start once they're ready.";
let lastReady: boolean | null = null;
function applyStatus(status: Status) {
  S.status = status;
  S.live = status.stations || {};
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
  const isNew = !S.alerts.has(a.id);
  S.alerts.set(a.id, a);
  if (isNew) S.freshAlert = a.id;
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
  const r = await fetch(`/api/history?station=${encodeURIComponent(id)}&before_id=${oldest.id}`);
  if (!r.ok) throw new Error(`the server answered ${r.status}`);
  const older: Utterance[] = await r.json();
  if (!older.length) return 0;
  older.forEach((u) => S.index.set(u.key, u));
  S.utts[id] = [...older, ...list];
  emit(`utt:${id}`);
  return older.length;
}

export function connect() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    S.conn = "live";
    emit("conn");
    loadState().catch((e) => setNotice(`Couldn't load the stations: ${e.message}`));
  };
  es.onerror = () => {
    S.conn = "reconnecting";
    emit("conn");
    if (es.readyState === EventSource.CLOSED) {
      // the browser gave up (e.g. the server answered with an error): try again ourselves
      es.close();
      setTimeout(connect, 3000);
    }
  };
  const on = (name: string, f: (data: never) => void) =>
    es.addEventListener(name, (e) => {
      let data;
      try {
        data = JSON.parse((e as MessageEvent).data);
      } catch {
        return; // a malformed event: skip it, keep the stream
      }
      f(data as never);
    });
  on("utterance", upsert);
  on("status", applyStatus);
  on("alert", upsertAlert);
}
