// What watches caught (GET /api/catches), grouped by frequency for "Caught here", and the few
// things the page reads out of the server's answers when it starts a watch. Plain functions,
// so they can be tested without a page.

/** Words heard in a catch, as the store has them. */
export interface Said {
  id: number;
  t: number;
  language: string;
  text: string;
}

/** One transmission a watch caught (radio/watch.py, store.py). */
export interface Catch {
  id: number;
  station: string;
  freq_hz: number;
  mhz: number;
  t_start: number;
  t_end: number;
  duration: number;
  mode: string;
  band: string | null;
  /** The band's name, with the channel number where it has one. */
  label: string;
  snr_db: number;
  peak_db: number;
  /** Speech found in it; null when it wasn't checked. */
  voice: boolean | null;
  reason: string | null;
  privacy: string;
  utterances: Said[];
  name?: string;
  note?: string;
  tags?: string[];
}

/** What the person noted about a frequency (GET /api/manage/radio/notes). */
export interface Note {
  freq_hz: number;
  name?: string;
  note?: string;
  tags?: string[];
  t?: number;
}

/** A frequency where something was caught, with everything caught there, newest first. */
export interface Spot {
  freq_hz: number;
  mhz: number;
  mode: string;
  band: string | null;
  label: string;
  privacy: string;
  catches: Catch[];
  /** When the newest catch started. */
  last: number;
  name: string;
  note: string;
  tags: string[];
}

export interface CatchFilter {
  /** A band id, or null for every band. */
  band: string | null;
  /** Only catches with speech in them. */
  speech: boolean;
  /** How far back, in hours. */
  hours: number;
}

/** The time ranges offered, in hours. */
export const RANGES: [number, string][] = [[1, "Last hour"], [24, "Last day"], [168, "Last week"]];

/** Whether anyone spoke: words were written down, or speech was found in its audio. */
export const hasSpeech = (c: Catch) => c.voice === true || c.utterances.some((u) => u.text.trim());

export function filterCatches(list: Catch[], f: CatchFilter, now: number): Catch[] {
  const since = now - f.hours * 3600;
  return list.filter((c) => c.t_start >= since && (!f.band || c.band === f.band) && (!f.speech || hasSpeech(c)));
}

/** Catches by frequency, the frequency with the newest catch first. A note made here wins over
 * the one the catches came with, which may be older. */
export function groupCatches(list: Catch[], notes: Note[] = []): Spot[] {
  const noted = new Map(notes.map((n) => [n.freq_hz, n]));
  const spots = new Map<number, Spot>();
  for (const c of [...list].sort((a, b) => b.t_start - a.t_start)) {
    let s = spots.get(c.freq_hz);
    if (!s) {
      const n = noted.get(c.freq_hz);
      s = {
        freq_hz: c.freq_hz, mhz: c.mhz, mode: c.mode, band: c.band, label: c.label, privacy: c.privacy, catches: [], last: c.t_start,
        name: (n ? n.name : c.name) || "", note: (n ? n.note : c.note) || "", tags: (n ? n.tags : c.tags) || [],
      };
      spots.set(c.freq_hz, s);
    }
    s.catches.push(c);
  }
  return [...spots.values()];
}

/** The bands catches came from, in the order first seen, for the band filter. */
export const bandsIn = (list: Catch[]): string[] => [...new Set(list.map((c) => c.band).filter((b): b is string => !!b))];

/** "voice", "no voice", "not checked". */
export const voiceWord = (v: boolean | null) => (v === true ? "voice" : v === false ? "no voice" : "not checked");

/** Channels that talk now and then: narrowband voice (NFM or AM) that isn't broadcast, which
 * talks all the time, and isn't digital, which gives only noise. What a watch can listen to. */
export const talksNowAndThen = (b: { mode: string | null; privacy: string }) =>
  (b.mode === "nfm" || b.mode === "am") && b.privacy !== "broadcast" && b.privacy !== "digital";

/** The slice a refusal of a too-wide window offers ("… watch a slice of it, e.g. 144.900:147.100"). */
export function sliceIn(message: string): [number, number] | null {
  const m = /slice of it, e\.g\. (\d+(?:\.\d+)?):(\d+(?:\.\d+)?)/.exec(message);
  return m ? [+m[1], +m[2]] : null;
}

/** Where a refusal says the receiver must be tuned for a watch ("Give the device center_mhz = …"). */
export function centreIn(message: string): number | null {
  const m = /center_mhz = (\d+(?:\.\d+)?)/.exec(message);
  return m ? +m[1] : null;
}

/** A watch's window in words: a band's name, or "446.000–446.100 MHz". */
export function windowWords(spec: string | number[], bandName: (id: string) => string | null): string {
  if (typeof spec === "string") return bandName(spec) || spec;
  return `${spec[0].toFixed(3)}–${spec[1].toFixed(3)} MHz`;
}
