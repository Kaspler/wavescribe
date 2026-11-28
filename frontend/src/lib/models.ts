// What this Wavescribe still has to download before its stations can be written down, from
// /api/manage/models (src/wavescribe/admin.py, models_view).

export interface ModelEntry {
  id: string;
  capability: string;
  installed: boolean;
  size_gb: number | null;
  tier: string;
  accepted: boolean;
  in_use: string[];
  /** Not installed: the bytes still to download, the free space on its disk, and which disk. */
  left_bytes?: number | null;
  free_bytes?: number | null;
  disk?: string | null;
}

export interface ModelsView {
  models: ModelEntry[];
  chains: Record<string, string[]>;
  recommended: Record<string, { id: string; why: string }[]>;
  downloads?: Record<string, { model: string; state: string }>;
}

/** What a model is for, in the words the page uses. */
export const NEED_LABEL: Record<string, string> = {
  vad: "Speech detection",
  gate: "Music filter",
  translation: "Translation",
};

export interface Need {
  /** "vad", "gate", "translation", or the language tags that share this speech model */
  what: string[];
  entry: ModelEntry;
}

/** The first choice for each job that has nothing installed yet: the speech detector every
 * written-down station needs, the music filter, a speech model for each station language and
 * a translator. The server recommends models per language and for translation; the detector
 * and the filter are the first in their order, as the server would pick them. */
export function neededModels(m: ModelsView): Need[] {
  const byId = new Map(m.models.map((e) => [e.id, e]));
  const has = (cap: string) => m.models.some((e) => e.capability === cap && e.installed);
  const out = new Map<string, Need>();
  const want = (what: string, id: string | undefined) => {
    const entry = id ? byId.get(id) : undefined;
    if (!entry || entry.installed) return;
    const need = out.get(entry.id);
    if (need) need.what.push(what);
    else out.set(entry.id, { what: [what], entry });
  };
  if (!has("vad")) want("vad", m.chains.vad?.[0]);
  if (!has("gate")) want("gate", m.chains.gate?.[0]);
  for (const [lang, list] of Object.entries(m.recommended)) {
    if (lang === "translation" || list.some((r) => byId.get(r.id)?.installed)) continue;
    want(lang, list[0]?.id);
  }
  const tr = m.recommended.translation || [];
  if (!tr.some((r) => byId.get(r.id)?.installed)) want("translation", tr[0]?.id);
  return [...out.values()];
}

/** A speech model some stations wait for: the languages of theirs it is for, and its size. */
export interface Awaited {
  id: string;
  languages: string[];
  gb: number | null;
}

/** The speech models the stations in `languages` wait for (each station's own language tag), in
 * the order the languages come; a language no missing model is for is left out. */
export function awaitedModels(languages: string[], needs: Need[]): Awaited[] {
  const out = new Map<string, Awaited>();
  for (const lang of languages) {
    const need = needs.find((n) => n.what.includes(lang));
    if (!need) continue;
    const have = out.get(need.entry.id);
    if (!have) out.set(need.entry.id, { id: need.entry.id, languages: [lang], gb: need.entry.size_gb });
    else if (!have.languages.includes(lang)) have.languages.push(lang);
  }
  return [...out.values()];
}

/** Whether a model can be downloaded with one click, or needs its licence accepted first. */
export const needsLicence = (e: ModelEntry) => (e.tier === "opt-in" || e.tier === "restricted") && !e.accepted;

/** The download size of a list of models in GB; models whose size isn't known count as nothing
 * (the detector and the filter are a few megabytes). */
export const totalGb = (needs: Need[]) => needs.reduce((sum, n) => sum + (n.entry.size_gb || 0), 0);

/** A download the page started, as /api/manage/models gives it (src/wavescribe/downloads.py). */
export interface DownloadJob {
  model: string;
  state: string; // running, stopping, done, failed, stopped
  error?: string | null;
  note?: string | null;
  bytes?: number | null;
  total_bytes?: number | null;
  percent?: number | null;
  speed?: number | null; // bytes a second, once measured
  eta_s?: number | null;
}

/** A download's speed: 12 MB/s, 2.4 MB/s, 800 kB/s. */
export function speedText(bps: number): string {
  if (bps >= 10e6) return `${Math.round(bps / 1e6)} MB/s`;
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} MB/s`;
  return `${Math.max(1, Math.round(bps / 1e3))} kB/s`;
}

/** The time a download has left at its measured speed, rounded as a person would say it. */
export function timeLeft(s: number): string {
  if (s < 60) return `about ${Math.max(5, Math.round(s / 5) * 5)} s left`;
  const min = Math.round(s / 60);
  if (min < 60) return `about ${min} min left`;
  const h = Math.floor(min / 60), rest = min % 60;
  return `about ${h} h${rest ? ` ${rest} min` : ""} left`;
}

/** A running download in two parts: how much has arrived, and its speed with the time left
 * (only once the speed is measured: nothing is guessed). */
export function progressText(j: DownloadJob): [string, string] {
  const done = j.bytes || 0;
  const have = done < 0.5e6 ? "0 MB" : sizeText(done);
  const amount = j.total_bytes ? `${have} of ${sizeText(j.total_bytes)}` : `${have} so far`;
  if (j.speed == null) return [amount, "measuring the speed…"];
  if (j.speed === 0) return [amount, "nothing arriving"];
  return [amount, speedText(j.speed) + (j.eta_s != null ? ` · ${timeLeft(j.eta_s)}` : "")];
}

/** Whether what is left to download fits the free space on its disk, and the sentence that
 * says so: the free space when it fits, the shortfall when it doesn't. Sizes unknown: fits. */
export function roomText(left: number | null | undefined, free: number | null | undefined): [boolean, string | null] {
  if (free == null) return [true, null];
  if (left != null && left > free) return [false, `Not enough room: ${sizeText(left)} to download and ${sizeText(free)} free on this disk. Make room first.`];
  return [true, `${sizeText(free)} free on this disk`];
}

/** The same for several models at once (Download what's missing), added up per disk; the first
 * disk without room, else the free space when they share one disk. */
export function roomForAll(entries: { left_bytes?: number | null; free_bytes?: number | null; disk?: string | null }[]): [boolean, string | null] {
  const disks = new Map<string, { left: number; free: number }>();
  for (const e of entries) {
    if (e.disk == null || e.free_bytes == null) continue;
    const d = disks.get(e.disk) || { left: 0, free: e.free_bytes };
    d.left += e.left_bytes || 0;
    disks.set(e.disk, d);
  }
  for (const d of disks.values()) if (d.left > d.free) return roomText(d.left, d.free);
  return disks.size === 1 ? roomText(0, [...disks.values()][0].free) : [true, null];
}

/** A sentence the server sends in lower case without its full stop, as the page shows it. */
export const sentence = (s: string) => s[0].toUpperCase() + s.slice(1) + (/[.?]$/.test(s) ? "" : ".");

/** Bytes as the command line says them (modelfetch.size_text): 7.9 GB, 340 MB, under 1 MB.
 * Decimal, as models.toml's sizes and the Finder are. */
export function sizeText(n: number): string {
  if (n >= 0.95e9) return `${(n / 1e9).toFixed(1)} GB`;
  return n >= 0.5e6 ? `${Math.max(1, Math.round(n / 1e6))} MB` : "under 1 MB";
}

/** What becomes of what uses a model once it is removed, by its job; null for a job with no
 * such sentence. Said only when something plans to use it. */
export function afterRemoval(capability: string): string | null {
  switch (capability) {
    case "asr": return "Stations that use it switch to the next installed model in the order, or play without text until one is downloaded.";
    case "vad": return "Unless another speech detector is installed, stations play without text until one is downloaded.";
    case "translate": return "Unless another translator is installed, nothing is translated until one is downloaded.";
    case "gate": return "Unless another music filter is installed, songs are written down too.";
    default: return null;
  }
}

/** A download's state in a word, and the sentence that goes under it: why it failed, or what
 * the next download does with what a stopped one kept. */
export function downloadState(j: DownloadJob): [string, string | null] {
  switch (j.state) {
    case "running": return ["downloading", null];
    case "stopping": return ["stopping…", null];
    case "stopped": return ["stopped", j.note ? sentence(j.note) : null];
    case "failed": return ["failed", j.error ? sentence(j.error) : null];
    default: return [j.state, null];
  }
}
