// Summaries on a schedule: how often in words, the table sent, and which field a refusal is about.
import assert from "node:assert/strict";
import { test } from "node:test";
import { EVERY_DEFAULT, bodyOf, everyWords, formOf, hasVoice, whereOf } from "../src/manage/schedule.ts";

test("how often, in words", () => {
  assert.equal(everyWords(60), "every hour");
  assert.equal(everyWords(15), "every 15 minutes");
  assert.equal(everyWords(90), "every 90 minutes");
  assert.equal(everyWords(120), "every 2 hours");
  assert.equal(everyWords(1440), "once a day");
});

test("with no schedule yet the form holds the server's defaults", () => {
  assert.deepEqual(formOf({ schedule: null, language: "pt" }),
    { every: EVERY_DEFAULT, stations: [], language: "", speak: false, quiet: null });
});

test("a schedule's language is kept only where it differs from the default", () => {
  const schedule = { every_minutes: 30, stations: ["tsf"], language: "pt", speak: true, quiet_hours: ["22:00", "07:00"] };
  assert.deepEqual(formOf({ schedule, language: "pt" }),
    { every: 30, stations: ["tsf"], language: "", speak: true, quiet: ["22:00", "07:00"] });
  assert.equal(formOf({ schedule: { ...schedule, language: "es" }, language: "pt" }).language, "es");
});

test("the whole table is sent, defaults as null for the server to fill in", () => {
  assert.deepEqual(bodyOf({ every: 60, stations: [], language: "", speak: false, quiet: null }, true),
    { enabled: true, every_minutes: 60, stations: null, language: null, speak: false, quiet_hours: null });
  assert.deepEqual(bodyOf({ every: 15, stations: ["tsf", "rr"], language: "es", speak: true, quiet: ["23:00", "06:30"] }, false),
    { enabled: false, every_minutes: 15, stations: ["tsf", "rr"], language: "es", speak: true, quiet_hours: ["23:00", "06:30"] });
});

test("a refusal is placed by its field, else by its code, else on the whole form", () => {
  assert.equal(whereOf({ error: "x", code: "bad_every", field: "every_minutes", choices: [5, 60] }), "every");
  assert.equal(whereOf({ error: "x", code: "unknown_station", field: "stations" }), "stations");
  assert.equal(whereOf({ error: "x", code: "no_voice", field: "speak", language: "fr" }), "speak");
  assert.equal(whereOf({ error: "x", code: "bad_quiet_hours" }), "quiet");
  assert.equal(whereOf({ error: "x", code: "unknown_keys", keys: ["often"] }), "form");
  assert.equal(whereOf({ error: "alerts are off in this run", code: "alerts_off" }), "form");
  assert.equal(whereOf(null), "form");
});

test("a voice reads a language when their primary languages agree", () => {
  assert.equal(hasVoice(["en"], "en-GB"), true);
  assert.equal(hasVoice(["pt-PT"], "pt"), true);
  assert.equal(hasVoice(["en"], "fr"), false);
  assert.equal(hasVoice([], "en"), false);
});
