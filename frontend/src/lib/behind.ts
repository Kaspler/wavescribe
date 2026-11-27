// Whether the machine keeps up, from what the server measures and sends with the status: per
// station, how far its text and its translation are behind (`behind_s`) and what was dropped in
// the last 10 minutes (`dropped`); overall, `load`. A server that sends none of it is taken to
// keep up, and nothing is said. Plain functions, so they can be tested without a page.

export interface Lag {
  text: number | null;
  translation: number | null;
}
export interface Drops {
  asr: number;
  mt: number;
  rules: number;
}
export interface Load {
  state: "ok" | "behind" | "overloaded";
  reason: string | null;
}

interface StationLike {
  behind_s?: Partial<Lag> | null;
  dropped?: Partial<Drops> | null;
}
interface StatusLike {
  load?: Load | null;
  stations?: Record<string, StationLike>;
}

/** A column says how far behind it is past a minute. A line normally reaches the page 6–30 s
 * after it was said and its translation well within a minute; a minute late is late enough for
 * a reader to take old news for live, and not so soon that a long sentence sets it off. */
export const BEHIND_S = 60;

/** Whole minutes, at least one: "Behind by 4 min". */
export const minutes = (s: number) => Math.max(1, Math.round(s / 60));

const seconds = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

/** False only when the server says the machine doesn't keep up. */
export const keepsUp = (st: StatusLike | null | undefined) => !st?.load || st.load.state === "ok";

/** The furthest any running station's text is behind, in seconds; null when none says. */
export function worstText(st: StatusLike | null | undefined): number | null {
  const all = Object.values(st?.stations || {}).map((s) => seconds(s?.behind_s?.text)).filter((x): x is number => x != null);
  return all.length ? Math.max(...all) : null;
}

/** What the header says: "Live", or how far behind. `minutes` is null when the server says it
 * is behind without saying by how much text (only translation is late, say). */
export function headerLag(st: StatusLike | null | undefined): { behind: boolean; minutes: number | null } {
  if (keepsUp(st)) return { behind: false, minutes: null };
  const worst = worstText(st);
  return { behind: true, minutes: worst != null && worst >= BEHIND_S ? minutes(worst) : null };
}

/** How many minutes one station's text and translation are behind, each only past BEHIND_S. */
export function columnLag(s: StationLike | null | undefined): { text: number | null; translation: number | null } {
  const over = (v: unknown) => {
    const x = seconds(v);
    return x != null && x >= BEHIND_S ? minutes(x) : null;
  };
  return { text: over(s?.behind_s?.text), translation: over(s?.behind_s?.translation) };
}

/** Alert checks skipped in the last 10 minutes, over every station. */
export function skippedChecks(st: StatusLike | null | undefined): number {
  return Object.values(st?.stations || {}).reduce((n, s) => n + (seconds(s?.dropped?.rules) ?? 0), 0);
}
