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

/** A search snippet: the server wraps matches in \x02 … \x03. */
export function snippet(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  text.split("\x02").forEach((part, i) => {
    if (i === 0) {
      out.push(...withInaudible(part, `s${i}`));
      return;
    }
    const [hit, rest = ""] = part.split("\x03");
    out.push(<mark key={`h${i}`} className="found">{hit}</mark>, ...withInaudible(rest, `r${i}`));
  });
  return out;
}
