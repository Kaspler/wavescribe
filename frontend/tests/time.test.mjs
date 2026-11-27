// A time the store couldn't give arrives as null and must show as "?", never throw.
import assert from "node:assert/strict";
import { test } from "node:test";
import { agoWords, hm, hms, isoTime, whenLabel } from "../src/lib/time.ts";

test("an unknown time shows as a question mark", () => {
  for (const t of [null, undefined, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.equal(hm(t), "?");
    assert.equal(hms(t), "?");
    assert.equal(whenLabel(t), "?");
    assert.equal(isoTime(t), undefined);
  }
});

test("a known time is written as a clock time", () => {
  const now = Date.now() / 1000;
  assert.match(hm(now), /^\d{2}:\d{2}$/);
  assert.match(whenLabel(now), /^\d{2}:\d{2}$/);
  assert.equal(isoTime(0), "1970-01-01T00:00:00.000Z");
});

test("an age is said in minutes, hours, then days", () => {
  const now = 1_800_000_000;
  assert.equal(agoWords(now, now), "1 min ago");
  assert.equal(agoWords(now - 20, now), "1 min ago");
  assert.equal(agoWords(now - 5 * 60, now), "5 min ago");
  assert.equal(agoWords(now - 59 * 60, now), "59 min ago");
  assert.equal(agoWords(now - 60 * 60, now), "1 h ago");
  assert.equal(agoWords(now - 47 * 3600, now), "47 h ago");
  assert.equal(agoWords(now - 3 * 86400, now), "3 days ago");
  assert.equal(agoWords(now + 30, now), "1 min ago", "a clock a little ahead is just now");
});

test("an age that isn't known is left out, never printed as NaN", () => {
  for (const t of [null, undefined, 0, Number.NaN, Number.POSITIVE_INFINITY]) assert.equal(agoWords(t, 1_800_000_000), null);
});
