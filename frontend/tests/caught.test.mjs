// Caught on the air: what was caught of open radio without being kept as a station, read as one
// feed beside the stations, in the reader's language where a line was translated into it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { CAUGHT, caughtFeed, feedOf, readable } from "../src/lib/caught.ts";
import { pick, shownLang } from "../src/lib/lang.ts";
import { placeWords } from "../src/manage/bands.ts";
import { channelHeads, channelText } from "../src/reading/channels.ts";

const line = (over) => ({
  id: 1, key: "watch:1", station: "watch", t_start: 1000, duration: 2, language: "de", text: "Rufzeichen bitte",
  translations: {}, pending: [], final: true, audio: false, freq_hz: 145337500, band: "2m", channel: null,
  label: "Amateur 2 m", caught: true, ...over,
});

test("a line caught on the air is read in that feed, any other in its station's column", () => {
  assert.equal(feedOf(line()), CAUGHT);
  assert.equal(feedOf(line({ caught: undefined, station: "ham" })), "ham", "a kept band's own line");
  assert.equal(feedOf({ station: "valley" }), "valley", "a server that doesn't say");
});

test("its column can be shown once the page has loaded; a station's where the page has it", () => {
  const has = (id) => id === "ham";
  assert.equal(readable(CAUGHT, has, true), true);
  assert.equal(readable(CAUGHT, has, false), false, "not yet: it would say nothing was caught");
  assert.equal(readable("ham", has, true), true);
  assert.equal(readable("watch", has, true), false, "the source of a caught line is no column of its own");
  assert.equal(CAUGHT.includes("."), true, "no station's id has a dot, so none is this one");
});

test("it reads in the reader's language where a line was translated, else as heard", () => {
  const st = caughtFeed("Caught on the air", "en");
  assert.equal(st.id, CAUGHT);
  assert.equal(st.language, "auto");
  const shown = shownLang(st, "en");
  assert.equal(shown, "en");
  assert.deepEqual(pick(line({ translations: { en: "Call sign please" } }), st, shown), ["Call sign please", "en", ""]);
  assert.deepEqual(pick(line({ pending: ["en"] }), st, shown), ["Rufzeichen bitte", "de", "pending"]);
  assert.deepEqual(pick(line(), st, shown), ["Rufzeichen bitte", "de", "untranslated"], "named by its language");
  assert.deepEqual(pick(line({ language: "en", text: "QRZ" }), st, shown), ["QRZ", "en", ""], "heard in it already");
  // A reader who hasn't chosen a language reads every line as it was heard.
  const asHeard = caughtFeed("Caught on the air", null);
  assert.deepEqual(asHeard.translate_to, []);
  assert.deepEqual(pick(line({ translations: { en: "Call sign please" } }), asHeard, shownLang(asHeard, null)), ["Rufzeichen bitte", "de", ""]);
});

test("the same feed object while nothing changes, so its lines aren't drawn again", () => {
  assert.equal(caughtFeed("Caught on the air", "uk"), caughtFeed("Caught on the air", "uk"));
  assert.notEqual(caughtFeed("Caught on the air", "uk"), caughtFeed("Caught on the air", "fr"));
});

test("each run of its lines on one channel says where it was heard", () => {
  const paras = [line(), line({ freq_hz: 156625000, band: "marine", label: "Marine VHF" }), line()].map((u) => ({ utts: [u] }));
  assert.deepEqual(channelHeads(paras, true), [true, true, true]);
  assert.equal(channelText(paras[1].utts[0], placeWords), "156.625 MHz · Boats and ports");
  assert.equal(channelText(paras[0].utts[0], placeWords), "145.3375 MHz · Amateurs 2 m");
});
