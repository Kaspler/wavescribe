// What a webhook's status and delivery log say about its alerts, for Settings, Connections and
// Health: the alerts that haven't got there yet, those given up, and how long one is still sent
// (outputs.py). Plain functions, so they can be tested without a page.

type Fields = Record<string, unknown>;

const count = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.round(v) : 0);
const alerts = (n: number) => `${n} ${n === 1 ? "alert" : "alerts"}`;
/** "40 s", "3 min", "5 h": how long ago, as the page says it elsewhere (lib/time's ago). */
const age = (s: number) => (s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`);

/** "2 alerts waiting, oldest 3 min · 1 alert given up as too old · 1 alert dropped from a full
 * queue": each part only when its count isn't 0 (the last two since the output started or was
 * changed); null when there is nothing to say. */
export function alertsLine(status: unknown): string | null {
  const st = status && typeof status === "object" ? (status as Fields) : {};
  const waiting = count(st.alerts_waiting), expired = count(st.alerts_expired), dropped = count(st.alerts_dropped);
  const oldest = typeof st.alerts_oldest_age_s === "number" && Number.isFinite(st.alerts_oldest_age_s) ? Math.max(0, st.alerts_oldest_age_s) : null;
  const parts = [
    waiting ? `${alerts(waiting)} waiting${oldest != null ? `, oldest ${age(oldest)}` : ""}` : "",
    expired ? `${alerts(expired)} given up as too old` : "",
    dropped ? `${alerts(dropped)} dropped from a full queue` : "",
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** "24 h", "30 min", "7 days": an output's alert_max_age_hours; null when it isn't a number. */
export function maxAge(hours: unknown): string | null {
  if (typeof hours !== "number" || !Number.isFinite(hours) || hours <= 0) return null;
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  if (hours >= 48 && hours % 24 === 0) return `${hours / 24} days`;
  return `${+hours.toFixed(1)} h`;
}

/** A delivery's key in the log: its row id in the store, else (in memory, under --no-store) the
 * output, time, event, what it was about and its delivery id, which together don't repeat. */
export function deliveryKey(d: Fields): string {
  if (d.id != null) return `row:${d.id}`;
  return ["mem", d.output_id ?? d.output, d.t, d.event, d.ref ?? "", d.delivery ?? ""].join("|");
}
