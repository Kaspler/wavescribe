// What an editor sends back: only what the person changed, and nothing it doesn't understand
// lost on the way. Plain functions, so they can be tested without a page.

type Fields = Record<string, unknown>;

/** Whether two values from the server, or from a form built in the same shape, say the same. */
export function same(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => same(x, b[i]));
  if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
    const ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every((k) => same((a as Fields)[k], (b as Fields)[k]));
  }
  return false;
}

/** The fields of `draft` that differ from `saved`, as the draft has them. A station's change is
 * saved as an override of each field it names, so a field sent unchanged would read as "changed
 * here", and one the editor only carries (`acknowledge`, which may be true, a class or a list
 * of classes) could be rewritten in another form. */
export function changedFields(saved: Fields, draft: Fields): Fields {
  return Object.fromEntries(Object.entries(draft).filter(([k, v]) => !same(saved[k], v)));
}

/** What the server adds to a rule for the page; never saved back. */
const VIEW_ONLY = ["origin", "enabled"];

/** Empty, or the default the server assumes when the field is left out. */
const unset = (k: string, v: unknown) =>
  v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length)
  || (typeof v === "object" && v !== null && !Array.isArray(v) && !Object.keys(v).length)
  || (k === "verify" && v === false) || (k === "min_mentions" && v === 1) || (k === "kind" && v === "keyword");

/** The rule to save: the one being edited with the editor's fields over it. Fields the editor
 * doesn't show (a rule's `regex` from an older page, a note in alerts.toml, anything a later
 * version adds) pass through as they are; a field emptied in the editor is left out, so the
 * server's default applies. */
export function ruleBody(saved: Fields | null, fields: Fields): Fields {
  const out: Fields = { ...(saved || {}) };
  for (const k of VIEW_ONLY) delete out[k];
  for (const [k, v] of Object.entries(fields)) out[k] = v;
  for (const [k, v] of Object.entries(out)) if (unset(k, v)) delete out[k];
  return out;
}

/** Regular expressions typed one per line. */
export const lines = (text: string) => text.split("\n").map((x) => x.trim()).filter(Boolean);
