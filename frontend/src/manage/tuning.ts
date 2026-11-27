// Stepping a receiver tuned by hand, as a radio steps: along a raster, or channel by channel
// where the band plan lists its channels, from a key, a button or the wheel. Pure functions of
// the band's record, so they can be tested without a page.
import type { Band } from "./bands";

/** What stepping needs of a band: its edges, demodulation, raster and channel list. */
export type StepBand = Pick<Band, "start_mhz" | "stop_mhz" | "mode" | "step_khz" | "channels_mhz">;

/** A step: a raster in kHz, or the band's own channels, one after another. */
export type Step = number | "channels";

/** The step each demodulation starts with where no band gives one, in kHz. */
const USUAL_KHZ: Record<string, number> = { wfm: 100, nfm: 12.5, am: 25 };
/** The rasters offered for each demodulation beside the band's own, in kHz. */
const RASTERS_KHZ: Record<string, number[]> = { wfm: [50, 100, 200], nfm: [5, 6.25, 12.5, 25, 100], am: [5, 25 / 3, 25, 100] };

/** Two frequencies closer than this are the same one: the page writes them to five decimals. */
const SAME_MHZ = 0.00002;

export const hasChannels = (band: StepBand | null | undefined): band is StepBand => !!band?.channels_mhz?.length;

/** The band's own raster where the band is used in this demodulation, else the mode's usual one. */
export const rasterKhz = (band: StepBand | null | undefined, mode: string): number =>
  band?.step_khz && band.mode === mode ? band.step_khz : USUAL_KHZ[mode] ?? USUAL_KHZ.nfm;

/** The step a band starts with: its channels where it lists them, else its raster. */
export const defaultStep = (band: StepBand | null | undefined, mode: string): Step =>
  hasChannels(band) && band.mode === mode ? "channels" : rasterKhz(band, mode);

/** The steps offered for a band and demodulation: its channels where it lists them, then the
 * rasters from the finest, the band's own among them. The plan's 8.3333333 kHz and this page's
 * 25/3 are one step, offered once. */
export function stepChoices(band: StepBand | null | undefined, mode: string): Step[] {
  const all = [rasterKhz(band, mode), ...(RASTERS_KHZ[mode] ?? RASTERS_KHZ.nfm)];
  const rasters = all.filter((k, i) => all.findIndex((x) => Math.abs(x - k) < 0.01) === i).sort((a, b) => a - b);
  return [...(hasChannels(band) ? ["channels" as const] : []), ...rasters];
}

/** A step as the page names it: "Channels", "12.5 kHz", "8.33 kHz" (at most two decimals). */
export const stepLabel = (s: Step): string => (s === "channels" ? "Channels" : `${+s.toFixed(2)} kHz`);

/** Where a raster starts. The band's own raster counts from its first channel where it lists
 * them (PMR446's 12.5 kHz from 446.00625), else from its start (the airband's 8.33 kHz from
 * 118 MHz); any other from 0 Hz (100 kHz steps land on 98.6, 98.7…). */
const origin = (band: StepBand | null | undefined, stepKhz: number) =>
  !band?.step_khz || Math.abs(band.step_khz - stepKhz) >= 0.01 ? 0 : band.channels_mhz?.[0] ?? band.start_mhz;

const round5 = (mhz: number) => +mhz.toFixed(5);

/** `mhz` on the raster of `stepKhz`. */
export function onGrid(mhz: number, stepKhz: number, band: StepBand | null | undefined): number {
  const step = stepKhz / 1000, o = origin(band, stepKhz);
  return round5(o + Math.round((mhz - o) / step) * step);
}

/** The nearest channel: the band's channel list inside the band, else the raster. A click on the
 * spectrum lands here. */
export function snap(mhz: number, stepKhz: number, band: StepBand | null | undefined): number {
  if (hasChannels(band) && band.start_mhz <= mhz && mhz <= band.stop_mhz)
    return band.channels_mhz.reduce((p, c) => (Math.abs(c - mhz) < Math.abs(p - mhz) ? c : p));
  return onGrid(mhz, stepKhz, band);
}

/** The channel `n` channels on from `mhz` in the band's list, in the list's order (a
 * walkie-talkie's channel 7 is followed by channel 8, wherever it is), going round from the last
 * to the first and back as a walkie-talkie does. From between two channels, the first step lands
 * on the nearest channel that way. */
export function nextChannel(channels: number[], mhz: number, n: number): number {
  const len = channels.length;
  const at = channels.findIndex((c) => Math.abs(c - mhz) <= 0.002); // as bandplan.channel_number
  let i: number;
  if (at >= 0) i = at + n;
  else if (n > 0) {
    const above = channels.reduce((b, c, k) => (c > mhz && (b < 0 || c < channels[b]) ? k : b), -1);
    i = (above < 0 ? len : above) + n - 1;
  } else {
    const below = channels.reduce((b, c, k) => (c < mhz && (b < 0 || c > channels[b]) ? k : b), -1);
    i = below + n + 1;
  }
  return channels[((i % len) + len) % len];
}

/** The frequency `n` steps above `mhz` (below for a negative `n`), kept inside `range`, the
 * tuner's reach in MHz. By channel, see nextChannel. On a raster: from a frequency on it, `n`
 * steps of it; from one between two of its points, the nearer point that way first. A frequency
 * half a step off (PMR446's channels on a 12.5 kHz raster from 0 Hz, the Americas' FM on odd
 * tenths) counts as on it, so a run of steps keeps its offset past a band's edge, as a radio's
 * frequency mode does. */
export function stepFrom(mhz: number, n: number, step: Step, band: StepBand | null | undefined, range: readonly [number, number]): number {
  if (!n) return mhz;
  if (step === "channels" && hasChannels(band)) return nextChannel(band.channels_mhz, mhz, n);
  const khz = typeof step === "number" ? step : band?.step_khz || USUAL_KHZ.nfm;
  const s = khz / 1000, o = origin(band, khz);
  const on = (x: number, every: number) => Math.abs(x - Math.round(x / every) * every) < SAME_MHZ;
  const i = (mhz - o) / s;
  const half = s / 2; // counted in whole points, so that 8.33 kHz steps don't drift
  const to = on(mhz - o, s) ? o + (Math.round(i) + n) * s
    : on(mhz, half) ? (Math.round(mhz / half) + 2 * n) * half
    : o + (n > 0 ? Math.ceil(i) + n - 1 : Math.floor(i) + n + 1) * s;
  return round5(Math.min(range[1], Math.max(range[0], to)));
}

/** What a key does to the tuning, or null for a key it leaves alone. Up and right go higher, down
 * and left lower, one step, ten with Shift; Page Up and Page Down go to the next channel where the
 * band lists its channels (`channel`), else ten steps. */
export function keyStep(key: string, shift: boolean, channels: boolean): { n: number; channel: boolean } | null {
  const dir = key === "ArrowUp" || key === "ArrowRight" ? 1 : key === "ArrowDown" || key === "ArrowLeft" ? -1 : 0;
  if (dir) return { n: dir * (shift ? 10 : 1), channel: false };
  const page = key === "PageUp" ? 1 : key === "PageDown" ? -1 : 0;
  if (page) return channels ? { n: page, channel: true } : { n: page * 10, channel: false };
  return null;
}
