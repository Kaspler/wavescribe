// What a column says at the foot of its text when no new line is coming, so that a station that
// plays music or can't be reached doesn't look frozen: what is happening now, and since when.
// It reads what the server's status says of the station (pipeline.status: `connected`, `error`,
// `music` from the music gate, `in_speech`, `quiet_s`, `last_data_age`) and when the column's
// last line was said. Plain functions, so they can be tested without a page; the words are in
// strings.ts.

/** The fields of a running station's status this reads. */
export interface LiveLike {
  connected?: boolean;
  reconnects?: number;
  in_speech?: boolean;
  /** The gate dropped the last utterance as a song, and it is still playing or just ended. */
  music?: boolean;
  /** Seconds since an utterance last passed the gate (since the start, when none has). */
  quiet_s?: number;
  stopping?: boolean;
  error?: string;
  retry_in_s?: number;
  /** Seconds since audio last arrived; null when none has yet. */
  last_data_age?: number | null;
  squelch_open?: boolean | null;
  signal_db?: number | null;
  behind_s?: { text?: number | null } | null;
}

export type ColNote =
  /** Its source can't be reached: nothing heard since `since`, the source's own words for why
   * (kept for a "details" disclosure) and how soon it is tried again. */
  | { kind: "down"; what: "stream" | "receiver" | "remote"; since: number | null; error: string | null; retryIn: number | null }
  | { kind: "connecting" }
  /** Music is playing and the gate skips it; no speech since `since`. */
  | { kind: "music"; since: number }
  /** No voice is heard: silence, or music without one. */
  | { kind: "quiet"; since: number }
  /** A voice is heard, yet nothing was written down since `since`: what isn't clear speech
   * (singing the gate let through, jingles) is left out after it is transcribed. */
  | { kind: "unwritten"; since: number };

/** No speech for this long reads as quiet (lib/station.ts says "quiet" in the head from then). */
export const QUIET_S = 60;
/** A voice heard this long with no line written is said to be left out. A line reaches the page
 * within half a minute of being said. */
export const UNWRITTEN_S = 120;
/** Text more than this far behind is late, not left out; the head says how late. */
const LATE_S = 30;

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

/** `kind`: the station's ("stream", "sdr", "remote"); `writes`: whether its speech is written down
 * at all (not listen-only, and its speech model is there); `lastText`: when its newest line on
 * the page ended, in seconds since 1970; `watched`: since when this page has seen the station
 * connected without a break (null: not known); `now` likewise. Null when there is nothing to
 * say: the station isn't running, is being stopped, or text is coming. */
export function colNote(
  s: LiveLike | null | undefined, o: { kind: string; writes: boolean; lastText: number | null; watched: number | null; now: number },
): ColNote | null {
  if (!s || s.stopping) return null;
  const quiet = num(s.quiet_s);
  if (!s.connected) {
    if (!s.error && !s.reconnects) return { kind: "connecting" };
    const age = num(s.last_data_age);
    const since = age != null ? o.now - age : quiet != null ? o.now - quiet : null;
    const what = o.kind === "remote" ? "remote" : o.kind === "sdr" || s.signal_db != null ? "receiver" : "stream";
    return { kind: "down", what, since, error: s.error || null, retryIn: num(s.retry_in_s) };
  }
  if (!o.writes || quiet == null) return null;
  if (s.music) return { kind: "music", since: o.now - quiet };
  // A walkie-talkie or aircraft channel is silent most of the time: its head says "channel quiet".
  if (s.squelch_open === false) return null;
  if (!s.in_speech && quiet >= QUIET_S) return { kind: "quiet", since: o.now - quiet };
  // Only for as long as this page has watched it: the newest line may be from before the station
  // was last started, and what wasn't written down then isn't known.
  if (o.watched == null) return null;
  const since = Math.max(o.lastText ?? o.watched, o.watched);
  const late = num(s.behind_s?.text);
  return o.now - since >= UNWRITTEN_S && (late == null || late < LATE_S) ? { kind: "unwritten", since } : null;
}

/** The time a note names, held steady: the status comes every second with whole seconds, so the
 * time worked out from it wanders by a second or two, and "since 20:43" would flicker into
 * "since 20:44" and back at the turn of a minute. The time already shown stays while the new one
 * is within `tol` seconds of it. */
export function steady(shown: number | null, next: number | null, tol = 5): number | null {
  if (shown == null || next == null) return next;
  return Math.abs(next - shown) <= tol ? shown : next;
}
