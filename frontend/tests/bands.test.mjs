// A band has one name on the page, the dial's, wherever it is called: on a button, in a choice,
// in a scan's rows. The plan's name is the technical one.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandLabel, channelAt, placeWords } from "../src/manage/bands.ts";

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

test("a frequency is named by its band's everyday name and its channel number, not the label's words", () => {
  assert.equal(placeWords({ band: "marine", channel: 16, label: "Marine VHF, channel 16" }, BANDS), "Boats and ports, channel 16");
  assert.equal(placeWords({ band: "fm", channel: null, label: "FM broadcast" }, BANDS), "FM radio");
  // The label's words don't matter where the band and channel are known.
  assert.equal(placeWords({ band: "marine", channel: 16, label: "Canal 16" }, BANDS), "Boats and ports, channel 16");
  assert.equal(placeWords({ band: "2m", channel: null, label: "Amateur 2 m" }, BANDS), "Amateurs 2 m");
  assert.equal(placeWords({ band: "someday", channel: 3, label: "A band this page has no words for, channel 3" }, BANDS),
    "A band this page has no words for, channel 3", "the plan's name where the page has none");
});

test("the label stands only where there is no band the page can name", () => {
  assert.equal(placeWords({ band: null, channel: null, label: "the whole tuner range" }, BANDS), "the whole tuner range");
  assert.equal(placeWords({ band: null, channel: null, label: "88 to 90 MHz" }, BANDS), "88 to 90 MHz", "a range scanned");
  assert.equal(placeWords({ band: "unknown", channel: 2, label: "Something" }, BANDS), "Something", "a band neither the plan nor the page knows");
  assert.equal(placeWords({ band: null, channel: null, label: null }, BANDS), "");
  // Without the plan's list (a reading column), the bands the page has words for are still named.
  assert.equal(placeWords({ band: "pmr446", channel: 1, label: "PMR446 walkie-talkies, channel 1" }), "Walkie-talkies, channel 1");
  assert.equal(placeWords({ band: "someday", channel: 3, label: "Someday, channel 3" }), "Someday, channel 3");
});

test("a row that doesn't carry its channel finds it in the band's list, as the server does", () => {
  const marine = { id: "marine", name: "Marine VHF", channels_mhz: [156.05, 156.8, 157.0] };
  assert.equal(channelAt(marine, 156.8), 2);
  assert.equal(channelAt(marine, 156.8015), 2, "within 2 kHz");
  assert.equal(channelAt(marine, 156.81), null);
  assert.equal(channelAt({ channels_mhz: [] }, 100), null, "a band without a list");
  assert.equal(channelAt(marine, null), null);
  assert.equal(placeWords({ band: "marine", label: "Marine VHF, channel 2", mhz: 156.8 }, [marine]), "Boats and ports, channel 2");
  assert.equal(placeWords({ band: "marine", label: "Marine VHF", mhz: 156.9 }, [marine]), "Boats and ports");
  // A scan of a whole band names no channel.
  assert.equal(placeWords({ band: "marine", channel: null, label: "Marine VHF" }, [marine]), "Boats and ports");
});
