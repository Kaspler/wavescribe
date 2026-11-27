// Choosing a language by its name in English, in the language itself, or its tag.
import assert from "node:assert/strict";
import { test } from "node:test";
import { LANGS, canonicalTag, countryLang, findLangs, resolveLang } from "../src/lib/lang.ts";

test("a name in English, in the language itself, or a tag finds it", () => {
  assert.equal(findLangs("ukr", LANGS)[0], "uk");
  assert.equal(findLangs("українська", LANGS)[0], "uk");
  assert.equal(findLangs("uk", LANGS)[0], "uk");
  assert.equal(findLangs("deutsch", LANGS)[0], "de");
  assert.equal(findLangs("francais", LANGS)[0], "fr", "accents are optional");
  assert.ok(findLangs("portug", LANGS).includes("pt-PT"));
});

test("a name that only has the words inside it is found after the ones that start with them", () => {
  const hits = findLangs("brazil", LANGS);
  assert.equal(hits[0], "pt-BR");
});

test("nothing typed lists the first languages; nonsense finds none", () => {
  assert.deepEqual(findLangs("", ["sw", "ko", "sw"], 5), ["sw", "ko"]);
  assert.deepEqual(findLangs("qqqq", LANGS), []);
});

test("typed text resolves to one tag, or to none", () => {
  assert.equal(resolveLang("Swahili", LANGS), "sw");
  assert.equal(resolveLang("한국어", LANGS), "ko");
  assert.equal(resolveLang("pt_br", LANGS), "pt-BR");
  assert.equal(resolveLang("es-MX", LANGS), "es-MX", "a valid tag needn't be in the list");
  assert.equal(resolveLang("Portuguese", LANGS), "pt", "the plainest tag of that name");
  assert.equal(resolveLang("Klingonese", LANGS), null);
  assert.equal(resolveLang("", LANGS), null);
});

test("tags are written as BCP-47 writes them", () => {
  assert.equal(canonicalTag("EN_gb"), "en-GB");
  assert.equal(canonicalTag("not a tag"), null);
});

test("a country gives the language most people there speak", () => {
  assert.equal(countryLang("pt"), "pt-PT");
  assert.equal(countryLang("UA"), "uk-UA");
  assert.equal(countryLang("JP"), "ja-JP");
  assert.equal(countryLang(null), null);
  assert.equal(countryLang("Europe"), null);
});
