// A station started from an off list keeps its row for a moment (lib/hold.ts): until the pointer
// leaves the list, or a few seconds after the last start or the last movement over it.
import { makeHolds } from "../lib/hold";
import { emit } from "../lib/store";

export const held = makeHolds<number>(
  { set: (f, ms) => window.setTimeout(f, ms), clear: (h) => clearTimeout(h) },
  () => emit("held"),
);

export const hold = (id: string) => held.hold(id);
export const letGo = () => held.release();
