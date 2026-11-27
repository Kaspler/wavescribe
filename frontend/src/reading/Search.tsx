// Search everything heard (SQLite FTS5 on the source text and every translation).
import { useEffect, useState } from "react";
import { playReplay } from "../lib/audio";
import { sameLang, shownLang } from "../lib/lang";
import { S } from "../lib/live";
import { P } from "../lib/prefs";
import { go, setCols } from "../lib/router";
import { goToLine } from "../lib/view";
import { useTopics } from "../lib/store";
import { hm, whenLabel } from "../lib/time";
import type { SearchHit, Station } from "../lib/types";
import { Button } from "../components/ui";
import { markWords, snippet } from "./text";
import { T } from "./strings";

const SEARCH_LIMIT = 60; // the server's (web/server.py, _search)

type State = { q: string; hits: SearchHit[] | null; error: string | null };

export function Search({ q }: { q: string }) {
  useTopics("state", "prefs");
  const [st, setSt] = useState<State>({ q, hits: null, error: null });
  useEffect(() => {
    let live = true;
    setSt({ q, hits: null, error: null });
    fetch(`/api/search?q=${encodeURIComponent(q)}`)
      .then(async (r) => {
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || "Search failed.");
        return r.json();
      })
      .then((hits: SearchHit[]) => live && setSt({ q, hits, error: null }))
      .catch((e) => live && setSt({ q, hits: [], error: e.message }));
    return () => { live = false; };
  }, [q]);
  const n = st.hits?.length ?? 0;
  return (
    <section className="panel" aria-labelledby="search-h">
      <header className="panel-head">
        <div>
          <h2 id="search-h">Search</h2>
          <p className="panel-sub" aria-live="polite">
            {st.hits == null ? "Searching…" : st.error ? st.error : n ? `${n >= SEARCH_LIMIT ? `${SEARCH_LIMIT}+` : n} results for “${q}”, newest first` : `Nothing found for “${q}”.`}
          </p>
        </div>
        <Button variant="ghost" icon="close" aria-label="Close search" onClick={() => go("#/")} />
      </header>
      <div className="panel-body">
        {st.hits && n > 0 && (
          <ol className="hits">
            {st.hits.map((h) => <HitRow key={`${h.id}:${h.field}`} h={h} q={st.q} />)}
          </ol>
        )}
        {st.hits && !n && !st.error && (
          <p className="panel-hint">Search finds words, and parts of words, in the original and in every translation. Try fewer words, or a word in the station's own language.</p>
        )}
      </div>
    </section>
  );
}

function HitRow({ h, q }: { h: SearchHit; q: string }) {
  const st: Station = S.byId[h.station] || { id: h.station, name: h.station, language: h.language, translate_to: [] } as unknown as Station;
  const shown = shownLang(st, P.lang);
  // The hit in the reader's language when it has one and the match was in another; the match
  // itself under it. Otherwise the match first, with the original under a translation's match.
  const mine = sameLang(h.field_lang, shown) ? null : sameLang(shown, h.language) ? h.text : h.translations[shown] ?? null;
  const original = !mine && h.field !== "source" ? h.text : null;
  return (
    <li className="hit">
      <div className="hit-meta">
        {S.byId[h.station] ? (
          <button type="button" className="hit-station" dir="auto" onClick={() => setCols([h.station])}>{st.name}</button>
        ) : (
          <span className="hit-station" dir="auto">{st.name}</span>
        )}
        {S.byId[h.station] && h.t_start != null ? (
          <button type="button" className="hit-time num" onClick={() => goToLine(h.station, h.id)} title={T.showInTranscript}>{whenLabel(h.t_start)}</button>
        ) : (
          <time className="num">{whenLabel(h.t_start)}</time>
        )}
        {h.audio && (
          <Button size="sm" variant="ghost" icon="play" onClick={() => playReplay([h.id], `${st.name} ${hm(h.t_start)}`)}>Play</Button>
        )}
      </div>
      {mine ? (
        <>
          <p className="hit-text" dir="auto" lang={shown}>{markWords(mine, q)}</p>
          <p className="hit-other" dir="auto" lang={h.field_lang}>{snippet(h.snippet)}</p>
        </>
      ) : (
        <>
          <p className="hit-text" dir="auto" lang={h.field_lang}>{snippet(h.snippet)}</p>
          {original && <p className="hit-other" dir="auto" lang={h.language}>{original}</p>}
        </>
      )}
    </li>
  );
}
