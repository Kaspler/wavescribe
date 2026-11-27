// Language tags are BCP-47; nothing here assumes a particular language.
import type { Station, Utterance } from "./types";

export const primary = (tag: string | null | undefined) =>
  (tag || "").replace("_", "-").split("-")[0].toLowerCase();
export const sameLang = (a: string | null | undefined, b: string | null | undefined) =>
  !!a && !!b && primary(a) === primary(b);
export const short = (tag: string | null | undefined) => primary(tag).toUpperCase();

// Scripts written right to left, by language (CLDR); Intl.Locale's textInfo isn't in every browser.
const RTL = new Set(["ar", "arc", "ckb", "dv", "fa", "he", "iw", "ks", "ku-arab", "nqo", "ps", "sd", "syr", "ug", "ur", "yi"]);
/** The base direction for text in this language: set it per block, so a block doesn't flip
 * when its first sentence changes language (a translation arriving). */
export const dirOf = (tag: string | null | undefined): "rtl" | "ltr" => (RTL.has(primary(tag)) ? "rtl" : "ltr");

const names = new Map<string, string>();
/** "Ukrainian · українська": the name in English and in the language itself, when they differ. */
export function langName(tag: string): string {
  if (tag === "auto") return "Language detected"; // the speech model tells each line's own
  const hit = names.get(tag);
  if (hit) return hit;
  let out = tag;
  try {
    const en = new Intl.DisplayNames(["en"], { type: "language" }).of(tag) ?? tag;
    const own = new Intl.DisplayNames([tag], { type: "language" }).of(tag) ?? tag;
    out = en === own ? en : `${en} · ${own}`;
  } catch {
    /* not a valid tag: show it as it is */
  }
  names.set(tag, out);
  return out;
}
export const langNameEn = (tag: string) => langName(tag).split(" · ")[0];

/** The language a station is shown in for this reader: theirs if the station speaks it or is
 * translated into it, else the station's first translation, else the original. */
export function shownLang(st: Station, readerLang: string | null): string {
  if (!readerLang || sameLang(readerLang, st.language)) return st.language;
  return st.translate_to.find((t) => sameLang(t, readerLang)) || st.translate_to[0] || st.language;
}

export type Shown = "" | "pending" | "untranslated";
/** [text, its language, state] for one utterance in the language shown for its station. A line
 * heard in that language already (one a station in language "auto" told apart) is as it is. */
export function pick(u: Utterance, st: Station, shown: string): [string, string, Shown] {
  const orig = u.language || st.language;
  if (sameLang(shown, st.language) || sameLang(shown, orig)) return [u.text, orig, ""];
  const tr = u.translations[shown];
  if (tr != null) return [tr, shown, ""];
  if (u.pending.includes(shown)) return [u.text, orig, "pending"];
  return [u.text, orig, "untranslated"];
}

/** The ASR checks flag text that is clearly in another language than the station's. */
export const otherLang = (u: Utterance) =>
  (u.flags || []).find((f) => f.startsWith("lang:"))?.slice(5) || null;

// -- choosing a language ------------------------------------------------------------------------

/** The languages a speech model can usually write down (Whisper's list), with the regional forms
 * that read differently enough to matter. Model languages and the stations' own add to them. */
export const LANGS = ["af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs", "ca", "cs", "cy", "da",
  "de", "el", "en", "en-GB", "en-US", "es", "es-ES", "es-419", "et", "eu", "fa", "fi", "fo", "fr", "fr-CA", "gl", "gu",
  "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja", "jv", "ka", "kk", "km", "kn", "ko", "la",
  "lb", "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my", "ne", "nl", "nn", "no", "oc",
  "pa", "pl", "ps", "pt", "pt-BR", "pt-PT", "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su",
  "sv", "sw", "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo", "yue", "zh"];

/** Lower case without accents, so "francais" finds "français". */
const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();

/** A tag as BCP-47 writes it ("pt_pt" → "pt-PT"), or null if it isn't one. */
export function canonicalTag(text: string): string | null {
  const t = text.trim().replace(/_/g, "-");
  if (!/^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$/.test(t)) return null;
  try {
    return Intl.getCanonicalLocales(t)[0] ?? null;
  } catch {
    return null;
  }
}

/** The tags in `tags` that `query` names, best first: the tag itself, then names in English or in
 * the language itself that start with it, then names with a word that starts with it. */
export function findLangs(query: string, tags: string[], max = 8): string[] {
  const q = fold(query);
  const all = [...new Set(tags)];
  if (!q) return all.slice(0, max);
  const tag = canonicalTag(query);
  const rank = (t: string): number => {
    if (tag && t.toLowerCase() === tag.toLowerCase()) return 0;
    const [en, own = en] = langName(t).split(" · ").map(fold);
    if (en === q || own === q) return 1;
    if (en.startsWith(q) || own.startsWith(q)) return 2;
    if (t.toLowerCase().startsWith(q)) return 3;
    if ([en, own].some((n) => n.split(/[\s(),-]+/).some((w) => w.startsWith(q)))) return 4;
    return 9;
  };
  return all.map((t, i) => [rank(t), i, t] as const).filter(([r]) => r < 9)
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]).slice(0, max).map(([, , t]) => t);
}

/** What typed text stands for: a tag Intl knows a name for, or the one language whose name it is
 * (in English or in the language itself). Null when it is neither. */
export function resolveLang(text: string, tags: string[]): string | null {
  const tag = canonicalTag(text);
  if (tag && langName(tag) !== tag) return tag;
  const q = fold(text);
  if (!q) return null;
  const hit = [...new Set(tags)].filter((t) => langName(t).split(" · ").some((n) => fold(n) === q));
  return hit.length ? hit.sort((a, b) => a.length - b.length)[0] : null;
}

/** The language most people speak in a country, as a tag with the country ("PT" → "pt-PT"). */
export function countryLang(country: string | null | undefined): string | null {
  if (!country || !/^[A-Za-z]{2}$/.test(country)) return null;
  try {
    const l = new Intl.Locale(`und-${country.toUpperCase()}`).maximize();
    return l.language && l.language !== "und" ? `${l.language}-${country.toUpperCase()}` : null;
  } catch {
    return null;
  }
}
