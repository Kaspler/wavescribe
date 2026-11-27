// Summaries: a few points the local model picks out of what some stations said, each with the
// lines it rests on (POST /api/digest; reading/digest.ts reads it). The server and the command
// line call it a digest; the page says "summary", the word its notifications and its voice use.
// One summary at a time per tab: a second waits for the first to be cancelled, since the model
// makes them one after the other anyway. The last one survives a reload of this tab.
import { fetchRead } from "../lib/auth";
import { sameLang } from "../lib/lang";
import { S } from "../lib/live";
import { P, visible } from "../lib/prefs";
import { MAX_COLS, setCols } from "../lib/router";
import { emit } from "../lib/store";
import { V, showLine } from "../lib/view";
import { type Answer, type Choice, type Failure, answerOf, digestBody, failureOf } from "./digest";
import { T } from "./strings";

export interface Job {
  stations: string[];
  choice: Choice;
  /** When it was asked, in ms, and how long the answer took. */
  asked: number;
  took: number | null;
  state: "working" | "done" | "failed" | "cancelled";
  answer: Answer | null;
  failure: Failure | null;
}

export const Sm = {
  job: null as Job | null,
  /** The answer shows beside the columns (on a phone, on its own page: #/summary). */
  open: false,
};

let ctl: AbortController | null = null;

const KEY = "wavescribe.summary";
try {
  const kept = JSON.parse(sessionStorage.getItem(KEY) || "null") as Job | null;
  if (kept && kept.state === "done" && answerOf(kept.answer)) Sm.job = kept;
} catch {
  /* private mode or blocked storage: nothing kept */
}
function keep() {
  try {
    if (Sm.job?.state === "done") sessionStorage.setItem(KEY, JSON.stringify(Sm.job));
  } catch {
    /* kept for this page only */
  }
}

/** The stations being read: the columns on screen, else every running station shown here. */
export function readingStations(): string[] {
  if (V.shown.length) return V.shown.filter((id) => Object.hasOwn(S.byId, id));
  return S.stations.filter((s) => visible(s.id) && !!S.live[s.id] && !S.live[s.id].stopping).map((s) => s.id);
}

/** The language to ask in: the one the page is read in, when one of `stations` is translated
 * into it or speaks it; otherwise the server's own choice (the language most stations are
 * translated into). */
export function readerLanguage(stations: string[]): string | undefined {
  const lang = P.lang;
  if (!lang) return undefined;
  const has = (id: string) => {
    const s = S.byId[id];
    return !!s && (sameLang(s.language, lang) || s.translate_to.some((t) => sameLang(t, lang)));
  };
  return stations.some(has) ? lang : undefined;
}

/** Asks for a summary of `stations` over `choice`; the answer opens beside the columns. */
export async function askSummary(choice: Choice, stations: string[]) {
  if (Sm.job?.state === "working") return;
  const job: Job = { stations, choice, asked: Date.now(), took: null, state: "working", answer: null, failure: null };
  const mine = new AbortController();
  ctl = mine;
  Sm.job = job;
  Sm.open = true;
  emit("summary");
  try {
    const r = await fetchRead("/api/digest", {
      method: "POST",
      headers: { "X-Wavescribe": "1", "Content-Type": "application/json" },
      body: JSON.stringify(digestBody(choice, { stations, language: readerLanguage(stations) })),
      signal: mine.signal,
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      job.failure = failureOf(r.status, data?.error, { retryAfter: r.headers.get("Retry-After") });
      job.state = "failed";
    } else {
      job.answer = answerOf(data);
      job.state = job.answer ? "done" : "failed";
      if (!job.answer) job.failure = { kind: "failed", text: T.answerUnreadable, retry: true, retryAfter: null };
    }
  } catch {
    if (mine.signal.aborted) job.state = "cancelled";
    else {
      job.failure = { kind: "unreachable", text: T.cantReach, retry: true, retryAfter: null };
      job.state = "failed";
    }
  }
  if (job.state !== "cancelled") job.took = Date.now() - job.asked;
  if (ctl === mine) ctl = null;
  keep();
  emit("summary");
}

/** Stops waiting for the summary being made. The server can't be told: it finishes it. */
export function cancelSummary() {
  if (Sm.job?.state !== "working") return;
  Sm.job.state = "cancelled";
  ctl?.abort();
  emit("summary");
}

export function showSummary(open: boolean) {
  Sm.open = open;
  emit("summary");
}

/** Opens a line in its station's column, beside the columns on screen (on a phone, alone). */
export function openLine(station: string, id: number) {
  if (!Object.hasOwn(S.byId, station)) return;
  showLine(station, id);
  if (V.narrow) setCols([station]);
  else setCols(V.shown.includes(station) ? V.shown : [...V.shown, station].slice(-MAX_COLS));
}
