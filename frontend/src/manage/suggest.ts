// What "Suggest in each language" puts in the rule editor, from the server's answer (POST
// /api/manage/rules/suggest, rules/suggest.py): the words added to each language's list, what
// to say beside a language that got none, and the sentence for the toast. A plain function, so
// it can be tested without a page.

type Answer = Record<string, unknown>;

export interface Suggested {
  /** Each language's list as the editor has it, one word per line, with the new words added. */
  terms: Record<string, string>;
  /** For the toast: the server's sentence, or one made from the words when it sends none. */
  toast: string;
  /** Nothing came back at all. */
  bad: boolean;
  /** Per language, a line to show beside its list: why none came, or why there may be more. */
  notes: Record<string, string>;
  /** Said once near the lists: the server's note when it is a sentence for the person. */
  note: string | null;
}

const words = (v: unknown): string[] => (Array.isArray(v) ? v.filter((w): w is string => typeof w === "string" && !!w.trim()).map((w) => w.trim()) : []);
const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);
const sentence = (s: string) => s.replace(/^./, (c) => c.toUpperCase()).replace(/([^.!?])$/, "$1.");

/** `lists`: the editor's text per language; `asked`: the languages asked for; `answer`: the
 * server's answer, or the fields beside a refusal that says none came back (`code`
 * no_suggestions), whose sentence is in `error`. `name` names a language for the person. */
export function takeSuggestions(lists: Record<string, string>, asked: string[], answer: unknown, name: (lang: string) => string = (l) => l): Suggested {
  const a: Answer = answer && typeof answer === "object" ? (answer as Answer) : {};
  const terms = a.terms && typeof a.terms === "object" ? (a.terms as Answer) : {};
  const per = a.languages && typeof a.languages === "object" ? (a.languages as Answer) : {};
  const out = { ...lists };
  const notes: Record<string, string> = {};
  const got: string[] = [];
  let count = 0;
  for (const lang of asked) {
    const entry = per[lang] && typeof per[lang] === "object" ? (per[lang] as Answer) : {};
    const ws = words(terms[lang] ?? entry.words);
    if (ws.length) {
      const have = new Set((out[lang] || "").split("\n").filter(Boolean));
      ws.forEach((w) => have.add(w));
      out[lang] = [...have].join("\n");
      got.push(lang);
      count += ws.length;
    }
    const why = text(entry.message);
    if (why) notes[lang] = ws.length ? sentence(why) : `No suggestions in ${name(lang)}: ${why.replace(/[.]$/, "")}.`;
  }
  const missing = asked.filter((l) => !got.includes(l));
  const said = text(a.message) ?? text(a.error);
  const toast = said ? sentence(said)
    : !count ? "No suggestions came back."
    : `Suggestions added in ${got.map(name).join(", ")}: check them before saving.${missing.length ? ` None came back for ${missing.map(name).join(", ")}.` : ""}`;
  const note = text(a.note);
  return { terms: out, toast, bad: !count, notes, note: note && !/^[[{]/.test(note) ? note : null };
}
