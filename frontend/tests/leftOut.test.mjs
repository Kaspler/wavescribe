// A line whose translation the translation checks left out says so where the translation would
// be, with the reason its flag (untranslated:<language>:<why>) gives.
import assert from "node:assert/strict";
import { test } from "node:test";
import { leftOut } from "../src/lib/lang.ts";
import { T } from "../src/reading/strings.ts";

test("why, for the language it was into", () => {
  const u = { flags: ["lang:es", "untranslated:en:remark", "untranslated:uk:too_long"] };
  assert.equal(leftOut(u, "en"), "remark");
  assert.equal(leftOut(u, "en-GB"), "remark", "tags compare by their language");
  assert.equal(leftOut(u, "uk"), "too_long");
  assert.equal(leftOut(u, "fr"), null, "into another language it wasn't left out");
  assert.equal(leftOut({ flags: [] }, "en"), null);
  assert.equal(leftOut({}, "en"), null);
  assert.equal(leftOut({ flags: ["untranslated:en"] }, "en"), "", "left out with no reason given");
});

test("the note, per reason", () => {
  assert.equal(T.noTranslation, "no translation");
  for (const why of ["declined", "remark"]) {
    assert.equal(T.noTranslationWhy(why, "French"), "No translation: the model couldn't translate this line. These are the words as heard, in French.");
  }
  assert.match(T.noTranslationWhy("too_long", "French"), /^No translation: the model's reply was far longer than the line/);
  assert.match(T.noTranslationWhy("empty", "French"), /^No translation: the model's reply was empty\. /);
  assert.match(T.noTranslationWhy("failed", "French"), /^No translation: translating this line failed\. /);
  assert.equal(T.noTranslationWhy("later_reason", "French"), "No translation. These are the words as heard, in French.");
});
