// What is on screen right now: the stations shown as columns (the pointer in the station list
// follows them), whether this is a phone, and whether the alerts rail is showing.
import { P, setPrefs } from "./prefs";
import { emit } from "./store";

export const V = {
  shown: [] as string[],
  narrow: false,
  wide: false,
  railOpen: false,
  /** A line a link asked to see (an alert's): its station's column scrolls to it and marks it. */
  focus: null as { station: string; id: number } | null,
};

/** Asks the station's column to bring one utterance into view; the caller opens the column. */
export function showLine(station: string, id: number) {
  V.focus = { station, id };
  emit("focus");
}

export function setShown(ids: string[]) {
  if (ids.join(",") === V.shown.join(",")) return;
  V.shown = ids;
  emit("shown");
}

function watch(query: string, set: (on: boolean) => void) {
  const m = matchMedia(query);
  set(m.matches);
  m.addEventListener("change", () => {
    set(m.matches);
    emit("narrow");
  });
}
watch("(max-width: 719px)", (on) => (V.narrow = on));
// Wide enough to keep the alerts rail beside the station list and two readable columns.
watch("(min-width: 1600px)", (on) => (V.wide = on));

/** The rail sits beside the columns on wide screens (a saved choice) and floats over them on
 *  narrower ones, only while the reader has it open. */
export function railShown(): boolean {
  if (V.narrow) return false;
  return V.wide ? P.alerts : V.railOpen;
}

export function toggleRail(open = !railShown()) {
  if (V.wide) setPrefs({ alerts: open });
  else {
    V.railOpen = open;
    emit("narrow");
  }
}
