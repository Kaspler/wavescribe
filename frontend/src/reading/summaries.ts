// Summaries: a few points the local model picks out of what some stations said, each with the
// lines it rests on (POST /api/digest; reading/digest.ts reads it). The server and the command
// line call it a digest; the page says "summary", the word its notifications and its voice use.
// One summary at a time per tab: a second waits for the first to be cancelled, since the model
// makes them one after the other anyway. The last one survives a reload of this tab. What a
// station is talking about now is asked once at a time per station; beyond the two the server
// makes at once, its own answer says to try again.
import { fetchRead } from "../lib/auth";
import { sameLang } from "../lib/lang";
import { S } from "../lib/live";
import { P, visible } from "../lib/prefs";
import { MAX_COLS, setCols } from "../lib/router";
import { emit } from "../lib/store";
import { V, showLine } from "../lib/view";
import { type Answer, type Choice, type Failure, answerOf, digestBody, failure, failureOf, nowBody, spokenSeconds } from "./digest";
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
  /** Asked to be read aloud on this machine; once it began, when (s) and for about how long. */
  speak: boolean;
  spoken: { at: number; seconds: number } | null;
}

export const Sm = {
  job: null as Job | null,
  /** The answer shows beside the columns (on a phone, on its own page: #/summary). */
  open: false,
  /** Read the next one aloud on this machine: the server reads a summary aloud only as it is
   * made, never one already shown. */
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

/** Sends `body` and fills `job` with the answer or why there is none; "cancelled" when `signal`
 * stopped the wait. */
async function fetchAnswer(job: Job | NowJob, body: Record<string, unknown>, signal: AbortSignal) {
  try {
    const r = await fetchRead("/api/digest", {
      method: "POST",
      headers: { "X-Wavescribe": "1", "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    const data = await r.json().catch(() => null);
    if (!r.ok) {
      job.failure = failureOf(r.status, data, { retryAfter: r.headers.get("Retry-After") });
      job.state = "failed";
    } else {
      job.answer = answerOf(data);
      job.state = job.answer ? "done" : "failed";
      if (!job.answer) job.failure = failure("failed", T.answerUnreadable);
    }
  } catch {
    if (signal.aborted) job.state = "cancelled";
    else {
      job.failure = failure("unreachable", T.cantReach);
      job.state = "failed";
    }
  }
  if (job.state !== "cancelled") job.took = Date.now() - job.asked;
}

/** Asks for a summary of `stations` over `choice`; the answer opens beside the columns. */
export async function askSummary(choice: Choice, stations: string[]) {
  if (Sm.job?.state === "working") return;
  const speak = Sm.speak && S.canControl;
  const job: Job = { stations, choice, asked: Date.now(), took: null, state: "working", answer: null, failure: null, speak, spoken: null };
  const mine = new AbortController();
  ctl = mine;
  Sm.job = job;
  Sm.open = true;
  emit("summary");
  await fetchAnswer(job, digestBody(choice, { stations, language: readerLanguage(stations), speak }), mine.signal);
  if (job.answer?.speech?.started) job.spoken = { at: Date.now() / 1000, seconds: spokenSeconds(job.answer) };
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

// -- what a station is talking about now: one line under its column's head --------------------

export interface NowJob {
  asked: number;
  took: number | null;
  state: Job["state"];
  answer: Answer | null;
  failure: Failure | null;
  ctl: AbortController;
}

/** By station, the last "now" asked in this tab. */
export const Now: Record<string, NowJob> = {};

export async function askNow(station: string) {
  if (Now[station]?.state === "working") return;
  const job: NowJob = { asked: Date.now(), took: null, state: "working", answer: null, failure: null, ctl: new AbortController() };
  Now[station] = job;
  emit(`now:${station}`);
  await fetchAnswer(job, nowBody(station, readerLanguage([station])), job.ctl.signal);
  emit(`now:${station}`);
}

/** Puts the line away, and stops waiting for it if it is still being made. */
export function dropNow(station: string) {
  const job = Now[station];
  if (!job) return;
  job.ctl.abort();
  delete Now[station];
  emit(`now:${station}`);
}

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
