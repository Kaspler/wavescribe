// Summaries on a schedule (GET/POST/DELETE /api/manage/digest/schedule): how often in words, the
// table the page sends, and which field a refusal is about. Plain functions over the server's
// JSON, so they can be tested without a page; SummarySchedule.tsx draws them.

/** The schedule as the server keeps it ([digest] in alerts.toml, or the page's in its place). */
export interface Table {
  every_minutes: number;
  stations: string[];
  language: string;
  speak: boolean;
  quiet_hours: [string, string] | null;
}

export interface View {
  enabled: boolean;
  source: "page" | "file" | null;
  file: string | null;
  schedule: Table | null;
  running: boolean;
  next_at: number | null;
  alerts: boolean;
  digest: string | null;
  digest_code: string | null;
  error: string | null;
  error_code: string | null;
  /** The language a schedule without one is written in. */
  language: string;
  /** The languages a voice reads aloud. */
  voices: string[];
  every_minutes_choices: number[];
}

/** The schedule a switch turned on without more starts with: the server's own default. */
export const EVERY_DEFAULT = 60;

/** "every hour", "every 15 minutes", "every 2 hours", "once a day". */
export function everyWords(minutes: number): string {
  if (minutes === 24 * 60) return "once a day";
  if (minutes === 60) return "every hour";
  if (minutes % 60 === 0) return `every ${minutes / 60} hours`;
  return `every ${minutes} minutes`;
}

/** What the page's form holds: `language` "" for the server's default (the notifications'
 * language), `stations` empty for every station, `quiet` null for none. */
export interface Form {
  every: number;
  stations: string[];
  language: string;
  speak: boolean;
  quiet: [string, string] | null;
}

/** The form for a schedule, or the defaults when there is none yet. A language the same as the
 * default is left as the default, so that it follows the notifications' language. */
export function formOf(v: Pick<View, "schedule" | "language">): Form {
  const t = v.schedule;
  if (!t) return { every: EVERY_DEFAULT, stations: [], language: "", speak: false, quiet: null };
  return {
    every: t.every_minutes,
    stations: [...t.stations],
    language: t.language && t.language !== v.language ? t.language : "",
    speak: t.speak,
    quiet: t.quiet_hours ? [t.quiet_hours[0], t.quiet_hours[1]] : null,
  };
}

/** The whole table to send (the server replaces the one it has): anything left at its default
 * is sent as null, for the server to fill in. */
export function bodyOf(f: Form, enabled: boolean): Record<string, unknown> {
  return {
    enabled,
    every_minutes: f.every,
    stations: f.stations.length ? f.stations : null,
    language: f.language || null,
    speak: f.speak,
    quiet_hours: f.quiet,
  };
}

/** Which of the form's fields a refusal is about, by its `field` and `code`, or "form" for the
 * schedule as a whole (unknown keys, alerts off, anything new). */
export type Where = "every" | "stations" | "language" | "speak" | "quiet" | "form";
const FIELDS: Record<string, Where> = {
  every_minutes: "every", stations: "stations", language: "language", speak: "speak", quiet_hours: "quiet",
};
const CODES: Record<string, Where> = {
  bad_every: "every", bad_stations: "stations", unknown_station: "stations", bad_language: "language",
  bad_speak: "speak", no_voice: "speak", bad_quiet_hours: "quiet",
};
export function whereOf(data: unknown): Where {
  const d = (data && typeof data === "object" ? data : {}) as { field?: unknown; code?: unknown };
  if (typeof d.field === "string" && FIELDS[d.field]) return FIELDS[d.field];
  if (typeof d.code === "string" && CODES[d.code]) return CODES[d.code];
  return "form";
}

/** Whether a voice reads `language` aloud (its primary language will do: "en" reads "en-GB"). */
export function hasVoice(voices: string[], language: string): boolean {
  const primary = (t: string) => t.split("-")[0].toLowerCase();
  return voices.some((v) => v === language || primary(v) === primary(language));
}
