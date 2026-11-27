// A receiver overloaded by a transmitter close by, as the page reads the server's report of it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { overloadOf, warningBeside } from "../src/lib/overload.ts";

const SAID = "A very strong signal nearby overloaded Receiver A. Move the radio further away, or lower the gain in Settings → Receivers.";

test("an overload is read with its time, its channel where known, and the server's sentence", () => {
  assert.deepEqual(overloadOf({ t: 1795795200.5, freq_hz: 446006250, message: SAID }), { t: 1795795200.5, freq_hz: 446006250, message: SAID });
  // Listening by hand: the channel on top isn't known.
  assert.deepEqual(overloadOf({ t: 10, freq_hz: null, message: SAID }), { t: 10, freq_hz: null, message: SAID });
  assert.equal(overloadOf({ t: 10, freq_hz: 0, message: SAID }).freq_hz, null);
});

test("no overload, or one that can't be read, is no note", () => {
  for (const bad of [null, undefined, "overload", {}, { t: 10 }, { message: SAID }, { t: "10", message: SAID }, { t: 10, message: "" }, { t: Number.NaN, message: SAID }]) {
    assert.equal(overloadOf(bad), null, JSON.stringify(bad));
  }
});

test("a watch's warning is said once: not again when it is the overload's own sentence", () => {
  const o = overloadOf({ t: 10, freq_hz: 446006250, message: SAID });
  assert.equal(warningBeside(SAID, o), null);
  const noise = "deaf: noise across the whole window; learnt as the new floor if it stays";
  assert.equal(warningBeside(noise, o), noise);
  assert.equal(warningBeside(noise, null), noise);
  assert.equal(warningBeside(null, o), null);
  assert.equal(warningBeside(undefined, null), null);
});
