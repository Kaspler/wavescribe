// A refusal is read by its code and the fields that go with it, never by its words.
import assert from "node:assert/strict";
import { test } from "node:test";
import { refused } from "../src/manage/refusals.ts";

test("a band too wide for one receiver offers the slice the server gives", () => {
  const r = refused("Airband is 19.00 MHz wide…", { error: "…", code: "too_wide", slice_mhz: [126.4, 128.6] });
  assert.equal(r.code, "too_wide");
  assert.deepEqual(r.slice, [126.4, 128.6]);
  assert.equal(r.text, "Airband is 19.00 MHz wide…", "its sentence as it is");
});

test("a slice of the wrong shape is no slice", () => {
  for (const slice_mhz of [[128.6, 126.4], [126.4], ["126.4", "128.6"], [126.4, Number.NaN], null, "126.4:128.6"]) {
    assert.equal(refused("x", { code: "too_wide", slice_mhz }).slice, null, JSON.stringify(slice_mhz));
  }
});

test("a retune names the running stations it would stop; busy too", () => {
  const r = refused("To hear Walkie-talkies, Receiver B moves to 445.75625 MHz…", {
    code: "retune", holders: ["Test FM One"], center_mhz: 445.75625, unfit: [{ id: "test-fm-one", name: "Test FM One", running: true }],
  });
  assert.deepEqual(r.stops, ["Test FM One"]);
  assert.deepEqual(refused("x", { code: "retune", holders: [] }).stops, [], "nothing running: nothing stops");
  assert.deepEqual(refused("x", { code: "busy", holders: ["A", 3, null] }).stops, ["A"]);
});

test("a confirmation asked for names the kind of radio", () => {
  assert.equal(refused("446.00625 MHz is PMR446 (private radio)…", { code: "acknowledge", privacy: "private" }).privacy, "private");
  assert.equal(refused("x", { code: "acknowledge" }).privacy, null);
});

test("an unknown code, or none, is the server's sentence and nothing else", () => {
  for (const data of [{ code: "out_of_reach" }, { code: "something_new", slice_mhz: [1, 2], holders: ["A"], privacy: "private" }, {}, null, undefined]) {
    const r = refused("Receiver B doesn't hear it where it is tuned now.", data);
    assert.equal(r.text, "Receiver B doesn't hear it where it is tuned now.");
    assert.deepEqual([r.stops, r.slice, r.privacy], [[], null, null], JSON.stringify(data));
  }
  assert.equal(refused("x", { code: 7 }).code, null, "a code that isn't a word is none");
});
