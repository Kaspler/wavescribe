// "Since you last looked": the last moment this browser showed the page before the present look.
import assert from "node:assert/strict";
import { test } from "node:test";
import { AWAY_MIN_S, BACK_FOR_S, awaySince, onScreen } from "../src/lib/away.ts";

const NONE = { seen: null, back: null, away: null };

test("a look begins after a gap; until then the page is still being looked at", () => {
  const first = onScreen(NONE, 1000);
  assert.deepEqual(first, { seen: 1000, back: 1000, away: null });
  const still = onScreen(onScreen(first, 1020), 1040);
  assert.deepEqual(still, { seen: 1040, back: 1000, away: null });
  // Out of sight (or asleep) for longer than the gap: the next look began, from when last seen.
  const again = onScreen(still, 1040 + AWAY_MIN_S + 5);
  assert.deepEqual(again, { seen: 1040 + AWAY_MIN_S + 5, back: 1040 + AWAY_MIN_S + 5, away: 1040 });
});

test("it is offered only when known, fresh, and within reach of a summary", () => {
  const r = { seen: 9000, back: 9000, away: 5400 };
  assert.equal(awaySince(r, 9000, 1440), 5400);
  assert.equal(awaySince(NONE, 9000, 1440), null);
  assert.equal(awaySince(onScreen(NONE, 9000), 9000, 1440), null); // the first look ever
  assert.equal(awaySince(r, 9000 + BACK_FOR_S + 1, 1440), null); // looking for a while already
  assert.equal(awaySince({ seen: 50_000, back: 50_000, away: 50_000 - 7 * 3600 }, 50_000, 360), null);
  assert.equal(awaySince({ seen: 50_000, back: 50_000, away: 50_000 - 7 * 3600 }, 50_000, 1440), 50_000 - 7 * 3600);
});
