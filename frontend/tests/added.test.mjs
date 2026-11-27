// A new station's toast says, in the server's words, what didn't happen as asked.
import assert from "node:assert/strict";
import { test } from "node:test";
import { addedToast } from "../src/manage/added.ts";

test("added and running as asked", () => {
  assert.deepEqual(addedToast({ name: "Rádio", listen_only: false, reason: null }, "Transcribing Rádio"), { text: "Transcribing Rádio", bad: false });
});

test("added, but it couldn't start", () => {
  assert.deepEqual(addedToast({ name: "Rádio", start_error: "the receiver is unplugged" }, "Transcribing Rádio"),
    { text: "Rádio was added and didn't start: the receiver is unplugged", bad: true });
});

test("added, but played without text for now", () => {
  const reason = "Plays without text until its speech model is downloaded: …";
  assert.deepEqual(addedToast({ name: "Rádio", listen_only: true, reason }, "Transcribing Rádio"), { text: `Rádio was added. ${reason}`, bad: true });
  assert.equal(addedToast({ name: "Rádio", listen_only: true, reason: null }, "ok").bad, false, "no sentence, nothing to say");
});
