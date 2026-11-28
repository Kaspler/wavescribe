// Alerts: what a rule caught, said plainly, newest first. They live in their own rail on wide
// screens and lead the station list on phones.
import { useEffect, useRef, useState } from "react";
import { playReplay } from "../lib/audio";
import { fetchRead } from "../lib/auth";
import { skippedChecks } from "../lib/behind";
import { langNameEn, sameLang } from "../lib/lang";
import { S, running, setNotice } from "../lib/live";
import { P } from "../lib/prefs";
import { MAX_COLS, alertHash, setCols } from "../lib/router";
import { noText } from "../lib/station";
import { emit, useTopics } from "../lib/store";
import { hm, isoTime, whenLabel } from "../lib/time";
import type { Alert } from "../lib/types";
import { V, railShown, showLine, toggleRail } from "../lib/view";
import { Icon } from "../components/Icon";
import { Button, Segmented, cx, toast } from "../components/ui";
import { alertSpans, excerptStart, findTerms } from "./alertText";
import { withoutWordLists } from "./rulesNote";
import { T } from "./strings";
import { withInaudible, withMarks } from "./text";

const RECENT_S = 12 * 3600;

/** Alerts newest first; a sentence that tripped several rules was announced once, so it is shown
 * once, under its lead, with the other rules named. */
export function alertList(): { list: Alert[]; also: Map<number, string[]> } {
  const also = new Map<number, string[]>();
  const list = [...S.alerts.values()].sort((x, y) => y.id - x.id).filter((a) => {
    const lead = a.actions?.covered_by as number | undefined;
    if (a.status !== "covered" || lead == null || !S.alerts.has(lead)) return true;
    also.set(lead, [...(also.get(lead) || []), a.rule]);
    return false;
  });
  return { list, also };
}

const recent = (a: Alert) => a.severity !== "low" && a.last_at >= Date.now() / 1000 - RECENT_S;
export const recentAlerts = () => alertList().list.filter(recent);

/** Opens the page at one alert (#/alert/<id>, an alert's page_url): the alerts show it, open and
 * marked, and its station's column scrolls to what was said. One older than the page loaded is
 * asked for; one no longer kept says so. */
export async function openAlert(id: number) {
  let a = S.alerts.get(id);
  if (!a) {
    const r = await fetchRead(`/api/alerts/${id}`).catch(() => null);
    a = r?.ok ? ((await r.json()) as Alert) : undefined;
  }
  if (!a) {
    V.alert = null;
    emit("focus");
    setNotice(T.alertGone);
    location.replace("#/");
    return;
  }
  V.alert = a;
  const hash = alertHash(a.stations, (st) => Object.hasOwn(S.byId, st));
  const st = a.stations.find((x) => Object.hasOwn(S.byId, x));
  if (st && a.segment_id != null && st === a.stations[0]) showLine(st, a.segment_id);
  if (!V.narrow) V.railOpen = true; // phones show it above the column instead
  emit("focus", "narrow", "alerts");
  location.replace(hash); // Back leaves the alert, rather than opening it again
}

export function AlertList({ max, compact }: { max?: number; compact?: boolean }) {
  useTopics("alerts", "prefs", "state", "status", "focus");
  const [all, setAll] = useState(false);
  const { list, also } = alertList();
  const rec = list.filter(recent);
  let shown = (all ? list : rec).slice(0, all ? 50 : max ?? 50);
  // The alert a link opened is shown whatever the filter, first when it wouldn't be.
  const target = V.alert && (S.alerts.get(V.alert.id) ?? V.alert);
  if (target && !compact && !shown.some((a) => a.id === target.id)) shown = [target, ...shown];
  // The stations being written down here now, none in a language the topic rules have a word
  // list for: such rules can't match, which an empty list wouldn't say.
  const heard = S.stations.filter((s) => running(s.id) && s.transcribe && !noText(s) && !s.listen_only_run && s.kind !== "remote");
  const without = withoutWordLists(S.topicLanguages, heard);
  return (
    <div className={cx("alerts", compact && "is-compact")}>
      <div className="alerts-head">
        <h2 id="alerts-h">{T.alerts}</h2>
        {list.length > 0 && (
          <Segmented label="Which alerts" value={all ? "all" : "recent"} onChange={(v) => setAll(v === "all")}
                     options={[{ value: "recent", label: `${T.alertsRecent} ${rec.length}` }, { value: "all", label: `${T.alertsAll} ${list.length}` }]} />
        )}
      </div>
      {shown.length ? (
        <ol className="alert-list" aria-labelledby="alerts-h">
          {shown.map((a) => <AlertItem key={a.id} a={a} also={also.get(a.id) || []} fresh={a.id === S.freshAlert} />)}
        </ol>
      ) : (
        <>
          <p className="alerts-none">
            {!list.length ? T.alertsNoneAtAll : list.some((a) => a.severity === "low") ? T.alertsOnlyLow(list.filter((a) => a.severity === "low").length) : T.alertsNone}
            {S.manage && !without && <> {T.alertRulesHint} <a href="#/settings/alerts">{T.alertRules}</a>.</>}
          </p>
          {without && (
            <p className="alerts-none alerts-why">
              {T.alertsNoWordList(without.map(langNameEn), (S.topicLanguages || []).map(langNameEn))} {T.alertsAnyLanguage}
              {S.manage && <> {T.alertsAddUnder} <a href="#/settings/alerts">{T.alertRules}</a>.</>}
            </p>
          )}
        </>
      )}
      {!all && rec.length > shown.length && (
        <button type="button" className="alerts-more" onClick={() => setAll(true)}>
          {rec.length - shown.length} more
        </button>
      )}
      {skippedChecks(S.status) > 0 && <p className="alerts-foot is-trouble">{T.checksSkipped}</p>}
      <p className="alerts-foot">{T.alertsCanFail}</p>
    </div>
  );
}

export const SEV_WORD = { critical: "Critical", high: "High", medium: "Medium", low: "Low" } as const;
export const human = (rule: string) => rule.replace(/[-_]+/g, " ").replace(/^./, (c) => c.toUpperCase());

function AlertItem({ a, also, fresh }: { a: Alert; also: string[]; fresh: boolean }) {
  useTopics(`utt:${a.stations[0]}`, "focus"); // the quoted utterance's translation may come later
  const focused = V.alert?.id === a.id;
  const [open, setOpen] = useState(focused);
  const item = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (!focused) return;
    setOpen(true);
    item.current?.scrollIntoView({ block: "nearest" });
  }, [focused]);
  const names = a.stations.map((id) => S.byId[id]?.name || id);
  const known = a.stations.filter((id) => S.byId[id]);
  // The alert's own translations exist only for the languages its actions announce in. Without
  // one, the page shows the quoted utterance's ordinary translation: it's for reading, and never
  // on the alert path.
  const said = a.quote && a.segment_id != null ? (S.utts[a.stations[0]] || []).find((u) => u.id === a.segment_id) : undefined;
  const summaryLang = Object.keys(a.summaries || {}).find((l) => sameLang(l, P.lang))
    || Object.keys(said?.translations || {}).find((l) => sameLang(l, P.lang));
  const translations = a.summaries?.[summaryLang || ""] ? a.summaries : said?.translations || {};
  const text = summaryLang ? translations[summaryLang] : a.quote;
  const untranslated = !!a.quote && !summaryLang && !sameLang(a.language, P.lang) && !!P.lang;
  const trouble = a.status === "failed" ? T.notDelivered : a.status === "partial" ? T.partlyDelivered : null;
  // Where the matched words are in the quote: the utterance's own hit spans when it is loaded,
  // else the terms looked up in the quote. A folded alert opens at them when they come late.
  const spans = said && said.text === a.quote ? alertSpans(said.hits || [], a.id, [a.rule, ...also])
    : a.quote ? findTerms(a.quote, a.terms || []) : [];
  const marked = !summaryLang && spans.length > 0;
  const words = () => {
    if (!a.quote) return T.wordsStayThere;
    if (!marked) return withInaudible(text);
    const from = open ? 0 : excerptStart(a.quote, spans);
    const rest = Array.from(a.quote).slice(from).join("");
    const marks = spans.filter(([s]) => s >= from).map(([s, e]) => [s - from, e - from, a.severity] as const);
    return from ? ["… ", ...withMarks(rest, marks)] : withMarks(rest, marks);
  };
  // A model's alert leads with its reason; the quote follows, folded to two lines.
  const reasoned = a.kind === "llm" && !!a.reason;
  // A station rule's alert (silent, traffic) quotes nothing heard: its text is Wavescribe's own,
  // and the later "audio is back" arrives as its reason.
  const noSegment = a.segment_id == null;
  const openLine = () => {
    const st = a.stations[0];
    if (a.segment_id == null) return;
    if (!S.byId[st]) return setCols(known.slice(0, MAX_COLS));
    showLine(st, a.segment_id);
    if (!V.wide) toggleRail(false); // the rail floats over the columns here and would cover it
    setCols(V.shown.includes(st) ? V.shown : V.narrow ? [st] : [...V.shown, st].slice(-MAX_COLS));
  };
  return (
    <li ref={item} className={cx("alert", `sev-${a.severity}`, (fresh || focused) && "is-fresh", open && "is-open")}
        aria-current={focused || undefined}>
      <button type="button" className="alert-sum" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className="alert-line">
          <span className={`sev sev-${a.severity}`}><i aria-hidden="true" />{SEV_WORD[a.severity]}</span>
          {a.checked === false && <span className="alert-unchecked" title={a.reason || T.notCheckedWhy}>{T.notChecked}</span>}
          <span className="alert-rule">{human(a.rule)}{also.length ? ` + ${also.map(human).join(", ")}` : ""}</span>
          <time className="alert-time num" dateTime={isoTime(a.first_at)}>{whenLabel(a.first_at)}</time>
        </span>
        {reasoned && <span className="alert-text">{a.reason}</span>}
        <span className={reasoned ? "alert-said" : "alert-text"} dir="auto" lang={summaryLang || a.language}>{words()}</span>
        {noSegment && !reasoned && a.reason && <span className="alert-text" dir="auto">{a.reason}</span>}
        {!marked && a.terms?.length > 0 && (
          <span className="alert-terms">
            <span className="alert-label">{T.matched}</span>
            {a.terms.map((t) => <mark key={t} className={`kw kw-${a.severity}`} lang={a.language}>{t}</mark>)}
          </span>
        )}
        <span className="alert-meta">
          {names.map((n, i) => <span key={n}>{i > 0 && ", "}<bdi>{n}</bdi></span>)}
          {a.mentions > 1 && ` · ${T.mentions(a.mentions)}`}
          {a.lately && ` · ${T.lately[a.lately]}`}
          {untranslated && ` · ${T.notTranslated(langNameEn(a.language))}`}
          {trouble && <span className="is-trouble"> · {trouble}</span>}
        </span>
      </button>
      {open && (
        <div className="alert-more">
          {summaryLang && (
            <p className="alert-quote" dir="auto" lang={a.language}>
              <span className="alert-label">{T.original} · {langNameEn(a.language)}</span>
              {withInaudible(a.quote)}
            </p>
          )}
          {a.reason && !reasoned && !noSegment && <p className="alert-reason">{a.reason}</p>}
          {a.checked === false && !a.reason && <p className="alert-reason">{T.notCheckedWhy}</p>}
          {!noSegment && (
            <div className="alert-actions">
              {S.hasStore && (
                <Button size="sm" variant="secondary" icon="play" onClick={() => playReplay([a.segment_id!], `${names[0]} ${hm(a.first_at)}`)}>
                  {T.play}
                </Button>
              )}
              {known.length > 0 && (
                <Button size="sm" variant="ghost" icon="right" onClick={openLine}>
                  {T.showInTranscript}
                </Button>
              )}
            </div>
          )}
          {trouble && a.actions && (
            <p className="alert-delivery">
              {Object.entries(a.actions).filter(([k]) => k !== "covered_by").map(([k, v]) => `${k}: ${v}`).join(" · ")}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

/** On phones, the alert a link opened, above its station's column, until it is closed. */
export function FocusedAlert({ station }: { station: string }) {
  useTopics("focus", "alerts");
  const a = V.alert && (S.alerts.get(V.alert.id) ?? V.alert);
  if (!a || !a.stations.includes(station)) return null;
  const close = () => {
    V.alert = null;
    emit("focus");
  };
  return (
    <div className="alerts is-focused">
      <ol className="alert-list" aria-label={T.alerts}>
        <AlertItem a={a} also={[]} fresh={false} />
      </ol>
      <Button size="sm" variant="ghost" icon="close" aria-label={T.closeAlert} onClick={close} />
    </div>
  );
}

/** Announces new alerts to screen readers: critical ones at once, the rest politely. */
export function AlertAnnouncer() {
  useTopics("alerts");
  const [polite, setPolite] = useState("");
  const [urgent, setUrgent] = useState("");
  const seen = useRef(S.rang);
  useEffect(() => {
    const id = S.freshAlert;
    if (id == null || S.rang === seen.current) return;
    seen.current = S.rang;
    const a = S.alerts.get(id);
    if (!a || a.severity === "low") return;
    const where = a.stations.map((s) => S.byId[s]?.name || s).join(", ");
    const msg = `${SEV_WORD[a.severity]} alert${a.checked === false ? `, ${T.notChecked}` : ""}, ${human(a.rule)}, on ${where}`;
    if (a.severity === "critical") setUrgent(msg);
    else setPolite(msg);
    // Where the rail isn't on screen, a new alert also shows for a moment; the bell keeps the count.
    if (!railShown() && !V.narrow) toast(T.newAlert(SEV_WORD[a.severity], human(a.rule), where), a.severity === "critical" || a.severity === "high");
  });
  return (
    <>
      <div className="sr-only" role="status" aria-live="polite">{polite}</div>
      <div className="sr-only" role="alert" aria-live="assertive">{urgent}</div>
    </>
  );
}

export function AlertsRail({ overlay }: { overlay?: boolean }) {
  const box = useRef<HTMLElement>(null);
  // Floating over the columns, it closes on a click outside (the bell toggles it itself).
  useEffect(() => {
    if (!overlay) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Element;
      if (!box.current?.contains(t) && !t.closest(".alerts-toggle, .pop")) toggleRail(false);
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [overlay]);
  return (
    <aside ref={box} className={cx("rail", overlay && "is-overlay")} aria-label={T.alerts}>
      <AlertList />
      <Health />
    </aside>
  );
}

/** One quiet line about the pipeline, for anyone wondering whether it keeps up. */
function Health() {
  useTopics("status");
  const st = S.status;
  if (!st) return null;
  const pct = (x?: number) => `${Math.round((x || 0) * 100)}%`;
  const dropped = (st.dropped?.asr || 0) + (st.dropped?.mt || 0);
  return (
    <p className="health num">
      <Icon name="info" size={12} />
      Speech recognition {pct(st.asr_busy)} busy · translation {pct(st.mt_busy)} · waiting {(st.asr_queue ?? 0) + (st.mt_queue ?? 0)}
      {dropped ? ` · dropped ${dropped}` : ""}
    </p>
  );
}
