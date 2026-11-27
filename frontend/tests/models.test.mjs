// The models banner names the stations that wait and the speech model each waits for.
import assert from "node:assert/strict";
import { test } from "node:test";
import { awaitedModels } from "../src/lib/models.ts";

const entry = (id, size_gb) => ({ id, capability: "asr", installed: false, size_gb, tier: "default", accepted: false, in_use: [] });

test("only the stations without their speech model, by the model they wait for", () => {
  const needs = [
    { what: ["he-IL"], entry: entry("whisper-test", 1.5) },
    { what: ["translation"], entry: entry("translator-test", 5.4) },
  ];
  assert.deepEqual(awaitedModels(["he-IL"], needs), [{ id: "whisper-test", languages: ["he-IL"], gb: 1.5 }]);
  // Two stations in the same language wait for one model, named once.
  assert.deepEqual(awaitedModels(["he-IL", "he-IL"], needs), [{ id: "whisper-test", languages: ["he-IL"], gb: 1.5 }]);
});

test("one model for two languages: named once, for both", () => {
  const needs = [{ what: ["am-ET", "ti-ER"], entry: entry("omni-test", 3) }];
  assert.deepEqual(awaitedModels(["ti-ER", "am-ET"], needs), [{ id: "omni-test", languages: ["ti-ER", "am-ET"], gb: 3 }]);
});

test("a language no missing model is for, or a list not loaded yet: nothing named", () => {
  assert.deepEqual(awaitedModels(["fr-FR"], [{ what: ["he-IL"], entry: entry("whisper-test", 1.5) }]), []);
  assert.deepEqual(awaitedModels(["fr-FR"], []), []);
});
