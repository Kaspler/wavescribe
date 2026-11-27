// How one utterance's words are drawn: keyword hits marked, the ASR's <unk> as an inaudible mark.
import type { ReactNode } from "react";
import { SEVERITIES, type Severity, type Utterance } from "../lib/types";
import { T } from "./strings";

export const topSeverity = (sevs: Severity[]): Severity =>
  sevs.reduce<Severity>((a, s) => (SEVERITIES.indexOf(s) > SEVERITIES.indexOf(a) ? s : a), "low");

/** Plain text with every <unk> drawn as a small inaudible mark. */
export function withInaudible(text: string, key = ""): ReactNode[] {
  const parts = text.split(/<unk>/gi);
  const out: ReactNode[] = [];
  parts.forEach((p, i) => {
    if (i) out.push(<span key={`${key}u${i}`} className="unk" title={T.inaudible} aria-label={T.inaudible}>…</span>);
    if (p) out.push(p);
  });
  return out;
}

/** `text` with each [start, end) span marked at its severity; spans count code points. */
export function withMarks(text: string, marks: (readonly [number, number, Severity])[]): ReactNode[] {
  const chars = Array.from(text); // the server counts code points, not UTF-16 units
  const out: ReactNode[] = [];
  let pos = 0;
  [...marks].sort((x, y) => x[0] - y[0]).forEach(([a, b, sev], i) => {
    if (a < pos) return;
    out.push(...withInaudible(chars.slice(pos, a).join(""), `p${i}`));
    out.push(<mark key={`m${i}`} className={`kw kw-${sev}`}>{chars.slice(a, b).join("")}</mark>);
    pos = b;
  });
  out.push(...withInaudible(chars.slice(pos).join(""), "end"));
  return out;
}

/** The words of `text`, with the rule hits marked when `text` is the original the rules read. */
export function utteranceWords(u: Utterance, text: string): ReactNode[] {
  const hits = u.hits || [];
  if (!hits.length || text !== u.text) return withInaudible(text);
  return withMarks(text, hits.flatMap((h) => h.spans.map(([a, b]) => [a, b, h.severity] as const)));
}

// The letters a match may stop in the middle of.
const WORD_END = /[\p{L}\p{N}\p{M}'’]+$/u;
const WORD_START = /^[\p{L}\p{N}\p{M}'’]+/u;

/** A search snippet: the server wraps matches in \x02 … \x03. A match is widened to the whole
 * word it falls in: "road" found in "roads" marks "roads". */
export function snippet(text: string): ReactNode[] {
  const parts = text.split("\x02");
  const out: ReactNode[] = [];
  let before = parts[0];
  parts.slice(1).forEach((part, i) => {
    const [hit, rest = ""] = part.split("\x03");
    const head = WORD_END.exec(before)?.[0] || "";
    const tail = hit ? WORD_START.exec(rest)?.[0] || "" : "";
    out.push(...withInaudible(before.slice(0, before.length - head.length), `s${i}`));
    out.push(<mark key={`h${i}`} className="found">{head + hit + tail}</mark>);
    before = rest.slice(tail.length);
  });
  out.push(...withInaudible(before, "end"));
  return out;
}

/** `text` with every whole word that holds one of the searched words marked, for text the
 * search didn't match itself (the reader's translation of a hit). */
export function markWords(text: string, q: string): ReactNode[] {
  const fold = (s: string) => s.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
  const terms = q.split(/\s+/).map((w) => fold(w.replace(/["*()^:]/g, ""))).filter((w) => w.length > 1 && !/^(and|or|not|near)$/i.test(w));
  if (!terms.length) return withInaudible(text);
  return text.split(/([\p{L}\p{N}\p{M}'’]+)/u).map((w, i) =>
    i % 2 && terms.some((t) => fold(w).includes(t)) ? <mark key={i} className="found">{w}</mark> : <span key={i}>{withInaudible(w, `w${i}`)}</span>);
}
