// The band plan as the page sees it (src/wavescribe/radio/bandplan.py): each band's record, one
// plain line per band in words a newcomer knows (the band picker, Heard here and the tuner), the
// one set of names the page calls bands by, and how a frequency is written.

/** A band of the plan for the chosen region, as `/api/manage/radio` sends it. */
export interface Band {
  id: string;
  name: string;
  start_mhz: number;
  stop_mhz: number;
  /** Whether the receiver's tuner reaches all of it. */
  tunable: boolean;
  /** "wfm", "nfm" or "am"; null for a digital band, which gives only noise. */
  mode: string | null;
  /** broadcast, amateur, utility, private or digital: decides what is kept and what asks first. */
  privacy: string;
  step_khz: number | null;
  channel_khz: number | null;
  /** The channels, when the band has a fixed list (PMR446, marine); else a raster of step_khz. */
  channels_mhz: number[];
  regions: string[];
}


const NO_DECODER = "It shows up as a signal only: Wavescribe has no decoder for it.";

const LINES: Record<string, string> = {
  fm: "FM radio stations, meant for everyone to hear.",
  "fm-americas": "FM radio stations, meant for everyone to hear.",
  "fm-japan": "FM radio stations in Japan, meant for everyone to hear.",
  mw: "Medium-wave (AM) radio stations. This kind of receiver doesn't tune this band yet.",
  "mw-americas": "Medium-wave (AM) radio stations. This kind of receiver doesn't tune this band yet.",
  airband: "Pilots and air traffic control, in short exchanges.",
  "airband-25": "Pilots and air traffic control, in short exchanges.",
  marine: "Marine radio: ships and coast stations.",
  "land-mobile-vhf": "Business and public-service radio: taxis, utilities, building sites and some public services.",
  "weather-us": "Weather broadcasts from NOAA.",
  "2m": "Radio amateurs on the 2 m band.",
  "2m-wide": "Radio amateurs on the 2 m band.",
  "70cm": "Radio amateurs on the 70 cm band.",
  "70cm-wide": "Radio amateurs on the 70 cm band.",
  pmr446: "Licence-free walkie-talkies.",
  frs: "Licence-free walkie-talkies (FRS and GMRS).",
  srd433: "Short-range gadgets: remotes, sensors and, where allowed, low-power walkie-talkies.",
  cb: "Citizens' band radio on 27 MHz.",
  dab: `Digital radio. ${NO_DECODER}`,
  "digital-emergency": "Digital and encrypted services such as TETRA. It shows up as a signal only: Wavescribe has no decoder and never decrypts.",
  dvbt: `Digital television. ${NO_DECODER}`,
  cellular: `Mobile phone networks. ${NO_DECODER}`,
  adsb: `Aircraft transponders. ${NO_DECODER}`,
  gnss: `Satellite navigation. ${NO_DECODER}`,
};

/** The band's line, or "" for a band this page has no words for yet. */
export const bandLine = (id: string | null | undefined): string => (id && LINES[id]) || "";

/** The kind of radio a band plan gives a band (its `privacy`), as a list of bands says it: it
 * decides what is kept of it and whether it asks first. */
const KINDS: Record<string, string> = {
  broadcast: "broadcast radio",
  amateur: "amateur radio",
  utility: "utility radio",
  private: "private radio",
  digital: "digital",
};
export const kindWords = (privacy: string): string => KINDS[privacy] || privacy;

/** A band as the dial explains it to someone new to radio: its everyday name (and a shorter one
 * to print on the dial, when it differs), what is heard there, when, and the antenna it wants.
 * The band plan's own name stays the technical one. */
export interface BandWords {
  name: string;
  label?: string;
  what: string;
  when?: string;
  antenna?: string;
}

const FM: BandWords = {
  name: "FM radio",
  what: "Music, news and talk from stations near you. They send their own names, so what's found here names itself.",
  when: "On the air all day.",
  antenna: "Any antenna hears nearby stations, even the small one that came with the receiver; an outdoor FM antenna brings in distant ones.",
};
const AIR: BandWords = {
  name: "Aircraft",
  what: "Pilots and air traffic control, in short exchanges: a few words, then silence until the next call.",
  when: "Busy by day near airports and under flight paths, quiet at night.",
  antenna: "An upright antenna about 55 cm long, as high as you can put it: aircraft in the sky carry far, the control tower only nearby.",
};
const HAM: BandWords = {
  name: "Radio amateurs",
  what: "Licensed hobbyists talking to each other, often through repeaters on high ground that pass a voice on over a wide area.",
  when: "Busiest in the evenings and at weekends; some groups meet on a repeater at fixed times.",
};
const WORDS: Record<string, BandWords> = {
  fm: FM,
  "fm-americas": FM,
  "fm-japan": FM,
  airband: AIR,
  "airband-25": AIR,
  marine: {
    name: "Boats and ports",
    what: "Ships, harbours and coast stations: a call on channel 16, then a move to a working channel. Coast stations read weather and safety messages at set times.",
    when: "Busy near the coast and on big rivers; inland, mostly silent.",
    antenna: "An upright antenna about 45 cm long; height matters more than anything.",
  },
  "land-mobile-vhf": {
    name: "Business radio",
    what: "The two-way radio of businesses and services, between the marine channels: taxis, utilities, building sites and some public services, each on channels of its own. Some of it is digital, which shows up as a signal only.",
    when: "Mostly in working hours.",
    antenna: "An upright antenna about 45 cm long, as for boats and ports.",
  },
  "weather-us": {
    name: "Weather radio",
    what: "Weather forecasts and warnings from NOAA, read out without a break.",
    when: "On all the time.",
    antenna: "Any antenna, within range of a transmitter.",
  },
  "2m": { ...HAM, label: "Amateurs 2 m", antenna: "An upright antenna about 50 cm long; one made for 2 m and 70 cm covers both amateur bands." },
  "2m-wide": { ...HAM, label: "Amateurs 2 m", antenna: "An upright antenna about 50 cm long; one made for 2 m and 70 cm covers both amateur bands." },
  "70cm": { ...HAM, label: "Amateurs 70 cm", what: "Licensed hobbyists, mostly through local repeaters.", antenna: "An upright antenna about 17 cm long, or one made for 2 m and 70 cm." },
  "70cm-wide": { ...HAM, label: "Amateurs 70 cm", what: "Licensed hobbyists, mostly through local repeaters.", antenna: "An upright antenna about 17 cm long, or one made for 2 m and 70 cm." },
  pmr446: {
    name: "Walkie-talkies",
    what: "Licence-free walkie-talkies anyone can buy, used by families, on building sites, at events. Their range is short, so what you hear is close by.",
    when: "Now and then, more at weekends and events.",
    antenna: "The receiver's own small antenna is enough.",
  },
  frs: {
    name: "Walkie-talkies",
    what: "Licence-free FRS radios and licensed GMRS ones, used by families and outdoor groups. Most of what you hear is close by.",
    when: "Now and then, more at weekends.",
    antenna: "The receiver's own small antenna is enough.",
  },
  srd433: {
    name: "Remotes and sensors",
    what: "Car keys, weather stations, doorbells and other gadgets send short bursts of data here; where they're allowed, low-power walkie-talkies talk here too.",
    when: "Short bursts at any time.",
    antenna: "The receiver's own small antenna.",
  },
  cb: {
    name: "CB radio",
    what: "Citizens' band: drivers and hobbyists on 40 shared channels.",
    when: "Often quiet; on days when conditions carry signals far, voices from other countries come in.",
    antenna: "A long one: CB antennas are 1 to 3 m. The receiver's small antenna hears only strong stations nearby.",
  },
  mw: { name: "Medium wave", what: LINES.mw },
  "mw-americas": { name: "Medium wave", what: LINES.mw },
  dab: { name: "Digital radio (DAB)", label: "Digital radio", what: `Radio stations sent digitally, several in each block 1.5 MHz wide. ${NO_DECODER}` },
  "digital-emergency": { name: "Emergency services", what: `Police, fire and ambulance radio, digital and encrypted. ${NO_DECODER.replace("for it.", "and never decrypts.")}` },
  dvbt: { name: "TV", what: `Digital television, each channel a block 8 MHz wide. ${NO_DECODER}` },
  cellular: { name: "Phones and data", what: `Mobile phone networks. ${NO_DECODER}` },
  adsb: { name: "Aircraft positions", what: `Aircraft send where they are and how high they fly in short digital bursts. ${NO_DECODER}` },
  gnss: { name: "Satellite navigation", label: "Satnav", what: "GPS, Galileo and other navigation satellites. Their signals arrive below the noise, so they don't show up here." },
};

/** The band's words for a newcomer; a band this page has none for is named as the plan names it. */
export const bandWords = (b: { id: string; name: string }): BandWords => WORDS[b.id] || { name: b.name, what: bandLine(b.id) };

/** A band's name where it is one among others (printed on the dial, a button, a choice, a row): the
 * shorter one where the everyday name is long or shared ("Amateurs 2 m", "Amateurs 70 cm"). The
 * plan's own name is the technical one, shown beside it only where the detail is asked for. */
export const bandLabel = (b: { id: string; name: string }): string => {
  const w = bandWords(b);
  return w.label || w.name;
};

/** The channel at `mhz` in a band's channel list, 1-based, within 2 kHz as the server finds it
 * (bandplan.channel_number); null off the list, or for a band without one. */
export function channelAt(b: { channels_mhz?: number[] }, mhz: number | null | undefined): number | null {
  if (mhz == null || !b.channels_mhz?.length) return null;
  const i = b.channels_mhz.findIndex((c) => Math.abs(c - mhz) <= 0.002);
  return i < 0 ? null : i + 1;
}

/** What a frequency is, in the page's words: its band as the page calls it, then the channel's
 * number where the band numbers its channels ("Boats and ports, channel 16"), from the server's
 * `band` (the plan's id) and `channel`; a row that doesn't carry `channel` (what a scan heard
 * before) finds it in the band's list at `mhz`. Without `bands`, only a band the page has words
 * for is named. The plan's `label` stands where there is no band, or one the page can't name
 * (scanning a range, say "88 to 90 MHz"); "" where nothing is known. */
export function placeWords(
  e: { band?: string | null; channel?: number | null; label?: string | null; mhz?: number | null },
  bands: { id: string; name: string; channels_mhz?: number[] }[] = [],
): string {
  const b = e.band ? bands.find((x) => x.id === e.band) : undefined;
  const own = e.band ? WORDS[e.band] : undefined;
  const name = b ? bandLabel(b) : own ? own.label || own.name : null;
  if (!name) return e.label || "";
  const n = e.channel !== undefined ? e.channel : b ? channelAt(b, e.mhz) : null;
  return n != null ? `${name}, channel ${n}` : name;
}

/** 98.6 for broadcast FM; 446.00625, 118.050 for channels (at least three decimals). */
export const fmtMhz = (mhz: number, mode?: string | null) =>
  mode === "wfm" ? mhz.toFixed(1) : mhz.toFixed(5).replace(/(\.\d{3}\d*?)0+$/, "$1");

/** "A", "A and B", "A, B and C". */
export const and = (xs: string[]) => (xs.length > 1 ? `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}` : xs[0] || "");

/** The channels a watch listens on: how many, their numbers in a band that numbers them (else
 * null), and the first and last in MHz. */
export interface Listening {
  channels: number;
  first: number | null;
  last: number | null;
  lo: number;
  hi: number;
  step_khz: number;
}

/** A narrowband channel's raster where no band gives one (DEFAULT_CHANNEL_HZ for nfm). */
const DEFAULT_STEP_KHZ = 12.5;

/** The band a watch is planned with: its own, or the narrowest one at the middle of its range. */
function watchBand(watch: string | number[], bands: Band[]): Band | undefined {
  if (typeof watch === "string") return bands.find((b) => b.id === watch);
  const mid = (watch[0] + watch[1]) / 2;
  return bands.filter((b) => b.start_mhz <= mid && mid <= b.stop_mhz)
    .sort((a, b) => (a.stop_mhz - a.start_mhz) - (b.stop_mhz - b.start_mhz))[0];
}

/** What a watch listens on, as the server plans it (radio/watch.py, _plan): the band's own
 * channels where it lists them, numbered from 1, else the channels of its raster from the band's
 * start, inside the band or the range [lo, hi] MHz. Null for a band the plan lacks, or a range
 * with no channel. */
export function watchListening(watch: string | number[], bands: Band[]): Listening | null {
  const band = watchBand(watch, bands);
  let lo: number, hi: number;
  if (typeof watch !== "string") [lo, hi] = [watch[0], watch[1]];
  else if (band) [lo, hi] = [band.start_mhz, band.stop_mhz];
  else return null;
  const step = band?.step_khz || DEFAULT_STEP_KHZ;
  if (band?.channels_mhz.length) {
    const nums = band.channels_mhz.map((c, i) => [c, i + 1] as const).filter(([c]) => lo - 1e-9 <= c && c <= hi + 1e-9);
    if (!nums.length) return null;
    return { channels: nums.length, first: nums[0][1], last: nums[nums.length - 1][1], lo: nums[0][0], hi: nums[nums.length - 1][0], step_khz: step };
  }
  const start = band ? band.start_mhz : 0;
  const s = step / 1000;
  const first = Math.ceil((lo - start) / s - 1e-9), last = Math.floor((hi - start) / s + 1e-9);
  if (last < first) return null;
  return { channels: last - first + 1, first: null, last: null, lo: start + first * s, hi: start + last * s, step_khz: step };
}

/** A frequency in MHz to the nearest kHz, by way of whole hertz: 433.0125 is 433.013, whatever
 * the float holds. */
const kHzText = (mhz: number) => (Math.round(Math.round(mhz * 1e6) / 1e3) / 1e3).toFixed(3);

/** "channels 1–16, 446.006 to 446.194 MHz"; "channel 8, 446.094 MHz"; "161 channels 12.5 kHz
 * apart, 430.000 to 432.000 MHz". The frequencies to a kHz. */
export function listeningWords(l: Listening): string {
  if (l.channels === 1) return `${l.first != null ? `channel ${l.first}` : "one channel"}, ${kHzText(l.lo)} MHz`;
  const which = l.first != null ? `channels ${l.first}–${l.last}` : `${l.channels} channels ${+l.step_khz.toFixed(4)} kHz apart`;
  return `${which}, ${kHzText(l.lo)} to ${kHzText(l.hi)} MHz`;
}

/** What a watch listens on, where a receiver's tuning would otherwise be the only frequency shown:
 * "Walkie-talkies, listening on channels 1–16, 446.006 to 446.194 MHz". A range names the band
 * it lies in, or else itself. Without the band plan (or a band it lacks) only the band's name or
 * the range is said: the channels can't be told. */
export function watchListensWords(watch: string | number[], bands: Band[] | null): string {
  const band = bands ? watchBand(watch, bands) : undefined;
  const range = typeof watch === "string" ? null : `${kHzText(watch[0])}–${kHzText(watch[1])} MHz`;
  const name = band ? bandWords(band).name : range ?? bandWords({ id: watch as string, name: watch as string }).name;
  const l = bands ? watchListening(watch, bands) : null;
  return l ? `${name}, listening on ${listeningWords(l)}` : name;
}
