// Hash routes:
//   #/                every live station that fits (wide screens) or the station list (phones)
//   #/s/a,b           exactly these stations as columns
//   #/find/<q>        search everything heard
//   #/ask             questions answered from what was said
//   #/summary         the last summary: on phones a page of its own, elsewhere beside the columns
//   #/listen          every station to play, and the receiver tuned by hand
//   #/add/<page>      Find stations: on the air, on the internet
//   #/settings/<page> stations, alerts, languages, receivers, storage, connections, health
// Older links (#/manage/<tab>, #/radio/<page>) still open the page that took their place.
import { useSyncExternalStore } from "react";

export type Area = "listen" | "add" | "settings";

export type Route =
  | { kind: "live" }
  | { kind: "cols"; ids: string[] }
  | { kind: "find"; q: string }
  | { kind: "ask" }
  | { kind: "summary" }
  | { kind: Area; page: string };

export const MAX_COLS = 4;

/** Where each page of the earlier layouts went: the first page's tabs, then the Radio pages. */
const MOVED: Record<string, [Area, string]> = {
  "manage/status": ["settings", "health"],
  "manage/stations": ["settings", "stations"],
  "manage/models": ["settings", "languages"],
  "manage/alerts": ["settings", "alerts"],
  "manage/outputs": ["settings", "connections"],
  "manage/api": ["settings", "connections"],
  "manage/listen": ["listen", ""],
  "manage/radio": ["add", "air"],
  "manage/receivers": ["settings", "receivers"],
  "manage/": ["settings", "health"],
  "radio/receivers": ["settings", "receivers"],
  "radio/find": ["add", "air"],
  "radio/tune": ["listen", ""],
  "radio/kept": ["settings", "storage"],
  "radio/": ["add", "air"],
};

export function parse(hash: string): Route {
  let m = hash.match(/^#\/s\/(.*)$/);
  if (m) return { kind: "cols", ids: m[1].split(",").filter(Boolean).slice(0, MAX_COLS) };
  m = hash.match(/^#\/find\/(.+)$/);
  if (m) {
    let q = m[1];
    try {
      q = decodeURIComponent(q);
    } catch {
      /* keep it raw */
    }
    return { kind: "find", q };
  }
  if (hash === "#/ask") return { kind: "ask" };
  if (hash === "#/summary") return { kind: "summary" };
  m = hash.match(/^#\/(listen|add|settings)(?:\/([a-z]+))?/);
  if (m) return { kind: m[1] as Area, page: m[2] || "" };
  m = hash.match(/^#\/(manage|radio)(?:\/([a-z]+))?/);
  if (m) {
    const [kind, page] = MOVED[`${m[1]}/${m[2] || ""}`] || MOVED[`${m[1]}/`];
    return { kind, page };
  }
  return { kind: "live" };
}

/** Whether `hash` shows reading: the live columns, chosen columns, a search, a question or a
 * summary. Only these are where the header's Read link may lead back to. */
export function isReading(hash: string): boolean {
  if (movedHash(hash)) return false;
  const { kind } = parse(hash);
  return kind === "live" || kind === "cols" || kind === "find" || kind === "ask" || kind === "summary";
}

/** The hash an older link should be replaced with, or null if it is current. */
export function movedHash(hash: string): string | null {
  if (!/^#\/(manage|radio)(?:\/|$)/.test(hash)) return null;
  const r = parse(hash);
  return r.kind === "listen" || r.kind === "add" || r.kind === "settings" ? `#/${r.kind}${r.page ? `/${r.page}` : ""}` : "#/";
}

const sub = (l: () => void) => {
  addEventListener("hashchange", l);
  return () => removeEventListener("hashchange", l);
};
export const useHash = () => useSyncExternalStore(sub, () => location.hash);
export const go = (hash: string) => {
  if (location.hash !== hash) location.hash = hash;
};
export const setCols = (ids: string[]) => go(`#/s/${ids.join(",")}`);
