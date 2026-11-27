// What the page asks for a summary and how it reads the answer (POST /api/digest).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ALERT_SPEECH_S, MAX_MINUTES, REMOTE_MINUTES, answerOf, asSentence, coveredStations, cutAt, digestBody, failureOf, lastNight, linksOf, newId,
  nowBody, readingAt, retryWait, saidBy, shown, sinceClock, speakResult, speechOf, spokenSeconds, untilClock, windowProblem,
} from "../src/reading/digest.ts";

test("a summary covers every running station in the list, not the columns on screen", () => {
  // Three run; the page has room for two columns. What is on screen isn't asked about at all.
  const running = [{ id: "test-fr", watch: false }, { id: "test-de", watch: false }, { id: "test-it", watch: false }];
  assert.deepEqual(coveredStations(running), ["test-fr", "test-de", "test-it"]);
  // Fewer is the reader's own choice, made in the summary's menu.
  assert.deepEqual(coveredStations(running, { "test-de": false }), ["test-fr", "test-it"]);
  assert.deepEqual(coveredStations(running, { "test-fr": false, "test-de": false, "test-it": false }), []);
  // A choice about a station that no longer runs changes nothing.
  assert.deepEqual(coveredStations(running, { "test-gone": true, constructor: false }), ["test-fr", "test-de", "test-it"]);
});

test("a watch is in a summary only when chosen", () => {
  const running = [{ id: "test-fr", watch: false }, { id: "test-band", watch: true }];
  assert.deepEqual(coveredStations(running), ["test-fr"]);
  assert.deepEqual(coveredStations(running, { "test-band": true }), ["test-fr", "test-band"]);
});

test("a refusal the page doesn't know, with a time to try again, waits until then", () => {
  const f = failureOf(503, { error: "a summary is ahead of this one", code: "queue_full", position: 2, extra: { x: 1 } },
    { retryAfter: "30", now: 1000.5 });
  assert.deepEqual([f.code, f.text, f.retry, f.retryAt], ["queue_full", "A summary is ahead of this one.", true, 1031]);
  // Without a time it is the server's sentence, as before.
  assert.deepEqual([failureOf(409, { error: "no", code: "something_new" }).retry, failureOf(409, { error: "no", code: "something_new" }).retryAt], [false, null]);
  // An answer with fields this page doesn't know is still an answer, those fields kept.
  const a = answerOf({ ...answer(), queue: { position: 1 } });
  assert.deepEqual(a.queue, { position: 1 });
});

test("trying again becomes possible at the second the button names", () => {
  // Refused at 21:03:50.4 with Retry-After: 60. The time named is a whole second, and the wait
  // ends exactly there: not at the start of its minute, not most of a minute after it.
  const refused = Date.UTC(2026, 10, 27, 21, 3, 50, 400) / 1000;
  const f = failureOf(503, { error: "try again in a minute", code: "busy" }, { retryAfter: "60", now: refused });
  assert.equal(f.retryAt, Date.UTC(2026, 10, 27, 21, 4, 51) / 1000);
  assert.equal(Number.isInteger(f.retryAt), true);
  assert.equal(retryWait(f.retryAt, Date.UTC(2026, 10, 27, 21, 4, 0)), 51000); // the minute it falls in has begun: not yet
  assert.equal(retryWait(f.retryAt, f.retryAt * 1000 - 1), 1);
  assert.equal(retryWait(f.retryAt, f.retryAt * 1000), 0);
  assert.equal(retryWait(f.retryAt, f.retryAt * 1000 + 5000), 0);
  assert.equal(retryWait(null, 0), 0); // a failure that names no time can be tried again at once
});

const at = (h, m) => new Date(2026, 10, 27, h, m, 0, 0);

const line = (n, station, t) => ({
  n, id: 100 + n, station, name: station.toUpperCase(), t_start: t, language: "pt", text: `linha ${n}`,
  translations: { en: `line ${n}` }, audio: true,
});

const answer = (over = {}) => ({
  kind: "digest",
  language: "en",
  stations: ["tsf", "rr"],
  names: { tsf: "TSF", rr: "Renascença" },
  window: { since: 1000, until: 4600, minutes: 60 },
  counts: { said: 40, read: 40 },
  alerts: [],
  points: [
    { text: "Water is cut tomorrow.", stations: ["tsf"], sources: [1, 2] },
    { text: "Both talk of the strike.", stations: ["tsf", "rr"], sources: [2, 3] },
  ],
  sources: [line(1, "tsf", 1200), line(2, "tsf", 1300), line(3, "rr", 1400)],
  outcome: "points",
  unread: {},
  notice: "Written by the local model from all 40 lines heard between 14:00 and 15:00; it can miss things or get them wrong.",
  text: "- TSF: Water is cut tomorrow. [1, 2]",
  model: "qwen3.5-9b",
  timings: { model_ms: 9000, total_ms: 9400 },
  ...over,
});

test("a time of day is today's, or yesterday's while it is still to come", () => {
  assert.equal(sinceClock("14:30", at(15, 0)), at(14, 30).getTime() / 1000);
  assert.equal(sinceClock(" 9:05 ", at(15, 0)), at(9, 5).getTime() / 1000);
  const late = new Date(at(23, 30));
  late.setDate(late.getDate() - 1);
  assert.equal(sinceClock("23:30", at(0, 15)), late.getTime() / 1000);
  for (const bad of ["", "24:00", "12:60", "noon", "12", "12:5"]) assert.equal(sinceClock(bad, at(15, 0)), null, bad);
});

test("a window must reach back at least a minute and no further than the server goes", () => {
  const now = 100_000;
  assert.equal(windowProblem(now - 30, now, 1440), "soon");
  assert.equal(windowProblem(now - 600, now, 1440), null);
  assert.equal(windowProblem(now - 7 * 3600, now, 360), "far");
  assert.equal(windowProblem(now - 7 * 3600, now, 1440), null);
});

test("a window that ends earlier must have ended, last a minute, and be no longer than the server reads", () => {
  const now = 100_000;
  assert.equal(windowProblem(now - 3600, now, 1440, now - 1800), null);
  assert.equal(windowProblem(now - 3600, now, 1440, now + 60), "later");
  assert.equal(windowProblem(now - 3600, now, 1440, now - 3570), "soon");
  // The length counts, not how far back it starts: a day ago for an hour is fine.
  assert.equal(windowProblem(now - 25 * 3600, now, 1440, now - 24 * 3600), null);
  assert.equal(windowProblem(now - 8 * 3600, now, 360, now - 1000), "far");
});

test("an end time is the first such time after the start", () => {
  const since = at(14, 0).getTime() / 1000;
  assert.equal(untilClock("15:30", since), at(15, 30).getTime() / 1000);
  // Before the start's time of day: the next day's, past midnight.
  const late = at(23, 0).getTime() / 1000;
  const next = at(1, 0);
  next.setDate(next.getDate() + 1);
  assert.equal(untilClock("01:00", late), next.getTime() / 1000);
  assert.equal(untilClock("14:00", since), since + 24 * 3600);
  for (const bad of ["", "25:00", "later"]) assert.equal(untilClock(bad, since), null, bad);
});

test("last night is offered by day, and only where the server reads a whole night", () => {
  const w = lastNight(at(9, 30), MAX_MINUTES);
  const start = at(22, 0);
  start.setDate(start.getDate() - 1);
  assert.deepEqual(w, { at: start.getTime() / 1000, until: at(7, 0).getTime() / 1000 });
  assert.notEqual(lastNight(at(21, 59), MAX_MINUTES), null);
  for (const night of [at(6, 59), at(22, 0), at(23, 30), at(0, 10)]) assert.equal(lastNight(night, MAX_MINUTES), null);
  assert.equal(lastNight(at(9, 30), REMOTE_MINUTES), null);
});

test("the request names its id, the stations, the window and the language, and only those", () => {
  const id = "a1b2c3d4";
  assert.deepEqual(digestBody({ kind: "minutes", minutes: 15 }, { id, stations: ["tsf"] }), { kind: "digest", id, stations: ["tsf"], minutes: 15 });
  assert.deepEqual(digestBody({ kind: "since", at: 1234.9 }, { id, stations: ["tsf", "rr"], language: "es" }),
    { kind: "digest", id, stations: ["tsf", "rr"], since: 1234, language: "es" });
  assert.deepEqual(digestBody({ kind: "away", at: 99 }, { id, stations: ["tsf"], speak: true }),
    { kind: "digest", id, stations: ["tsf"], since: 99, speak: true });
  assert.deepEqual(digestBody({ kind: "since", at: 1000.5, until: 4600.9 }, { id, stations: ["tsf"] }),
    { kind: "digest", id, stations: ["tsf"], since: 1000, until: 4600 });
  assert.deepEqual(digestBody({ kind: "night", at: 1000, until: 33400 }, { id, stations: ["tsf"] }),
    { kind: "digest", id, stations: ["tsf"], since: 1000, until: 33400 });
});

test("what a station is talking about now names its id, the one station, and the language when given", () => {
  assert.deepEqual(nowBody("a1b2c3d4", "tsf"), { kind: "now", id: "a1b2c3d4", stations: ["tsf"] });
  assert.deepEqual(nowBody("a1b2c3d4", "tsf", "es"), { kind: "now", id: "a1b2c3d4", stations: ["tsf"], language: "es" });
});

test("a request's id is one the server takes, also where the browser makes no UUID", () => {
  const takes = /^[A-Za-z0-9_-]{8,64}$/;
  const ids = new Set(Array.from({ length: 50 }, newId));
  assert.equal(ids.size, 50);
  for (const id of ids) assert.match(id, takes);
  // A page read over plain http from another device: no randomUUID there.
  const real = crypto.randomUUID;
  Object.defineProperty(crypto, "randomUUID", { value: undefined, configurable: true });
  try {
    const id = newId();
    assert.match(id, /^[0-9a-f]{32}$/);
    assert.notEqual(newId(), id);
  } finally {
    Object.defineProperty(crypto, "randomUUID", { value: real, configurable: true });
  }
});

test("points are shown with the stations not read; without points, the server's own sentences", () => {
  const v = shown(answer({ unread: { rr: "nothing it said was translated in time" } }));
  assert.equal(v.points.length, 2);
  assert.equal(v.sentences, null);
  assert.deepEqual(v.unread, [["Renascença", "nothing it said was translated in time"]]);
  assert.match(v.notice, /^Written by the local model/);
  for (const outcome of ["nothing_said", "not_read", "nothing_stood_out", "unsourced"]) {
    const text = `the server's words for ${outcome}`;
    const e = shown(answer({ outcome, points: [], text, unread: { rr: "why" }, notice: null }));
    assert.deepEqual(e, { points: [], sentences: text, unread: [], notice: null }, outcome);
  }
  // Points the server didn't stand behind are never drawn as if it had.
  assert.equal(shown(answer({ outcome: "unsourced", text: "Not sure." })).points.length, 0);
});

test("each point opens its own lines, naming the station only when they come from several", () => {
  const a = answer();
  assert.deepEqual(linksOf(a.points[0], a), [
    { n: 1, id: 101, station: "tsf", t: 1200, who: null },
    { n: 2, id: 102, station: "tsf", t: 1300, who: null },
  ]);
  assert.deepEqual(linksOf(a.points[1], a).map((l) => [l.id, l.station, l.who]), [[102, "tsf", "TSF"], [103, "rr", "Renascença"]]);
  // A number the answer has no line for is left out rather than drawn as a broken link.
  assert.deepEqual(linksOf({ text: "x", stations: ["tsf"], sources: [9, 1] }, a).map((l) => l.n), [1]);
});

test("who said a point is named in a summary of several stations, not of one", () => {
  const a = answer();
  assert.equal(saidBy(a.points[1], a), "TSF, Renascença");
  assert.equal(saidBy(a.points[0], answer({ stations: ["tsf"] })), null);
  assert.equal(saidBy(a.points[0], answer({ kind: "now" })), null);
});

test("a refusal is read from its code, and keeps the server's sentence", () => {
  const busy = failureOf(503, { error: "2 digests are already being made; try again in a minute", code: "busy", id: "x" },
    { retryAfter: "60", now: 1000 });
  assert.deepEqual(busy, { kind: "busy", code: "busy", text: "2 digests are already being made; try again in a minute.",
    retry: true, retryAt: 1060, maxMinutes: null });
  // Busy says when to try again even without Retry-After: the server's minute.
  assert.equal(failureOf(503, { error: "the model's queue is full", code: "busy" }, { now: 1000 }).retryAt, 1060);
  for (const code of ["no_store", "no_llm", "not_set_up"]) {
    const f = failureOf(409, { error: "why", code });
    assert.deepEqual([f.kind, f.code, f.retry], ["unavailable", code, false], code);
  }
  const far = failureOf(400, { error: "from another machine, minutes must be a number from 1 to 360", code: "bad_window", max_minutes: 360 });
  assert.deepEqual([far.kind, far.maxMinutes, far.retry], ["refused", 360, false]);
  assert.equal(failureOf(400, { error: "until must be a time that has passed", code: "bad_until" }).maxMinutes, null);
  // An id another request took: asked again, the summary has a new one.
  assert.equal(failureOf(400, { error: "a digest with this id is already being made", code: "id_in_use" }).retry, true);
  const failed = failureOf(500, { error: "the digest couldn't be made: boom", code: "failed", id: "x" });
  assert.deepEqual([failed.kind, failed.retry, failed.retryAt], ["failed", true, null]);
  // A code the page doesn't know: the server's words, by its status.
  const odd = failureOf(409, { error: "something new", code: "new_reason" });
  assert.deepEqual([odd.kind, odd.code, odd.text, odd.retry], ["refused", "new_reason", "Something new.", false]);
  assert.equal(failureOf(500, { error: "x", code: "new_reason" }).kind, "failed");
});

test("a refusal without a code is the gate's: a token, or this device not allowed", () => {
  assert.equal(failureOf(401, { error: "a feed token is needed", token_required: true }).kind, "token");
  assert.equal(failureOf(403, { error: "asking from another machine is off" }).kind, "off_here");
  assert.equal(failureOf(502, null).text, "The server answered 502.");
  assert.equal(failureOf(502, null).kind, "failed");
  assert.equal(failureOf(400, "not an object").kind, "refused");
});

test("the server's words stand as a sentence, otherwise unchanged", () => {
  assert.equal(asSentence("a digest needs an LLM translator such as qwen3.5-9b (none is loaded)"),
    "A digest needs an LLM translator such as qwen3.5-9b (none is loaded).");
  assert.equal(asSentence(" Already one. "), "Already one.");
  assert.equal(asSentence("why?"), "Why?");
  assert.equal(asSentence("été coupé"), "Été coupé.");
  assert.equal(asSentence(""), "");
});

test("only an answer with a known outcome, points, lines and a window is drawn", () => {
  assert.equal(answerOf(answer()).outcome, "points");
  assert.deepEqual(answerOf(answer({ alerts: undefined, unread: undefined })).alerts, []);
  for (const bad of [null, "text", {}, answer({ outcome: "maybe" }), answer({ points: null }), answer({ window: {} })]) {
    assert.equal(answerOf(bad), null);
  }
});

test("reading aloud takes the opening and the points that fit in 45 seconds", () => {
  const short = spokenSeconds(answer({ points: [answer().points[0]] }));
  assert.equal(short % 5, 0);
  assert.ok(short >= 10 && short <= 20, `${short}`);
  const long = { text: "x".repeat(280), stations: ["tsf"], sources: [1] };
  const many = spokenSeconds(answer({ points: Array.from({ length: 8 }, () => long) }));
  // Two such points fill the 45 seconds; the rest aren't read.
  assert.ok(many >= 45 && many <= 65, `${many}`);
  assert.ok(spokenSeconds(answer({ points: [{ text: "東京で地震の情報が伝えられた".repeat(4), stations: ["tsf"], sources: [1] }] })) > short);
});

test("an alert read aloud during the reading, or just after it, cut it short", () => {
  const speech = { at: 1000, seconds: 40 };
  const spoke = (at) => ({ delivered_at: at, actions: { notify: "ok", "speak:en": "ok" } });
  assert.equal(cutAt(speech, [spoke(1012)]), 1012);
  assert.equal(cutAt(speech, [spoke(1040 + ALERT_SPEECH_S)]), 1040 + ALERT_SPEECH_S);
  assert.equal(cutAt(speech, [spoke(990), spoke(1100)]), null); // before it, long after it
  assert.equal(cutAt(speech, [{ delivered_at: 1010, actions: { notify: "ok" } }]), null); // not spoken
  assert.equal(cutAt(speech, [{ delivered_at: 1010, actions: { "speak:en": "failed" } }]), null);
  assert.equal(cutAt(speech, [spoke(1030), spoke(1015)]), 1015);
});

test("asking to read a summary aloud: begun, refused by its code, no longer kept, or failed", () => {
  assert.deepEqual(speakResult(200, { id: "x", speech: { started: true } }), { kind: "started", code: null, text: "" });
  for (const code of ["no_voice", "no_speech", "voice_missing", "nothing_to_read", "no_phrases", "speaking"]) {
    const r = speakResult(409, { error: "why not", code, id: "x" });
    assert.deepEqual(r, { kind: "refused", code, text: "Why not." }, code);
  }
  // A code the page has no words for keeps the server's sentence.
  assert.deepEqual(speakResult(409, { error: "a new reason", code: "brand_new" }), { kind: "refused", code: "brand_new", text: "A new reason." });
  assert.equal(speakResult(404, { error: "no digest with this id is kept here: make it again", code: "not_found" }).kind, "gone");
  assert.equal(speakResult(403, { error: "this machine only" }).kind, "failed");
  assert.equal(speakResult(400, { error: "send an id", code: "bad_id" }).kind, "failed");
  assert.equal(speakResult(200, { stopped: true }).kind, "failed");
});

test("a summary asked with speak says whether its reading began, and why not", () => {
  assert.equal(speechOf(answer()), null);
  assert.deepEqual(speechOf(answer({ speech: { started: true } })), { kind: "started", code: null, text: "" });
  assert.deepEqual(speechOf(answer({ speech: { started: false, error: "another digest is being read aloud; try again when it ends", code: "speaking" } })),
    { kind: "refused", code: "speaking", text: "Another digest is being read aloud; try again when it ends." });
});

test("a reading is being read until its estimate runs out, unless an alert's voice cut it", () => {
  const r = { at: 1000, seconds: 30 };
  assert.deepEqual(readingAt(r, 1010, []), { state: "reading", cut: null });
  assert.deepEqual(readingAt(r, 1031, []), { state: "read", cut: null });
  const spoke = { delivered_at: 1012, actions: { "speak:en": "ok" } };
  assert.deepEqual(readingAt(r, 1015, [spoke]), { state: "cut", cut: 1012 });
});
