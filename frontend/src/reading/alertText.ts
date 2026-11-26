// Which part of an alert's quote a folded alert shows: the words that matched, not only the
// opening of a long quote. Plain functions, so they can be tested without a page.

/** [start, end) in code points, as the server counts them. */
export type Span = [number, number];

/** A rule hit on the quoted utterance, as the server sends it with the utterance. */
interface HitLike {
  rule: string;
  alert?: number;
  spans: Span[];
}

/** A quote longer than this many code points before its first match opens at the match. */
const OPENING = 90;
/** How much comes before the match when it does: enough to read it in context. */
const LEAD = 40;

const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase();
const wordChar = /[\p{L}\p{N}]/u;
const space = /\s/u;

/** The spans of the utterance's hits that belong to this alert (or to the rules it names). */
export function alertSpans(hits: HitLike[], alertId: number, rules: string[]): Span[] {
  return hits
    .filter((h) => h.alert === alertId || rules.includes(h.rule))
    .flatMap((h) => h.spans)
    .sort((x, y) => x[0] - y[0]);
}

/** Where `terms` begin a word in `text`, ignoring case and accents, for when the utterance and
 * its hit spans aren't at hand. The rules match word stems, so this finds the usual forms (a term
 * and its plural) but can miss an inflected one; a trailing * in a term is dropped. */
export function findTerms(text: string, terms: string[]): Span[] {
  const chars = Array.from(text);
  const folded = chars.map(fold); // one entry per code point keeps the server's indices
  const out: Span[] = [];
  for (const term of terms) {
    const want = Array.from(fold(term.replace(/\*+$/, "").trim()));
    if (!want.length) continue;
    const joined = want.join("");
    for (let i = 0; i + want.length <= chars.length; i++) {
      if (i > 0 && wordChar.test(chars[i - 1])) continue;
      if (folded.slice(i, i + want.length).join("") !== joined) continue;
      let end = i + want.length;
      while (end < chars.length && wordChar.test(chars[end])) end++; // the whole word: "storm" marks "storms"
      out.push([i, end]);
      i = end;
    }
  }
  return out.sort((x, y) => x[0] - y[0]).filter((s, i, all) => !i || s[0] >= all[i - 1][1]);
}

/** Where a folded alert's text starts, in code points: at the beginning, unless the first match
 * comes late, then at a word a little before it. */
export function excerptStart(text: string, spans: Span[]): number {
  if (!spans.length) return 0;
  const first = Math.min(...spans.map(([a]) => a));
  if (first <= OPENING) return 0;
  const chars = Array.from(text);
  let i = first - LEAD;
  while (i < first && !space.test(chars[i - 1])) i++;
  return i;
}
