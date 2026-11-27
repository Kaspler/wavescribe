// "Caught here": what watches caught, grouped by frequency and filtered.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandsIn, centreIn, deviceIn, filterCatches, groupCatches, hasSpeech, sliceIn, stationIn, talksNowAndThen, technical, voiceWord, windowWords } from "../src/manage/catches.ts";

const NOW = 1_800_000_000;
const caught = (id, freq_hz, ago, more = {}) => ({
  id, station: "walkies", freq_hz, mhz: freq_hz / 1e6, t_start: NOW - ago, t_end: NOW - ago + 4, duration: 4, mode: "nfm",
  band: "pmr446", label: "PMR446 ch 1", snr_db: 18, peak_db: -40, voice: true, reason: null, privacy: "private", utterances: [], ...more,
});

const list = [
  caught(1, 446006250, 7200, { utterances: [{ id: 10, t: NOW - 7200, language: "es", text: "¿Me oyes?" }] }),
  caught(2, 446018750, 600, { voice: false, label: "PMR446 ch 2" }),
  caught(3, 446006250, 60, { voice: null, name: "Old name" }),
  caught(4, 145500000, 90000, { band: "2m", label: "Amateur 2 m", privacy: "amateur", voice: null }),
];

test("catches group by frequency, the newest frequency first and each one's catches newest first", () => {
  const spots = groupCatches(list);
  assert.deepEqual(spots.map((s) => s.freq_hz), [446006250, 446018750, 145500000]);
  assert.deepEqual(spots[0].catches.map((c) => c.id), [3, 1]);
  assert.equal(spots[0].last, NOW - 60);
  assert.equal(spots[0].label, "PMR446 ch 1");
});

test("a note made here wins over the one the catches came with", () => {
  assert.equal(groupCatches(list)[0].name, "Old name");
  const spots = groupCatches(list, [{ freq_hz: 446006250, name: "Family", tags: ["home"], note: "Calls before dinner" }]);
  assert.deepEqual([spots[0].name, spots[0].tags, spots[0].note], ["Family", ["home"], "Calls before dinner"]);
  assert.deepEqual([spots[1].name, spots[1].tags, spots[1].note], ["", [], ""]);
});

test("filters by time, band and speech", () => {
  const ids = (f) => filterCatches(list, f, NOW).map((c) => c.id);
  assert.deepEqual(ids({ band: null, speech: false, hours: 1 }), [2, 3]);
  assert.deepEqual(ids({ band: null, speech: false, hours: 168 }), [1, 2, 3, 4]);
  assert.deepEqual(ids({ band: "2m", speech: false, hours: 168 }), [4]);
  assert.deepEqual(ids({ band: null, speech: true, hours: 168 }), [1]);
});

test("speech is words written down or speech found; not checked isn't speech", () => {
  assert.equal(hasSpeech(list[0]), true);
  assert.equal(hasSpeech(caught(5, 1, 1, { voice: true })), true);
  assert.equal(hasSpeech(caught(6, 1, 1, { voice: null, utterances: [{ id: 1, t: 0, language: "en", text: "  " }] })), false);
  assert.deepEqual([true, false, null].map(voiceWord), ["voice", "no voice", "not checked"]);
});

test("bands come from the catches, once each", () => {
  assert.deepEqual(bandsIn(list), ["pmr446", "2m"]);
});

test("a band can be watched when its channels talk now and then", () => {
  assert.equal(talksNowAndThen({ mode: "nfm", privacy: "private" }), true);
  assert.equal(talksNowAndThen({ mode: "am", privacy: "utility" }), true);
  assert.equal(talksNowAndThen({ mode: "wfm", privacy: "broadcast" }), false);
  assert.equal(talksNowAndThen({ mode: "am", privacy: "broadcast" }), false);
  assert.equal(talksNowAndThen({ mode: null, privacy: "digital" }), false);
});

test("what the server's refusals offer is read out of them", () => {
  assert.deepEqual(sliceIn("Airband is 19.00 MHz wide, and a watch covers what one receiver hears at once, about 2.2 MHz: watch a slice of it, e.g. 126.400:128.600"), [126.4, 128.6]);
  assert.equal(sliceIn("no band 'x' in the band plan (wavescribe radio bands lists them), or give a range in MHz, e.g. 446.0:446.2"), null);
  assert.equal(centreIn("station w: device 'a' doesn't hear all of PMR446 (16 of its 16 channels are outside its band). Give the device center_mhz = 445.97500, or watch the part it hears"), 445.975);
  assert.equal(centreIn("there is already a station called 'w'"), null);
  const moved = "station 'w' (w): device 'b' doesn't hear all of PMR446 (16 of its 16 channels are outside its band). Give the device center_mhz = 445.97500, or watch the part it hears";
  assert.equal(stationIn(moved), "w");
  assert.equal(stationIn("station 'Walkies' (walkies): device 'a' centre_mhz must be a number"), "walkies", "a stations-file station: its id in brackets");
  assert.equal(stationIn("Receiver A is scanning: wait for it, or stop the scan."), null);
  assert.equal(deviceIn(moved), "b");
  assert.ok(technical(moved), "ids in quotes and setting names go under Details");
  assert.ok(!technical("Receiver A is scanning: wait for it, or stop the scan."), "a sentence is said as it is");
});

test("a window in words", () => {
  const names = (id) => ({ pmr446: "Walkie-talkies" })[id] || null;
  assert.equal(windowWords("pmr446", names), "Walkie-talkies");
  assert.equal(windowWords("unknown", names), "unknown");
  assert.equal(windowWords([446, 446.1], names), "446.000–446.100 MHz");
});
