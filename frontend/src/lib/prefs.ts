// What each reader chose, in this browser only.
import { emit } from "./store";

export type Size = "s" | "m" | "l" | "xl";
export type Theme = "" | "light" | "dark";
export interface Prefs {
  lang: string | null; // null until a station is translated and one of its languages is picked
  size: Size;
  src: boolean; // show the original under translations
  theme: Theme;
  hidden: string[]; // stations not shown on this device
  alerts: boolean; // the alerts rail is open (wide screens)
}

const KEY = "wavescribe.prefs";
export const P: Prefs = { lang: null, size: "m", src: false, theme: "", hidden: [], alerts: true };
try {
  Object.assign(P, JSON.parse(localStorage.getItem(KEY) || "null") || {});
} catch {
  /* private mode or blocked storage: defaults */
}

export function setPrefs(change: Partial<Prefs>) {
  Object.assign(P, change);
  try {
    localStorage.setItem(KEY, JSON.stringify(P));
  } catch {
    /* ignore */
  }
  applyLook();
  emit("prefs");
}

export function applyLook() {
  const root = document.documentElement;
  root.dataset.size = P.size;
  if (P.theme) root.dataset.theme = P.theme;
  else delete root.dataset.theme;
}

export const visible = (id: string) => !P.hidden.includes(id);
