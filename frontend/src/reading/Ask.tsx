// Questions answered from what the stations said, with the excerpts the answer cites.
import { Fragment, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { playReplay } from "../lib/audio";
import { sameLang, shownLang } from "../lib/lang";
import { S } from "../lib/live";
import { P } from "../lib/prefs";
import { go } from "../lib/router";
import { emit, useTopics } from "../lib/store";
import { hm, whenLabel } from "../lib/time";
import type { Station } from "../lib/types";
import { Icon } from "../components/Icon";
import { Button, cx } from "../components/ui";

interface Source {
  n: number;
  id: number;
  station: string;
  name: string;
  t_start: number | null;
  language: string;
  text: string;
  translations: Record<string, string>;
  audio: boolean;
}
interface Plan {
  stations: string[];
  since: number;
  until: number;
  terms: string[];
  sampled?: boolean;
  overview?: boolean;
  widened?: boolean;
}
interface Turn {
  q: string;
  plan: Plan | null;
  sources: Source[];
  text: string;
  done: boolean;
  error: string | null;
  meta: { model: string; total_ms: number } | null;
}

// This tab's conversation survives a reload, not a new tab.
const KEY = "wavescribe.ask";
const CHAT: Turn[] = (() => {
  try {
    return (JSON.parse(sessionStorage.getItem(KEY) || "[]") as Turn[]).filter((t) => t.done);
  } catch {
    return [];
  }
})();
const save = () => {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(CHAT.filter((t) => t.done)));
  } catch {
    /* ignore */
  }
};

export function sendQuestion(q: string) {
  const history = CHAT.filter((t) => t.done && !t.error && t.text).slice(-2).map((t) => [t.q, t.text]);
  const turn: Turn = { q, plan: null, sources: [], text: "", done: false, error: null, meta: null };
  CHAT.push(turn);
  emit("ask");
  const handle = (name: string, data: Record<string, unknown>) => {
    if (name === "plan") turn.plan = data as unknown as Plan;
    else if (name === "sources") turn.sources = data.sources as Source[];
    else if (name === "text") turn.text += data.text as string;
    else if (name === "done") { turn.meta = data as Turn["meta"]; turn.done = true; save(); }
    else if (name === "error") { turn.error = data.error as string; turn.done = true; save(); }
    emit("ask");
  };
  (async () => {
    try {
      const r = await fetch("/api/ask", {
        method: "POST",
        headers: { "X-Wavescribe": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, history }),
      });
      if (!r.ok || !r.body) {
        handle("error", { error: (await r.json().catch(() => ({}))).error || r.statusText });
        return;
      }
      const reader = r.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          let name = "message", data = "";
          for (const line of block.split("\n")) {
            if (line.startsWith("event:")) name = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (data) handle(name, JSON.parse(data));
        }
      }
      if (!turn.done) handle("error", { error: "The answer stopped before it finished." });
    } catch (e) {
      handle("error", { error: `Couldn't ask: ${(e as Error).message}` });
    }
  })();
}

function examples(): string[] {
  const names = S.stations.filter((s) => S.live[s.id]).map((s) => s.name);
  const one = names[0];
  return [
    "What happened in the last hour?",
    one ? `What has ${one} talked about since this morning?` : "What did they talk about this morning?",
    "Was anything said about the weather or the roads today?",
  ];
}

export function Ask() {
  useTopics("ask", "state", "prefs");
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const [draft, setDraft] = useState("");
  const stick = useRef(true);
  useLayoutEffect(() => {
    const l = list.current;
    if (l && stick.current) l.scrollTop = l.scrollHeight;
  });
  useEffect(() => { if (!matchMedia("(pointer: coarse)").matches) input.current?.focus(); }, []);
  const submit = (q: string) => {
    q = q.trim();
    if (!q || S.ask) return;
    setDraft("");
    stick.current = true;
    sendQuestion(q);
  };
  return (
    <section className="panel ask" aria-labelledby="ask-h">
      <header className="panel-head">
        <div>
          <h2 id="ask-h">Ask</h2>
          <p className="panel-sub">Answers come only from what the stations said, with the excerpts they cite.</p>
        </div>
        <span className="panel-tools">
          {CHAT.length > 0 && (
            <Button variant="ghost" size="sm" icon="plus" onClick={() => { CHAT.length = 0; save(); emit("ask"); input.current?.focus(); }}>
              New conversation
            </Button>
          )}
          <Button variant="ghost" icon="close" aria-label="Close Ask" onClick={() => go("#/")} />
        </span>
      </header>
      <div ref={list} className="panel-body" onScroll={(e) => {
        const l = e.currentTarget;
        stick.current = l.scrollHeight - l.scrollTop - l.clientHeight < 80;
      }}>
        <div className="turns">
          {!CHAT.length && (
            <div className="ask-empty">
              <p>Ask in any language. For example:</p>
              <ul>
                {examples().map((x) => (
                  <li key={x}><button type="button" onClick={() => submit(x)} disabled={!!S.ask}><Icon name="ask" />{x}</button></li>
                ))}
              </ul>
            </div>
          )}
          {CHAT.map((t, i) => <TurnView key={i} t={t} />)}
        </div>
      </div>
      <form className="composer" onSubmit={(e) => { e.preventDefault(); submit(draft); }}>
        <textarea ref={input} rows={1} value={draft} disabled={!!S.ask} enterKeyHint="send"
                  placeholder={S.ask ? `Ask isn't available: ${S.ask}` : CHAT.length ? "Ask a follow-up…" : "Ask about what was said…"}
                  aria-label="Your question" onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit(draft); } }} />
        <Button type="submit" variant="primary" icon="ask" disabled={!draft.trim() || !!S.ask}>Ask</Button>
      </form>
    </section>
  );
}

function planLabel(plan: Plan): string {
  const names = plan.stations.map((id) => S.byId[id]?.name || id);
  const where = names.length ? names.join(", ") : "all stations";
  const when = `${whenLabel(plan.since)}–${whenLabel(plan.until)}`;
  const terms = plan.terms.slice(0, 10).join(", ");
  const what = plan.sampled ? `nothing matched ${terms}, so a sample of everything said`
    : plan.overview ? "a sample of everything said" : terms;
  const wide = plan.widened ? " · nothing in the time asked about, so the last 24 h" : "";
  return `Looked in ${where} · ${when} · ${what}${wide}`;
}

function TurnView({ t }: { t: Turn }) {
  const [open, setOpen] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);
  const uid = useId(); // unique per turn: two questions of the same length used to share ids
  const cite = (n: number) => {
    setOpen(true);
    setFlash(n);
    requestAnimationFrame(() => document.getElementById(`${uid}-src-${n}`)?.scrollIntoView({ block: "nearest" }));
    setTimeout(() => setFlash(null), 1600);
  };
  return (
    <article className="turn">
      <p className="turn-q" dir="auto">{t.q}</p>
      <p className="turn-plan">{t.plan ? planLabel(t.plan) : t.done ? "" : "Working out where to look…"}</p>
      <div className="turn-a" dir="auto" aria-live="polite" aria-busy={!t.done}>
        {t.error ? <p className="turn-err"><Icon name="warn" /> {t.error}</p>
          : t.text ? <Answer text={t.text} onCite={cite} />
          : t.plan && !t.done ? <p className="turn-reading">Reading the excerpts<span className="dots"><i /><i /><i /></span></p> : null}
      </div>
      {t.sources.length > 0 && (
        <details className="turn-srcs" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
          <summary>{t.sources.length} excerpt{t.sources.length > 1 ? "s" : ""} it read</summary>
          <ol>
            {t.sources.map((s) => <SourceRow key={s.n} s={s} id={`${uid}-src-${s.n}`} flash={flash === s.n} />)}
          </ol>
        </details>
      )}
      {t.meta && <p className="turn-meta num">{t.meta.model} · {(t.meta.total_ms / 1000).toFixed(1)} s</p>}
    </article>
  );
}

/** Paragraphs, lists, **bold** and citations like [3], [3, 7] or [2-4], each a button. */
function Answer({ text, onCite }: { text: string; onCite: (n: number) => void }) {
  const blocks: { list: boolean; lines: string[] }[] = [];
  for (const line of text.split("\n")) {
    if (!line.trim()) { blocks.push({ list: false, lines: [] }); continue; }
    const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    const last = blocks[blocks.length - 1];
    if (item) {
      if (last?.list) last.lines.push(item[1]);
      else blocks.push({ list: true, lines: [item[1]] });
    } else blocks.push({ list: false, lines: [line.replace(/^#+\s*/, "")] });
  }
  const inline = (s: string) =>
    s.split(/(\*\*[^*]+\*\*|\[\d+(?:\s*[,–-]\s*\d+)*\])/).map((part, i) => {
      if (!part) return null;
      if (part.startsWith("**") && part.endsWith("**")) return <b key={i}>{part.slice(2, -2)}</b>;
      const c = part.match(/^\[(.*)\]$/);
      if (!c) return <Fragment key={i}>{part}</Fragment>;
      const nums: number[] = [];
      for (const piece of c[1].split(",")) {
        const [a, b] = piece.split(/[–-]/).map((x) => parseInt(x, 10));
        for (let n = a; n <= (b || a) && nums.length < 40; n++) nums.push(n);
      }
      return <Fragment key={i}>{nums.map((n) => <button key={n} type="button" className="cite" onClick={() => onCite(n)} aria-label={`Excerpt ${n}`}>{n}</button>)}</Fragment>;
    });
  return (
    <>
      {blocks.filter((b) => b.lines.length).map((b, i) => b.list
        ? <ul key={i}>{b.lines.map((l, j) => <li key={j}>{inline(l)}</li>)}</ul>
        : <p key={i}>{inline(b.lines[0])}</p>)}
    </>
  );
}

function SourceRow({ s, id, flash }: { s: Source; id: string; flash: boolean }) {
  const st: Station = S.byId[s.station] || ({ id: s.station, name: s.name, language: s.language, translate_to: [] } as unknown as Station);
  const shown = shownLang(st, P.lang);
  const translated = !sameLang(shown, s.language) ? s.translations[shown] : null;
  return (
    <li id={id} className={cx("hit", flash && "is-flash")}>
      <div className="hit-meta">
        <span className="cite is-static">{s.n}</span>
        <span className="hit-station" dir="auto">{st.name}</span>
        <time className="num">{whenLabel(s.t_start)}</time>
        {s.audio && <Button size="sm" variant="ghost" icon="play" onClick={() => playReplay([s.id], `${st.name} ${hm(s.t_start)}`)}>Play</Button>}
      </div>
      <p className="hit-text" dir="auto" lang={translated ? shown : s.language}>{translated || s.text}</p>
      {translated && <p className="hit-other" dir="auto" lang={s.language}>{s.text}</p>}
    </li>
  );
}
