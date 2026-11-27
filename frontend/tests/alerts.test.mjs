// Alert events: only a new or more serious alert is news; reminders and ended incidents are noted.
import assert from "node:assert/strict";
import { test } from "node:test";
import { takeAlert } from "../src/lib/alerts.ts";

const alert = (change, extra = {}) => ({ id: 7, rule: "road-closed", severity: "medium", change, ...extra });

test("a new alert and one grown more serious ring; nothing else does", () => {
  let held;
  const seen = [];
  for (const [change, severity] of [["new", "medium"], ["update", "medium"], ["reminder", "medium"], ["escalated", "high"], ["update", "high"], ["ended", "high"]]) {
    const { alert: a, rings } = takeAlert(held, alert(change, { severity }));
    held = a;
    seen.push(`${change}:${rings ? "rings" : "quiet"}:${a.lately ?? "-"}`);
  }
  assert.deepEqual(seen, [
    "new:rings:-",
    "update:quiet:-",
    "reminder:quiet:reminder",
    "escalated:rings:-",
    "update:quiet:-",
    "ended:quiet:ended",
  ]);
});

test("a reminder is kept through later updates, and an ended incident stays ended", () => {
  let { alert: a } = takeAlert(undefined, alert("new"));
  a = takeAlert(a, alert("reminder")).alert;
  a = takeAlert(a, alert("update", { mentions: 4 })).alert;
  assert.equal(a.lately, "reminder");
  assert.equal(a.mentions, 4);
  a = takeAlert(a, alert("ended")).alert;
  a = takeAlert(a, alert("update")).alert;
  assert.equal(a.lately, "ended");
});

test("a reminder for an alert the page never had doesn't ring", () => {
  assert.equal(takeAlert(undefined, alert("reminder")).rings, false);
});

test("without a change, only an alert not seen before rings", () => {
  const first = takeAlert(undefined, alert(undefined));
  assert.equal(first.rings, true);
  assert.equal(takeAlert(first.alert, alert(undefined, { mentions: 2 })).rings, false);
});
