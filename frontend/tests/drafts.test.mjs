// What the rule and station editors send back: their own fields, and everything else untouched.
import assert from "node:assert/strict";
import { test } from "node:test";
import { changedFields, lines, ruleBody } from "../src/manage/drafts.ts";

// A rule as GET /api/manage/rules lists it: alerts.toml's fields plus what the page is told.
const fromFile = {
  id: "road-closed",
  terms: { en: ["ring road"] },
  regex: ["\\broad (closed|blocked)\\b"],
  severity: "high",
  note: "a field this page doesn't know",
  later: { kept: [1, 2] },
  kind: "keyword",
  origin: "file",
  enabled: true,
};

test("a rule saved without changes keeps every field it came with", () => {
  const body = ruleBody(fromFile, { id: "road-closed", severity: "high", terms: { en: ["ring road"] }, min_mentions: 1 });
  assert.deepEqual(body, {
    id: "road-closed",
    terms: { en: ["ring road"] },
    regex: ["\\broad (closed|blocked)\\b"],
    severity: "high",
    note: "a field this page doesn't know",
    later: { kept: [1, 2] },
  });
});

test("what the page is told about a rule isn't saved back, nor the defaults", () => {
  const body = ruleBody(fromFile, { verify: false, min_mentions: 1, stations: [], near: [] });
  for (const k of ["origin", "enabled", "kind", "verify", "min_mentions", "stations", "near"]) assert.equal(k in body, false, k);
});

test("an edited field replaces the old one, and an emptied one is left out", () => {
  const body = ruleBody(fromFile, { severity: "critical", regex: lines("  \\bbridge closed\\b \n\n") });
  assert.equal(body.severity, "critical");
  assert.deepEqual(body.regex, ["\\bbridge closed\\b"]);
  assert.equal("regex" in ruleBody(fromFile, { regex: lines("") }), false);
});

test("a station rule keeps its kind and its own fields", () => {
  const silent = { id: "quiet", kind: "silence", after_minutes: 5, level_db: -60, severity: "medium", origin: "page", enabled: false };
  assert.deepEqual(ruleBody(silent, { severity: "high", after_minutes: 2.5 }),
    { id: "quiet", kind: "silence", after_minutes: 2.5, level_db: -60, severity: "high" });
  assert.deepEqual(ruleBody(null, { id: "ta", kind: "traffic", severity: "low", stations: [] }), { id: "ta", kind: "traffic", severity: "low" });
});

const station = { name: "Coast FM", language: "es-ES", translate_to: ["en"], priority: 3, disabled: null };

test("a station saved unchanged sends nothing", () => {
  assert.deepEqual(changedFields(station, { ...station, translate_to: ["en"] }), {});
});

test("only the fields that changed are sent", () => {
  assert.deepEqual(changedFields(station, { ...station, name: "Coast FM 2", translate_to: ["en", "fr"] }),
    { name: "Coast FM 2", translate_to: ["en", "fr"] });
});

test("a confirmation to receive a kind of radio goes back as it came, in each of its forms", () => {
  for (const ack of [true, "utility", ["utility", "private"]]) {
    const saved = { ...station, acknowledge: ack };
    assert.deepEqual(changedFields(saved, { ...saved, acknowledge: structuredClone(ack) }), {}, JSON.stringify(ack));
  }
  const saved = { ...station, acknowledge: "utility" };
  assert.deepEqual(changedFields(saved, { ...saved, acknowledge: ["utility", "private"] }), { acknowledge: ["utility", "private"] });
  assert.deepEqual(changedFields({ ...station, acknowledge: ["utility"] }, { ...station, acknowledge: "private" }), { acknowledge: "private" });
});
