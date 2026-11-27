// A station started from the list of stopped ones: its row is held for a moment, never for good.
import assert from "node:assert/strict";
import { test } from "node:test";
import { HOLD_MS, makeHolds, placeRows } from "../src/lib/hold.ts";

/** Timers run by hand: `tick(ms)` moves the clock and runs what is due. */
function clock() {
  let now = 0;
  let next = 1;
  const due = new Map();
  return {
    set(f, ms) { due.set(next, { f, at: now + ms }); return next++; },
    clear(h) { due.delete(h); },
    tick(ms) {
      now += ms;
      for (const [h, t] of [...due]) if (t.at <= now) { due.delete(h); t.f(); }
    },
  };
}

const stations = [{ id: "one" }, { id: "two" }, { id: "three" }];
const rows = (live, held) => {
  const p = placeRows(stations, (id) => id in live, (id) => !!live[id]?.connected, (id) => held.has(id));
  return { on: p.on.map((s) => s.id), off: p.off.map((s) => s.id), live: p.live, stopped: p.stopped };
};

test("a started station moves to the running ones by itself, with the pointer still on its row", () => {
  const timers = clock();
  let drawn = 0;
  const held = makeHolds(timers, () => drawn++);
  const live = {};
  held.hold("two"); // Start pressed
  live.two = { connected: true }; // the server's next status says it runs
  assert.deepEqual(rows(live, held), { on: [], off: ["one", "two", "three"], live: 1, stopped: 2 });
  timers.tick(HOLD_MS - 1);
  assert.equal(held.has("two"), true);
  timers.tick(1); // nothing left the list, nothing moved: the hold ends anyway
  assert.equal(drawn, 1);
  assert.deepEqual(rows(live, held), { on: ["two"], off: ["one", "three"], live: 1, stopped: 2 });
});

test("the headings count what runs, wherever a held row is drawn", () => {
  const held = makeHolds(clock(), () => {});
  held.hold("one");
  const p = rows({ one: { connected: false } }, held); // started, still connecting
  assert.deepEqual([p.live, p.stopped], [0, 2]);
});

test("moving over the list keeps the rows a little longer; leaving it lets them go at once", () => {
  const timers = clock();
  let drawn = 0;
  const held = makeHolds(timers, () => drawn++);
  held.hold("one");
  timers.tick(HOLD_MS - 500);
  held.stir(); // on the way to the next Start
  timers.tick(HOLD_MS - 500);
  assert.equal(held.has("one"), true);
  held.hold("three"); // a second start holds both
  timers.tick(HOLD_MS - 1);
  assert.deepEqual([held.has("one"), held.has("three"), drawn], [true, true, 0]);
  held.release(); // the pointer left
  assert.deepEqual([held.has("one"), held.has("three"), drawn], [false, false, 1]);
  timers.tick(HOLD_MS * 2); // the timer went with them
  assert.equal(drawn, 1);
});

test("nothing held: moving and leaving do nothing", () => {
  const timers = clock();
  let drawn = 0;
  const held = makeHolds(timers, () => drawn++);
  held.stir();
  held.release();
  timers.tick(HOLD_MS * 2);
  assert.equal(drawn, 0);
});
