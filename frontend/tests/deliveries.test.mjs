// A webhook says which alerts haven't got there yet and which were given up, and its log keys
// each delivery without the store's row id.
import assert from "node:assert/strict";
import { test } from "node:test";
import { alertsLine, deliveryKey, maxAge } from "../src/manage/deliveries.ts";

const st = { sent: 3, failed: 1, dropped: 0, queued: 2, alerts_waiting: 0, alerts_oldest_age_s: null, alerts_expired: 0, alerts_dropped: 0 };

test("alerts waiting, with how long the oldest has waited", () => {
  assert.equal(alertsLine({ ...st, alerts_waiting: 2, alerts_oldest_age_s: 185.4 }), "2 alerts waiting, oldest 3 min");
  assert.equal(alertsLine({ ...st, alerts_waiting: 1, alerts_oldest_age_s: 12 }), "1 alert waiting, oldest 12 s");
  assert.equal(alertsLine({ ...st, alerts_waiting: 1, alerts_oldest_age_s: 5 * 3600 }), "1 alert waiting, oldest 5 h");
  assert.equal(alertsLine({ ...st, alerts_waiting: 1 }), "1 alert waiting", "no age known: none made up");
});

test("given up and dropped since the start, only when not 0", () => {
  assert.equal(alertsLine({ ...st, alerts_expired: 1 }), "1 alert given up as too old");
  assert.equal(alertsLine({ ...st, alerts_waiting: 3, alerts_oldest_age_s: 60, alerts_expired: 2, alerts_dropped: 1 }),
    "3 alerts waiting, oldest 1 min · 2 alerts given up as too old · 1 alert dropped from a full queue");
});

test("nothing waiting and nothing lost: nothing to say, also from a server before the fields", () => {
  assert.equal(alertsLine(st), null);
  assert.equal(alertsLine({ sent: 1 }), null);
  assert.equal(alertsLine(null), null);
});

test("how long an alert is still sent, in the unit that reads best", () => {
  assert.equal(maxAge(24), "24 h");
  assert.equal(maxAge(1.5), "1.5 h");
  assert.equal(maxAge(0.5), "30 min");
  assert.equal(maxAge(168), "7 days");
  assert.equal(maxAge(36), "36 h");
  for (const bad of [undefined, null, 0, -1, "24"]) assert.equal(maxAge(bad), null, String(bad));
});

test("a delivery is keyed by its row in the store, else by what it was", () => {
  assert.equal(deliveryKey({ id: 41, output_id: "hub", t: 1, event: "alert", ref: "7" }), "row:41");
  // In memory (--no-store): no id, and `output` where the store has `output_id`.
  const a = { output: "hub", t: 1764280000.123, event: "alert", ref: "7", delivery: "ab12", after_restart: false };
  const b = { ...a, t: 1764280000.456 };
  const c = { output: "hub", t: a.t, event: "utterance", ref: "5,6", delivery: null };
  assert.notEqual(deliveryKey(a), deliveryKey(b));
  assert.notEqual(deliveryKey(a), deliveryKey(c));
  assert.equal(deliveryKey(a), deliveryKey({ ...a }), "the same entry keeps its key from one poll to the next");
  assert.equal(deliveryKey({ output: "hub", t: 2, event: "test", ref: null }).includes("undefined"), false);
});
