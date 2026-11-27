// What the page asks POST /api/digest for and how it reads the answer (src/wavescribe/digest/,
// web/digest.py): the windows a summary can cover, the request, which of the server's sentences
// stand for the outcome and the lines each point opens. Plain functions over the server's JSON,
// so they can be tested without a page; the words around them are in strings.ts.

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
}

// -- the window ------------------------------------------------------------------------------

/** The server's longest window for a summary, and for one asked from another machine. */
export const MAX_MINUTES = 24 * 60;
export const REMOTE_MINUTES = 6 * 60;
/** The two fixed windows offered, the shorter first. */
export const SPANS = [15, 60] as const;

export type Choice =
  | { kind: "minutes"; minutes: number }
  /** Since a time of day the reader gave, in seconds since 1970. */
  | { kind: "since"; at: number }
  /** Since the reader last looked (lib/away.ts). */
  | { kind: "away"; at: number };

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

/** Why a window from `since` can't be asked for: "soon" (less than a minute ago), "far" (further
 * back than `longest` minutes), or null when it can. */
export function windowProblem(since: number, now: number, longest: number): "soon" | "far" | null {
  if (now - since < 60) return "soon";
  if (now - since > longest * 60) return "far";
  return null;
}

/** The request for a summary of `stations` over `choice`, in `language` (the server's default
 * when none). */
export function digestBody(choice: Choice, o: { stations: string[]; language?: string }): Record<string, unknown> {
  const out: Record<string, unknown> = { kind: "digest", stations: o.stations };
  if (choice.kind === "minutes") out.minutes = choice.minutes;
  else out.since = Math.floor(choice.at);
  if (o.language) out.language = o.language;
  return out;
}

/** The request for what one station is talking about now (the server's last few minutes). */
export function nowBody(station: string, language?: string): Record<string, unknown> {
  const out: Record<string, unknown> = { kind: "now", stations: [station] };
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
 * (a read token is needed), "unreachable" (no answer at all). `text` is the server's sentence,
 * shown as it is; `retry` whether trying again later can help. */
export interface Failure {
  kind: "off_here" | "unavailable" | "busy" | "failed" | "refused" | "token" | "unreachable";
  text: string;
  retry: boolean;
  retryAfter: number | null;
}

export function failureOf(status: number, error: unknown, o: { retryAfter?: string | null } = {}): Failure {
  const text = typeof error === "string" && error ? error : `the server answered ${status}`;
  const after = Number.parseInt(o.retryAfter ?? "", 10);
  const retryAfter = Number.isFinite(after) && after > 0 ? after : null;
  const kind: Failure["kind"] = status === 401 ? "token"
    : status === 403 ? "off_here"
    : status === 409 ? "unavailable"
    : status === 503 ? "busy"
    : status >= 500 ? "failed"
    : "refused";
  return { kind, text, retry: kind === "busy" || kind === "failed", retryAfter };
}
