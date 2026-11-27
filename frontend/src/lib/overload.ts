// A receiver overloaded by a transmitter close by (a walkie-talkie a few metres from the
// antenna), as the server reports it: on a watch's status, and on a receiver tuned by hand. It is
// a note about the reception, never an alert, and is shown in the server's words, which already
// say where a transmitter off the channels was.
import type { Overload } from "./types";

/** The overload in what the server sent, or null where there is none or it can't be read. */
export function overloadOf(x: unknown): Overload | null {
  if (!x || typeof x !== "object") return null;
  const o = x as Record<string, unknown>;
  if (typeof o.t !== "number" || !Number.isFinite(o.t) || typeof o.message !== "string" || !o.message) return null;
  const f = o.freq_hz;
  return {
    t: o.t,
    freq_hz: typeof f === "number" && Number.isFinite(f) && f > 0 ? f : null,
    on_channel: typeof o.on_channel === "boolean" ? o.on_channel : null,
    message: o.message,
  };
}

/** The channel to name beside an overload's time: only one it was caught on. Off the channels its
 * frequency is the transmitter's, which no channel was on, and the server's sentence says it. */
export function channelOf(o: Overload): number | null {
  return o.on_channel === true ? o.freq_hz : null;
}

/** A watch's warning as said beside its overload: nothing when it is the overload's own sentence
 * (deaf because of it), so it is said once. */
export function warningBeside(warning: string | null | undefined, overload: Overload | null): string | null {
  if (!warning) return null;
  return overload && warning === overload.message ? null : warning;
}
