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
