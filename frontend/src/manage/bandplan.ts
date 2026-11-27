// The band plan of the chosen region, for saying what a watch listens on where the page has no
// radio view of its own (Read's column heads). /api/manage answers only the Wavescribe machine:
// on any other there is no plan, and a watch is said by its band's name or its range alone.
import { useEffect, useState } from "react";
import { S } from "../lib/live";
import type { Band } from "./bands";

let plan: Promise<Band[] | null> | null = null;

/** Asked for once a page, and again after the region changes (forgetBandPlan). */
function load(): Promise<Band[] | null> {
  plan ||= fetch("/api/manage/radio", { headers: { "X-Wavescribe": "1" } })
    .then((r) => (r.ok ? r.json() : null))
    .then((v) => (Array.isArray(v?.bands) ? (v.bands as Band[]) : null), () => null)
    // Not loaded: asked for again by the next column that wants it.
    .then((b) => { if (!b) plan = null; return b; });
  return plan;
}

/** A change of region changes the bands, and some of their channels. */
export function forgetBandPlan() {
  plan = null;
}

/** The band plan, once loaded, while `wanted` (a watch is in view) on the Wavescribe machine. */
export function useBandPlan(wanted: boolean): Band[] | null {
  const [bands, setBands] = useState<Band[] | null>(null);
  useEffect(() => {
    if (!wanted || !S.manage) return;
    let live = true;
    load().then((b) => { if (live) setBands(b); });
    return () => { live = false; };
  }, [wanted, S.manage]);
  return bands;
}
