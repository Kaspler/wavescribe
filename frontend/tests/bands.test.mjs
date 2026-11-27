// A band has one name on the page, the dial's, wherever it is called: on a button, in a choice,
// in a scan's rows. The plan's name is the technical one.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandLabel, labelWords } from "../src/manage/bands.ts";

const BANDS = [
  { id: "fm", name: "FM broadcast" },
  { id: "marine", name: "Marine VHF" },
  { id: "2m", name: "Amateur 2 m" },
  { id: "70cm", name: "Amateur 70 cm" },
  { id: "land-mobile-vhf", name: "Business and public-service radio (VHF)" },
  { id: "someday", name: "A band this page has no words for" },
];
const by = (id) => BANDS.find((b) => b.id === id);

test("a band is called by its everyday name, the shorter one where that is shared", () => {
  assert.equal(bandLabel(by("fm")), "FM radio");
  assert.equal(bandLabel(by("marine")), "Boats and ports");
  assert.equal(bandLabel(by("land-mobile-vhf")), "Business radio");
  assert.notEqual(bandLabel(by("2m")), bandLabel(by("70cm")), "two amateur bands, two names");
  assert.equal(bandLabel(by("someday")), "A band this page has no words for", "as the plan names it");
});

test("what a scan says a frequency is keeps its channel and takes the band's everyday name", () => {
  assert.equal(labelWords({ band: "marine", label: "Marine VHF, channel 16" }, BANDS), "Boats and ports, channel 16");
  assert.equal(labelWords({ band: "fm", label: "FM broadcast" }, BANDS), "FM radio");
  assert.equal(labelWords({ band: null, label: "the whole tuner range" }, BANDS), "the whole tuner range", "no band: as it is");
  assert.equal(labelWords({ band: "fm", label: "88 to 90 MHz" }, BANDS), "88 to 90 MHz", "not the band's name: as it is");
  assert.equal(labelWords({ band: "unknown", label: "Something" }, BANDS), "Something", "a band not in the plan");
  assert.equal(labelWords({ band: "fm", label: null }, BANDS), "");
});
