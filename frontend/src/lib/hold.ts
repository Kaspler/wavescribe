// A station started from a list of stopped ones keeps its row for a moment, so the rows under the
// pointer don't jump while the next Start is being aimed at. The hold is the list's own note, kept
// apart from what the server says is running, so it must end by itself: a little after the last
// start or the last movement over the list, or at once when the pointer leaves it. (A hold that
// only waited for the pointer to leave kept a started station under "Stopped" for as long as the
// pointer stayed where it had clicked.) Plain functions, so they can be tested without a page.

/** How long a started station keeps its row after the last start, or the last movement of the
 * pointer over its list. */
export const HOLD_MS = 4000;

export interface Timers<H> {
  set(f: () => void, ms: number): H;
  clear(h: H): void;
}

export interface Holds {
  has(id: string): boolean;
  /** Keeps the row of a station just started where it is. */
  hold(id: string): void;
  /** The pointer moved over the list: the rows stay a little longer. */
  stir(): void;
  /** The rows go where they belong now (the pointer left the list). */
  release(): void;
}

/** `changed` runs when the held rows are let go, for the lists to draw themselves again. */
export function makeHolds<H>(timers: Timers<H>, changed: () => void): Holds {
  const ids = new Set<string>();
  let timer: H | null = null;
  const disarm = () => {
    if (timer != null) timers.clear(timer);
    timer = null;
  };
  const release = () => {
    disarm();
    if (!ids.size) return;
    ids.clear();
    changed();
  };
  const arm = () => {
    disarm();
    timer = timers.set(release, HOLD_MS);
  };
  return {
    has: (id) => ids.has(id),
    hold(id) {
      ids.add(id);
      arm();
    },
    stir() {
      if (ids.size) arm();
    },
    release,
  };
}

/** Where each station's row is drawn: with the running ones, or with the stopped ones, where a
 * station just started stays while it is held. `live` and `stopped` are what the two headings
 * count, which a held row doesn't change: a station that runs counts as running wherever its
 * row is. */
export function placeRows<S extends { id: string }>(
  stations: S[], running: (id: string) => boolean, connected: (id: string) => boolean, held: (id: string) => boolean,
): { on: S[]; off: S[]; live: number; stopped: number } {
  return {
    on: stations.filter((s) => running(s.id) && !held(s.id)),
    off: stations.filter((s) => !running(s.id) || held(s.id)),
    live: stations.filter((s) => running(s.id) && connected(s.id)).length,
    stopped: stations.filter((s) => !running(s.id)).length,
  };
}
