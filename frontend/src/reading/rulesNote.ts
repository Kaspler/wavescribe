// When the rules that listen for topics can't match on any station being written down: a topic
// is a word list per language (config/keywords), and none of those stations is in a language
// that has one. The alerts list says so where it is empty, since "no alerts yet" would read as
// "nothing happened". From /api/state's `topic_languages`. Plain functions, so they can be
// tested without a page.

const primary = (tag: string) => tag.replace("_", "-").split("-")[0].toLowerCase();

/** A station being written down now: its language, or for one in language "auto" the
 * languages it expects ([] or null: any the speech model tells). */
export interface Heard {
  language: string;
  languages?: string[] | null;
}

/** The stations' languages, once each, when none of them has a word list; null when one has,
 * when no rule listens for topics (`topicLanguages` null), when no station is being written
 * down, or when a station may speak any language, which nothing can be said about. */
export function withoutWordLists(topicLanguages: string[] | null | undefined, stations: Heard[]): string[] | null {
  if (!Array.isArray(topicLanguages) || !stations.length) return null;
  const lists = new Set(topicLanguages.map(primary));
  const out: string[] = [];
  for (const st of stations) {
    const langs = st.language === "auto" ? st.languages || [] : [st.language];
    if (!langs.length) return null;
    for (const lang of langs) {
      if (lists.has(primary(lang))) return null;
      if (!out.some((x) => primary(x) === primary(lang))) out.push(lang);
    }
  }
  return out;
}
