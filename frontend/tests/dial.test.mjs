// The dial's geometry: one bar for everything a receiver tunes, on which the popular bands stay
// legible, receivers never vanish, and a receiver sent to a band looks where its stations are.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandAt, bandOfWindow, bestCentre, dragCentre, labelRows, lookTune, makeScale, marks, segments, ticks } from "../src/manage/dial.ts";

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

test("a receiver sent to a band looks where most of what was heard there is", () => {
  const fm = BANDS[0];
  assert.equal(bestCentre(fm, [88.1, 91.2, 101.5, 101.9, 102.6, 103.1], 2.4), 102.3);
  assert.equal(bestCentre(fm, [], 2.4), 88.58, "nothing heard: from the bottom of the band");
  assert.equal(bestCentre(fm, [107.9], 2.4), 106.92, "never past the band's end");
  assert.equal(bestCentre(BANDS.find((b) => b.id === "pmr446"), [446.00625], 2.4), 446.1, "a narrow band whole");
});

test("the frequency tuned puts the dongle's centre where the receiver should look", () => {
  assert.equal(lookTune(98, "wfm"), 98.3);
  assert.equal(lookTune(127.5, "am"), 127.75);
  assert.equal(lookTune(600, null), 600.25, "a digital band is looked at as narrowband");
});

test("a receiver dragged onto the dial lands on a frequency it can look at", () => {
  assert.equal(dragCentre(scale.x(446.1), scale, BANDS, 2.4), 446.1, "a band narrower than the window: its middle");
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
