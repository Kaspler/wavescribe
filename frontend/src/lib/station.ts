// What a station is doing, in words and as a lamp.
import { S } from "./live";
import { ago } from "./time";
import type { Live, Station } from "./types";

const QUIET_S = 60; // "no speech" after this long without any

/** off: not running · wait: connecting · trouble: down or retrying · dim: on air, nothing to
 * transcribe · on: listening · speech: someone is talking now */
export type Lamp = "off" | "wait" | "trouble" | "dim" | "on" | "speech";

export interface StationState {
  label: string;
  lamp: Lamp;
  detail: string; // a longer explanation for a tooltip
}

/** A station that should be written down but has no speech model downloaded yet: it plays,
 * without text, until one arrives (the server plans no model for it meanwhile). */
export const noText = (st: Station | undefined): boolean =>
  !!st && st.transcribe && !st.asr_model && st.kind !== "remote" && st.available && !st.disabled;

/** "next try in 12 s", from the moment the status was sent. */
export const nextTry = (s: Pick<Live, "retry_in_s">) =>
  s.retry_in_s != null && s.retry_in_s >= 1 ? `next try in ${Math.ceil(s.retry_in_s)} s` : "trying again now";

export function stateOf(id: string): StationState {
  const s = S.live[id];
  if (!s) return { label: "stopped", lamp: "off", detail: "Not running" };
  const st = S.byId[id];
  if (s.stopping) return { label: "stopping", lamp: "wait", detail: "" };
  if (!s.connected) {
    const what = s.signal_db != null ? "receiver" : "stream"; // SDRs report a signal level
    // The source's own words for why are a tooltip here; the column says it in plain words and
    // keeps them under "Details" (reading/colNote.ts).
    if (s.error) return { label: `${what} down`, lamp: "trouble", detail: `${s.error} · ${nextTry(s)}` };
    if (s.reconnects >= 3)
      return { label: `${what} down`, lamp: "trouble", detail: `Retrying (${s.reconnects} attempts so far)` };
    return s.reconnects
      ? { label: "reconnecting", lamp: "trouble", detail: `Attempt ${s.reconnects}` }
      : { label: "connecting", lamp: "wait", detail: "" };
  }
  if (st?.listen_only_run) return { label: "playing only", lamp: "on", detail: "Start to write it down" };
  if (noText(st)) return { label: "no text yet", lamp: "on", detail: "Plays now; written down once its speech model is downloaded" };
  if (st?.transcribe === false) return { label: "listen only", lamp: "on", detail: "Played, not written down" };
  if (s.music) return { label: "music", lamp: "dim", detail: "Music is skipped, not written down" };
  if (s.in_speech) return { label: "speech", lamp: "speech", detail: "Someone is talking" };
  if (s.squelch_open === false)
    return { label: "channel quiet", lamp: "dim", detail: "Nobody is transmitting on this channel" };
  if (s.quiet_s >= QUIET_S)
    return { label: `quiet ${ago(s.quiet_s)}`, lamp: "dim", detail: "No speech: silence, or music without a voice" };
  return { label: "listening", lamp: "on", detail: "" };
}

/** Audio level 0–1 on a −60…0 dB scale. */
export function levelOf(id: string): number {
  const s = S.live[id];
  const db = s && s.level > 0 ? 20 * Math.log10(s.level) : -90;
  return Math.max(0, Math.min(1, (db + 60) / 60));
}

/** An SDR's 8-bit converter clips when the gain is too high. */
export const overloaded = (id: string) => (S.live[id]?.clipped ?? 0) > 0.001;
