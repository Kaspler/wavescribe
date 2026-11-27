// What a refusal from the server asks of the page: its sentence for the person, and its code with
// the fields that go with it for the page (src/wavescribe/refusal.py), so the page never judges
// from the words. A plain function, so it can be tested without a page.

export interface Refused {
  /** The server's sentence, shown as it is. */
  text: string;
  /** What it is, for the page: "retune", "too_wide", "acknowledge", "busy"…; null without one. */
  code: string | null;
  /** retune, busy: the running stations the change would stop, by name. */
  stops: string[];
  /** too_wide: a slice of the band one receiver hears whole, in MHz, as the server offers it. */
  slice: [number, number] | null;
  /** acknowledge: the kind of radio to confirm (private, utility…). */
  privacy: string | null;
}

const finite = (x: unknown): x is number => typeof x === "number" && Number.isFinite(x);

/** `message` and `data`: the server's 400 answer (`error`, and `code` with its fields). A field
 * of the wrong shape counts as absent. */
export function refused(message: string, data?: Record<string, unknown> | null): Refused {
  const code = typeof data?.code === "string" ? data.code : null;
  const holders = Array.isArray(data?.holders) ? data.holders.filter((h): h is string => typeof h === "string") : [];
  const s = data?.slice_mhz;
  const slice = Array.isArray(s) && s.length === 2 && finite(s[0]) && finite(s[1]) && s[0] < s[1] ? [s[0], s[1]] as [number, number] : null;
  return {
    text: message,
    code,
    stops: code === "retune" || code === "busy" ? holders : [],
    slice: code === "too_wide" ? slice : null,
    privacy: code === "acknowledge" && typeof data?.privacy === "string" ? data.privacy : null,
  };
}
