// The alerts list says when the rules that listen for topics can't match on any running station.
import assert from "node:assert/strict";
import { test } from "node:test";
import { withoutWordLists } from "../src/reading/rulesNote.ts";

const st = (language, languages) => ({ language, languages });

test("no running station is in a language with a word list: their languages, once each", () => {
  assert.deepEqual(withoutWordLists(["nl"], [st("fr-FR"), st("de-DE"), st("fr-CA"), st("it")]), ["fr-FR", "de-DE", "it"]);
  // No word list has the rules' topics at all.
  assert.deepEqual(withoutWordLists([], [st("sv-SE")]), ["sv-SE"]);
});

test("one station in a language with a word list: nothing to say", () => {
  assert.equal(withoutWordLists(["nl"], [st("fr-FR"), st("nl-BE")]), null);
  assert.equal(withoutWordLists(["NL"], [st("nl_NL")]), null); // tags are compared by their language
});

test("no rule listens for topics, or alerts are off: nothing to say", () => {
  assert.equal(withoutWordLists(null, [st("fr-FR")]), null);
  assert.equal(withoutWordLists(undefined, [st("fr-FR")]), null); // a server from before the field
});

test("no station is being written down: nothing to say", () => {
  assert.equal(withoutWordLists(["nl"], []), null);
});

test("a station whose language the speech model tells goes by the languages it expects", () => {
  assert.deepEqual(withoutWordLists(["nl"], [st("auto", ["de-DE", "en"])]), ["de-DE", "en"]);
  assert.equal(withoutWordLists(["nl"], [st("auto", ["de-DE", "nl"])]), null);
  // Any language at all: it may speak one that has a list, so nothing is claimed.
  assert.equal(withoutWordLists(["nl"], [st("fr-FR"), st("auto", [])]), null);
  assert.equal(withoutWordLists(["nl"], [st("auto", null)]), null);
});
