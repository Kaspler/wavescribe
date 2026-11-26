// A station started from an off list keeps its row until the pointer leaves the list, so the row
// under the pointer doesn't jump away to the running ones. Touch has no "leave": a few seconds.
import type { MouseEvent } from "react";
import { emit } from "../lib/store";

const HOLD_TOUCH_MS = 4000;
export const held = new Set<string>();
let release = 0;

export function hold(id: string, e: MouseEvent) {
  held.add(id);
  clearTimeout(release);
  if ((e.nativeEvent as PointerEvent).pointerType !== "mouse") release = window.setTimeout(letGo, HOLD_TOUCH_MS);
}

export function letGo() {
  if (!held.size) return;
  held.clear();
  emit("held");
}
