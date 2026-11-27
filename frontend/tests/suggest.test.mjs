// "Suggest in each language" says what came back, per language, and never "added" for nothing.
import assert from "node:assert/strict";
import { test } from "node:test";
import { takeSuggestions } from "../src/manage/suggest.ts";

const NAMES = { fr: "French", de: "German", it: "Italian" };
const name = (l) => NAMES[l] || l;
const entry = (words, why = null, message = null, dropped = 0) => ({ words, why, dropped, message });

test("the words go into each language's list, after what is there, once each", () => {
  const answer = {
    terms: { fr: ["coupure d'eau", "eau coupée"], de: ["Wasser abgestellt"] },
    languages: { fr: entry(["coupure d'eau", "eau coupée"]), de: entry(["Wasser abgestellt"]) },
    count: 3, empty: [], message: "3 suggested in 2 of 2 languages. They are the model's proposals and can be wrong: check each one before saving.",
  };
  const s = takeSuggestions({ fr: "coupure d'eau", de: "" }, ["fr", "de"], answer, name);
  assert.equal(s.terms.fr, "coupure d'eau\neau coupée");
  assert.equal(s.terms.de, "Wasser abgestellt");
  assert.equal(s.toast, answer.message, "the server's sentence, as it is");
  assert.equal(s.bad, false);
  assert.deepEqual(s.notes, {});
});

test("a language that got none says why beside its list; the others get their words", () => {
  const answer = {
    terms: { fr: ["grève"], de: [], it: [] },
    languages: { fr: entry(["grève"]), de: entry([], "busy", "the model was busy"), it: entry([], "timeout", "the time ran out before the model got to it") },
    count: 1, empty: ["de", "it"],
    message: "1 suggested in 1 of 3 languages. They are the model's proposals and can be wrong: check each one before saving. None for German (the model was busy), Italian (the time ran out before the model got to it).",
  };
  const s = takeSuggestions({}, ["fr", "de", "it"], answer, name);
  assert.equal(s.terms.fr, "grève");
  assert.equal(s.terms.de, undefined, "nothing added where nothing came");
  assert.deepEqual(s.notes, { de: "No suggestions in German: the model was busy.", it: "No suggestions in Italian: the time ran out before the model got to it." });
  assert.equal(s.bad, false);
});

test("a reply cut short keeps its words and says there may be more", () => {
  const answer = { terms: { fr: ["grève"] }, languages: { fr: entry(["grève"], "cut", "the model's reply was cut short: there may have been more") }, count: 1 };
  assert.deepEqual(takeSuggestions({}, ["fr"], answer, name).notes, { fr: "The model's reply was cut short: there may have been more." });
});

test("nothing at all came back: the refusal's sentence, marked as such, and nothing added", () => {
  // As the page passes a 400 with code no_suggestions: its fields, and its sentence as `error`.
  const refusal = {
    error: "No suggestions came back. None for French (the model's reply couldn't be read).", code: "no_suggestions",
    terms: { fr: [] }, languages: { fr: entry([], "unreadable", "the model's reply couldn't be read") }, count: 0, empty: ["fr"],
  };
  const s = takeSuggestions({ fr: "grève" }, ["fr"], refusal, name);
  assert.equal(s.toast, refusal.error);
  assert.equal(s.bad, true);
  assert.equal(s.terms.fr, "grève");
  assert.deepEqual(s.notes, { fr: "No suggestions in French: the model's reply couldn't be read." });
});

test("an answer from before per-language reasons: true to what it has", () => {
  assert.equal(takeSuggestions({}, ["fr", "de"], { terms: { fr: [], de: [] } }, name).toast, "No suggestions came back.");
  assert.equal(takeSuggestions({}, ["fr", "de"], { terms: { fr: [], de: [] } }, name).bad, true);
  const some = takeSuggestions({}, ["fr", "de"], { terms: { fr: ["grève"], de: [] } }, name);
  assert.equal(some.toast, "Suggestions added in French: check them before saving. None came back for German.");
  assert.equal(some.bad, false);
  assert.equal(takeSuggestions({}, ["fr"], {}, name).toast, "No suggestions came back.");
  assert.equal(takeSuggestions({}, ["fr"], null, name).bad, true);
});

test("words of the wrong kind are skipped, and fields it doesn't know are left alone", () => {
  const s = takeSuggestions({}, ["fr"], { terms: { fr: ["grève", 3, "", "  ", null, " arrêt "] }, later: { x: 1 } }, name);
  assert.equal(s.terms.fr, "grève\narrêt");
  assert.equal(takeSuggestions({}, ["fr"], { terms: { fr: "grève" } }, name).bad, true, "a list that isn't one adds nothing");
});

test("a note for the person is said once; the typed words the server echoes as JSON are not", () => {
  assert.equal(takeSuggestions({}, ["fr"], { terms: { fr: ["grève"] }, note: "[\"strike\"]" }, name).note, null);
  assert.equal(takeSuggestions({}, ["fr"], { terms: { fr: ["grève"] }, note: "Check each word before saving." }, name).note, "Check each word before saving.");
});
