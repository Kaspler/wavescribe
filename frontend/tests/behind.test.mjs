// How the page says whether the machine keeps up, from what the status carries.
import assert from "node:assert/strict";
import { test } from "node:test";
import { BEHIND_S, columnLag, headerLag, keepsUp, skippedChecks, worstText } from "../src/lib/behind.ts";

test("a status without load, or with load ok, keeps up", () => {
  assert.equal(keepsUp(null), true);
  assert.equal(keepsUp({ stations: {} }), true);
  assert.equal(keepsUp({ load: { state: "ok", reason: null } }), true);
  assert.deepEqual(headerLag({ load: { state: "ok", reason: null }, stations: { a: { behind_s: { text: 600 } } } }), { behind: false, minutes: null });
});

test("behind or overloaded says by how much, from the station furthest behind", () => {
  const st = {
    load: { state: "behind", reason: "speech recognition can't keep up" },
    stations: { a: { behind_s: { text: 95, translation: 400 } }, b: { behind_s: { text: 250 } }, c: {} },
  };
  assert.equal(worstText(st), 250);
  assert.deepEqual(headerLag(st), { behind: true, minutes: 4 });
  assert.deepEqual(headerLag({ ...st, load: { state: "overloaded", reason: null } }), { behind: true, minutes: 4 });
});

test("behind without a text lag to show still isn't live", () => {
  assert.deepEqual(headerLag({ load: { state: "behind", reason: null }, stations: { a: { behind_s: { text: 12 } } } }), { behind: true, minutes: null });
  assert.deepEqual(headerLag({ load: { state: "behind", reason: null } }), { behind: true, minutes: null });
});

test("a column says it is behind only past the threshold, text and translation apart", () => {
  assert.deepEqual(columnLag(undefined), { text: null, translation: null });
  assert.deepEqual(columnLag({ behind_s: { text: BEHIND_S - 1, translation: null } }), { text: null, translation: null });
  assert.deepEqual(columnLag({ behind_s: { text: BEHIND_S, translation: 185 } }), { text: 1, translation: 3 });
  assert.deepEqual(columnLag({ behind_s: { text: "soon", translation: -5 } }), { text: null, translation: null });
});

test("skipped alert checks add up over stations, and a server that doesn't say counts none", () => {
  assert.equal(skippedChecks({ stations: { a: { dropped: { asr: 2, rules: 3 } }, b: { dropped: { rules: 1 } }, c: {} } }), 4);
  assert.equal(skippedChecks({ stations: { a: { dropped: null } } }), 0);
  assert.equal(skippedChecks(undefined), 0);
});
