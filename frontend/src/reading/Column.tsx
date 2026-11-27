// One station's transcript: paragraphs split at pauses, times in the margin, newest at the bottom.
import { Suspense, lazy, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { A, playReplay, startAndPlay, toggleAudio } from "../lib/audio";
import { columnLag } from "../lib/behind";
import { dirOf, langNameEn, otherLang, pick, sameLang, shownLang } from "../lib/lang";
import { S, linesAround, loadEarlier, post, running, setNotice, startStation } from "../lib/live";
import { P } from "../lib/prefs";
import { go, setCols } from "../lib/router";
import { noText, overloaded, stateOf } from "../lib/station";
import { useTopics } from "../lib/store";
import { ago, hm, hms, whenLabel } from "../lib/time";
import type { Station, Utterance } from "../lib/types";
import { Icon } from "../components/Icon";
import { Button, Lamp, cx } from "../components/ui";
import { V } from "../lib/view";
import { bandWords } from "../manage/bands";

const FLASH_MS = 2400; // how long a line asked for by a link stays marked
const PAGES_BACK = 20; // how far "Load earlier" may page back to reach such a line
import { channelHeads, channelText } from "./channels";
import { StationMenu } from "./StationMenu";
import { T } from "./strings";
import { topSeverity, utteranceWords, withInaudible } from "./text";

// Moving a receiver back to a station it doesn't hear is the control centre's: loaded only on the
// Wavescribe machine, and only for a station that needs it.
const MoveStation = lazy(() => import("../manage/MoveStation"));

const PARA_GAP_S = 3; // a pause this long starts a new paragraph…
const PARA_MAX_S = 45; // …and so does 45 s of continuous speech

/** A paragraph's span; null when its first line has no known time. */
interface Para {
  start: number | null;
  end: number | null;
  utts: Utterance[];
}

/** A line without a known time joins nothing and nothing joins it: it stands alone, timed "?".
 * A watch's lines join only lines from the same channel. */
function group(list: Utterance[]): Para[] {
  const paras: Para[] = [];
  for (const u of list) {
    const last = paras[paras.length - 1];
    const t = u.t_start;
    if (last && t != null && last.start != null && last.end != null && t - last.end < PARA_GAP_S && t - last.start < PARA_MAX_S
        && (last.utts[0].freq_hz ?? null) === (u.freq_hz ?? null)) {
      last.utts.push(u);
      last.end = Math.max(last.end, t + u.duration);
    } else paras.push({ start: t, end: t == null ? null : t + u.duration, utts: [u] });
  }
  return paras;
}

/** "watching Walkie-talkies", "watching 446.000–446.100 MHz": what a watch listens to, a band
 * by the name the dial gives it. */
const watching = (w: string | [number, number]) =>
  T.watching(typeof w === "string" ? bandWords({ id: w, name: w }).name : `${w[0].toFixed(3)}–${w[1].toFixed(3)} MHz`);

/** The language a station speaks, in words: its own, or where the speech model tells each line's
 * (language "auto"), the ones it expects, if it was told any. */
export function spoken(st: Station): string {
  if (st.language !== "auto") return langNameEn(st.language);
  const expected = st.languages || [];
  return expected.length ? T.detectedOf(T.oneOf(expected.map(langNameEn))) : T.detected;
}

/** "Hebrew → English", or just "Hebrew" when the station is shown in its own language. */
export function langPair(st: Station): string {
  const shown = shownLang(st, P.lang);
  return sameLang(shown, st.language) ? spoken(st) : `${spoken(st)} → ${langNameEn(shown)}`;
}

export function lastTextAgo(id: string): string | null {
  const list = S.utts[id];
  const last = list?.[list.length - 1];
  if (!last || last.t_start == null) return null;
  return ago(Math.max(0, Date.now() / 1000 - (last.t_start + last.duration)));
}

export function Column({ id, onlyOne }: { id: string; onlyOne?: boolean }) {
  useTopics("state");
  const st = S.byId[id];
  const [span, setSpan] = useState<string | null>(null);
  if (!st) return null;
  return (
    <section className="col" aria-labelledby={`col-${id}`} data-id={id}>
      <ColumnHead st={st} span={span} onlyOne={onlyOne} />
      <Transcript st={st} onSpan={setSpan} />
    </section>
  );
}

function ColumnHead({ st, span, onlyOne }: { st: Station; span: string | null; onlyOne?: boolean }) {
  useTopics("status", "audio", "prefs", `utt:${st.id}`, "conn");
  const state = stateOf(st.id);
  const on = running(st.id);
  const stale = S.conn !== "live";
  const playing = A.playing === st.id;
  const last = on ? lastTextAgo(st.id) : null;
  const canListen = on && st.kind !== "remote" && !!S.live[st.id]?.connected && !stale; // remote: text only
  const speech = !stale && state.lamp === "speech";
  // Translation lag matters only to someone reading the translation.
  const lag = on && !stale ? columnLag(S.live[st.id]) : null;
  const translated = !sameLang(shownLang(st, P.lang), st.language);
  // Its receiver is tuned where it doesn't hear it: the reason, and on this machine the move back
  // (not for a station switched off, which the move would start).
  const away = !on && !st.disabled && st.out_of_reach;
  // It can't start as it is set up (switched off, no source): the server's reason as its state.
  const cant = !on && !st.available && !away ? st.reason || T.unavailable : null;
  return (
    <header className="col-head">
      <div className="col-title">
        <Lamp state={stale ? "off" : state.lamp} />
        <h2 id={`col-${st.id}`} dir="auto">{st.name}</h2>
        <span className="col-actions">
          {on ? (
            <>
              <Button size="sm" variant={playing ? "primary" : "ghost"} icon={playing ? "stop" : "speaker"}
                      disabled={!canListen} onClick={() => toggleAudio(st.id)} aria-pressed={playing}>
                {playing ? T.stopListening : T.listen}
              </Button>
              {st.listen_only_run && S.canControl && (
                <Button size="sm" variant="secondary" icon="power" onClick={() => startStation(st.id)} aria-label={T.startWriting(st.name)}>
                  {T.start}
                </Button>
              )}
            </>
          ) : S.canControl && st.available && !st.disabled ? (
            <>
              {st.kind !== "remote" && (
                <Button size="sm" variant="ghost" icon="speaker" disabled={!S.status?.ready} onClick={() => startAndPlay(st.id)}
                        title={T.startAndListenHint}>
                  {T.listen}
                </Button>
              )}
              <Button size="sm" variant="primary" icon="power" disabled={!S.status?.ready}
                      onClick={() => post(`/api/stations/${st.id}/start`)}>
                {T.start}
              </Button>
            </>
          ) : null}
          <StationMenu st={st} onlyOne={onlyOne} />
          {!onlyOne && (
            <Button size="sm" variant="ghost" icon="close" aria-label={`${T.close} ${st.name}`}
                    onClick={() => setCols(V.shown.filter((x) => x !== st.id))} />
          )}
        </span>
      </div>
      <p className="col-meta">
        <span>{langPair(st)}</span>
        {st.watch && <span>{watching(st.watch)}</span>}
        {lag?.text && <span className="is-trouble num">{T.colBehind(lag.text)}</span>}
        {lag?.translation && translated && <span className="num">{T.colTranslationBehind(lag.translation)}</span>}
        {away || cant ? (
          <span className="col-state is-trouble">{away ? T.outOfReach : cant}</span>
        ) : (stale || !speech) && (
          <span className={cx("col-state", state.lamp === "trouble" && "is-trouble")} title={state.detail || undefined}>
            {stale ? T.reconnecting : state.why || state.label}
          </span>
        )}
        {last && !speech && <span className="num">{T.lastText(last)}</span>}
        {overloaded(st.id) && <span className="is-trouble">RF overload: lower the gain</span>}
        {S.live[st.id]?.rds?.rt && (
          <span className="col-rds" title="What the station sends about itself (RDS radiotext)">{S.live[st.id]!.rds!.rt}</span>
        )}
        {span && <span className="col-span num" title="The time span in view">{span}</span>}
      </p>
      {away && (
        <div className="col-reach">
          <p>{st.out_of_reach}</p>
          {S.manage && st.device && <Suspense fallback={null}><MoveStation st={st} /></Suspense>}
        </div>
      )}
    </header>
  );
}

function Transcript({ st, onSpan }: { st: Station; onSpan: (s: string | null) => void }) {
  const v = useTopics(`utt:${st.id}`, "prefs");
  // An older line a link asked for, with the lines around it, shown in place of the live ones
  // until "Back to live": the lines between it and now aren't loaded, and a gap would read as
  // silence.
  const [around, setAround] = useState<Utterance[] | null>(null);
  const list = around || S.utts[st.id] || [];
  const paras = useMemo(() => group(list), [v, around]);
  const heads = channelHeads(paras, st.watch != null);
  const shown = shownLang(st, P.lang);
  const body = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const [behind, setBehind] = useState(false);
  const [more, setMore] = useState<"idle" | "busy" | "none" | "failed">("idle");
  const keep = useRef<number | null>(null); // distance from the bottom to restore after "Load earlier"
  const lastTop = useRef(0);

  const toLive = useCallback(() => {
    const b = body.current;
    if (b) b.scrollTop = b.scrollHeight;
  }, []);

  useLayoutEffect(() => {
    const b = body.current;
    if (!b) return;
    if (keep.current != null) {
      b.scrollTop = b.scrollHeight - keep.current;
      keep.current = null;
    } else if (follow.current) b.scrollTop = b.scrollHeight;
  }, [v]);

  useEffect(() => () => { S.reading.delete(st.id); }, [st.id]);

  // Text that reflows after it is drawn (a banner above, the column resized, a translation
  // arriving in a stopped station) would leave a following column short of its newest line.
  useEffect(() => {
    const b = body.current;
    const inner = b?.firstElementChild;
    if (!b || !inner) return;
    const ro = new ResizeObserver(() => { if (follow.current) b.scrollTop = b.scrollHeight; });
    ro.observe(b);
    ro.observe(inner);
    return () => ro.disconnect();
  }, []);

  // A link asked for one line of this station (an alert's): page back to it if it's older than
  // what is loaded, bring it to the middle and mark it for a moment.
  const asked = useTopics("focus");
  useEffect(() => {
    const want = V.focus;
    if (!want || want.station !== st.id) return;
    V.focus = null;
    let live = true;
    const hold = () => { follow.current = false; keep.current = null; S.reading.add(st.id); setBehind(true); };
    (async () => {
      const loaded = S.utts[st.id] || [];
      const oldest = loaded.find((u) => u.id != null);
      // Older than the oldest line loaded: its own window where the server gives one, else page
      // back to it.
      if (!loaded.some((u) => u.id === want.id) && oldest && oldest.id! > want.id) {
        hold();
        const win = await linesAround(st.id, want.id).catch(() => null);
        if (!live) return;
        if (win) setAround(win);
        else {
          for (let i = 0; i < PAGES_BACK && live; i++) {
            if ((S.utts[st.id] || []).some((u) => u.id === want.id)) break;
            if (!(await loadEarlier(st.id).catch(() => 0))) break;
          }
        }
      }
      // The column draws what arrived on its next render: look for the line for a few frames.
      for (let f = 0; f < 30 && live; f++) {
        const b = body.current;
        const el = b?.querySelector<HTMLElement>(`[data-u="${want.id}"]`);
        if (b && el) {
          hold();
          // At once, not smoothly: a glide starts near the newest line, where scrolling turns
          // following back on, and the line can be pages away.
          const r = el.getBoundingClientRect(), box = b.getBoundingClientRect();
          b.scrollTop += r.top - box.top - (b.clientHeight - r.height) / 2;
          b.focus({ preventScroll: true });
          el.classList.add("u-flash");
          setTimeout(() => el.classList.remove("u-flash"), FLASH_MS);
          return;
        }
        await new Promise((ok) => requestAnimationFrame(ok));
      }
      if (live) setNotice(T.lineGone);
    })();
    return () => { live = false; };
  }, [asked, st.id]);

  const measureSpan = () => {
    const b = body.current;
    if (!b || follow.current) return onSpan(null);
    const top = b.scrollTop, bottom = top + b.clientHeight;
    let first: number | null = null, last: number | null = null;
    for (const el of b.querySelectorAll<HTMLElement>(".para")) {
      const y = el.offsetTop, h = el.offsetHeight;
      if (y + h < top) continue;
      if (y > bottom) break;
      const t = Number(el.dataset.t);
      if (first == null) first = t;
      last = t;
    }
    onSpan(first == null ? null : first === last ? whenLabel(first) : `${whenLabel(first)}–${hm(last!)}`);
  };

  const onScroll = () => {
    const b = body.current!;
    const atEnd = b.scrollHeight - b.scrollTop - b.clientHeight < 48;
    if (atEnd && !around) follow.current = true; // the end of a window of older lines isn't now
    else if (b.scrollTop < lastTop.current) follow.current = false;
    lastTop.current = b.scrollTop;
    if (follow.current) S.reading.delete(st.id);
    else S.reading.add(st.id);
    setBehind(!follow.current);
    measureSpan();
  };

  const earlier = async () => {
    setMore("busy");
    const b = body.current!;
    keep.current = b.scrollHeight - b.scrollTop;
    follow.current = false;
    S.reading.add(st.id);
    setBehind(true);
    const top = b.scrollTop;
    try {
      const n = await loadEarlier(st.id);
      if (!n) keep.current = null;
      setMore(n ? "idle" : "none");
    } catch {
      // Nothing arrived, so nothing moves: the reader stays where they were, with a retry.
      keep.current = null;
      b.scrollTop = top;
      setMore("failed");
    }
  };

  const hasHistory = S.hasStore && list.some((u) => u.id != null);
  const on = running(st.id);
  return (
    <div className="transcript-wrap">
      <div ref={body} className="transcript" tabIndex={0} onScroll={onScroll}
           role="log" aria-live="off" aria-label={`${st.name}: transcript`}>
        <div className="transcript-inner">
          {hasHistory && !around && (
            <div className="earlier">
              <Button size="sm" variant="ghost" icon={more === "failed" ? "refresh" : "history"} busy={more === "busy"} disabled={more === "none"} onClick={earlier}>
                {more === "none" ? T.nothingEarlier : more === "failed" ? T.earlierFailed : T.loadEarlier}
              </Button>
            </div>
          )}
          {paras.map((p, i) => (
            <ParaView key={p.utts[0].key} para={p} st={st} shown={shown} src={P.src} channel={heads[i]} />
          ))}
          {!list.length && (
            <p className="col-empty">
              {!on ? (!st.disabled && st.out_of_reach ? null : T.stationOff) : noText(st) ? T.noTextYet : st.transcribe === false ? T.listenOnly : T.waiting}
              {on && noText(st) && S.manage && (
                <> <button type="button" className="col-empty-link" onClick={() => go("#/settings/languages")}>{T.getModels}</button></>
              )}
            </p>
          )}
        </div>
      </div>
      {behind && (
        <button type="button" className="jump" onClick={() => {
          follow.current = true;
          setBehind(false);
          setAround(null);
          S.reading.delete(st.id);
          onSpan(null);
          requestAnimationFrame(toLive);
        }}>
          <Icon name="arrowDown" size={14} /> {T.backToLive}
        </button>
      )}
    </div>
  );
}

/** `channel`: the paragraph says the channel its lines were heard on (channels.ts). */
const ParaView = memo(function ParaView({ para, st, shown, src, channel }: { para: Para; st: Station; shown: string; src: boolean; channel: boolean }) {
  const [linked, setLinked] = useState<string | null>(null);
  const [reveal, setReveal] = useState<string | null>(null);
  const translated = !sameLang(shown, st.language);
  const ids = para.utts.filter((u) => u.audio && u.id != null).map((u) => u.id!);
  const pending = para.utts.some((u) => pick(u, st, shown)[2] === "pending");

  const keyOf = (e: React.SyntheticEvent) => (e.target as HTMLElement).closest<HTMLElement>("[data-k]")?.dataset.k ?? null;
  const onClick = (e: React.MouseEvent) => {
    if (!translated || !window.getSelection()?.isCollapsed) return;
    const k = keyOf(e);
    if (k) setReveal(reveal === k ? null : k);
  };
  const revealed = reveal ? para.utts.find((u) => u.key === reveal) : null;

  return (
    <div className="para" data-t={para.start}>
      {ids.length ? (
        <button type="button" className="ts" title={`Play from ${hms(para.start)}`} aria-label={`Play from ${whenLabel(para.start)}`}
                onClick={() => playReplay(ids, `${st.name} ${hm(para.start)}`)}>
          <Icon name="play" size={10} /><time className="num">{whenLabel(para.start)}</time>
        </button>
      ) : (
        <time className="ts num" title={hms(para.start)}>{whenLabel(para.start)}</time>
      )}
      <div className="para-body">
        {channel && <p className="para-ch num">{channelText(para.utts[0])}</p>}
        <p className="para-text" dir={dirOf(shown)} onClick={onClick}
           onMouseOver={src ? (e) => setLinked(keyOf(e)) : undefined} onMouseLeave={src ? () => setLinked(null) : undefined}>
          {para.utts.map((u, i) => (
            <UttView key={`${u.key}:${pick(u, st, shown)[2]}`} u={u} st={st} shown={shown} linked={linked === u.key}
                     label={!!labelOf(u, st, shown) && labelOf(u, st, shown) !== (i ? labelOf(para.utts[i - 1], st, shown) : "")} />
          ))}
          {pending && !src && <span className="sr-only">{T.translating}</span>}
        </p>
        {src && translated && (
          <p className="para-src" dir={dirOf(st.language)}>
            {para.utts.map((u) => (
              <span key={u.key} data-k={u.key} lang={u.language || st.language} className={cx(linked === u.key && "is-linked")}
                    onMouseOver={() => setLinked(u.key)}>
                {withInaudible(u.text)}{" "}
              </span>
            ))}
          </p>
        )}
        {!src && revealed && (
          <p className="para-src is-one" dir={dirOf(revealed.language || st.language)} lang={revealed.language || st.language}>
            <span className="para-src-lang">{langNameEn(revealed.language || st.language)}</span>
            {withInaudible(revealed.text)}
          </p>
        )}
      </div>
    </div>
  );
}, (a, b) =>
  a.shown === b.shown && a.src === b.src && a.st === b.st && a.channel === b.channel &&
  a.para.utts.length === b.para.utts.length && a.para.utts.every((u, i) => u === b.para.utts[i]));

/** Why an utterance isn't in the reader's language, if it isn't, or for a station whose speech
 * model tells each line's language, the language it told: the chip a run of them shares. */
function labelOf(u: Utterance, st: Station, shown: string): string {
  const [, lang, state] = pick(u, st, shown);
  const other = otherLang(u);
  if (state === "untranslated") return langNameEn(lang);
  if (other) return T.saidIn(langNameEn(other));
  return st.language === "auto" && sameLang(shown, st.language) && u.language ? langNameEn(u.language) : "";
}

function UttView({ u, st, shown, linked, label }: { u: Utterance; st: Station; shown: string; linked: boolean; label: boolean }) {
  const [text, lang, state] = pick(u, st, shown);
  const other = otherLang(u);
  const hits = u.hits || [];
  const fresh = S.fresh.has(u.key);
  if (state === "pending") {
    // Grey means "not yet in your language": the words as heard, until the translation replaces
    // them. Names and numbers can be read before it arrives.
    return (
      <span data-k={u.key} data-u={u.id ?? undefined} lang={lang} dir="auto" className={cx("u", "u-pending", linked && "is-linked")}
            title={`Translating into ${langNameEn(shown)}`}>
        {withInaudible(text)}{" "}
      </span>
    );
  }
  const title = [
    other ? `Said in ${langNameEn(other)}: maybe a song or a clip in another language` : "",
    hits.length ? `Alert rule: ${[...new Set(hits.map((h) => h.rule))].join(", ")}` : "",
    state === "untranslated" ? `Not translated into ${langNameEn(shown)}` : "",
  ].filter(Boolean).join(" · ");
  const sev = hits.length ? topSeverity(hits.map((h) => h.severity)) : null;
  return (
    <span data-k={u.key} data-u={u.id ?? undefined} lang={lang} dir="auto" title={title || undefined}
          className={cx("u", fresh && "u-fresh", state === "untranslated" && "u-untranslated", other && "u-other",
                        sev && text !== u.text && `u-hit kw-${sev}`, linked && "is-linked")}>
      {label && <span className="u-lang">{labelOf(u, st, shown)}</span>}
      {utteranceWords(u, text)}{" "}
    </span>
  );
}
