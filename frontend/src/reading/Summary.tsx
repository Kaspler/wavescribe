// Summaries on the page: the header's control and its choice of window, and the answer, which
// opens beside the columns (on a phone, as a page of its own) and stays there while new lines
// arrive and while its lines are opened in the transcript. And what one station is talking about
// now, a line under its column's head.
import { useEffect, useId, useRef, useState } from "react";
import { awaySince, looked } from "../lib/away";
import { S } from "../lib/live";
import { go, parse } from "../lib/router";
import { useTopics } from "../lib/store";
import { hm, hms, whenLabel } from "../lib/time";
import { V } from "../lib/view";
import { Icon } from "../components/Icon";
import { Button, Check, LinkButton, cx, useDismiss } from "../components/ui";
import { SEV_WORD, human } from "./Alerts";
import { SourceRow } from "./Ask";
import { type Answer, type Choice, type Failure, type Link, type Point, MAX_MINUTES, REMOTE_MINUTES, SPANS, cutAt, linksOf, saidBy, shown, sinceClock, windowProblem } from "./digest";
import { T } from "./strings";
import { type Job, Now, Sm, askNow, askSummary, cancelSummary, dropNow, openLine, readingStations, showSummary, speakNext } from "./summaries";

const names = (ids: string[]) => ids.map((id) => S.byId[id]?.name || id);

/** Being made: asked, and neither answered nor done cancelling. */
const making = (job: Job | null) => job?.state === "working" || job?.state === "cancelling";

/** What became of a cancelled summary. */
const cancelText = (job: Job) => (job.cancel === "unreached" ? T.cancelled.unreached : T.cancelled.stopped);

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
  if (c.kind === "minutes") return T.lastMinutes(c.minutes);
  return c.kind === "away" ? T.sinceLookedAt(whenLabel(c.at)) : T.sinceLabel(whenLabel(c.at));
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
  const working = making(Sm.job);
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

/** What can be done about summaries being off, by the server's code for why (/api/state's
 * digest_code, or a refusal's): nothing beyond its sentence for a reason the page has no
 * remedy for. */
function offWhat(code: string | null): { text: string; settings: boolean } | null {
  if (code === "remote_off") return { text: T.summaryOffWhat.remote_off, settings: false };
  if (code === "no_llm") return S.manage ? { text: T.summaryOffWhat.no_llm_here, settings: true } : { text: T.summaryOffWhat.no_llm, settings: false };
  if (code === "no_store") return { text: S.manage ? T.summaryOffWhat.no_store_here : T.summaryOffWhat.no_store, settings: false };
  return null;
}

function OffWhat({ code }: { code: string | null }) {
  const what = offWhat(code);
  if (!what) return null;
  return (
    <>
      <p className="sum-cost">{what.text}</p>
      {what.settings && <LinkButton size="sm" variant="secondary" icon="right" href="#/settings/languages">{T.languagesAndModels}</LinkButton>}
    </>
  );
}

/** The window is the only choice; the stations are those being read (or the one whose menu it
 * came from). The last summary asked comes first, whatever became of it, as it may have been
 * answered while closed; another waits until one being made is cancelled. */
export function Chooser({ stations, onDone, one }: { stations: string[]; onDone: () => void; one?: boolean }) {
  useTopics("summary", "state");
  const uid = useId();
  const [time, setTime] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const job = Sm.job;
  const busy = making(job);
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
  const away = awaySince(looked(), Date.now() / 1000, longest);
  return (
    <div className="sum-pick">
      <h2 className="sum-pick-h" dir="auto">{one ? T.summaryOf(list[0] ?? "") : T.summary}</h2>
      <p className="sum-pick-what">{T.summaryWhat}</p>
      {job && (
        <div className="sum-pick-job">
          <p>
            {busy ? <>{T.writing} · {choiceLabel(job.choice)} <Elapsed since={job.asked} /></>
              : T.lastSummary(choiceLabel(job.choice), hm(job.asked / 1000))}
          </p>
          <span className="sum-pick-acts">
            <Button size="sm" variant="ghost" onClick={() => { onDone(); reveal(); }}>{T.show}</Button>
            {busy && <CancelButton job={job} />}
          </span>
        </div>
      )}
      {S.digest ? (
        <div className="sum-off">
          <p className="sum-said">{T.summaryOff(S.digest)}</p>
          <OffWhat code={S.digestCode} />
        </div>
      ) : !stations.length ? (
        <p className="sum-pick-for">{T.summaryNoStation}</p>
      ) : (
        <>
          {!one && <p className="sum-pick-for" dir="auto">{T.summaryFor(list)}</p>}
          <div className="sum-pick-list" role="group" aria-label={T.summaryWindow}>
            {away != null && (
              <button type="button" disabled={busy} onClick={() => ask({ kind: "away", at: away })}>
                <span className="sum-pick-two">
                  <span>{T.sinceLooked} <span className="num sum-pick-at">{whenLabel(away)}</span></span>
                  <span className="sum-pick-sub">{T.sinceLookedWhat}</span>
                </span>
                <Icon name="right" />
              </button>
            )}
            {SPANS.map((m) => (
              <button key={m} type="button" disabled={busy} onClick={() => ask({ kind: "minutes", minutes: m })}>
                {T.lastMinutes(m)}<Icon name="right" />
              </button>
            ))}
            <form className="sum-pick-since" onSubmit={since}>
              <label htmlFor={`${uid}-t`}>{T.sinceTime}</label>
              <input id={`${uid}-t`} className="input num" type="time" name="since" autoComplete="off" value={time} disabled={busy} required
                     onChange={(e) => { setTime(e.target.value); setProblem(null); }}
                     aria-invalid={problem ? true : undefined} aria-describedby={problem ? `${uid}-p` : undefined} />
              <Button type="submit" size="sm" variant="secondary" disabled={busy || !time}>{T.summarise}</Button>
            </form>
          </div>
          {problem && <p className="sum-pick-why" id={`${uid}-p`} role="alert">{problem}</p>}
          {S.canControl && <Check checked={Sm.speak} disabled={busy} onChange={speakNext}>{T.readAloud}</Check>}
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
        <p className="sum-meta sum-stations" dir="auto">{(a ? a.stations.map((id) => a.names[id] || id) : names(job.stations)).join(", ")}</p>
      </header>
      <div className="sum-body" aria-busy={making(job)}>
        <Body job={job} />
      </div>
      <p className="sr-only" role="status">{said(job)}</p>
    </section>
  );
}

/** What screen readers are told once the state changes; the ticking time isn't read out. */
function said(job: Job): string {
  if (job.state === "working") return T.writing;
  if (job.state === "cancelling") return T.cancelling;
  if (job.state === "cancelled") return cancelText(job);
  if (job.state === "failed") return job.failure?.text || "";
  if (!job.answer) return "";
  const v = shown(job.answer);
  return v.points.length ? T.summaryReady(v.points.length) : v.sentences || "";
}

/** Cancel, which stays in place, unavailable, while the server is being told. */
function CancelButton({ job }: { job: Job }) {
  const going = job.state === "cancelling";
  return (
    <Button size="sm" variant="secondary" disabled={going} onClick={cancelSummary}>
      {going ? T.cancelling : T.cancel}
    </Button>
  );
}

function Body({ job }: { job: Job }) {
  if (making(job)) {
    return (
      <div className="sum-working">
        <p className="sum-working-line">{T.writing} <Elapsed since={job.asked} /></p>
        <p className="sum-cost">{T.writingCost}</p>
        <CancelButton job={job} />
      </div>
    );
  }
  if (job.state === "cancelled") return <p className="sum-said">{cancelText(job)}</p>;
  if (job.state === "failed" && job.failure) return <Failed f={job.failure} again={() => askSummary(job.choice, job.stations)} />;
  if (!job.answer) return null;
  return (
    <>
      {job.cancel === "late" && <p className="sum-speech">{T.cancelledLate}</p>}
      <AnswerView a={job.answer} took={job.took} speech={job.speak ? <Speech job={job} /> : null} />
    </>
  );
}

/** Whether the summary is being read aloud on this machine, was, or was cut short by an alert's
 * voice; or the server's reason it isn't read. */
function Speech({ job }: { job: Job }) {
  useTopics("alerts");
  const [, tick] = useState(0);
  const sp = job.spoken;
  const ends = sp ? sp.at + sp.seconds : 0;
  useEffect(() => {
    const left = ends * 1000 - Date.now();
    if (left <= 0) return;
    const t = setTimeout(() => tick((n) => n + 1), left + 500);
    return () => clearTimeout(t);
  }, [ends]);
  const why = job.answer?.speech?.error;
  if (!sp) return why ? <p className="sum-speech" dir="auto">{T.notReadAloud(why)}</p> : null;
  const cut = cutAt(sp, S.alerts.values());
  const text = cut != null ? T.readAloudCut(hms(cut))
    : Date.now() / 1000 < ends ? T.readingAloud(sp.seconds) : T.readAloudAt(hm(sp.at));
  return <p className="sum-speech" role="status">{text}</p>;
}

/** The server's sentence as it is (a window too long in the page's words, with how long one may
 * be), then what can be done about it. */
function Failed({ f, again }: { f: Failure; again: () => void }) {
  return (
    <div className="sum-failed">
      <p className="sum-said" dir="auto">{f.maxMinutes ? T.windowFar(f.maxMinutes / 60, S.canControl) : f.text}</p>
      {f.kind === "off_here" && <p className="sum-cost">{T.summaryOffWhat.remote_off}</p>}
      {f.kind === "unavailable" && <OffWhat code={f.code} />}
      {f.retry && <Again f={f} again={again} variant="secondary" />}
    </div>
  );
}

/** Try again; while the server is busy, not before the time it gave, which the button names. */
function Again({ f, again, variant }: { f: Failure; again: () => void; variant: "secondary" | "ghost" }) {
  const [, tick] = useState(0);
  const wait = f.retryAt != null ? f.retryAt * 1000 - Date.now() : 0;
  useEffect(() => {
    if (wait <= 0) return;
    const t = setTimeout(() => tick((n) => n + 1), wait + 100);
    return () => clearTimeout(t);
  }, [wait > 0]);
  return (
    <Button size="sm" variant={variant} icon="refresh" disabled={wait > 0} onClick={again}>
      {wait > 0 ? T.tryAgainAt(hm(f.retryAt)) : T.tryAgain}
    </Button>
  );
}

function AnswerView({ a, took, speech }: { a: Answer; took: number | null; speech: React.ReactNode }) {
  const v = shown(a);
  const uid = useId();
  const foot = [took != null ? T.took(Math.max(1, Math.round(took / 1000))) : null, a.model].filter(Boolean).join(" · ");
  return (
    <>
      {v.notice && <p className="sum-notice">{v.notice}</p>}
      {speech}
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
  return (
    <li className="sum-point">
      {by && <p className="sum-who" dir="auto" title={by}>{by}</p>}
      <p className="sum-text" dir="auto" lang={a.language}>{p.text}</p>
      <Lines links={linksOf(p, a)} />
    </li>
  );
}

/** The times of the lines a point rests on, each opening its line in the transcript. */
function Lines({ links }: { links: Link[] }) {
  if (!links.length) return null;
  // Two lines in the same minute are told apart by their seconds.
  const minutes = links.map((l) => whenLabel(l.t));
  const label = new Set(minutes).size < minutes.length ? hms : whenLabel;
  return (
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
  );
}

/** What a station is talking about now: under its column's head, so the transcript keeps its
 * place, until put away. */
export function NowLine({ id }: { id: string }) {
  useTopics(`now:${id}`, "prefs");
  const job = Now[id];
  if (!job) return null;
  const a = job.answer;
  const v = a ? shown(a) : null;
  const close = <Button size="sm" variant="ghost" icon="close" aria-label={T.nowClose} title={T.nowClose} onClick={() => dropNow(id)} />;
  return (
    <div className="col-now" aria-busy={job.state === "working"}>
      {job.state === "working" ? (
        <div className="col-now-row">
          <p className="col-now-wait">{T.nowWorking} <Elapsed since={job.asked} /></p>
          <Button size="sm" variant="ghost" onClick={() => dropNow(id)}>{T.cancel}</Button>
        </div>
      ) : job.state === "failed" && job.failure ? (
        <div className="col-now-row">
          <p className="col-now-said" dir="auto">{job.failure.text}</p>
          {job.failure.retry && <Again f={job.failure} again={() => askNow(id)} variant="ghost" />}
          {close}
        </div>
      ) : a && v ? (
        <>
          <div className="col-now-row">
            <div className="col-now-body">
              {v.points.length ? v.points.map((p, i) => (
                <div key={i} className="col-now-point">
                  <p className="col-now-text" dir="auto" lang={a.language}>{p.text}</p>
                  <Lines links={linksOf(p, a)} />
                </div>
              )) : <p className="col-now-said">{v.sentences}</p>}
            </div>
            {close}
          </div>
          {v.notice && <p className="col-now-notice">{v.notice}</p>}
        </>
      ) : null}
      <p className="sr-only" role="status">
        {job.state === "working" ? T.nowWorking : job.state === "failed" ? job.failure?.text : v ? (v.points[0]?.text ?? v.sentences) : ""}
      </p>
    </div>
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
