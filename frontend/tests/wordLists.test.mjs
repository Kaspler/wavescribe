// Settings, Alerts says where the rules that listen for topics have a word list, from the rules
// it shows, as /api/state's topic_languages does for the bell.
import assert from "node:assert/strict";
import { test } from "node:test";
import { topicLanguages } from "../src/manage/wordLists.ts";
import { withoutWordLists } from "../src/reading/rulesNote.ts";

// The packs as the rules view sends them: per language, its topics and places.
const packs = {
  nl: { topics: { water: ["waterleiding"], power: ["stroomstoring"] }, places: [] },
  sv: { topics: { power: ["strömavbrott"] }, places: [] },
};
const rule = (id, topics, enabled = true) => ({ id, kind: "keyword", topics, enabled });

test("the languages with a list for a topic of a rule that is on, once each", () => {
  assert.deepEqual(topicLanguages([rule("w", ["water"]), rule("p", ["power"])], packs), ["nl", "sv"]);
  assert.deepEqual(topicLanguages([rule("w", ["water"])], packs), ["nl"]);
  // A topic no list has: the rule listens for topics, and no language can match it.
  assert.deepEqual(topicLanguages([rule("x", ["flood"])], packs), []);
});

test("a rule switched off doesn't count, nor one without topics", () => {
  assert.deepEqual(topicLanguages([rule("w", ["water"], false), rule("p", ["power"])], packs), ["nl", "sv"]);
  assert.equal(topicLanguages([rule("w", ["water"], false)], packs), null);
  assert.equal(topicLanguages([{ id: "quiet", kind: "silence", enabled: true }, rule("own", [])], packs), null);
  assert.equal(topicLanguages([], packs), null);
});

test("no packs at all: the topic rules match nowhere", () => {
  assert.deepEqual(topicLanguages([rule("w", ["water"])], {}), []);
  assert.deepEqual(topicLanguages([rule("w", ["water"])], undefined), []);
});

test("with the stations being written down, it says what the bell says", () => {
  const st = [{ language: "fr-FR" }, { language: "de-DE" }];
  assert.deepEqual(withoutWordLists(topicLanguages([rule("w", ["water"])], packs), st), ["fr-FR", "de-DE"]);
  assert.equal(withoutWordLists(topicLanguages([rule("w", ["water"])], packs), [...st, { language: "nl-BE" }]), null);
  assert.equal(withoutWordLists(topicLanguages([rule("w", ["water"], false)], packs), st), null);
});
