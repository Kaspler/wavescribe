// Where a column's lines were heard: said once per run of a watch's channel, never for a station
// whose channel is the station itself.
import assert from "node:assert/strict";
import { test } from "node:test";
import { channelHeads, channelText } from "../src/reading/channels.ts";

const para = (freq_hz, label = null) => ({ utts: [{ freq_hz, label }] });

test("a channel is written as its frequency and the band plan's name for it", () => {
  assert.equal(channelText({ freq_hz: 446006250, label: "PMR446 walkie-talkies, channel 1" }), "446.00625 MHz · PMR446 walkie-talkies, channel 1");
  assert.equal(channelText({ freq_hz: 145500000, label: "Amateur 2 m" }), "145.500 MHz · Amateur 2 m");
  assert.equal(channelText({ freq_hz: 1200125000, label: null }), "1200.125 MHz", "a frequency no plan lists");
  assert.equal(channelText({ freq_hz: null, label: null }), null, "a stream's line");
  assert.equal(channelText({}), null, "a server that doesn't say");
});

test("in a watch's column, the first paragraph of each run on one channel names it", () => {
  const paras = [para(446006250), para(446006250), para(446018750), para(446006250), para(446006250)];
  assert.deepEqual(channelHeads(paras, true), [true, false, true, true, false]);
});

test("a station's own channel never changes, so its column names none", () => {
  assert.deepEqual(channelHeads([para(98600000, "FM broadcast"), para(98600000, "FM broadcast")], false), [false, false]);
  assert.deepEqual(channelHeads([para(null), para(null)], false), [false, false], "a stream");
});

test("a line with no frequency names nothing, and the next channel is named again", () => {
  assert.deepEqual(channelHeads([para(446006250), para(null), para(446006250)], true), [true, false, true]);
  assert.deepEqual(channelHeads([], true), []);
});
