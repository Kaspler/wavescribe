// When the reader last looked, for a summary "since you last looked": the last moment this
// browser showed Wavescribe before the present look began. A tab on screen notes the time every
// little while (and as it goes out of view); a look begins when a tab comes on screen after a
// longer gap than that, which also catches a machine woken from sleep with the tab still showing.
// Kept in this browser, so other tabs and a reload share it; nothing leaves it.

const KEY = "wavescribe.looked";
/** Out of sight for less than this, the reader is still looking. */
export const AWAY_MIN_S = 120;
/** Looking again for longer than this, "since you last looked" has been read already. */
export const BACK_FOR_S = 30 * 60;
const BEAT_MS = 20_000;

export interface Looked {
  /** The last moment the page was on screen. */
  seen: number | null;
  /** When the present look began, and the last moment seen before it. */
  back: number | null;
  away: number | null;
}

/** The record after the page was on screen at `now`. */
export function onScreen(r: Looked, now: number): Looked {
  const gap = r.seen == null || now - r.seen >= AWAY_MIN_S;
  return gap ? { seen: now, back: now, away: r.seen } : { ...r, seen: now };
}

/** Since when the reader hasn't looked, as a window's start, or null where that window wouldn't
 * mean what it says: never seen before, looking again for a while already, or further back than
 * a summary reaches (`longest`, in minutes). */
export function awaySince(r: Looked, now: number, longest: number): number | null {
  if (r.away == null || r.back == null || !Number.isFinite(r.away) || r.away >= now) return null;
  if (now - r.back > BACK_FOR_S) return null;
  if (now - r.away > longest * 60) return null;
  return r.away;
}

const NONE: Looked = { seen: null, back: null, away: null };

export function looked(): Looked {
  try {
    return { ...NONE, ...JSON.parse(localStorage.getItem(KEY) || "null") };
  } catch {
    return NONE; // private mode or blocked storage: not known, so not offered
  }
}

function keep(r: Looked) {
  try {
    localStorage.setItem(KEY, JSON.stringify(r));
  } catch {
    /* not remembered */
  }
}

/** Starts noting when the page is on screen. */
export function watchLooks() {
  const beat = () => {
    if (!document.hidden) keep(onScreen(looked(), Date.now() / 1000));
  };
  // Going out of view is the last moment on screen; coming back may begin a look.
  const out = () => keep({ ...looked(), seen: Date.now() / 1000 });
  document.addEventListener("visibilitychange", () => (document.hidden ? out() : beat()));
  addEventListener("pagehide", out);
  setInterval(beat, BEAT_MS);
  beat();
}
