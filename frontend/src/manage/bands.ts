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

/** What a scan or a watch says a frequency is, with its band called as the page calls it. `label`
 * is the plan's band name, then the channel where the band numbers its channels ("Marine VHF,
 * channel 16"); `band` is that band's id. "" where it says nothing. */
export function labelWords(e: { band?: string | null; label?: string | null }, bands: { id: string; name: string }[]): string {
  const b = e.band ? bands.find((x) => x.id === e.band) : undefined;
  if (!e.label || !b || !e.label.startsWith(b.name)) return e.label || "";
  return bandLabel(b) + e.label.slice(b.name.length);
}

/** 98.6 for broadcast FM; 446.00625, 118.050 for channels (at least three decimals). */
export const fmtMhz = (mhz: number, mode?: string | null) =>
  mode === "wfm" ? mhz.toFixed(1) : mhz.toFixed(5).replace(/(\.\d{3}\d*?)0+$/, "$1");

/** "A", "A and B", "A, B and C". */
export const and = (xs: string[]) => (xs.length > 1 ? `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}` : xs[0] || "");
