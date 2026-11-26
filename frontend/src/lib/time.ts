const clock = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", hour12: false });
const clockS = new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const dated = new Intl.DateTimeFormat([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const long = new Intl.DateTimeFormat([], { dateStyle: "medium", timeStyle: "short" });

/** A stored time the server couldn't give is null (the store had no finite one): shown as "?",
 * since formatting it would throw and take the view down with it. */
type When = number | null | undefined;
const known = (t: When): t is number => typeof t === "number" && Number.isFinite(t);

export const hm = (t: When) => (known(t) ? clock.format(t * 1000) : "?");
export const hms = (t: When) => (known(t) ? clockS.format(t * 1000) : "?");
export const day = (t: When) => (known(t) && t ? long.format(t * 1000) : "–");
/** For a <time> element's dateTime; nothing when the time isn't known. */
export const isoTime = (t: When) => (known(t) ? new Date(t * 1000).toISOString() : undefined);
export const ago = (s: number) =>
  s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;

/** A time today as 14:05, an older one with its date. */
export function whenLabel(t: When): string {
  if (!known(t)) return "?";
  const d = new Date(t * 1000);
  return d.toDateString() === new Date().toDateString() ? hm(t) : dated.format(d);
}
