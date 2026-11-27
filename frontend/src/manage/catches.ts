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

/** Whether the server's words are its own shorthand (ids in quotes, setting names) rather than a
 * sentence for the person: those go under Details, below one the page writes. */
export const technical = (message: string) => /'[a-z0-9_.-]+'|\b[a-z]+_[a-z_]+\b/.test(message);

/** A watch's window as a receiver must hear it, in MHz: its first and last channel, the step
 * between channels and a channel's width. */
export interface WatchWindow { lo: number; hi: number; step: number; channel: number }

type WindowBand = { start_mhz: number; stop_mhz: number; step_khz: number | null; channel_khz: number | null; channels_mhz: number[] };

/** The window a watch of `band` (or of the slice `spec` of it) listens to. */
export function watchWindow(band: WindowBand, spec: string | number[]): WatchWindow {
  const channel = (band.channel_khz || 12.5) / 1000;
  const list = band.channels_mhz.filter((c) => typeof spec === "string" || (c >= spec[0] - 1e-6 && c <= spec[1] + 1e-6));
  if (list.length) return { lo: list[0], hi: list[list.length - 1], step: list.length > 1 ? +(list[1] - list[0]).toFixed(5) : channel, channel };
  const [lo, hi] = typeof spec === "string" ? [band.start_mhz, band.stop_mhz] : spec;
  return { lo, hi, step: (band.step_khz || band.channel_khz || 12.5) / 1000, channel };
}

const NARROW_MARGIN = 0.06; // NARROW_MARGIN_HZ in dsp/__init__.py, in MHz
const OFF_CENTRE_NFM = 0.25; // OFF_CENTRE_HZ["nfm"]

/** Where a receiver `spanMhz` wide is tuned to watch `w`, as plan_centre in radio/watch.py: a
 * narrow window wholly above the centre, clear of the dongle's spike and of the mirror it makes
 * of strong signals; a wide one centred between two channels. */
export function watchCentre(w: WatchWindow, spanMhz: number): number {
  const reach = spanMhz / 2 - NARROW_MARGIN;
  const below = w.lo - OFF_CENTRE_NFM;
  if (w.hi + w.channel / 2 - below <= reach + 1e-9) return +below.toFixed(5);
  const k = Math.floor(((w.lo + w.hi) / 2 - w.lo) / w.step + 1e-9);
  return +(w.lo + (k + 0.5) * w.step).toFixed(5);
}

/** Whether one receiver `spanMhz` wide can hear all of `w` at once: a wider band is refused by
 * the server, with a slice that fits. */
export const fitsOneReceiver = (w: WatchWindow, spanMhz: number) => w.hi - w.lo + w.channel <= spanMhz - 2 * NARROW_MARGIN + 1e-9;

/** Whether a receiver tuned to `centre` hears every channel of `w` (outside in radio/watch.py). */
export const hearsWindow = (w: WatchWindow, centre: number, spanMhz: number) => {
  const reach = spanMhz / 2 - NARROW_MARGIN - w.channel / 2;
  return Math.abs(w.lo - centre) <= reach + 1e-9 && Math.abs(w.hi - centre) <= reach + 1e-9;
};

/** A watch's window in words: a band's name, or "446.000–446.100 MHz". */
export function windowWords(spec: string | number[], bandName: (id: string) => string | null): string {
  if (typeof spec === "string") return bandName(spec) || spec;
  return `${spec[0].toFixed(3)}–${spec[1].toFixed(3)} MHz`;
}
