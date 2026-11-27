// Stepping by hand as a radio steps: along the band's raster, channel by channel where the band
// lists them, ten at a time with Shift, on past a band's edge and never past the tuner's reach.
import assert from "node:assert/strict";
import { test } from "node:test";
import { defaultStep, keyStep, nextChannel, snap, stepChoices, stepFrom, stepLabel } from "../src/manage/tuning.ts";

const TUNER = [24, 1766];
const band = (id, start_mhz, stop_mhz, mode, step_khz, channels_mhz = []) => ({ id, start_mhz, stop_mhz, mode, step_khz, channels_mhz });
const FM = band("fm", 87.5, 108, "wfm", 100);
const AIR = band("airband", 118, 137, "am", 8.3333333);
const TWO_M = band("2m", 144, 146, "nfm", 12.5);
const PMR = band("pmr446", 446, 446.2, "nfm", 12.5, Array.from({ length: 16 }, (_, i) => +(446.00625 + i * 0.0125).toFixed(5)));
// FRS numbers its channels out of frequency order: 7 is at 462.7125, 8 at 467.5625.
const FRS = band("frs", 462.55, 467.725, "nfm", 12.5, [462.5625, 462.5875, 462.6125, 462.6375, 462.6625, 462.6875, 462.7125, 467.5625, 467.5875]);

test("a step on the raster goes one raster point up or down", () => {
  assert.equal(stepFrom(98.6, 1, 100, FM, TUNER), 98.7);
  assert.equal(stepFrom(98.6, -1, 100, FM, TUNER), 98.5);
  assert.equal(stepFrom(145.5, 1, 12.5, TWO_M, TUNER), 145.5125);
  assert.equal(stepFrom(145.5, -1, 6.25, TWO_M, TUNER), 145.49375, "a raster finer than the band's");
  assert.equal(stepFrom(145.5, 1, 25, TWO_M, TUNER), 145.525);
});

test("from between two raster points, the first step lands on the nearer one that way", () => {
  assert.equal(stepFrom(98.63, 1, 100, FM, TUNER), 98.7);
  assert.equal(stepFrom(98.63, -1, 100, FM, TUNER), 98.6);
  assert.equal(stepFrom(98.67, -1, 100, FM, TUNER), 98.6);
  assert.equal(stepFrom(98.67, 10, 100, FM, TUNER), 99.6);
});

test("the airband's 8.33 kHz raster counts from the band's start", () => {
  assert.equal(stepFrom(118, 1, 8.3333333, AIR, TUNER), 118.00833);
  assert.equal(stepFrom(118.00833, 1, 8.3333333, AIR, TUNER), 118.01667, "a frequency written to five decimals is on its raster");
  assert.equal(stepFrom(118.025, -3, 8.3333333, AIR, TUNER), 118);
  assert.equal(stepFrom(118, 1, 25 / 3, AIR, TUNER), 118.00833, "this page's 25/3 is the plan's 8.3333333");
  let f = 118;
  for (let k = 0; k < 120; k++) f = stepFrom(f, 1, 8.3333333, AIR, TUNER);
  assert.equal(f, 119, "a hundred and twenty steps without drifting");
});

test("Shift is ten steps, and the keys go up with up and right, down with down and left", () => {
  assert.deepEqual(keyStep("ArrowUp", false, false), { n: 1, channel: false });
  assert.deepEqual(keyStep("ArrowRight", false, false), { n: 1, channel: false });
  assert.deepEqual(keyStep("ArrowDown", false, false), { n: -1, channel: false });
  assert.deepEqual(keyStep("ArrowLeft", true, false), { n: -10, channel: false });
  assert.deepEqual(keyStep("ArrowUp", true, true), { n: 10, channel: false });
  assert.equal(stepFrom(98.6, 10, 100, FM, TUNER), 99.6);
  assert.equal(stepFrom(446.00625, -10, 12.5, PMR, TUNER), 445.88125);
  assert.equal(keyStep("Enter", false, false), null);
  assert.equal(keyStep("a", false, false), null);
});

test("Page Up and Page Down go channel by channel where the band lists them, else ten steps", () => {
  assert.deepEqual(keyStep("PageUp", false, true), { n: 1, channel: true });
  assert.deepEqual(keyStep("PageDown", true, true), { n: -1, channel: true });
  assert.deepEqual(keyStep("PageUp", false, false), { n: 10, channel: false });
  assert.deepEqual(keyStep("PageDown", false, false), { n: -10, channel: false });
});

test("by channel, a step goes to the next channel of the list, round from the last to the first", () => {
  assert.equal(stepFrom(446.09375, 1, "channels", PMR, TUNER), 446.10625);
  assert.equal(stepFrom(446.09375, -1, "channels", PMR, TUNER), 446.08125);
  assert.equal(stepFrom(446.19375, 1, "channels", PMR, TUNER), 446.00625, "after channel 16, channel 1");
  assert.equal(stepFrom(446.00625, -1, "channels", PMR, TUNER), 446.19375, "before channel 1, channel 16");
  assert.equal(stepFrom(446.09375, 10, "channels", PMR, TUNER), 446.01875, "channel 8, ten on: channel 2");
  assert.equal(nextChannel(PMR.channels_mhz, 446.0937, 1), 446.10625, "within 2 kHz of a channel is on it");
});

test("by channel from between two channels, the first step lands on the nearest one that way", () => {
  assert.equal(stepFrom(446.1, 1, "channels", PMR, TUNER), 446.10625);
  assert.equal(stepFrom(446.1, -1, "channels", PMR, TUNER), 446.09375);
  assert.equal(stepFrom(446.25, 1, "channels", PMR, TUNER), 446.00625, "above every channel, going up starts again at the first");
  assert.equal(stepFrom(445.9, -1, "channels", PMR, TUNER), 446.19375, "below every channel, going down starts at the last");
});

test("channels follow the list's numbering, not the frequency order", () => {
  assert.equal(stepFrom(462.7125, 1, "channels", FRS, TUNER), 467.5625, "channel 7, then channel 8");
  assert.equal(stepFrom(467.5625, -1, "channels", FRS, TUNER), 462.7125);
});

test("on a raster, a step goes on past the band's edge; by channel it stays in the list", () => {
  assert.equal(stepFrom(108, 1, 100, FM, TUNER), 108.1);
  assert.equal(stepFrom(87.5, -1, 100, FM, TUNER), 87.4);
  assert.equal(stepFrom(137, 1, 8.3333333, AIR, TUNER), 137.00833);
  assert.equal(stepFrom(446.19375, 1, 12.5, PMR, TUNER), 446.20625, "a raster step leaves the channels behind");
  assert.equal(stepFrom(108.1, 1, 100, null, TUNER), 108.2, "outside every band, on the raster from 0 Hz");
  assert.equal(stepFrom(108.1, 1, "channels", null, TUNER), 108.1125, "channels where there are none: the usual raster");
});

test("a run of steps keeps its offset past a band's edge", () => {
  // Past PMR446's last channel the 12.5 kHz raster goes on from the channels, not from 0 Hz.
  assert.equal(stepFrom(446.20625, 1, 12.5, null, TUNER), 446.21875);
  assert.equal(stepFrom(446.26875, -10, 12.5, null, TUNER), 446.14375);
  // The Americas' FM channels sit on odd tenths, half a 200 kHz step from 0 Hz.
  assert.equal(stepFrom(88.1, 1, 200, null, TUNER), 88.3);
  assert.equal(stepFrom(137.00833, 1, 8.3333333, null, TUNER), 137.01667, "past the airband, on its raster");
});

test("a step never goes past the tuner's reach", () => {
  assert.equal(stepFrom(1765.95, 10, 100, null, TUNER), 1766);
  assert.equal(stepFrom(24.05, -10, 100, null, TUNER), 24);
  assert.equal(stepFrom(24, -1, 12.5, null, TUNER), 24);
});

test("a click lands on the band's nearest channel, else on the raster", () => {
  assert.equal(snap(446.098, 12.5, PMR), 446.09375);
  assert.equal(snap(446.302, 12.5, PMR), 446.30625, "beyond the band, on the raster of its channels");
  assert.equal(snap(98.64, 100, FM), 98.6);
  assert.equal(snap(118.005, 8.3333333, AIR), 118.00833);
});

test("a band starts at its channels where it lists them, else at its own raster", () => {
  assert.equal(defaultStep(PMR, "nfm"), "channels");
  assert.equal(defaultStep(FM, "wfm"), 100);
  assert.equal(defaultStep(AIR, "am"), 8.3333333);
  assert.equal(defaultStep(TWO_M, "nfm"), 12.5);
  assert.equal(defaultStep(FM, "nfm"), 12.5, "a narrow channel heard in the FM band takes the mode's step");
  assert.equal(defaultStep(null, "am"), 25);
});

test("the steps offered: channels first where there are some, then each raster once, finest first", () => {
  assert.deepEqual(stepChoices(PMR, "nfm"), ["channels", 5, 6.25, 12.5, 25, 100]);
  assert.deepEqual(stepChoices(AIR, "am"), [5, 8.3333333, 25, 100]);
  assert.deepEqual(stepChoices(FM, "wfm"), [50, 100, 200]);
  assert.deepEqual(stepChoices(null, "nfm"), [5, 6.25, 12.5, 25, 100]);
  assert.deepEqual(stepChoices(band("2m-wide", 144, 148, "nfm", 5), "nfm"), [5, 6.25, 12.5, 25, 100]);
  assert.deepEqual(stepChoices(band("x", 1, 2, "nfm", 7.5), "nfm"), [5, 6.25, 7.5, 12.5, 25, 100], "a band's own raster among them");
  assert.equal(stepLabel(8.3333333), "8.33 kHz");
  assert.equal(stepLabel("channels"), "Channels");
});
