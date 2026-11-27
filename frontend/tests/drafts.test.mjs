// What the rule and station editors send back: their own fields, and of the rest what the server
// takes.
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
// The keys each kind takes, as the rules view sends them (rules.RULE_KEYS).
const TAKES = {
  keyword: ["id", "kind", "min_mentions", "near", "regex", "severity", "stations", "terms", "topics", "unverified", "verify"],
  silence: ["after_minutes", "id", "kind", "level_db", "severity", "stations"],
  traffic: ["id", "kind", "severity", "stations"],
};

test("a rule saved without changes keeps every field its kind takes", () => {
  const { body } = ruleBody(fromFile, { id: "road-closed", severity: "high", terms: { en: ["ring road"] }, min_mentions: 1 }, TAKES.keyword);
  assert.deepEqual(body, {
    id: "road-closed",
    terms: { en: ["ring road"] },
    regex: ["\\broad (closed|blocked)\\b"],
    severity: "high",
  });
});

test("a key the rule's kind doesn't take is left out and named, else the rule would be refused", () => {
  const { body, dropped } = ruleBody(fromFile, { severity: "critical" }, TAKES.keyword);
  assert.deepEqual(dropped, ["later", "note"]);
  assert.equal("note" in body || "later" in body, false);
  assert.deepEqual(ruleBody({ ...fromFile, note: undefined, later: undefined }, {}, TAKES.keyword).dropped, []);
  // Told nothing about the keys (a server that doesn't say), everything passes.
  assert.deepEqual(ruleBody(fromFile, {}).body.note, "a field this page doesn't know");
});

test("what the page is told about a rule isn't saved back, nor the defaults", () => {
  const { body, dropped } = ruleBody(fromFile, { verify: false, min_mentions: 1, stations: [], near: [] }, TAKES.keyword);
  for (const k of ["origin", "enabled", "kind", "verify", "min_mentions", "stations", "near"]) assert.equal(k in body, false, k);
  assert.equal(dropped.includes("origin") || dropped.includes("enabled"), false, "what the page was told isn't counted as dropped");
});

test("an edited field replaces the old one, and an emptied one is left out", () => {
  const { body } = ruleBody(fromFile, { severity: "critical", regex: lines("  \\bbridge closed\\b \n\n") }, TAKES.keyword);
  assert.equal(body.severity, "critical");
  assert.deepEqual(body.regex, ["\\bbridge closed\\b"]);
  assert.equal("regex" in ruleBody(fromFile, { regex: lines("") }, TAKES.keyword).body, false);
});

// The check's two keys, as a body has them.
const pick = (body) => Object.fromEntries(["verify", "unverified"].filter((k) => k in body).map((k) => [k, body[k]]));

test("a rule whose check is switched off is saved without what becomes of an unchecked match", () => {
  // The server refuses `unverified` without `verify = true`; unticking the check dropped only verify.
  const held = { ...fromFile, verify: true, unverified: "hold" };
  for (const off of [{ verify: false }, { verify: false, unverified: "hold" }]) {
    const { body, dropped } = ruleBody(held, off, TAKES.keyword);
    assert.equal("verify" in body, false, JSON.stringify(off));
    assert.equal("unverified" in body, false, JSON.stringify(off));
    assert.equal(dropped.includes("unverified"), false, "left out on purpose, not as a key this version doesn't take");
  }
  // A file rule with unverified and no verify at all (refused at start) is saved without it too.
  assert.equal("unverified" in ruleBody({ ...fromFile, unverified: "announce" }, {}, TAKES.keyword).body, false);
});

test("with the check on, the choice goes with it, and the default is left out", () => {
  assert.deepEqual(pick(ruleBody(fromFile, { verify: true, unverified: "hold" }, TAKES.keyword).body), { verify: true, unverified: "hold" });
  assert.deepEqual(pick(ruleBody(fromFile, { verify: true, unverified: "announce" }, TAKES.keyword).body), { verify: true, unverified: "announce" });
  // Back to one step lower, the server's default: nothing to say.
  const held = { ...fromFile, verify: true, unverified: "hold" };
  assert.deepEqual(pick(ruleBody(held, { verify: true, unverified: "lower" }, TAKES.keyword).body), { verify: true });
});

test("a station rule keeps its kind and its own fields", () => {
  const silent = { id: "quiet", kind: "silence", after_minutes: 5, level_db: -60, severity: "medium", origin: "page", enabled: false };
  assert.deepEqual(ruleBody(silent, { severity: "high", after_minutes: 2.5 }, TAKES.silence),
    { body: { id: "quiet", kind: "silence", after_minutes: 2.5, level_db: -60, severity: "high" }, dropped: [] });
  assert.deepEqual(ruleBody(null, { id: "ta", kind: "traffic", severity: "low", stations: [] }, TAKES.traffic).body, { id: "ta", kind: "traffic", severity: "low" });
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
