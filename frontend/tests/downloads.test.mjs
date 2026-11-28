// The Downloads table on the models page: a download's state in a word, and the sentence under it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { afterRemoval, downloadState, progressText, roomForAll, roomText, sizeText, speedText, timeLeft } from "../src/lib/models.ts";

test("a running download says nothing of its speed until it is measured", () => {
  const j = { model: "m", state: "running", bytes: 1_200_000_000, total_bytes: 5_500_000_000 };
  assert.deepEqual(progressText(j), ["1.2 GB of 5.5 GB", "measuring the speed…"]);
  assert.deepEqual(progressText({ ...j, speed: 0 }), ["1.2 GB of 5.5 GB", "nothing arriving"]);
  assert.deepEqual(progressText({ ...j, speed: 12_400_000, eta_s: 347 }), ["1.2 GB of 5.5 GB", "12 MB/s · about 6 min left"]);
  assert.deepEqual(progressText({ model: "m", state: "running", bytes: 100, speed: 800_000 }), ["0 MB so far", "800 kB/s"]);
});

test("speeds and times left as a person says them", () => {
  assert.equal(speedText(2_440_000), "2.4 MB/s");
  assert.equal(speedText(300), "1 kB/s");
  assert.equal(timeLeft(3), "about 5 s left");
  assert.equal(timeLeft(42), "about 40 s left");
  assert.equal(timeLeft(3 * 3600 + 5 * 60), "about 3 h 5 min left");
  assert.equal(timeLeft(2 * 3600), "about 2 h left");
});

test("the room on the disk before a download: the free space, or why it won't fit", () => {
  assert.deepEqual(roomText(5_500_000_000, 80_000_000_000), [true, "80.0 GB free on this disk"]);
  assert.deepEqual(roomText(5_500_000_000, 2_100_000_000), [false, "Not enough room: 5.5 GB to download and 2.1 GB free on this disk. Make room first."]);
  assert.deepEqual(roomText(null, 2_100_000_000), [true, "2.1 GB free on this disk"]);
  assert.deepEqual(roomText(5_500_000_000, null), [true, null]);
});

test("several downloads are added up on the disk they share", () => {
  const a = { left_bytes: 3e9, free_bytes: 5e9, disk: "1" };
  assert.deepEqual(roomForAll([a, { left_bytes: 1e9, free_bytes: 5e9, disk: "1" }]), [true, "5.0 GB free on this disk"]);
  assert.equal(roomForAll([a, { left_bytes: 3e9, free_bytes: 5e9, disk: "1" }])[0], false);
  assert.deepEqual(roomForAll([a, { left_bytes: 3e9, free_bytes: 5e9, disk: "2" }]), [true, null]);
  assert.deepEqual(roomForAll([{ left_bytes: 1e9, free_bytes: null, disk: null }]), [true, null]);
});

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
