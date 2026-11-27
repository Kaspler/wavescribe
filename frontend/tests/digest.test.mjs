// What the page asks for a summary and how it reads the answer (POST /api/digest).
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ALERT_SPEECH_S, answerOf, asSentence, cutAt, digestBody, failureOf, linksOf, nowBody, saidBy, shown, sinceClock, spokenSeconds,
  windowProblem,
} from "../src/reading/digest.ts";

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

test("the request names the stations, the window and the language, and only those", () => {
  assert.deepEqual(digestBody({ kind: "minutes", minutes: 15 }, { stations: ["tsf"] }), { kind: "digest", stations: ["tsf"], minutes: 15 });
  assert.deepEqual(digestBody({ kind: "since", at: 1234.9 }, { stations: ["tsf", "rr"], language: "es" }),
    { kind: "digest", stations: ["tsf", "rr"], since: 1234, language: "es" });
  assert.deepEqual(digestBody({ kind: "away", at: 99 }, { stations: ["tsf"], speak: true }), { kind: "digest", stations: ["tsf"], since: 99, speak: true });
});

test("what a station is talking about now names the one station, and the language when given", () => {
  assert.deepEqual(nowBody("tsf"), { kind: "now", stations: ["tsf"] });
  assert.deepEqual(nowBody("tsf", "es"), { kind: "now", stations: ["tsf"], language: "es" });
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
