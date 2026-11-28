// Caught on the air: what was caught of open radio without being kept as a station (a watch run
// from a terminal, one deleted since), read as one more feed beside the stations. Private and
// digital radio never is. The server sends its newest lines as /api/state's `caught`, pages it as
// /api/history's station `CAUGHT`, and marks a live line of it `caught` (web/server.py,
// WebHub.caught). Plain functions, so they can be tested without a page.
import type { Station, Utterance } from "./types";

/** Its id, as a column's and as /api/history's station: a station's id has no dot. */
export const CAUGHT = "caught.air";

/** The feed a line is read in: Caught on the air, or its station's column. */
export const feedOf = (u: Pick<Utterance, "station" | "caught">): string => (u.caught === true ? CAUGHT : u.station);

/** Whether a column can be shown: a station this page has, or Caught on the air once the page
 * has loaded (before that, an empty feed would say that nothing was caught). */
export const readable = (id: string, has: (id: string) => boolean, loaded: boolean): boolean =>
  id === CAUGHT ? loaded : has(id);

let made: { name: string; lang: string | null; st: Station } | null = null;

/** Caught on the air as a column reads it: a station that speaks whatever each line was heard
 * in, shown in the reader's language where a line was translated into it (else as heard, the
 * language named). The same object while nothing changes, so its lines aren't drawn again. */
export function caughtFeed(name: string, lang: string | null): Station {
  if (made && made.name === name && made.lang === lang) return made.st;
  const st: Station = {
    id: CAUGHT,
    name,
    language: "auto",
    translate_to: lang && lang !== "auto" ? [lang] : [],
    priority: 0,
    available: false, // nothing to start, and no speech model to wait for (station.ts, noText)
    reason: null,
    running: false,
    asr_model: null,
    transcribe: true,
    kind: "caught",
    disabled: false,
    watch: null,
    languages: [],
  };
  made = { name, lang, st };
  return st;
}
