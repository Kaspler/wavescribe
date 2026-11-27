// The shapes /api/state and /api/events send (src/wavescribe/web/server.py, events.py).
import type { Drops, Lag, Load } from "./behind";

export type Severity = "low" | "medium" | "high" | "critical";
export const SEVERITIES: Severity[] = ["low", "medium", "high", "critical"];

export interface Station {
  id: string;
  name: string;
  language: string;
  translate_to: string[];
  priority: number;
  available: boolean;
  reason: string | null;
  running: boolean;
  asr_model: string | null;
  transcribe: boolean; // false: listened to, not written down
  /** Started only to play, for this run: not written down until started again without it. Absent
   * from servers that can't start a station that way. */
  listen_only_run?: boolean;
  kind: string;
  disabled: boolean;
  fm_mhz?: number[];
  /** Radio stations: the kind of radio it counts as (broadcast, amateur, utility, private…). */
  privacy?: string;
  /** Set after a change of region would make the station's kind of radio less strict: it keeps
   * `privacy` until the operator confirms, and this is what the region says. */
  privacy_region?: string;
  /** A watch: every channel of a band (its id) or of a range in MHz, each line with its own. */
  watch?: string | [number, number] | null;
}

export interface Hit {
  rule: string;
  severity: Severity;
  spans: [number, number][];
  /** The alert this hit raised or joined, when it raised one. */
  alert?: number;
}

export interface Utterance {
  id: number | null;
  key: string;
  station: string;
  /** Null when the store holds no finite time for it. */
  t_start: number | null;
  duration: number;
  language: string | null;
  text: string;
  translations: Record<string, string>;
  pending: string[];
  final: boolean;
  audio: boolean;
  flags?: string[];
  hits?: Hit[];
  /** A watch's line: the channel it was caught on, and the band plan's name for it. */
  freq_hz?: number | null;
  label?: string | null;
}

export interface Alert {
  id: number;
  rule: string;
  severity: Severity;
  first_at: number;
  last_at: number;
  stations: string[];
  language: string;
  quote: string;
  terms: string[];
  mentions: number;
  segment_id: number | null;
  status: string;
  actions?: Record<string, string | number> & { covered_by?: number };
  kind: string;
  reason: string | null;
  summaries: Record<string, string>;
}

/** One running station as the pipeline reports it. */
export interface Live {
  connected: boolean;
  reconnects: number;
  in_speech: boolean;
  music: boolean;
  quiet_s: number;
  level: number;
  stopping: boolean;
  signal_db?: number | null;
  clipped?: number;
  remote?: boolean;
  /** FM: what the station sends about itself (RDS). */
  rds?: { pi: string | null; ps: string | null; rt: string | null; pty_name: string | null; ta: boolean } | null;
  /** Walkie-talkie, aircraft and amateur channels: whether anyone is transmitting. */
  squelch_open?: boolean | null;
  /** Why its capture failed, while it is down, and how soon it is tried again. */
  error?: string;
  retry_in_s?: number;
  /** How far its text and translation are behind the radio, and what was skipped in the last
   * 10 minutes to keep up (lib/behind.ts). */
  behind_s?: Partial<Lag> | null;
  dropped?: Partial<Drops> | null;
}

export interface Status {
  ready: boolean;
  asr_queue?: number;
  mt_queue?: number;
  asr_busy?: number;
  mt_busy?: number;
  dropped?: Record<string, number>;
  /** Whether this machine keeps up with every running station. */
  load?: Load | null;
  /** Changes whenever a station is added, changed or removed. */
  stations_rev?: number | string;
  stations: Record<string, Live>;
}

export interface PageState {
  stations: Station[];
  can_control: boolean;
  store: boolean;
  mt_model: string | null;
  ask: string | null;
  noncommercial: string[];
  audio_rate: number;
  recent: Utterance[];
  alerts: Alert[];
  status: Status;
  node: { name: string; url: string };
  manage: boolean;
}

export interface SearchHit {
  id: number;
  station: string;
  t_start: number | null;
  language: string;
  field: string;
  field_lang: string;
  snippet: string;
  text: string;
  translations: Record<string, string>;
  audio: boolean;
}
