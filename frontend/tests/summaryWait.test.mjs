// While a summary is being made, its panel says where it stands, from the status's digests; a
// summary refused as not made in time can be asked again from the second the answer names.
import assert from "node:assert/strict";
import { test } from "node:test";
import { failureOf, waitOf } from "../src/reading/digest.ts";
import { T } from "../src/reading/strings.ts";

const row = (o) => ({ id: "abc", kind: "summary", here: true, state: "waiting", waited_s: 12, behind: 0, behind_what: [], turn_in_s: null,
  call: null, calls: null, gives_up_in_s: 588, ...o });

test("the row with this request's id, in the page's terms", () => {
  const status = { digests: [row({ id: "other", state: "writing" }), row({ behind: 7, behind_what: ["translation", "alert"], turn_in_s: 41.5, call: 1, calls: 3 })] };
  assert.deepEqual(waitOf(status, "abc"), { state: "waiting", behind: 7, what: ["translation", "alert"], turnIn: 41.5, call: 1, calls: 3 });
  assert.equal(waitOf(status, "zzz"), null, "not this request's");
});

test("no row: a server from before, a status not yet with it, or an unknown state", () => {
  assert.equal(waitOf({ stations: {} }, "abc"), null);
  assert.equal(waitOf(null, "abc"), null);
  assert.equal(waitOf({ digests: [row({ id: undefined })] }, "abc"), null, "another machine's rows come without ids");
  assert.equal(waitOf({ digests: [row({ state: "pondering" })] }, "abc"), null);
});

const pickCalls = (w) => [w.call, w.calls];

test("call and calls only together, the call never past the calls", () => {
  assert.deepEqual(pickCalls(waitOf({ digests: [row({ state: "writing", call: 2, calls: 2 })] }, "abc")), [2, 2]);
  assert.deepEqual(pickCalls(waitOf({ digests: [row({ state: "writing", call: 4, calls: 3 })] }, "abc")), [3, 3]);
  assert.deepEqual(pickCalls(waitOf({ digests: [row({ state: "writing", call: 1, calls: null })] }, "abc")), [null, null]);
  assert.deepEqual(pickCalls(waitOf({ digests: [row({ state: "writing", call: "1", calls: 2 })] }, "abc")), [null, null]);
});

test("each state in words", () => {
  assert.equal(T.summaryInLine(1), "Waiting for its turn, behind 1 other summary");
  assert.equal(T.summaryInLine(3), "Waiting for its turn, behind 3 other summaries");
  assert.equal(T.summaryInLine(0), "Waiting for its turn");
  assert.equal(T.summaryBehind(["translation", "alert"], 41.5), "Waiting behind live translation and alerts; its turn within about 42 s");
  assert.equal(T.summaryBehind(["translation"], 150), "Waiting behind live translation; its turn within about 3 min");
  assert.equal(T.summaryBehind(["question"], 0), "Waiting behind questions", "0: ahead of the station lines already");
  assert.equal(T.summaryBehind([], null), "Waiting for the model");
  assert.equal(T.summaryBehind(["something_new"], null), "Waiting behind something new");
  assert.equal(T.summaryPart(2, 3), "Writing: part 2 of 3");
});

test("refused as not made in time: the button goes by the answer's retry_after_s", () => {
  const refused = 1764280000;
  const data = { error: "live translation of Test: French talk kept the model busy for 10 minutes, so no digest was made in time; try again in a minute",
    code: "busy", retry_after_s: 75, waited_s: 600, busy_with: [{ kind: "translation", names: ["Test: French talk"], seconds: 540 }] };
  const f = failureOf(503, data, { retryAfter: "60", now: refused });
  assert.equal(f.kind, "busy");
  assert.equal(f.retryAt, refused + 75, "the answer's own seconds over the header's");
  assert.match(f.text, /^Live translation of Test: French talk kept the model busy/, "its sentence, as it is");
  assert.equal(failureOf(503, { ...data, retry_after_s: 12.2 }, { now: refused }).retryAt, refused + 13, "to the next whole second");
  // Without it, the header, as before.
  assert.equal(failureOf(503, { error: "busy", code: "busy" }, { retryAfter: "60", now: refused }).retryAt, refused + 60);
  assert.equal(failureOf(503, { error: "busy", code: "busy", retry_after_s: "soon" }, { retryAfter: "30", now: refused }).retryAt, refused + 30);
});
