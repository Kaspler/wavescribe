// A receiver overloaded by a transmitter close by, as the page reads the server's report of it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { channelOf, overloadOf, warningBeside } from "../src/lib/overload.ts";

const SAID = "A very strong signal nearby overloaded Receiver A. Move the radio further away, or lower the gain in Settings → Receivers.";
const OFF = "A very strong signal nearby overloaded Receiver A. It was at about 445.906 MHz, not on one of the channels of PMR446 walkie-talkies (446.00625 to 446.19375 MHz), so nothing was caught. Move the radio further away, or lower the gain in Settings → Receivers.";

test("on a channel: the channel it was caught on is named beside the time", () => {
  const o = overloadOf({ t: 1795795200.5, freq_hz: 446006250, on_channel: true, message: SAID });
  assert.deepEqual(o, { t: 1795795200.5, freq_hz: 446006250, on_channel: true, message: SAID });
  assert.equal(channelOf(o), 446006250);
});

test("off the channels: no channel is named, the frequency is left to the server's sentence", () => {
  const o = overloadOf({ t: 10, freq_hz: 445906000, on_channel: false, message: OFF });
  assert.deepEqual(o, { t: 10, freq_hz: 445906000, on_channel: false, message: OFF });
  assert.equal(channelOf(o), null);
  // Its frequency not clear either.
  assert.equal(channelOf(overloadOf({ t: 10, freq_hz: null, on_channel: false, message: SAID })), null);
});

test("on a channel but not clearly: neither channel nor frequency", () => {
  const o = overloadOf({ t: 10, freq_hz: null, on_channel: null, message: SAID });
  assert.deepEqual(o, { t: 10, freq_hz: null, on_channel: null, message: SAID });
  assert.equal(channelOf(o), null);
});

test("listening by hand: the tuner's report has no on_channel, and names no channel", () => {
  const o = overloadOf({ t: 10, freq_hz: null, message: SAID });
  assert.deepEqual(o, { t: 10, freq_hz: null, on_channel: null, message: SAID });
  assert.equal(channelOf(o), null);
});

test("a frequency that can't be one is none, and an unreadable on_channel is not known", () => {
  assert.equal(overloadOf({ t: 10, freq_hz: 0, on_channel: true, message: SAID }).freq_hz, null);
  assert.equal(channelOf(overloadOf({ t: 10, freq_hz: 0, on_channel: true, message: SAID })), null);
  assert.equal(overloadOf({ t: 10, freq_hz: 446006250, on_channel: "yes", message: SAID }).on_channel, null);
  assert.equal(channelOf(overloadOf({ t: 10, freq_hz: 446006250, on_channel: "yes", message: SAID })), null);
});

test("no overload, or one that can't be read, is no note", () => {
  for (const bad of [null, undefined, "overload", {}, { t: 10 }, { message: SAID }, { t: "10", message: SAID }, { t: 10, message: "" }, { t: Number.NaN, message: SAID }]) {
    assert.equal(overloadOf(bad), null, JSON.stringify(bad));
  }
});

test("a watch's warning is said once: not again when it is the overload's own sentence", () => {
  const o = overloadOf({ t: 10, freq_hz: 446006250, on_channel: true, message: SAID });
  assert.equal(warningBeside(SAID, o), null);
  const noise = "deaf: noise across the whole window; learnt as the new floor if it stays";
  assert.equal(warningBeside(noise, o), noise);
  assert.equal(warningBeside(noise, null), noise);
  assert.equal(warningBeside(null, o), null);
  assert.equal(warningBeside(undefined, null), null);
});
