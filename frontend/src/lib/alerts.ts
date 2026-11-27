// What an alert event changes. The server says so in `change`: `new`, `escalated`, `reminder`,
// `ended`, or `update` for anything else (a further mention, a translation, a delivery). Only a
// new alert, or one grown more serious, draws the eye; a reminder and an ended incident are noted
// quietly on the alert. Plain functions, so they can be tested without a page.
import type { Alert } from "./types";

/** Takes an alert event into what the page holds (`prev`, if it had the alert already): the
 * alert to keep, and whether it should show and be announced as news. */
export function takeAlert(prev: Alert | undefined, a: Alert): { alert: Alert; rings: boolean } {
  const c = a.change;
  // A server that doesn't say what changed: an alert not seen before is the only news.
  const rings = c ? c === "new" || c === "escalated" : !prev;
  // An incident that ended stays ended; a reminder holds until it is announced again as more serious.
  const lately = c === "ended" || prev?.lately === "ended" ? "ended"
    : c === "reminder" ? "reminder"
    : c === "new" || c === "escalated" ? undefined
    : prev?.lately;
  return { alert: { ...a, lately }, rings };
}
