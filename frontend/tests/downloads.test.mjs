// The Downloads table on the models page: a download's state in a word, and the sentence under it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { afterRemoval, downloadState, sizeText } from "../src/lib/models.ts";

test("sizes as the command line says them, in decimal units", () => {
  assert.equal(sizeText(1_600_000_000), "1.6 GB");
  assert.equal(sizeText(960_000_000), "1.0 GB");
  assert.equal(sizeText(340_400_000), "340 MB");
  assert.equal(sizeText(2_000), "under 1 MB");
});

test("what becomes of what used a removed model, by its job", () => {
  assert.match(afterRemoval("asr"), /^Stations that use it switch to the next installed model/);
  assert.match(afterRemoval("vad"), /play without text until one is downloaded\.$/);
  assert.match(afterRemoval("translate"), /nothing is translated/);
  assert.match(afterRemoval("gate"), /songs are written down too/);
  assert.equal(afterRemoval("langid"), null);
});

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
