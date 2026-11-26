// A time the store couldn't give arrives as null and must show as "?", never throw.
import assert from "node:assert/strict";
import { test } from "node:test";
import { hm, hms, isoTime, whenLabel } from "../src/lib/time.ts";

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
