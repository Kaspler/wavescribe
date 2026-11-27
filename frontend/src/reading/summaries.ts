// Summaries: a few points the local model picks out of what some stations said, each with the
// lines it rests on (POST /api/digest; reading/digest.ts reads it). The server and the command
// line call it a digest; the page says "summary", the word its notifications and its voice use.
// One summary at a time per tab: a second waits for the first to be cancelled, since the model
// makes them one after the other anyway. The last one survives a reload of this tab. What a
// station is talking about now is asked once at a time per station; beyond the two the server
// makes at once, its own answer says to try again. Each request carries an id, by which Cancel
// tells the server to stop making it (POST /api/digest/<id>/cancel).
import { fetchRead, withToken } from "../lib/auth";
import { sameLang } from "../lib/lang";
import { S } from "../lib/live";
import { P, visible } from "../lib/prefs";
import { MAX_COLS, setCols } from "../lib/router";
import { emit } from "../lib/store";
import { V, showLine } from "../lib/view";
import { heard } from "./aloud";
import { type Answer, type Choice, type Failure, answerOf, digestBody, failure, failureOf, newId, nowBody, speechOf } from "./digest";
import { T } from "./strings";

export interface Job {
  /** The request's id, by which it is cancelled. */
  id: string;
  stations: string[];
  choice: Choice;
  /** When it was asked, in ms, and how long the answer took. */
  asked: number;
  took: number | null;
  state: "working" | "cancelling" | "done" | "failed" | "cancelled";
  /** How a cancel ended: "stopped" (the server stopped making it, or had nothing by its id),
   * "late" (it was made just before, so its answer shows), "unreached" (the server couldn't be
   * told; the page only stopped waiting). */
  cancel: "stopped" | "late" | "unreached" | null;
  answer: Answer | null;
  failure: Failure | null;
  /** Asked to be read aloud on this machine as soon as it is made. */
  speak: boolean;
}

export const Sm = {
  job: null as Job | null,
  /** The answer shows beside the columns (on a phone, on its own page: #/summary). */
  open: false,
  /** Read the next one aloud on this machine as soon as it is made: whoever wants to hear it may
   * be away from the screen by then. Any answer with points can be read aloud later too. */
  speak: false,
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

/** The ids of the requests sent and not answered yet. */
const waiting = new Set<string>();

/** Sends `body` and fills `job` with the answer or why there is none: "cancelled" when the
 * server answers that it was (by its id), or `signal` stopped the wait. An answer that comes
 * while a cancel is on its way was made just before it. */
async function fetchAnswer(job: Job | NowJob, body: Record<string, unknown>, signal: AbortSignal) {
  const cancelling = () => job.state === "cancelling";
  waiting.add(job.id);
  try {
    const r = await fetchRead("/api/digest", {
      method: "POST",
      headers: { "X-Wavescribe": "1", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    waiting.delete(job.id);
    const data = await r.json().catch(() => null);
    if (!r.ok && data?.code === "cancelled") {
      job.state = "cancelled";
      job.cancel ??= "stopped";
    } else if (!r.ok) {
      job.failure = failureOf(r.status, data, { retryAfter: r.headers.get("Retry-After") });
      job.state = "failed";
    } else {
      job.answer = answerOf(data);
      if (job.answer && cancelling()) job.cancel = "late";
      job.state = job.answer ? "done" : "failed";
      if (!job.answer) job.failure = failure("failed", T.answerUnreadable);
    }
  } catch {
    // Leaving the page fails its requests before "pagehide" is told, so the id is kept a moment
    // for that to cancel it.
    setTimeout(() => waiting.delete(job.id), 1000);
    if (signal.aborted || cancelling()) {
      job.state = "cancelled";
      job.cancel ??= "unreached";
    } else {
      job.failure = failure("unreachable", T.cantReach);
      job.state = "failed";
    }
  }
  if (job.state !== "cancelled") job.took = Date.now() - job.asked;
}

/** How long a cancelled request's own answer may take to come back before the page stops
 * waiting for it: the server answers it within a quarter of a second of the cancel. */
const SETTLE_MS = 5000;

/** Tells the server to stop making the digest asked under `id`: its code ("cancelled",
 * "finished", "not_found"), or null when it couldn't be reached or answered otherwise.
 * `keepalive` lets it go out while the page is being left. */
async function tellCancel(id: string, keepalive = false): Promise<string | null> {
  try {
    const r = await fetch(`/api/digest/${encodeURIComponent(id)}/cancel`,
      withToken({ method: "POST", headers: { "X-Wavescribe": "1" }, keepalive }));
    const data = await r.json().catch(() => null);
    if (r.ok && data?.cancelled) return "cancelled";
    return typeof data?.code === "string" ? data.code : null;
  } catch {
    return null;
  }
}

/** Asks for a summary of `stations` over `choice`; the answer opens beside the columns. */
export async function askSummary(choice: Choice, stations: string[]) {
  if (Sm.job?.state === "working") return;
  const speak = Sm.speak && S.canControl;
  const id = newId();
  const job: Job = {
    id, stations, choice, asked: Date.now(), took: null, state: "working", cancel: null, answer: null, failure: null,
    speak,
  };
  const mine = new AbortController();
  ctl = mine;
  Sm.job = job;
  Sm.open = true;
  emit("summary");
  await fetchAnswer(job, digestBody(choice, { id, stations, language: readerLanguage(stations), speak }), mine.signal);
  const speech = job.answer && speechOf(job.answer);
  if (job.answer && speech) heard(job.answer, speech);
  if (ctl === mine) ctl = null;
  keep();
  emit("summary");
}

/** Cancels the summary being made, by its id. The server answers the request itself: cancelled,
 * or with the summary when it was made just before. When the server isn't reached or knows no
 * such summary, and that answer doesn't come back soon, the page stops waiting. */
export async function cancelSummary() {
  const job = Sm.job;
  if (job?.state !== "working") return;
  job.state = "cancelling";
  emit("summary");
  let said = await tellCancel(job.id);
  // Cancel pressed at once can reach the server before the request it cancels: once more.
  if (said === "not_found") {
    await new Promise((r) => setTimeout(r, 1000));
    if (job.state === "cancelling") said = await tellCancel(job.id);
  }
  if (said === "cancelled" || said === "not_found") job.cancel ??= "stopped";
  else if (said === "finished") job.cancel = "late";
  await new Promise((r) => setTimeout(r, SETTLE_MS));
  if (job.state !== "cancelling") return;
  if (job.cancel === "late" || !said) job.cancel = "unreached";
  if (ctl && Sm.job === job) ctl.abort();
}

// -- what a station is talking about now: one line under its column's head --------------------

export interface NowJob {
  id: string;
  asked: number;
  took: number | null;
  state: Job["state"];
  cancel: Job["cancel"];
  answer: Answer | null;
  failure: Failure | null;
  ctl: AbortController;
}

/** By station, the last "now" asked in this tab. */
export const Now: Record<string, NowJob> = {};

export async function askNow(station: string) {
  if (Now[station]?.state === "working") return;
  const id = newId();
  const job: NowJob = {
    id, asked: Date.now(), took: null, state: "working", cancel: null, answer: null, failure: null, ctl: new AbortController(),
  };
  Now[station] = job;
  emit(`now:${station}`);
  await fetchAnswer(job, nowBody(id, station, readerLanguage([station])), job.ctl.signal);
  emit(`now:${station}`);
}

/** Puts the line away; one still being made is cancelled, as there is nowhere left to show it. */
export function dropNow(station: string) {
  const job = Now[station];
  if (!job) return;
  if (job.state === "working") void tellCancel(job.id);
  job.ctl.abort();
  delete Now[station];
  emit(`now:${station}`);
}

// Leaving the page (closing the tab, reloading, opening another address in it) cancels what is
// being made: no page is left to show the answer, so the model would spend its time on it for
// nobody. Closing the summary doesn't: the header keeps counting its time, and Show opens it.
addEventListener("pagehide", () => {
  for (const id of waiting) void tellCancel(id, true);
});

export function speakNext(on: boolean) {
  Sm.speak = on;
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
