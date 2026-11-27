// A band has one name on the page, the dial's, wherever it is called: on a button, in a choice,
// in a scan's rows. The plan's name is the technical one.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bandLabel, channelAt, listeningWords, placeWords, watchListening, watchListensWords } from "../src/manage/bands.ts";

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

test("a watch listens on its band's numbered channels, first to last", () => {
  const pmr = { id: "pmr446", name: "PMR446", start_mhz: 446.0, stop_mhz: 446.2, step_khz: 12.5, channels_mhz: Array.from({ length: 16 }, (_, i) => 446.00625 + i * 0.0125) };
  const l = watchListening("pmr446", [pmr]);
  assert.deepEqual([l.channels, l.first, l.last], [16, 1, 16]);
  assert.equal(listeningWords(l), "channels 1–16, 446.006 to 446.194 MHz");
  // A range of the band: only its channels, still by the band's numbers.
  assert.equal(listeningWords(watchListening([446.05, 446.1], [pmr])), "channels 5–8, 446.056 to 446.094 MHz");
  assert.equal(listeningWords(watchListening([446.09, 446.1], [pmr])), "channel 8, 446.094 MHz");
  assert.equal(watchListening([446.0, 446.005], [pmr]), null);
  assert.equal(watchListening("marine", [pmr]), null);
});

test("without a channel list a watch listens on the band's raster, from its start", () => {
  const cm70 = { id: "70cm", name: "70 cm", start_mhz: 430, stop_mhz: 440, step_khz: 12.5, channels_mhz: [] };
  const all = watchListening("70cm", [cm70]);
  assert.deepEqual([all.channels, all.first, all.lo, all.hi], [801, null, 430, 440]);
  assert.equal(listeningWords(watchListening([433.0, 433.1], [cm70])), "9 channels 12.5 kHz apart, 433.000 to 433.100 MHz");
  // The edges round onto the raster as the server's do: inward.
  assert.equal(listeningWords(watchListening([433.001, 433.03], [cm70])), "2 channels 12.5 kHz apart, 433.013 to 433.025 MHz");
  // Outside every band: 12.5 kHz from zero.
  assert.equal(watchListening([1.0, 1.02], [cm70]).channels, 2);
});

test("what a watch listens on is said with its band's name, or its range, never a tuning", () => {
  const pmr = { id: "pmr446", name: "PMR446", start_mhz: 446.0, stop_mhz: 446.2, step_khz: 12.5, channels_mhz: Array.from({ length: 16 }, (_, i) => 446.00625 + i * 0.0125) };
  assert.equal(watchListensWords("pmr446", [pmr]), "Walkie-talkies, listening on channels 1–16, 446.006 to 446.194 MHz");
  assert.equal(watchListensWords([446.05, 446.1], [pmr]), "Walkie-talkies, listening on channels 5–8, 446.056 to 446.094 MHz");
  assert.equal(watchListensWords([1.0, 1.0125], [pmr]), "1.000–1.013 MHz, listening on 2 channels 12.5 kHz apart, 1.000 to 1.013 MHz");
  // Without the band plan the channels can't be told: the band's name, or the range, alone.
  assert.equal(watchListensWords("pmr446", null), "Walkie-talkies");
  assert.equal(watchListensWords([446.05, 446.1], null), "446.050–446.100 MHz");
  // A band the region's plan lacks is named, and no more.
  assert.equal(watchListensWords("frs", [pmr]), "Walkie-talkies");
});
