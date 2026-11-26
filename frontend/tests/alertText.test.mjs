// Run with `npm --prefix frontend test`. Node runs the TypeScript module as it is.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { alertSpans, excerptStart, findTerms } from "../src/reading/alertText.ts";

const fx = JSON.parse(readFileSync(new URL("./fixtures/alerts.json", import.meta.url), "utf8"));
const word = (text, [a, b]) => Array.from(text).slice(a, b).join("");

test("an alert keeps its own hit spans and the rules it names", () => {
  assert.deepEqual(alertSpans(fx.late.hits, 7, ["strike"]), [[204, 210]]);
  assert.deepEqual(alertSpans(fx.late.hits, 7, ["strike", "other"]), [[5, 12], [204, 210]]);
  assert.deepEqual(alertSpans(fx.late.hits, 3, []), []);
});

test("a late match opens the folded text at a word a little before it", () => {
  const spans = alertSpans(fx.late.hits, 7, ["strike"]);
  assert.equal(word(fx.late.quote, spans[0]), "strike");
  const start = excerptStart(fx.late.quote, spans);
  assert.ok(start >= 204 - 40 && start <= 204, `start ${start}`);
  assert.match(fx.late.quote[start - 1], /\s/);
  assert.match(Array.from(fx.late.quote).slice(start).join(""), /^\S.*general strike/);
});

test("an early match, or none, keeps the opening", () => {
  assert.equal(excerptStart(fx.early.quote, findTerms(fx.early.quote, fx.early.terms)), 0);
  assert.equal(excerptStart(fx.early.quote, []), 0);
});

test("terms are found at word starts, whole words marked, case and accents aside", () => {
  assert.deepEqual(findTerms(fx.early.quote, fx.early.terms).map((s) => word(fx.early.quote, s)), ["Flood"]);
  assert.deepEqual(findTerms(fx.accents.quote, fx.accents.terms).map((s) => word(fx.accents.quote, s)), ["ÉVACUATION", "évacuations"]);
  assert.deepEqual(findTerms(fx.early.quote, ["lood"]), []);
});

test("spans count code points, as the server does", () => {
  const spans = findTerms(fx.astral.quote, fx.astral.terms);
  assert.deepEqual(spans.map((s) => word(fx.astral.quote, s)), ["Storms", "storm"]);
  assert.equal(spans[0][0], 3);
});
