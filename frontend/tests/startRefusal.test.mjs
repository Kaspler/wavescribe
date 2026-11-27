// A refused Start offers "Switch it on" only for a station switched off on the page.
import assert from "node:assert/strict";
import { test } from "node:test";
import { switchOnFor } from "../src/lib/startRefusal.ts";

const off = { error: "switched off on the page", code: "switched_off" };

test("a start refused because the station is switched off on the page can be switched on", () => {
  assert.equal(switchOnFor("/api/stations/rte1/start", off), "rte1");
  assert.equal(switchOnFor("/api/manage/stations/rte1/start", off), "rte1", "started to play only");
  assert.equal(switchOnFor("/api/stations/r%C3%A1dio/start", off), "rádio");
});

test("anything else is only said", () => {
  const unconfirmed = { error: "switched off until you confirm you may receive it under the region chosen", code: "unconfirmed" };
  assert.equal(switchOnFor("/api/stations/pmr/start", unconfirmed), null);
  assert.equal(switchOnFor("/api/stations/rte1/start", { error: "Receiver A is tuned by hand" }), null);
  assert.equal(switchOnFor("/api/stations/rte1/stop", off), null, "not a start");
  assert.equal(switchOnFor("/api/stations/rte1/start", null), null);
  assert.equal(switchOnFor("/api/stations/rte1/start", "switched_off"), null);
});
