// Summaries on the page: the header's control and its choice of window, and the answer, which
// opens beside the columns (on a phone, as a page of its own) and stays there while new lines
// arrive and while its lines are opened in the transcript.
import { useEffect, useId, useRef, useState } from "react";
import { S } from "../lib/live";
import { go, parse } from "../lib/router";
import { useTopics } from "../lib/store";
import { hm, hms, whenLabel } from "../lib/time";
import { V } from "../lib/view";
import { Icon } from "../components/Icon";
import { Button, LinkButton, cx, useDismiss } from "../components/ui";
import { SEV_WORD, human } from "./Alerts";
import { SourceRow } from "./Ask";
import { type Answer, type Choice, type Failure, type Point, MAX_MINUTES, REMOTE_MINUTES, SPANS, linksOf, saidBy, shown, sinceClock, windowProblem } from "./digest";
import { T } from "./strings";
import { type Job, Sm, askSummary, cancelSummary, openLine, readingStations, showSummary } from "./summaries";

const names = (ids: string[]) => ids.map((id) => S.byId[id]?.name || id);

/** Minutes and seconds since `since` (ms), ticking; screen readers are told once instead. */
function Elapsed({ since }: { since: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const s = Math.max(0, Math.floor((Date.now() - since) / 1000));
  return <span className="num sum-elapsed" aria-hidden="true">{Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")}</span>;
}

function choiceLabel(c: Choice): string {
  return c.kind === "minutes" ? T.lastMinutes(c.minutes) : T.sinceLabel(whenLabel(c.at));
}

/** Shows the answer: beside the columns, or on a phone as its own page. */
function reveal() {
  if (V.narrow) go("#/summary");
  else {
    const kind = parse(location.hash).kind;
    if (kind !== "live" && kind !== "cols") go("#/");
  }
  showSummary(true);
  requestAnimationFrame(() => document.getElementById("sum-h")?.focus({ preventScroll: true }));
}

/** The header's control: always in the same place, for the stations being read. */
export function SummaryButton() {
  useTopics("summary");
  const [open, setOpen] = useState(false);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  useDismiss(open, () => setOpen(false), [btn, pop]);
  const working = Sm.job?.state === "working";
  return (
    <span className="menu-anchor">
      <Button ref={btn} variant="ghost" icon="summary" className="hdr-sum" title={T.summaryHint}
              aria-label={T.summary} aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen(!open)}>
        <span className="hdr-sum-label">{T.summary}</span>
        {working && <Elapsed since={Sm.job!.asked} />}
      </Button>
      {open && (
        <div ref={pop} className="pop sum-pop" role="dialog" aria-label={T.summary}>
          <Chooser stations={readingStations()} onDone={() => setOpen(false)} />
        </div>
      )}
    </span>
  );
}

/** What can be done about summaries being off, by Ask's code for the same gate. */
function offWhat(): { text: string; settings: boolean } | null {
  const code = S.ask ? S.askCode : null;
  if (code === "remote_off") return { text: T.summaryOffWhat.remote_off, settings: false };
  if (code === "no_llm") return S.manage ? { text: T.summaryOffWhat.no_llm_here, settings: true } : { text: T.summaryOffWhat.no_llm, settings: false };
  return null;
}

function OffWhat() {
  const what = offWhat();
  if (!what) return null;
  return (
    <>
      <p className="sum-cost">{what.text}</p>
      {what.settings && <LinkButton size="sm" variant="secondary" icon="right" href="#/settings/languages">{T.languagesAndModels}</LinkButton>}
    </>
  );
}

/** The window is the only choice; the stations are those being read (or the one whose menu it
 * came from). A summary being made is shown first, and another waits until it is cancelled. */
export function Chooser({ stations, onDone, one }: { stations: string[]; onDone: () => void; one?: boolean }) {
  useTopics("summary", "state");
  const uid = useId();
  const [time, setTime] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const job = Sm.job;
  const busy = job?.state === "working";
  const longest = S.canControl ? MAX_MINUTES : REMOTE_MINUTES;
  const ask = (choice: Choice) => {
    onDone();
    askSummary(choice, stations);
    reveal();
  };
  const since = (e: React.FormEvent) => {
    e.preventDefault();
    const at = sinceClock(time, new Date());
    if (at == null) return;
    const why = windowProblem(at, Date.now() / 1000, longest);
    if (why) setProblem(why === "soon" ? T.windowSoon : T.windowFar(longest / 60, S.canControl));
    else ask({ kind: "since", at });
  };
  const list = names(stations);
  return (
    <div className="sum-pick">
      <h2 className="sum-pick-h" dir="auto">{one ? T.summaryOf(list[0] ?? "") : T.summary}</h2>
      <p className="sum-pick-what">{T.summaryWhat}</p>
      {job && (job.state === "working" || job.state === "done") && (
        <div className="sum-pick-job">
          <p>
            {busy ? <>{T.writing} · {choiceLabel(job.choice)} <Elapsed since={job.asked} /></>
              : T.lastSummary(choiceLabel(job.choice), hm(job.asked / 1000))}
          </p>
          <span className="sum-pick-acts">
            <Button size="sm" variant="ghost" onClick={() => { onDone(); reveal(); }}>{T.show}</Button>
            {busy && <Button size="sm" variant="secondary" onClick={cancelSummary}>{T.cancel}</Button>}
          </span>
        </div>
      )}
      {S.digest ? (
        <div className="sum-off">
          <p className="sum-said">{T.summaryOff(S.digest)}</p>
          <OffWhat />
        </div>
      ) : !stations.length ? (
        <p className="sum-pick-for">{T.summaryNoStation}</p>
      ) : (
        <>
          {!one && <p className="sum-pick-for" dir="auto">{T.summaryFor(list)}</p>}
          <div className="sum-pick-list" role="group" aria-label={T.summaryWindow}>
            {SPANS.map((m) => (
              <button key={m} type="button" disabled={busy} onClick={() => ask({ kind: "minutes", minutes: m })}>
                {T.lastMinutes(m)}<Icon name="right" />
              </button>
            ))}
            <form className="sum-pick-since" onSubmit={since}>
              <label htmlFor={`${uid}-t`}>{T.sinceTime}</label>
              <input id={`${uid}-t`} className="input num" type="time" value={time} disabled={busy} required
                     onChange={(e) => { setTime(e.target.value); setProblem(null); }}
                     aria-invalid={problem ? true : undefined} aria-describedby={problem ? `${uid}-p` : undefined} />
              <Button type="submit" size="sm" variant="secondary" disabled={busy || !time}>{T.summarise}</Button>
            </form>
          </div>
          {problem && <p className="sum-pick-why" id={`${uid}-p`} role="alert">{problem}</p>}
          {busy && <p className="sum-pick-why">{T.oneAtATime}</p>}
        </>
      )}
    </div>
  );
}

/** The answer, beside the columns, or alone on a phone. */
export function SummaryPanel({ phone }: { phone?: boolean }) {
  useTopics("summary", "state", "prefs");
  const job = Sm.job;
  if (!job) {
    // A phone opened at #/summary before anything was asked: the choice, as the page.
    return phone ? (
      <section className="sum is-phone" aria-label={T.summary}>
        <div className="sum-body"><Chooser stations={readingStations()} onDone={() => {}} /></div>
      </section>
    ) : null;
  }
  const close = () => {
    showSummary(false);
    if (phone) go("#/");
    else document.querySelector<HTMLElement>(".hdr-sum")?.focus();
  };
  const a = job.answer;
  return (
    <section className={cx("sum", phone && "is-phone")} aria-labelledby="sum-h"
             onKeyDown={(e) => { if (e.key === "Escape" && !phone && !e.defaultPrevented) { e.stopPropagation(); close(); } }}>
      <header className="sum-head">
        <div className="sum-title">
          <h2 id="sum-h" tabIndex={-1}>{T.summary}</h2>
          <Button size="sm" variant="ghost" icon="close" aria-label={T.closeSummary} title={T.closeSummary} onClick={close} />
        </div>
        <p className="sum-meta">
          <span>{choiceLabel(job.choice)}</span>
          {a && <span className="num">{whenLabel(a.window.since)}–{hm(a.window.until)}</span>}
        </p>
        <p className="sum-meta sum-stations" dir="auto">{names(job.stations).join(", ")}</p>
      </header>
      <div className="sum-body" aria-busy={job.state === "working"}>
        <Body job={job} />
      </div>
      <p className="sr-only" role="status">{said(job)}</p>
    </section>
  );
}

/** What screen readers are told once the state changes; the ticking time isn't read out. */
function said(job: Job): string {
  if (job.state === "working") return T.writing;
  if (job.state === "cancelled") return T.cancelled;
  if (job.state === "failed") return job.failure?.text || "";
  if (!job.answer) return "";
  const v = shown(job.answer);
  return v.points.length ? T.summaryReady(v.points.length) : v.sentences || "";
}

function Body({ job }: { job: Job }) {
  if (job.state === "working") {
    return (
      <div className="sum-working">
        <p className="sum-working-line">{T.writing} <Elapsed since={job.asked} /></p>
        <p className="sum-cost">{T.writingCost}</p>
        <Button size="sm" variant="secondary" onClick={cancelSummary}>{T.cancel}</Button>
      </div>
    );
  }
  if (job.state === "cancelled") return <p className="sum-said">{T.cancelled}</p>;
  if (job.state === "failed" && job.failure) return <Failed f={job.failure} again={() => askSummary(job.choice, job.stations)} />;
  return job.answer ? <AnswerView a={job.answer} took={job.took} /> : null;
}

/** The server's sentence as it is, then what can be done about it. */
function Failed({ f, again }: { f: Failure; again: () => void }) {
  return (
    <div className="sum-failed">
      <p className="sum-said" dir="auto">{f.text}</p>
      {f.kind === "off_here" && <p className="sum-cost">{T.summaryOffWhat.remote_off}</p>}
      {f.kind === "unavailable" && <OffWhat />}
      {f.retry && <Button size="sm" variant="secondary" icon="refresh" onClick={again}>{T.tryAgain}</Button>}
    </div>
  );
}

function AnswerView({ a, took }: { a: Answer; took: number | null }) {
  const v = shown(a);
  const uid = useId();
  const foot = [took != null ? T.took(Math.max(1, Math.round(took / 1000))) : null, a.model].filter(Boolean).join(" · ");
  return (
    <>
      {v.notice && <p className="sum-notice">{v.notice}</p>}
      {a.alerts.length > 0 && <WindowAlerts a={a} />}
      {v.points.length ? (
        <ol className="sum-points">{v.points.map((p, i) => <PointView key={i} p={p} a={a} />)}</ol>
      ) : (
        v.sentences && <p className="sum-said sum-outcome">{v.sentences}</p>
      )}
      {v.unread.length > 0 && <p className="sum-unread" dir="auto">{T.notRead(v.unread.map(([n, why]) => `${n} (${why})`).join(", "))}</p>}
      {a.sources.length > 0 && (
        <details className="turn-srcs sum-srcs">
          <summary>{T.linesItRests(a.sources.length)}</summary>
          <ol>{a.sources.map((s) => <SourceRow key={s.n} s={s} id={`${uid}-src-${s.n}`} flash={false} open={openLine} />)}</ol>
        </details>
      )}
      {foot && <p className="sum-foot num">{foot}</p>}
    </>
  );
}

function PointView({ p, a }: { p: Point; a: Answer }) {
  const by = saidBy(p, a);
  const links = linksOf(p, a);
  // Two lines in the same minute are told apart by their seconds.
  const minutes = links.map((l) => whenLabel(l.t));
  const label = new Set(minutes).size < minutes.length ? hms : whenLabel;
  return (
    <li className="sum-point">
      {by && <p className="sum-who" dir="auto">{by}</p>}
      <p className="sum-text" dir="auto" lang={a.language}>{p.text}</p>
      {links.length > 0 && (
        <p className="sum-lines">
          <span className="sum-from">{T.from}</span>
          {links.map((l) => {
            const who = l.who || S.byId[l.station]?.name || l.station;
            const at = label(l.t);
            return Object.hasOwn(S.byId, l.station) ? (
              <button key={l.n} type="button" className="sum-line num" onClick={() => openLine(l.station, l.id)}
                      aria-label={T.lineAt(who, at)} title={T.showInTranscript}>
                {l.who ? `${l.who} ${at}` : at}
              </button>
            ) : (
              <span key={l.n} className="sum-line is-static num">{l.who ? `${l.who} ${at}` : at}</span>
            );
          })}
        </p>
      )}
    </li>
  );
}

/** The alerts announced in the window, each once and quietly: the alerts list has them in full,
 * and a summary must not look like one. */
function WindowAlerts({ a }: { a: Answer }) {
  return (
    <div className="sum-alerts">
      <p className="sum-label">{T.alertsInWindow}</p>
      <ul>
        {a.alerts.map((al) => {
          const lang = Object.keys(al.summaries || {}).find((l) => l.split("-")[0] === a.language.split("-")[0]);
          const words = (lang && al.summaries?.[lang]) || al.quote || "";
          const where = (al.names?.length ? al.names : names(al.stations)).join(", ");
          const sev = SEV_WORD[al.severity as keyof typeof SEV_WORD] || al.severity;
          const head = <span className="num">{hm(al.announced_at)} · {sev} · {human(al.rule)} · <span dir="auto">{where}</span></span>;
          const body = words && <span className="sum-alert-words" dir="auto">{words}</span>;
          const station = al.stations[0];
          return (
            <li key={al.id}>
              {al.segment_id != null && station && Object.hasOwn(S.byId, station) ? (
                <button type="button" className="sum-alert" onClick={() => openLine(station, al.segment_id!)} title={T.showInTranscript}>{head}{body}</button>
              ) : (
                <p className="sum-alert">{head}{body}</p>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
