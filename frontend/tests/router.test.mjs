// The header's Read link leads back only to a reading view, whatever was open before.
import assert from "node:assert/strict";
import { test } from "node:test";
import { alertHash, isReading, movedHash, parse } from "../src/lib/router.ts";

test("reading views are the live columns, chosen columns, a search, a question and a summary", () => {
  for (const h of ["", "#/", "#/s/a,b", "#/find/storm", "#/ask", "#/summary"]) assert.equal(isReading(h), true, h);
  assert.deepEqual(parse("#/summary"), { kind: "summary" });
});

test("Listen, Find, Settings and the older links to them are not reading views", () => {
  for (const h of ["#/listen", "#/add", "#/add/air", "#/add/internet", "#/settings", "#/settings/health",
    "#/manage/stations", "#/radio/find", "#/radio"]) assert.equal(isReading(h), false, h);
});

test("older links open the page that took their place", () => {
  assert.equal(movedHash("#/radio/kept"), "#/settings/storage");
  assert.equal(movedHash("#/radio/tune"), "#/listen");
  assert.equal(movedHash("#/manage/models"), "#/settings/languages");
  assert.equal(movedHash("#/radio"), "#/add/air");
  assert.equal(movedHash("#/listen"), null);
  assert.deepEqual(parse("#/add/internet"), { kind: "add", page: "internet" });
});

test("a link to an alert opens at the column of its first station the page has", () => {
  assert.deepEqual(parse("#/alert/77"), { kind: "alert", id: 77 });
  assert.deepEqual(parse("#/alert/x"), { kind: "live" });
  assert.equal(isReading("#/alert/77"), false); // it moves on to the column at once
  const known = (id) => id === "b" || id === "c";
  assert.equal(alertHash(["b", "c"], known), "#/s/b");
  assert.equal(alertHash(["gone", "c"], known), "#/s/c");
  assert.equal(alertHash(["gone"], known), "#/");
});
