// Where the rules that listen for topics have a word list, from the rules view itself, so that
// switching a rule on or off on the page changes what the page says at once. The same as
// /api/state's `topic_languages`, which the reading page's bell goes by. A plain function, so it
// can be tested without a page.

interface RuleLike {
  enabled?: boolean;
  topics?: string[] | null;
}
type Packs = Record<string, { topics?: Record<string, unknown> | null } | null> | null | undefined;

/** The languages (as the packs name them) with a word list for a topic of a rule that is on;
 * null when no rule that is on listens for topics. */
export function topicLanguages(rules: RuleLike[], packs: Packs): string[] | null {
  const topics = new Set(rules.filter((r) => r.enabled !== false).flatMap((r) => (Array.isArray(r.topics) ? r.topics : [])));
  if (!topics.size) return null;
  return Object.entries(packs || {})
    .filter(([, pack]) => Object.keys(pack?.topics || {}).some((t) => topics.has(t)))
    .map(([lang]) => lang)
    .sort();
}
