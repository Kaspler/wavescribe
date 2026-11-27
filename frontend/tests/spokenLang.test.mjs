// A new station's language comes from the person's own stations on the band, then on the
// receiver, and from nowhere else.
import assert from "node:assert/strict";
import { test } from "node:test";
import { spokenFields, spokenLang, stationsOnAir } from "../src/manage/spokenLang.ts";

const bandOf = (mhz) => (mhz >= 87.5 && mhz <= 108 ? "fm" : mhz >= 446 && mhz <= 446.2 ? "pmr446" : mhz >= 144 && mhz <= 146 ? "2m" : null);
const st = (device, mhz, language, watch = null) => ({ device, mhz, language, watch });

test("the language most of the stations on the band speak", () => {
  const kept = [st("a", 90.2, "pt-PT"), st("a", 91.3, "pt-PT"), st("b", 97.4, "es-ES"), st("a", 446.00625, "en")];
  assert.equal(spokenLang(kept, { mhz: 99.0, device: "b" }, bandOf), "pt-PT", "the band before the receiver");
});

test("else the language most of the receiver's stations speak", () => {
  const kept = [st("a", 90.2, "pt-PT"), st("b", 145.5, "de"), st("b", 145.6, "de"), st("b", 146.0, "fr")];
  assert.equal(spokenLang(kept, { mhz: 446.00625, device: "b" }, bandOf), "de");
});

test("nothing to go on: empty, for the page to ask", () => {
  assert.equal(spokenLang([], { mhz: 99.0, device: "a" }, bandOf), "");
  assert.equal(spokenLang([st("a", 90.2, "pt-PT")], { mhz: 446.00625, device: "b" }, bandOf), "", "another band, another receiver");
  assert.equal(spokenLang([st("a", 90.2, "pt-PT")], { mhz: 1200, device: "b" }, bandOf), "", "outside every band");
});

test("a watch, or a station whose language is detected, says nothing", () => {
  const kept = [st("a", 446.00625, "auto", "pmr446"), st("a", 446.1, "es", "pmr446"), st("a", 446.05, "auto")];
  assert.equal(spokenLang(kept, { mhz: 446.0187, device: "a" }, bandOf), "");
});

test("on a tie, the first of them", () => {
  assert.equal(spokenLang([st("a", 90.2, "pt-PT"), st("a", 91.3, "es-ES")], { mhz: 99, device: "a" }, bandOf), "pt-PT");
});

test("the radio view's stations take their language from the page's list", () => {
  const receivers = [{ id: "a", stations: [{ id: "one", mhz: 90.2 }, { id: "gone", mhz: 91.0 }] }, { id: "b", stations: [{ id: "w", mhz: 446.1 }] }];
  const stations = [{ id: "one", language: "pt-PT" }, { id: "w", language: "auto", watch: "pmr446" }];
  assert.deepEqual(stationsOnAir(receivers, stations), [
    { device: "a", mhz: 90.2, language: "pt-PT", watch: undefined },
    { device: "b", mhz: 446.1, language: "auto", watch: "pmr446" },
  ]);
});

test("a request names one language, or asks to detect it among the few given", () => {
  assert.deepEqual(spokenFields("pt-PT"), { language: "pt-PT" });
  assert.deepEqual(spokenFields("auto"), { language: "auto" }, "any language the model tells apart");
  assert.deepEqual(spokenFields("auto", []), { language: "auto" });
  assert.deepEqual(spokenFields("auto", ["es", "en"]), { language: "auto", languages: ["es", "en"] });
  assert.deepEqual(spokenFields("es", ["es", "en"]), { language: "es" }, "a list beside one language would be refused");
  assert.deepEqual(spokenFields(" de "), { language: "de" });
});
