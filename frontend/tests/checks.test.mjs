// Health counts the checks of word matches since the start, in plain words.
import assert from "node:assert/strict";
import { test } from "node:test";
import { checksLine } from "../src/manage/checks.ts";

const zero = { confirmed: 0, rejected: 0, held: 0, not_run: { timeout: 0, failed: 0, queue_full: 0, no_model: 0 } };
const HEAD = "Word matches the model was asked to confirm, since start:";

test("each count, and why the checks that didn't happen didn't, leaving out the reasons at 0", () => {
  const v = { confirmed: 4, rejected: 1, held: 1, not_run: { timeout: 2, failed: 0, queue_full: 0, no_model: 1 } };
  assert.equal(checksLine(v, true), `${HEAD} 4 confirmed, 1 rejected, 3 not run (2 timed out, 1 no model loaded), 1 waiting for a retry.`);
  assert.equal(checksLine({ ...zero, not_run: { queue_full: 1, failed: 2 } }, false), `${HEAD} 0 confirmed, 0 rejected, 3 not run (1 model busy, 2 failed), 0 waiting for a retry.`);
});

test("nothing counted and no rule asks for the check: nothing to say", () => {
  assert.equal(checksLine(zero, false), null);
  assert.equal(checksLine(undefined, false), null, "a server from before the counts");
});

test("a rule asks for the check and none was made yet: the counts, at 0", () => {
  assert.equal(checksLine(zero, true), `${HEAD} 0 confirmed, 0 rejected, 0 not run, 0 waiting for a retry.`);
});

test("counts left over from rules since switched off are still said", () => {
  assert.match(checksLine({ ...zero, confirmed: 2 }, false), /2 confirmed/);
});

test("a reason this page doesn't know is said in its own words; what isn't a count is 0", () => {
  assert.match(checksLine({ ...zero, not_run: { model_unloaded: 1 } }, false), /1 not run \(1 model unloaded\)/);
  assert.equal(checksLine({ confirmed: "3", rejected: -1, held: null, not_run: "x" }, false), null);
});
