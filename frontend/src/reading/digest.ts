// What the page asks POST /api/digest for and how it reads the answer (src/wavescribe/digest/,
// web/digest.py): the windows a summary can cover, the request, which of the server's sentences
// stand for the outcome, the lines each point opens, and what its refusals' codes mean. Plain
// functions over the server's JSON, so they can be tested without a page; the words around them
// are in strings.ts.

export const OUTCOMES = ["points", "nothing_said", "not_read", "nothing_stood_out", "unsourced"] as const;
export type Outcome = (typeof OUTCOMES)[number];

/** A line a point rests on, as Ask's answers give theirs. */
export interface SourceLine {
  n: number;
  id: number;
  station: string;
  name: string;
  t_start: number | null;
  language: string;
  text: string;
  translations: Record<string, string>;
  audio: boolean;
}

export interface Point {
  text: string;
  stations: string[];
  /** The numbers (`n`) of the lines it rests on. */
  sources: number[];
}

export interface WindowAlert {
  id: number;
  rule: string;
  severity: string;
  stations: string[];
  names?: string[];
  announced_at: number;
  language?: string;
  quote?: string;
  summaries?: Record<string, string>;
  segment_id?: number | null;
}

export interface Answer {
  /** The request's id: what reads it aloud again (POST /api/digest/speak). */
  id?: string;
  kind: "digest" | "now";
  language: string;
  stations: string[];
  names: Record<string, string>;
  window: { since: number; until: number; minutes: number };
  counts?: { said: number; read: number };
  alerts: WindowAlert[];
  points: Point[];
  sources: SourceLine[];
  outcome: Outcome;
  unread: Record<string, string>;
  notice: string | null;
  text: string;
  model?: string | null;
  timings?: { model_ms?: number; total_ms?: number };
  /** Asked with `speak`: whether reading it aloud began, and the server's reason and its code
   * (digest/speech.py, SPEECH_CODES) when not. */
  speech?: { started: boolean; error?: string; code?: string };
}

/** An id for a request, by which it can be cancelled: a random UUID where the browser makes one
 * (only on a secure page, which this machine's own address is), else 32 random hex digits, as for
 * a phone reading the page over plain http on the home network. */
export function newId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// -- the window ------------------------------------------------------------------------------

/** The server's longest window for a summary, and for one asked from another machine. */
export const MAX_MINUTES = 24 * 60;
export const REMOTE_MINUTES = 6 * 60;
/** The two fixed windows offered, the shorter first. */
export const SPANS = [15, 60] as const;

export type Choice =
  | { kind: "minutes"; minutes: number }
  /** Since a time of day the reader gave, in seconds since 1970, to another one or to now. */
  | { kind: "since"; at: number; until?: number }
  /** Since the reader last looked (lib/away.ts). */
  | { kind: "away"; at: number }
  /** Last night, from NIGHT[0] to NIGHT[1] o'clock. */
  | { kind: "night"; at: number; until: number };

/** "14:30" as the start of a window: today's, or yesterday's when it is still to come (at 00:15,
 * 23:30 is three quarters of an hour ago), as `wavescribe digest --since` reads it. Null for
 * anything but a time of day. */
export function sinceClock(text: string, now: Date): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  if (!m || +m[1] > 23 || +m[2] > 59) return null;
  const t = new Date(now);
  t.setHours(+m[1], +m[2], 0, 0);
  if (t > now) t.setDate(t.getDate() - 1);
  return t.getTime() / 1000;
}

/** "15:30" as the end of a window that starts at `since`: the first such time after it, or null
 * for anything but a time of day. It may be still to come; windowProblem says so. */
export function untilClock(text: string, since: number): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  if (!m || +m[1] > 23 || +m[2] > 59) return null;
  const t = new Date(since * 1000);
  t.setHours(+m[1], +m[2], 0, 0);
  if (t.getTime() / 1000 <= since) t.setDate(t.getDate() + 1);
  return t.getTime() / 1000;
}

/** Why a window from `since` to `until` (now when none) can't be asked for: "later" (it ends
 * after now), "soon" (it is less than a minute long), "far" (longer than `longest` minutes, the
 * most the server reads), or null when it can. */
export function windowProblem(since: number, now: number, longest: number, until?: number): "later" | "soon" | "far" | null {
  const end = until ?? now;
  if (end > now) return "later";
  if (end - since < 60) return "soon";
  if (end - since > longest * 60) return "far";
  return null;
}

/** The night a summary of last night covers, in hours of the clock. */
export const NIGHT = [22, 7] as const;

/** Last night, offered from its end until it begins again, and only where the server reads that
 * far back (not from another machine: REMOTE_MINUTES is shorter than a night). */
export function lastNight(now: Date, longest: number): { at: number; until: number } | null {
  const h = now.getHours();
  if (h < NIGHT[1] || h >= NIGHT[0]) return null;
  const until = new Date(now);
  until.setHours(NIGHT[1], 0, 0, 0);
  const at = new Date(until);
  at.setDate(at.getDate() - 1);
  at.setHours(NIGHT[0], 0, 0, 0);
  const w = { at: at.getTime() / 1000, until: until.getTime() / 1000 };
  return w.until - w.at > longest * 60 ? null : w;
}

/** The request for a summary of `stations` over `choice`, in `language` (the server's default
 * when none), read aloud on the Wavescribe machine with `speak`, under the id `id`. */
export function digestBody(choice: Choice, o: { id: string; stations: string[]; language?: string; speak?: boolean }): Record<string, unknown> {
  const out: Record<string, unknown> = { kind: "digest", id: o.id, stations: o.stations };
  if (choice.kind === "minutes") out.minutes = choice.minutes;
  else out.since = Math.floor(choice.at);
  if (choice.kind !== "minutes" && choice.kind !== "away" && choice.until != null) out.until = Math.floor(choice.until);
  if (o.language) out.language = o.language;
  if (o.speak) out.speak = true;
  return out;
}

/** The request for what one station is talking about now (the server's last few minutes). */
export function nowBody(id: string, station: string, language?: string): Record<string, unknown> {
  const out: Record<string, unknown> = { kind: "now", id, stations: [station] };
  if (language) out.language = language;
  return out;
}

// -- the answer ------------------------------------------------------------------------------

/** The server's answer, or null when it isn't one (a server from before digests, a proxy's page):
 * the page then says the answer couldn't be read rather than drawing half of it. */
export function answerOf(data: unknown): Answer | null {
  const a = data as Answer | null;
  if (!a || typeof a !== "object" || !(OUTCOMES as readonly string[]).includes(a.outcome)) return null;
  if (!Array.isArray(a.points) || !Array.isArray(a.sources) || !Array.isArray(a.stations)) return null;
  if (!a.window || !Number.isFinite(a.window.since) || !Number.isFinite(a.window.until)) return null;
  return { ...a, alerts: Array.isArray(a.alerts) ? a.alerts : [], unread: a.unread || {}, names: a.names || {} };
}

/** What the page shows of an answer: its points, or else the server's own sentences for the
 * outcome, as they are (a summary without points never reads as "nothing happened" in the
 * page's words); the stations it couldn't read beside points (without points the sentences say
 * so); and who wrote it from how much, as given. */
export interface Shown {
  points: Point[];
  sentences: string | null;
  unread: [name: string, why: string][];
  notice: string | null;
}

export function shown(a: Answer): Shown {
  const points = a.outcome === "points" ? a.points || [] : [];
  const notice = a.notice || null;
  if (!points.length) return { points: [], sentences: a.text || null, unread: [], notice };
  const unread = Object.entries(a.unread || {}).map(([id, why]) => [a.names?.[id] || id, why] as [string, string]);
  return { points, sentences: null, unread, notice };
}

/** One of the lines a point rests on, as a link to it in the transcript. `who`: the station's
 * name, given only when the point's lines come from more than one station (otherwise the point
 * names it once). */
export interface Link {
  n: number;
  id: number;
  station: string;
  t: number | null;
  who: string | null;
}

export function linksOf(point: Point, a: Answer): Link[] {
  const byN = new Map((a.sources || []).map((s) => [s.n, s]));
  const lines = point.sources.map((n) => byN.get(n)).filter((s): s is SourceLine => !!s);
  const mixed = new Set(lines.map((s) => s.station)).size > 1;
  return lines.map((s) => ({
    n: s.n,
    id: s.id,
    station: s.station,
    t: s.t_start,
    who: mixed ? a.names?.[s.station] || s.name || s.station : null,
  }));
}

/** Who said a point, as the server names a station: for a summary of several stations; "now" is
 * about one, named already. */
export function saidBy(point: Point, a: Answer): string | null {
  if (a.kind === "now" || a.stations.length < 2) return null;
  return point.stations.map((id) => a.names?.[id] || id).join(", ") || null;
}

// -- failures --------------------------------------------------------------------------------

/** Why an answer didn't come, for the page: "off_here" (this device may not use the model),
 * "unavailable" (no model or store in this run), "busy" (the queue was needed, or two are being
 * made), "failed" (the model failed), "refused" (the request can't be served as asked), "token"
 * (a read token is needed), "unreachable" (no answer at all). `code` is the server's for it, and
 * `text` its sentence, shown as it is; `retry` whether trying again can help, and `retryAt` when
 * (busy: a whole second since 1970, from Retry-After, so the time the page names to the second
 * is the time trying again becomes possible); `maxMinutes` the longest window, for a window
 * refused as too long. */
export interface Failure {
  kind: "off_here" | "unavailable" | "busy" | "failed" | "refused" | "token" | "unreachable";
  code: string | null;
  text: string;
  retry: boolean;
  retryAt: number | null;
  maxMinutes: number | null;
}

/** A failure without an answer from the server, or with one the page can't read. */
export const failure = (kind: "failed" | "unreachable", text: string): Failure =>
  ({ kind, code: null, text, retry: true, retryAt: null, maxMinutes: null });

/** The server's words, set as a sentence of their own: it writes them to follow a colon. */
export function asSentence(s: string): string {
  const t = s.trim();
  if (!t) return t;
  return t[0].toUpperCase() + t.slice(1) + (/[.!?…]$/.test(t) ? "" : ".");
}

/** The codes that mean no summary can be made in this run, as /api/state's digest_code says them
 * too. */
const OFF = ["no_store", "no_llm", "not_set_up"];

/** A refusal from its `code` (web/digest.py): `busy` with when to try again, the run's reasons
 * for having none, a window too long with how long one may be, the model failing, and an id
 * another request took (asked again, it gets a new one). A refusal with no code is the gate's,
 * before any digest is looked at: a read token wanted (401), or this device not allowed to use
 * the model (403). Any other code is shown in the server's words. `now`: seconds since 1970. */
export function failureOf(status: number, data: unknown, o: { retryAfter?: string | null; now?: number } = {}): Failure {
  const d = (data && typeof data === "object" ? data : {}) as { error?: unknown; code?: unknown; max_minutes?: unknown; retry_after_s?: unknown };
  const code = typeof d.code === "string" && d.code ? d.code : null;
  const text = asSentence(typeof d.error === "string" && d.error ? d.error : `the server answered ${status}`);
  const kind: Failure["kind"] = code === "busy" ? "busy"
    : code === "failed" ? "failed"
    : code && OFF.includes(code) ? "unavailable"
    : code ? (status >= 500 ? "failed" : "refused")
    : status === 401 ? "token"
    : status === 403 ? "off_here"
    : status >= 500 ? "failed"
    : "refused";
  // The answer's own retry_after_s, else its Retry-After header: the same time, in seconds.
  const own = typeof d.retry_after_s === "number" && Number.isFinite(d.retry_after_s) && d.retry_after_s > 0 ? d.retry_after_s : null;
  const after = own ?? Number.parseInt(o.retryAfter ?? "", 10);
  const given = Number.isFinite(after) && after > 0;
  // A refusal the page has no code for, sent with a time to try again (a server newer than this
  // page, say), is a wait as "busy" is: its sentence as it is, and its time.
  const waits = kind === "busy" || (given && (status === 429 || status === 503));
  const retryAt = waits ? Math.ceil((o.now ?? Date.now() / 1000) + (given ? after : 60)) : null;
  const max = Number(d.max_minutes);
  return {
    kind, code, text,
    retry: waits || kind === "failed" || code === "id_in_use",
    retryAt,
    maxMinutes: code === "bad_window" && Number.isFinite(max) && max > 0 ? max : null,
  };
}

// -- waiting for one -----------------------------------------------------------------------

/** Where a summary being made stands, from the status's `digests` (one row per digest not made
 * yet, web: Digester.status): "in_line" behind `behind` other summaries; "waiting" for the
 * model behind `behind` units of its work, of the kinds in `what` (most first), its turn ahead
 * of the station lines within `turnIn` seconds (0: it is ahead of them already); "writing" part
 * `call` of `calls`. Null when the status has no row with this request's `id`: a server from
 * before, a row not there yet, or a summary asked for from another device. */
export interface Wait {
  state: "in_line" | "waiting" | "writing";
  behind: number;
  what: string[];
  turnIn: number | null;
  call: number | null;
  calls: number | null;
}

const STATES: readonly string[] = ["in_line", "waiting", "writing"];
const count = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

export function waitOf(status: unknown, id: string): Wait | null {
  const rows = status && typeof status === "object" ? (status as { digests?: unknown }).digests : null;
  if (!Array.isArray(rows)) return null;
  const row = rows.find((r) => r && typeof r === "object" && (r as { id?: unknown }).id === id) as Record<string, unknown> | undefined;
  if (!row || typeof row.state !== "string" || !STATES.includes(row.state)) return null;
  const calls = count(row.calls), call = count(row.call);
  return {
    state: row.state as Wait["state"],
    behind: Math.round(count(row.behind) ?? 0),
    what: Array.isArray(row.behind_what) ? row.behind_what.filter((k): k is string => typeof k === "string") : [],
    turnIn: count(row.turn_in_s),
    call: call != null && calls != null && call >= 1 && calls >= 1 ? Math.min(call, calls) : null,
    calls: call != null && calls != null && call >= 1 && calls >= 1 ? calls : null,
  };
}

/** How long until a refused request may be tried again, in ms: nothing once `retryAt` is here,
 * or when the refusal named no time. */
export const retryWait = (retryAt: number | null, nowMs: number): number =>
  retryAt == null ? 0 : Math.max(0, retryAt * 1000 - nowMs);

// -- the stations ------------------------------------------------------------------------------

/** A station a summary can cover: one this device shows that is running now. */
export interface Coverable {
  id: string;
  /** A watch (every channel of a band): short lines from many speakers, so it is in a summary
   * only when chosen, as on the command line. */
  watch: boolean;
}

/** The stations a summary covers: every running station in the reader's list, whether or not its
 * column is on screen, but for the ones the reader left out in the summary's own choice
 * (`picked`: id to in or out); a watch is out until chosen. */
export function coveredStations(list: Coverable[], picked: Record<string, boolean> = {}): string[] {
  return list.filter((s) => (Object.hasOwn(picked, s.id) ? picked[s.id] : !s.watch)).map((s) => s.id);
}

// -- reading aloud ---------------------------------------------------------------------------

// As the server reads a summary aloud (digest/speech.py): an opening sentence, the earlier alerts
// by time and rule, then each point after the stations that said it, as many as fit in 45
// seconds and at least one, at `say`'s usual rate.
const SPOKEN_S = 45;
const CHARS_PER_S = 14;
const DENSE_CHARS_PER_S = 6;
const SPOKEN_CHARS = 300;
const OPENING_CHARS = 140; // "Wavescribe summary of …, 14:05 to 15:05. Written by the local model…"
const EARLIER_LISTED = 5;
const NO_SPACES = /[\u0e00-\u0e7f\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]/;

/** About how long reading `a` aloud takes, in seconds, rounded up to five: an estimate. */
export function spokenSeconds(a: Answer): number {
  const name = (id: string) => a.names[id] || id;
  const opening = Math.min(SPOKEN_CHARS, OPENING_CHARS + a.stations.map(name).join(", ").length);
  const earlier = a.alerts.length ? Math.min(SPOKEN_CHARS, 30 + 20 * Math.min(EARLIER_LISTED, a.alerts.length)) : 0;
  let s = (opening + earlier) / CHARS_PER_S;
  let left = SPOKEN_S;
  let said = 0;
  for (const p of a.points) {
    const line = `${a.kind === "now" ? "" : `${p.stations.map(name).join(", ")}: `}${p.text}`.slice(0, SPOKEN_CHARS);
    const t = line.length / (NO_SPACES.test(line) ? DENSE_CHARS_PER_S : CHARS_PER_S);
    left -= t;
    if (said && left < 0) break;
    s += t;
    said++;
  }
  return Math.ceil(s / 5) * 5;
}

/** An alert as the live stream sends it: when it reached someone, and how. */
interface Delivered {
  delivered_at?: number | null;
  actions?: Record<string, string | number>;
}

/** An alert's voice stops a summary being read aloud at once (the server sees to that). An alert
 * records its delivery once its own voice has finished, so one read aloud counts when that falls
 * within the summary's reading or up to `ALERT_SPEECH_S` after it. When the first did, or null.
 * `speech`: when the summary began to be read, and for about how long (seconds). */
export const ALERT_SPEECH_S = 20;
export function cutAt(speech: { at: number; seconds: number }, alerts: Iterable<Delivered>): number | null {
  let first: number | null = null;
  for (const a of alerts) {
    const at = a.delivered_at;
    if (at == null || !Number.isFinite(at)) continue;
    const spoke = Object.entries(a.actions || {}).some(([k, v]) => k.startsWith("speak:") && v === "ok");
    if (!spoke || at < speech.at || at > speech.at + speech.seconds + ALERT_SPEECH_S) continue;
    if (first == null || at < first) first = at;
  }
  return first;
}

/** What asking to read a summary aloud came to (POST /api/digest/speak, or `speech` in the
 * answer of one asked with `speak`): begun; refused, with the server's code (digest/speech.py,
 * SPEECH_CODES: no_voice, no_speech, voice_missing, nothing_to_read, no_phrases, speaking) and
 * sentence; "gone", no longer kept on the Wavescribe machine (404); or "failed", any other
 * answer, the server's sentence when it gave one. */
export interface SpeakResult {
  kind: "started" | "refused" | "gone" | "failed";
  code: string | null;
  text: string;
}

export function speakResult(status: number, data: unknown): SpeakResult {
  const d = (data && typeof data === "object" ? data : {}) as { error?: unknown; code?: unknown; speech?: unknown };
  const speech = (d.speech && typeof d.speech === "object" ? d.speech : null) as { started?: unknown } | null;
  const code = typeof d.code === "string" && d.code ? d.code : null;
  const text = asSentence(typeof d.error === "string" && d.error ? d.error : `the server answered ${status}`);
  if (status >= 200 && status < 300 && speech?.started === true) return { kind: "started", code: null, text: "" };
  if (status === 404 || code === "not_found") return { kind: "gone", code, text };
  if (status === 409 && code) return { kind: "refused", code, text };
  return { kind: "failed", code, text };
}

/** The same, from the answer to a summary asked with `speak`; null when it wasn't. */
export function speechOf(a: Answer): SpeakResult | null {
  const sp = a.speech;
  if (!sp) return null;
  if (sp.started) return { kind: "started", code: null, text: "" };
  return { kind: "refused", code: sp.code ?? null, text: asSentence(sp.error || "") };
}

/** Where a reading that began at `at` and takes about `seconds` stands at `now`: still being
 * read, read to the end, or cut short by an alert's voice (when: `cut`). */
export function readingAt(r: { at: number; seconds: number }, now: number, alerts: Iterable<Delivered>):
  { state: "reading" | "read" | "cut"; cut: number | null } {
  const cut = cutAt(r, alerts);
  if (cut != null) return { state: "cut", cut };
  return { state: now < r.at + r.seconds ? "reading" : "read", cut: null };
}
