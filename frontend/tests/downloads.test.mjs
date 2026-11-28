// The Downloads table on the models page: a download's state in a word, and the sentence under it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { downloadState } from "../src/lib/models.ts";

test("a stopped download says what the next one does with what was kept", () => {
  const note = "what was downloaded is kept: start the download again and it continues where it stopped";
  assert.deepEqual(downloadState({ model: "m", state: "stopped", note }), [
    "stopped",
    "What was downloaded is kept: start the download again and it continues where it stopped.",
  ]);
  assert.deepEqual(downloadState({ model: "m", state: "stopped", note: null }), ["stopped", null]);
});

test("a failed download keeps its own sentence, with one full stop", () => {
  const error = "couldn't download m: no connection to example.org; start the download again.";
  assert.deepEqual(downloadState({ model: "m", state: "failed", error }), [
    "failed",
    "Couldn't download m: no connection to example.org; start the download again.",
  ]);
});

test("running, stopping and done are one word each", () => {
  assert.deepEqual(downloadState({ model: "m", state: "running" }), ["downloading", null]);
  assert.deepEqual(downloadState({ model: "m", state: "stopping" }), ["stopping…", null]);
  assert.deepEqual(downloadState({ model: "m", state: "done" }), ["done", null]);
});
