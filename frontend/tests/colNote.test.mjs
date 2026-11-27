// What a column says at the foot of its text while no new line is coming: music, no voice, a
// stream that can't be reached. From the station's status as the server sends it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { QUIET_S, UNWRITTEN_S, colNote, steady } from "../src/reading/colNote.ts";

const NOW = 1_795_800_000;
/** A stream station that is written down, watched by this page for an hour. */
const at = (over = {}) => ({ kind: "stream", writes: true, lastText: NOW - 5, watched: NOW - 3600, now: NOW, ...over });
/** A station's status while it is heard and someone talks. */
const live = (over = {}) => ({
  connected: true, reconnects: 0, last_data_age: 0.4, in_speech: true, music: false, quiet_s: 3, level: 0.1, stopping: false,
  behind_s: { text: null, translation: null }, ...over,
});

test("text is coming: nothing to say", () => {
  assert.equal(colNote(live(), at()), null);
  assert.equal(colNote(live({ in_speech: false, quiet_s: 20 }), at()), null); // a pause between sentences
  assert.equal(colNote(undefined, at()), null); // not running
  assert.equal(colNote(live({ stopping: true, music: true }), at()), null);
});

test("music: playing now and skipped, with no speech since the last line that passed the gate", () => {
  // A song has played for 17 minutes; the gate drops each stretch of it.
  const note = colNote(live({ music: true, quiet_s: 17 * 60 }), at({ lastText: NOW - 17 * 60 - 8 }));
  assert.deepEqual(note, { kind: "music", since: NOW - 17 * 60 });
  // Between two sung stretches the song is still "music" for a minute, by the server's own flag.
  assert.equal(colNote(live({ music: true, in_speech: false, quiet_s: 40 }), at()).kind, "music");
});

test("no voice for a minute: quiet, since when", () => {
  assert.equal(colNote(live({ in_speech: false, quiet_s: QUIET_S - 1 }), at()), null);
  assert.deepEqual(colNote(live({ in_speech: false, quiet_s: 300 }), at()), { kind: "quiet", since: NOW - 300 });
  // A walkie-talkie channel with its squelch closed is silent most of the time: its head says so.
  assert.equal(colNote(live({ in_speech: false, quiet_s: 300, squelch_open: false }), at()), null);
});

test("a voice is heard but nothing was written for two minutes: said to be left out", () => {
  // Singing the gate lets through is transcribed and then dropped: speech "passes", no line comes.
  const sung = live({ in_speech: true, quiet_s: 4 });
  assert.equal(colNote(sung, at({ lastText: NOW - UNWRITTEN_S + 1 })), null);
  assert.deepEqual(colNote(sung, at({ lastText: NOW - 17 * 60 })), { kind: "unwritten", since: NOW - 17 * 60 });
  // Text that is late is late, not left out: the column's head says how far behind it is.
  assert.equal(colNote(live({ behind_s: { text: 95 } }), at({ lastText: NOW - 17 * 60 })), null);
  // The newest line is from before this page began to watch (yesterday's, say): only the time
  // watched is spoken for.
  assert.equal(colNote(sung, at({ lastText: NOW - 86400, watched: NOW - 30 })), null);
  assert.deepEqual(colNote(sung, at({ lastText: NOW - 86400, watched: NOW - 600 })), { kind: "unwritten", since: NOW - 600 });
  assert.deepEqual(colNote(sung, at({ lastText: null, watched: NOW - 600 })), { kind: "unwritten", since: NOW - 600 });
  assert.equal(colNote(sung, at({ lastText: NOW - 17 * 60, watched: null })), null);
});

test("a stream that can't be reached: since when, with the source's own words kept for the details", () => {
  const raw = "Failed to resolve hostname radio.example.invalid; check the address, the network, and whether the stream only plays in a web browser";
  // Never reached since it was started five minutes ago.
  const never = colNote(live({ connected: false, reconnects: 7, last_data_age: null, in_speech: false, quiet_s: 300, error: raw, retry_in_s: 31.2 }), at({ lastText: null }));
  assert.deepEqual(never, { kind: "down", what: "stream", since: NOW - 300, error: raw, retryIn: 31.2 });
  // Heard until 90 s ago, then lost.
  const lost = colNote(live({ connected: false, reconnects: 2, last_data_age: 90, in_speech: false, quiet_s: 400, error: raw, retry_in_s: 0 }), at());
  assert.deepEqual([lost.kind, lost.since, lost.retryIn], ["down", NOW - 90, 0]);
  // Another machine isn't told why (the server keeps the error to itself): still down, no details.
  const elsewhere = colNote(live({ connected: false, reconnects: 3, last_data_age: 90 }), at());
  assert.deepEqual([elsewhere.kind, elsewhere.error, elsewhere.retryIn], ["down", null, null]);
  // Down comes first, whatever else the status says, and for a station that isn't written down.
  assert.equal(colNote(live({ connected: false, reconnects: 1, music: true }), at({ writes: false })).kind, "down");
});

test("what can't be reached is named by the kind of station", () => {
  const down = live({ connected: false, reconnects: 4, error: "no answer" });
  assert.equal(colNote(down, at({ kind: "sdr" })).what, "receiver");
  assert.equal(colNote({ ...down, signal_db: -31.5 }, at()).what, "receiver");
  assert.equal(colNote(down, at({ kind: "remote" })).what, "remote");
});

test("still connecting for the first time: that, and not \"waiting for speech\"", () => {
  assert.deepEqual(colNote(live({ connected: false, reconnects: 0, last_data_age: null, in_speech: false }), at({ lastText: null })), { kind: "connecting" });
});

test("a station that isn't written down says nothing about music or silence", () => {
  assert.equal(colNote(live({ music: true, quiet_s: 600 }), at({ writes: false })), null);
  assert.equal(colNote(live({ in_speech: false, quiet_s: 600 }), at({ writes: false })), null);
});

test("a status without the fields (another Wavescribe's station) says nothing", () => {
  assert.equal(colNote({ connected: true }, at()), null);
});

test("the time named doesn't flicker as the status wanders by a second", () => {
  assert.equal(steady(null, 1000), 1000);
  assert.equal(steady(1000, 1001), 1000);
  assert.equal(steady(1000, 996), 1000);
  assert.equal(steady(1000, 1040), 1040); // speech came and went: a new time
  assert.equal(steady(1000, null), null);
});
