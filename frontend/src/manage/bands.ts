// The band plan as the page sees it (src/wavescribe/radio/bandplan.py): each band's record, one
// plain line per band in words a newcomer knows (the band picker, Heard here and the tuner), and how
// a frequency is written.

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

/** 98.6 for broadcast FM; 446.00625, 118.050 for channels (at least three decimals). */
export const fmtMhz = (mhz: number, mode?: string | null) =>
  mode === "wfm" ? mhz.toFixed(1) : mhz.toFixed(5).replace(/(\.\d{3}\d*?)0+$/, "$1");
