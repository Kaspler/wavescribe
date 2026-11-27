// The dial's geometry: one bar for everything a receiver tunes, on which the popular bands stay
// legible, receivers never vanish, a channel counts as heard only where the server hears it, and
// a receiver sent to a band looks where its stations are.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandAt, bandOfWindow, dragCentre, hears, labelRows, lookAt, lookTune, makeScale, marks, onRaster, segments, ticks, usable, width, windowWords } from "../src/manage/dial.ts";

const band = (id, start_mhz, stop_mhz, mode, privacy) => ({ id, start_mhz, stop_mhz, mode, privacy, tunable: true });
// A European band plan's shape, as /api/manage/radio sends it.
const BANDS = [
  band("fm", 87.5, 108, "wfm", "broadcast"),
  { ...band("mw", 0.531, 1.611, "am", "broadcast"), tunable: false },
  band("airband", 118, 137, "am", "utility"),
  band("marine", 156, 162.025, "nfm", "utility"),
  band("2m", 144, 146, "nfm", "amateur"),
  band("70cm", 430, 440, "nfm", "amateur"),
  band("pmr446", 446, 446.2, "nfm", "private"),
  band("srd433", 433.05, 434.79, "nfm", "private"),
  band("cb", 26.965, 27.405, "am", "private"),
  band("dab", 174, 240, null, "digital"),
  band("dvbt", 470, 694, null, "digital"),
  band("cellular", 694, 960, null, "digital"),
  band("adsb", 1089, 1091, null, "digital"),
];
const scale = makeScale(BANDS);
const share = (lo, hi) => scale.x(hi) - scale.x(lo);

test("the dial runs from the tuner's lowest frequency to its highest, always rising", () => {
  assert.equal(scale.x(24), 0);
  assert.equal(scale.x(1766), 1);
  assert.equal(scale.x(10), 0, "below the tuner sits at its end");
  let last = -1;
  for (let f = 24; f <= 1766; f *= 1.003) {
    const x = scale.x(f);
    assert.ok(x > last, `${f} MHz`);
    last = x;
  }
});

test("a position and its frequency map back to each other", () => {
  for (const f of [24, 27.2, 50, 98.6, 121.5, 145.5, 433.92, 446.00625, 700, 1090, 1766]) {
    assert.ok(Math.abs(scale.mhz(scale.x(f)) - f) < f * 1e-9, `${f} MHz`);
  }
});

test("FM broadcast is no sliver, and every band is wide enough to point at", () => {
  assert.ok(share(87.5, 108) >= 0.1, `FM has ${share(87.5, 108)}`);
  assert.ok(share(87.5, 108) > share(470, 694), "FM gets more room than digital TV");
  for (const b of BANDS.filter((x) => x.tunable && x.id !== "srd433")) {
    assert.ok(share(b.start_mhz, b.stop_mhz) >= 0.02, `${b.id} has ${share(b.start_mhz, b.stop_mhz)}`);
  }
  // On a plain logarithmic dial the empty stretch from 27 to 87 MHz takes over a quarter.
  assert.ok(share(27.405, 87.5) < 0.1);
});

test("a band inside another is drawn over it, wide enough to point at; the rest are the bar", () => {
  const segs = segments(BANDS, scale, 0.015);
  assert.ok(!segs.some((s) => s.band.id === "mw"), "a band the tuner can't reach isn't drawn");
  const srd = segs.find((s) => s.band.id === "srd433");
  const outer = segs.find((s) => s.band.id === "70cm");
  assert.equal(srd.row, 1);
  assert.equal(outer.row, 0);
  assert.ok(srd.x1 - srd.x0 >= 0.015 - 1e-12);
  assert.ok(srd.x0 >= outer.x0 && srd.x1 <= outer.x1);
  assert.deepEqual([...new Set(segs.filter((s) => s.row === 0).map((s) => s.kind))].sort(), ["shown", "text"]);
  assert.equal(segs.find((s) => s.band.id === "dab").kind, "shown");
  assert.equal(segs.find((s) => s.band.id === "pmr446").kind, "text");
  for (let i = 1; i < segs.length; i++) assert.ok(segs[i].x0 >= segs[i - 1].x0, "in order along the dial");
});

test("printed frequencies are round, in order, and never crowd", () => {
  for (const width of [960, 600]) {
    const t = ticks(scale, width);
    assert.ok(t.includes(100) && t.includes(1000), `${width}: ${t}`);
    for (let i = 1; i < t.length; i++) {
      assert.ok(t[i] > t[i - 1]);
      assert.ok((scale.x(t[i]) - scale.x(t[i - 1])) * width >= 44, `${t[i - 1]} and ${t[i]} at ${width}px`);
    }
  }
});

test("band names are printed under their bands, on a second row when crowded, else left out", () => {
  const rows = labelRows([
    { id: "fm", x0: 0.1, x1: 0.2, width: 60 },
    { id: "air", x0: 0.21, x1: 0.26, width: 60 },
    { id: "2m", x0: 0.27, x1: 0.29, width: 60 },
    { id: "edge", x0: 0.98, x1: 1, width: 60 },
  ], 1000, 2);
  assert.deepEqual(rows.fm, { row: 0, left: 120 });
  assert.equal(rows.air.row, 0, "far enough from FM");
  assert.equal(rows["2m"].row, 1, "too close to its neighbour: the second row");
  assert.equal(rows.edge.left, 940, "kept inside the dial");
  const crowded = labelRows([0, 1, 2].map((i) => ({ id: `b${i}`, x0: 0.5, x1: 0.51, width: 60 })), 1000, 2);
  assert.equal(crowded.b2, null, "no room in either row");
});

test("a receiver's window never vanishes, and nearby receivers' labels stack", () => {
  const m = marks([
    { id: "a", lo: 96.8, hi: 99.2 },
    { id: "b", lo: 97.3, hi: 99.7 },
    { id: "c", lo: 600, hi: 602.4 },
  ], scale, 960);
  const by = Object.fromEntries(m.map((x) => [x.id, x]));
  assert.ok((by.c.x1 - by.c.x0) * 960 >= 8 - 1e-9, "2.4 MHz of TV is still a mark");
  assert.notEqual(by.a.row, by.b.row, "two receivers side by side in FM");
  assert.equal(by.c.row, 0);
  assert.ok(by.a.x0 < by.a.xc && by.a.xc < by.a.x1);
});

// Bands with their rasters, as the band plan sends them.
const FM = { ...BANDS[0], step_khz: 100, channel_khz: null, channels_mhz: [] };
const TWO_M = { ...BANDS[4], step_khz: 12.5, channel_khz: 12.5, channels_mhz: [] };
const PMR = { ...BANDS[6], step_khz: null, channel_khz: 12.5, channels_mhz: Array.from({ length: 16 }, (_, i) => +(446.00625 + i * 0.0125).toFixed(5)) };
const AIR = { ...BANDS[2], step_khz: 25 / 3, channel_khz: 8.33, channels_mhz: [] };
const heard = (mhz, snr_db) => ({ mhz, snr_db });

test("a frequency is put on its band's raster, and never past the band", () => {
  assert.equal(onRaster(FM, 98.86), 98.9);
  assert.equal(onRaster(FM, 98.84), 98.8);
  assert.equal(onRaster(FM, 120), 108, "inside the band");
  assert.equal(onRaster(TWO_M, 145.006), 145.0);
  assert.equal(onRaster(PMR, 446.097), 446.09375, "the nearest channel of the list");
  assert.equal(onRaster(AIR, 127.501), 127.5);
  assert.equal(onRaster(null, 1200.12345), 1200.123, "outside every band: to the kHz");
});

// A receiver centred on 90.5 MHz at 2.4 MS/s, as span_mhz gives it.
const AT_90_5 = [89.3, 91.7];

test("broadcast FM is heard from 120 kHz inside the edges, a narrow channel from 60 kHz", () => {
  assert.deepEqual(usable(AT_90_5, "wfm").map((f) => +f.toFixed(6)), [89.42, 91.58], "the server's 'outside 89.42 to 91.58 MHz'");
  assert.deepEqual(usable(AT_90_5, "nfm").map((f) => +f.toFixed(6)), [89.36, 91.64]);
  assert.deepEqual(usable(AT_90_5, "am").map((f) => +f.toFixed(6)), [89.36, 91.64]);
  assert.deepEqual(usable(AT_90_5, null).map((f) => +f.toFixed(6)), [89.36, 91.64], "an unknown mode: the narrower margin");
});

test("a channel on the limit is heard, a kHz further out isn't", () => {
  assert.equal(hears(AT_90_5, 91.58, "wfm"), true);
  assert.equal(hears(AT_90_5, 91.581, "wfm"), false);
  assert.equal(hears(AT_90_5, 89.42, "wfm"), true);
  assert.equal(hears(AT_90_5, 89.419, "wfm"), false);
  assert.equal(hears(AT_90_5, 91.64, "nfm"), true);
  assert.equal(hears(AT_90_5, 91.641, "nfm"), false);
});

test("a narrow channel on the spike at the centre isn't heard; broadcast FM is", () => {
  assert.equal(hears(AT_90_5, 90.5, "wfm"), true);
  assert.equal(hears(AT_90_5, 90.5, "nfm"), false);
  assert.equal(hears(AT_90_5, 90.5125, "nfm"), true, "a 12.5 kHz channel's width away");
  assert.equal(hears(AT_90_5, 90.51, "nfm", 25), false, "a 25 kHz channel needs 25 kHz");
  assert.equal(hears(AT_90_5, 90.509, "am"), true, "an 8.33 kHz channel");
});

test("the width is the edges' difference, whatever the sample rate", () => {
  assert.equal(+width(AT_90_5).toFixed(6), 2.4);
  assert.equal(+width([145.02, 145.98]).toFixed(6), 0.96);
  assert.deepEqual(usable([145.02, 145.98], "nfm").map((f) => +f.toFixed(6)), [145.08, 145.92], "0.96 MS/s");
});

test("Look here tunes the strongest station heard in the band, else the band's middle, on its raster", () => {
  assert.equal(lookAt(FM, [heard(88.1, 12), heard(102.4, 37), heard(91.9, 32)]), 102.4);
  assert.equal(lookAt(FM, []), 97.8, "the middle of 87.5 to 108, on the 100 kHz raster");
  assert.equal(lookAt(TWO_M, []), 145.0);
  assert.ok(PMR.channels_mhz.includes(lookAt(PMR, [])), "a channel of the list, not between two");
  assert.equal(lookAt(FM, [heard(150, 40), heard(91.9, 10)]), 91.9, "only what is in the band");
});

test("a receiver that stays where it is looks inside what it hears, clear of its centre", () => {
  const span = [143.8, 146.2];
  assert.equal(lookAt(TWO_M, [heard(147, 30), heard(145.5, 9)], span), 145.5);
  assert.equal(lookAt(TWO_M, [heard(145.0, 20)], span), 145.25, "a station on the centre spike is passed over");
  assert.equal(lookAt(TWO_M, [heard(146.15, 30), heard(145.5, 9)], span), 145.5, "nor one inside its edges but not heard there");
  assert.equal(lookAt(FM, [], [101.3, 103.7]), 102.8, "nothing heard there: near the middle, on the raster");
});

test("the frequency tuned puts the dongle's centre where the receiver should look", () => {
  assert.equal(lookTune(98, "wfm"), 98.3);
  assert.equal(lookTune(127.5, "am"), 127.75);
  assert.equal(lookTune(600, null), 600.25, "a digital band is looked at as narrowband");
});

test("a receiver dragged onto the dial lands on a frequency it can look at", () => {
  assert.equal(dragCentre(scale.x(446.1), scale, BANDS, 2.4), 446.1, "a band narrower than the window: its middle");
  assert.equal(dragCentre(scale.x(144.3), scale, BANDS, 2.4), 145, "2 m, heard whole from its middle at 2.4 MS/s");
  assert.equal(dragCentre(scale.x(144.3), scale, BANDS, 1.92), 144.3, "not at 1.92 MS/s: where the pointer is");
  assert.equal(dragCentre(scale.x(98.64), scale, BANDS, 2.4), 98.6, "to 100 kHz");
  assert.equal(dragCentre(0, scale, BANDS, 2.4), 25.2, "never past the tuner's end");
  assert.equal(dragCentre(1, scale, BANDS, 2.4), 1764.8);
});

test("a window is said to be on the band at its centre, else the one it shows most of", () => {
  assert.equal(bandAt(BANDS, 434).id, "srd433", "the narrowest band");
  assert.equal(bandOfWindow(BANDS, 96.8, 99.2).id, "fm");
  assert.equal(bandOfWindow(BANDS, 106.9, 109.3).id, "fm", "half out of the band");
  assert.equal(bandOfWindow(BANDS, 1200, 1202.4), null, "outside the plan");
});

// The line beside a receiver's window: the dial's pieced scale makes 2.4 MHz look wide on the
// broadcast band and narrow on the TV band, so the words say how much of the band it really is.
const says = (span) => windowWords("A", span, BANDS, (b) => b.id);

test("a receiver's window holding a whole band says so", () => {
  assert.equal(says([444.56, 446.96]), "Receiver A hears about 2.3 MHz at once: all of the pmr446 band (446–446.2 MHz)");
  assert.equal(says([143.8, 146.2]), "Receiver A hears about 2.3 MHz at once: all of the 2m band (144–146 MHz)");
});

test("a window on part of a band says which share, from the band's own kind of channel", () => {
  // Broadcast FM keeps 120 kHz from each edge: 2.16 of the band's 20.5 MHz.
  assert.equal(says([97.9, 100.3]), "Receiver A hears about 2.2 MHz at once: about a tenth of the fm band (87.5–108 MHz)");
  assert.equal(says([157.8, 160.2]), "Receiver A hears about 2.3 MHz at once: about a third of the marine band (156–162.025 MHz)");
  assert.equal(says([500, 502.4]), "Receiver A hears about 2.3 MHz at once: a small part of the dvbt band (470–694 MHz), 2.3 MHz of its 224 MHz");
});

test("a window across a band's edge says how much is in it and how far past", () => {
  assert.equal(says([107, 109.4]), "Receiver A hears about 2.2 MHz at once: the top 0.9 MHz of the fm band (87.5–108 MHz) and 1.3 MHz above it");
  assert.equal(says([117, 119.4]), "Receiver A hears about 2.3 MHz at once: the lowest 1.3 MHz of the airband band (118–137 MHz) and 0.9 MHz below it");
});

test("a window outside every band says where it is", () => {
  assert.equal(says([300, 302.4]), "Receiver A hears about 2.3 MHz at once, 300.06 to 302.34 MHz, outside every band on the dial");
});
