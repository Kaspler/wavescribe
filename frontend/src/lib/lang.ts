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
/** [text, its language, state] for one utterance in the language shown for its station. */
export function pick(u: Utterance, st: Station, shown: string): [string, string, Shown] {
  const orig = u.language || st.language;
  if (sameLang(shown, st.language)) return [u.text, orig, ""];
  const tr = u.translations[shown];
  if (tr != null) return [tr, shown, ""];
  if (u.pending.includes(shown)) return [u.text, orig, "pending"];
  return [u.text, orig, "untranslated"];
}

/** The ASR checks flag text that is clearly in another language than the station's. */
export const otherLang = (u: Utterance) =>
  (u.flags || []).find((f) => f.startsWith("lang:"))?.slice(5) || null;
