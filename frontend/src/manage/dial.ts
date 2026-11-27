// The dial's geometry: where a frequency sits on the one bar that holds everything a receiver
// tunes, where each band and receiver is drawn on it, and where a receiver is sent to look. Pure
// functions of the band plan the server sends, so they can be tested without a page.
import type { Band } from "./bands";

/** What an RTL-SDR's tuner reaches, in MHz (TUNER_RANGE_HZ in radio/bandplan.py). */
export const TUNER_MHZ: readonly [number, number] = [24, 1766];

/** How far below a frequency the server centres a dongle it moves for it, so the dongle's own
 * spike at its centre stays out of the channel (OFF_CENTRE_HZ in dsp/__init__.py). */
const OFF_CENTRE_MHZ: Record<string, number> = { wfm: 0.3, nfm: 0.25, am: 0.25 };

export type DialBand = Pick<Band, "id" | "start_mhz" | "stop_mhz" | "mode" | "privacy" | "tunable">;

/** What the dial says a band is: voice Wavescribe can turn into text, or a digital service it can
 * only show. A stretch no band covers is neither: it is outside the plan. */
export type Kind = "text" | "shown";
export const kindOf = (b: DialBand): Kind => (b.mode && b.privacy !== "digital" ? "text" : "shown");

/** The bands the dial draws: the ones the tuner reaches. */
export const onDial = <B extends DialBand>(bands: B[]): B[] =>
  bands.filter((b) => b.tunable && b.start_mhz >= TUNER_MHZ[0] && b.stop_mhz <= TUNER_MHZ[1]);

/** The narrowest band holding `mhz`, or null outside the plan. */
export function bandAt<B extends DialBand>(bands: B[], mhz: number): B | null {
  let best: B | null = null;
  for (const b of bands) {
    if (b.start_mhz <= mhz && mhz <= b.stop_mhz && (!best || b.stop_mhz - b.start_mhz < best.stop_mhz - best.start_mhz)) best = b;
  }
  return best;
}

// -- the scale ----------------------------------------------------------------------------------

/** A stretch of the dial: from lo to hi MHz, drawn from x0 to x1 (0 is the left end, 1 the
 * right). Inside it, frequency runs logarithmically. */
export interface Piece {
  lo: number;
  hi: number;
  x0: number;
  x1: number;
  /** The band this stretch is, or null for a gap between bands. */
  band: string | null;
}

export interface Scale {
  pieces: Piece[];
  /** Where `mhz` is on the dial, 0 to 1; frequencies beyond the tuner sit at its ends. */
  x(mhz: number): number;
  /** The frequency at `x`. */
  mhz(x: number): number;
}

// A purely logarithmic dial gives FM broadcast 5 % of the bar and walkie-talkies a hair, while
// the empty stretch from 27 to 87 MHz takes a quarter. The dial is pieced instead: each band and
// each gap is a piece whose share grows with the square root of its logarithmic width (so wide
// stretches are compressed and narrow ones widened), voice bands get more room than digital ones
// (broadcast most, where a newcomer starts) and gaps less, and no band falls below a share it can
// be pointed at and labelled with.
const BROADCAST_WEIGHT = 2.2;
const VOICE_WEIGHT = 1.6;
const DIGITAL_WEIGHT = 1;
const GAP_WEIGHT = 0.4;
const MIN_VOICE = 0.3;
const MIN_DIGITAL = 0.16;

/** The pieced scale for these bands. Bands that overlap (433 MHz gadgets inside the 70 cm amateur
 * band) share their outer band's piece. */
export function makeScale(bands: DialBand[]): Scale {
  const [lo, hi] = TUNER_MHZ;
  // Outer bands: merge overlapping ones, keeping the kind of the widest.
  const sorted = onDial(bands).slice().sort((a, b) => a.start_mhz - b.start_mhz || b.stop_mhz - a.stop_mhz);
  const outer: { lo: number; hi: number; id: string; kind: Kind; broadcast: boolean }[] = [];
  for (const b of sorted) {
    const last = outer[outer.length - 1];
    if (last && b.start_mhz < last.hi) {
      if (b.stop_mhz > last.hi) last.hi = b.stop_mhz;
      continue;
    }
    outer.push({ lo: b.start_mhz, hi: b.stop_mhz, id: b.id, kind: kindOf(b), broadcast: b.privacy === "broadcast" });
  }
  const raw: { lo: number; hi: number; band: string | null; w: number }[] = [];
  let at = lo;
  for (const o of outer) {
    if (o.lo > at) raw.push({ lo: at, hi: o.lo, band: null, w: Math.sqrt(Math.log(o.lo / at)) * GAP_WEIGHT });
    const w = Math.sqrt(Math.log(o.hi / o.lo));
    const weight = o.kind === "shown" ? DIGITAL_WEIGHT : o.broadcast ? BROADCAST_WEIGHT : VOICE_WEIGHT;
    raw.push({ lo: o.lo, hi: o.hi, band: o.id, w: Math.max(o.kind === "text" ? MIN_VOICE : MIN_DIGITAL, w * weight) });
    at = o.hi;
  }
  if (at < hi) raw.push({ lo: at, hi, band: null, w: Math.sqrt(Math.log(hi / at)) * GAP_WEIGHT });
  const total = raw.reduce((s, p) => s + p.w, 0);
  let x = 0;
  const pieces: Piece[] = raw.map((p, i) => {
    const x0 = x;
    x = i === raw.length - 1 ? 1 : x + p.w / total;
    return { lo: p.lo, hi: p.hi, x0, x1: x, band: p.band };
  });
  const toX = (mhz: number) => {
    const f = Math.min(hi, Math.max(lo, mhz));
    const p = pieces.find((q) => f <= q.hi) || pieces[pieces.length - 1];
    return p.x0 + (p.x1 - p.x0) * (Math.log(f / p.lo) / Math.log(p.hi / p.lo));
  };
  const toMhz = (xx: number) => {
    const v = Math.min(1, Math.max(0, xx));
    const p = pieces.find((q) => v <= q.x1) || pieces[pieces.length - 1];
    const t = p.x1 > p.x0 ? (v - p.x0) / (p.x1 - p.x0) : 0;
    return p.lo * Math.exp(t * Math.log(p.hi / p.lo));
  };
  return { pieces, x: toX, mhz: toMhz };
}

// -- what is drawn on it ------------------------------------------------------------------------

/** A band as drawn: its span on the dial and its row. Row 0 is the bar itself; a band inside
 * another sits in row 1, over its outer band. */
export interface Segment<B extends DialBand = DialBand> {
  band: B;
  x0: number;
  x1: number;
  row: 0 | 1;
  kind: Kind;
}

export function segments<B extends DialBand>(bands: B[], scale: Scale, minWidth = 0): Segment<B>[] {
  const shown = onDial(bands);
  return shown
    .map((b) => {
      const inside = shown.some((o) => o !== b && o.start_mhz <= b.start_mhz && b.stop_mhz <= o.stop_mhz
        && o.stop_mhz - o.start_mhz > b.stop_mhz - b.start_mhz);
      let x0 = scale.x(b.start_mhz), x1 = scale.x(b.stop_mhz);
      if (inside && x1 - x0 < minWidth) {
        // a band inside another keeps a width it can be pointed at, around where it really is
        const c = (x0 + x1) / 2;
        [x0, x1] = [Math.max(0, c - minWidth / 2), Math.min(1, c + minWidth / 2)];
      }
      return { band: b, x0, x1, row: (inside ? 1 : 0) as 0 | 1, kind: kindOf(b) };
    })
    .sort((a, b) => a.x0 - b.x0 || a.row - b.row);
}

/** Round frequencies to print under the dial, at least `gapPx` apart on a bar `widthPx` wide.
 * Rounder numbers win where two would crowd each other. */
export function ticks(scale: Scale, widthPx: number, gapPx = 44): number[] {
  const byRoundness = [100, 1000, 50, 500, 200, 30, 1500, 300, 150, 400, 70, 700, 40, 250];
  const kept: number[] = [];
  for (const f of byRoundness) {
    if (f < TUNER_MHZ[0] || f > TUNER_MHZ[1]) continue;
    const px = scale.x(f) * widthPx;
    if (kept.every((k) => Math.abs(scale.x(k) * widthPx - px) >= gapPx)) kept.push(f);
  }
  return kept.sort((a, b) => a - b);
}

/** Where each band's name is printed: under the middle of its segment, in the first of `rows`
 * rows where it clears the names already there, or nowhere (null) when no row has room; its name
 * is then said when the band is pointed at or chosen. Names are placed in the order given, so
 * the bands that matter most go first. `width` is each name's width in px. */
export function labelRows(items: { id: string; x0: number; x1: number; width: number }[], widthPx: number, rows = 2, padPx = 12): Record<string, { row: number; left: number } | null> {
  const taken: [number, number][][] = Array.from({ length: rows }, () => []);
  const out: Record<string, { row: number; left: number } | null> = {};
  for (const it of items) {
    const centre = ((it.x0 + it.x1) / 2) * widthPx;
    const left = Math.round(Math.min(widthPx - it.width, Math.max(0, centre - it.width / 2)));
    const span: [number, number] = [left - padPx / 2, left + it.width + padPx / 2];
    const row = taken.findIndex((r) => r.every(([a, b]) => span[1] <= a || span[0] >= b));
    if (row < 0) { out[it.id] = null; continue; }
    taken[row].push(span);
    out[it.id] = { row, left };
  }
  return out;
}

/** A receiver's window as drawn: its true span (x0 to x1, at least `minPx` wide so it never
 * vanishes), the centre its label hangs from, and the label's row, so that labels of receivers
 * looking at nearby frequencies stack instead of covering each other. */
export interface Mark {
  id: string;
  x0: number;
  x1: number;
  xc: number;
  row: number;
}

export function marks(windows: { id: string; lo: number; hi: number }[], scale: Scale, widthPx: number, labelPx = 28, minPx = 8): Mark[] {
  const min = minPx / Math.max(1, widthPx);
  const placed = windows
    .map((w) => {
      const a = scale.x(w.lo), b = scale.x(w.hi), xc = (a + b) / 2;
      const half = Math.max(b - a, min) / 2;
      return { id: w.id, x0: Math.max(0, xc - half), x1: Math.min(1, xc + half), xc, row: 0 };
    })
    .sort((a, b) => a.xc - b.xc || a.id.localeCompare(b.id));
  const rowEnds: number[] = [];
  const half = labelPx / 2 / Math.max(1, widthPx);
  for (const m of placed) {
    let row = rowEnds.findIndex((end) => m.xc - half >= end);
    if (row < 0) row = rowEnds.length;
    rowEnds[row] = m.xc + half;
    m.row = row;
  }
  return placed;
}

// -- where a receiver looks ----------------------------------------------------------------------

/** A band as far as its raster goes: its channels, or the step its channels sit on. */
export type RasterBand = Pick<Band, "start_mhz" | "stop_mhz" | "mode" | "step_khz" | "channel_khz" | "channels_mhz">;

/** `mhz` on the band's raster: its nearest channel where the band lists them, else the nearest
 * step of its raster from the band's start, inside the band; to the kHz outside every band. */
export function onRaster(band: RasterBand | null | undefined, mhz: number): number {
  if (band?.channels_mhz?.length) return band.channels_mhz.reduce((p, c) => (Math.abs(c - mhz) < Math.abs(p - mhz) ? c : p));
  if (band?.step_khz) {
    const step = band.step_khz / 1000;
    const last = Math.floor((band.stop_mhz - band.start_mhz) / step + 1e-6);
    const n = Math.min(last, Math.max(0, Math.round((mhz - band.start_mhz) / step)));
    return +(band.start_mhz + n * step).toFixed(5);
  }
  return round3(mhz);
}

/** How far from the edges of what a dongle hears a channel must sit (CHANNEL_STOP_HZ and
 * NARROW_MARGIN_HZ in dsp/__init__.py), in MHz. */
export const edgeMargin = (mode: string | null | undefined) => (mode === "wfm" ? 0.12 : 0.06);

/** Whether a channel at `mhz` keeps clear of the spike at the centre of a dongle tuned to
 * `centre` (off_centre in dsp/__init__.py): broadcast FM always does, a narrow channel from a
 * channel's width away. */
export const clearOfCentre = (mhz: number, centre: number, mode: string | null | undefined, channelKhz?: number | null) =>
  mode === "wfm" || Math.abs(mhz - centre) >= (channelKhz || (mode === "am" ? 8.33 : 12.5)) / 1000 - 1e-9;

/** Where Look here tunes a receiver in a band: always on the band's raster, so the frequency the
 * page shows is the one the receiver is on. The strongest station heard in the band, else the
 * band's middle. A receiver that should stay where it is (it follows stations in that band, and
 * moving it would pause them) is given its `window`: the strongest heard inside it, else a
 * channel near its middle, clear of the dongle's own spike at its centre. */
export function lookAt(band: RasterBand, heard: { mhz: number; snr_db?: number | null }[], window?: { lo: number; hi: number } | null): number {
  const centre = window ? (window.lo + window.hi) / 2 : 0;
  const m = edgeMargin(band.mode);
  const fits = (f: number) => f >= band.start_mhz && f <= band.stop_mhz
    && (!window || (f >= window.lo + m && f <= window.hi - m && clearOfCentre(f, centre, band.mode, band.channel_khz)));
  const best = heard.filter((e) => fits(e.mhz)).sort((a, b) => (b.snr_db ?? 0) - (a.snr_db ?? 0))[0];
  if (best) return onRaster(band, best.mhz);
  if (window) return onRaster(band, centre + (OFF_CENTRE_MHZ[band.mode || "nfm"] ?? 0.25));
  return onRaster(band, (band.start_mhz + band.stop_mhz) / 2);
}

/** The frequency to tune so that the server, moving the dongle for it, centres it on `centre`. */
export const lookTune = (centre: number, mode: string | null | undefined) => round3(centre + (OFF_CENTRE_MHZ[mode || "nfm"] ?? 0.25));

/** Where a receiver dragged to `x` looks: on a band narrower than its window, the band's middle;
 * otherwise the frequency under the pointer, to 100 kHz, kept inside the tuner's range. */
export function dragCentre(x: number, scale: Scale, bands: DialBand[], spanMhz: number): number {
  const f = scale.mhz(x);
  const b = bandAt(onDial(bands), f);
  if (b && b.stop_mhz - b.start_mhz <= spanMhz - 0.24) return round3((b.start_mhz + b.stop_mhz) / 2);
  const half = spanMhz / 2;
  return round3(Math.min(TUNER_MHZ[1] - half, Math.max(TUNER_MHZ[0] + half, Math.round(f * 10) / 10)));
}

/** Whether a window from lo to hi MHz shows any of the band. */
export const overlaps = (lo: number, hi: number, b: Pick<Band, "start_mhz" | "stop_mhz">) => lo < b.stop_mhz && hi > b.start_mhz;

/** The band a receiver looking from lo to hi is said to be on: the band holding its centre,
 * else the band it shows most of, else null (it looks outside the plan). */
export function bandOfWindow<B extends DialBand>(bands: B[], lo: number, hi: number): B | null {
  const shown = onDial(bands);
  const centre = bandAt(shown, (lo + hi) / 2);
  if (centre) return centre;
  let best: B | null = null, most = 0;
  for (const b of shown) {
    const part = Math.min(hi, b.stop_mhz) - Math.max(lo, b.start_mhz);
    if (part > most) { best = b; most = part; }
  }
  return best;
}

// -- where a receiver is set to follow several stations ------------------------------------------

/** A channel a receiver must hear: its frequency in MHz, its mode, and its width when not the
 * mode's usual one. */
export interface Channel {
  mhz: number;
  mode: string | null;
  channelKhz?: number | null;
}

/** A centre for a dongle `spanMhz` wide that hears every channel, each clear of the edges its
 * filter rolls off at and of the spike at its centre (in_band and off_centre in
 * dsp/__init__.py): `prefer` (where the dongle is set now) when that does, else the one nearest
 * to it that does. Null when no centre hears them all. Worked in whole Hz, as the server
 * checks it. */
export function centreFor(channels: Channel[], spanMhz: number, prefer: number): number | null {
  if (!channels.length) return prefer;
  const hz = (mhz: number) => Math.round(mhz * 1e6);
  const half = hz(spanMhz) / 2;
  let lo = -Infinity, hi = Infinity;
  for (const c of channels) {
    const reach = half - hz(edgeMargin(c.mode));
    lo = Math.max(lo, hz(c.mhz) - reach);
    hi = Math.min(hi, hz(c.mhz) + reach);
  }
  if (lo > hi) return null;
  const clear = (at: number) => channels.every((c) => clearOfCentre(c.mhz, at / 1e6, c.mode, c.channelKhz));
  // Where the answer can be: the preferred centre held inside the range, the range's ends, and
  // a channel's width either side of each narrow channel, the nearest the centre may come to it.
  const tries = [Math.min(hi, Math.max(lo, hz(prefer))), lo, hi];
  for (const c of channels) {
    if (c.mode === "wfm") continue;
    const away = hz((c.channelKhz || (c.mode === "am" ? 8.33 : 12.5)) / 1000);
    tries.push(hz(c.mhz) - away, hz(c.mhz) + away);
  }
  const fine = tries.filter((t) => t >= lo && t <= hi && clear(t)).sort((a, b) => Math.abs(a - hz(prefer)) - Math.abs(b - hz(prefer)));
  return fine.length ? fine[0] / 1e6 : null;
}

const round3 = (v: number) => Math.round(v * 1000) / 1000;
