// Watching a band (src/wavescribe/radio/watch.py): a receiver listens to every channel of a band
// at once and writes down whatever is said, with its channel. "Caught here" lists what it
// caught by frequency, newest first, with what the person noted about each frequency.
import { type ReactNode, useEffect, useRef, useState } from "react";
import { fetchRead } from "../lib/auth";
import { sameLang, langNameEn } from "../lib/lang";
import { P } from "../lib/prefs";
import { isoTime, whenLabel } from "../lib/time";
import { ActButton, Button, Check, Field, Input, Section, Segmented, Select, Tag, Textarea, act, toast } from "../components/ui";
import { addedToast } from "./added";
import { type Any, ApiError, docUrl, get, listenTo, post, secs, useEvery, useLoad } from "./api";
import { type Band, and, bandWords, fmtMhz } from "./bands";
import { type Catch, type Note, RANGES, type Spot, bandsIn, filterCatches, groupCatches, voiceWord, windowWords } from "./catches";
import { Refusal } from "./Refusal";
import { type Refused, refused } from "./refusals";
import { rxName } from "./Receivers";

let known: Promise<boolean> | null = null;

/** Whether this Wavescribe watches bands: one that doesn't answers 404 for its catches. */
function watchesHere(): Promise<boolean> {
  known ||= fetchRead("/api/catches?hours=1&limit=1").then((r) => r.status !== 404, () => {
    known = null; // not an answer: ask again next time
    return false;
  });
  return known;
}

export function useWatchesHere(): boolean {
  const [on, setOn] = useState(false);
  useEffect(() => {
    let live = true;
    watchesHere().then((x) => live && setOn(x));
    return () => { live = false; };
  }, []);
  return on;
}

async function loadCatches(hours: number): Promise<Catch[]> {
  const r = await fetchRead(`/api/catches?hours=${hours}&limit=500`);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body.catches || [];
}

/** A band's everyday name ("Walkie-talkies"), from the radio view's band plan. */
export const bandNamer = (bands: Band[]) => (id: string) => {
  const b = bands.find((x) => x.id === id);
  return b ? bandWords(b).name : null;
};

/** What a watch listens to, in words. */
export const watchWords = (w: Any, bands: Band[]) => windowWords(w.watch, bandNamer(bands));

const WHAT_IT_DOES = "Listens to every channel of the band at once and turns whatever is said into text, with its channel. One receiver is busy with it while it runs.";
// Said once, where what was caught is listed (Caught here, just below the watch panel).
const CAN_MISS = "It can miss a transmission that is weak, very short, or under another one.";

/** Whether two watch windows are the same: a band's id, or a range in MHz. */
const sameWindow = (a: string | number[], b: string | number[]) =>
  typeof a === "string" || typeof b === "string" ? a === b : Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6;

/** A watch asked for: on which receiver, and the band or the slice of it. */
interface Want { dev: string; spec: string | [number, number] }

/** The second action on a band whose channels talk now and then: watch it with a receiver.
 * Asks for the confirmation the kind of radio needs, and lets a receiver tuned by hand go. The
 * server makes the watch station and starts it, beside the stations the receiver hears: it moves
 * a receiver that doesn't hear the band, and asks first when that leaves stations out. A watch
 * made before for this band (or slice) and receiver is started again. A refusal is said in words. */
export function WatchBand({ band, device, holders, byHand, letGo, liveClosed, free, others, needsAck, watches, bands, onDone, onCancel }: {
  band: Band;
  device: string;
  /** Running stations on the receiver, by name. */
  holders: string[];
  /** The receiver is tuned by hand: listening that way ends first (`letGo`). */
  byHand: boolean;
  letGo: (dev: string) => Promise<unknown>;
  /** The server ended listening by hand on `dev` to make the watch. */
  liveClosed: (dev: string) => void;
  /** An idle receiver to offer instead. */
  free: string | null;
  /** Every other receiver, for when this one can't watch. */
  others: string[];
  needsAck: boolean;
  watches: Any[];
  bands: Band[];
  onDone: () => Promise<void>;
  onCancel: () => void;
}) {
  const [ack, setAck] = useState(false);
  // The kind of radio to confirm: the band's, or the one the server names when it asks.
  const [ackFor, setAckFor] = useState<string | null>(needsAck ? band.privacy : null);
  const [trouble, setTrouble] = useState<Refused | null>(null);
  // The server's question before a move leaves stations out; nothing has changed while it is asked.
  const [asked, setAsked] = useState<(Want & { question: string; stops: string[] }) | null>(null);
  const box = useRef<HTMLDivElement>(null);
  // The panel replaces the button that opened it: the focus moves into it.
  useEffect(() => { box.current?.querySelector<HTMLElement>("input, button")?.focus(); }, []);
  // The question's answers, or the slice offered, replace the buttons, and the other way round:
  // the focus goes along.
  const actions = useRef<HTMLDivElement>(null);
  const opened = useRef(false);
  const sliced = !!trouble?.slice;
  useEffect(() => {
    if (opened.current) (sliced ? box.current?.querySelector<HTMLElement>(".m-trouble button") : actions.current?.querySelector<HTMLElement>("button"))?.focus();
    opened.current = true;
  }, [!!asked, sliced]);
  const name = bandWords(band).name;
  const start = async ({ dev, spec }: Want, confirmed = false) => {
    setTrouble(null);
    setAsked(null);
    const mine = watches.find((w) => w.device === dev && sameWindow(w.watch, spec));
    let made: Any = null;
    try {
      if (dev === device && byHand) await letGo(dev);
      if (mine) await post(`/stations/${mine.id}/start`);
      else {
        made = await post("/stations", {
          name: typeof spec === "string" ? name : `${name} ${spec[0]}–${spec[1]}`, device: dev, watch: spec, language: "auto", start: true,
          retune: confirmed, ...(ackFor && ack ? { acknowledge: ackFor } : {}),
        });
      }
    } catch (e) {
      const no = e instanceof ApiError ? refused(e.message, e.data) : refused((e as Error).message);
      if (no.code === "retune" && !confirmed) { setAsked({ dev, spec, question: no.text, stops: no.stops }); return; }
      if (no.privacy && no.privacy !== ackFor) { setAckFor(no.privacy); setAck(false); }
      setTrouble(no);
      return;
    }
    if (made?.live_closed) liveClosed(dev);
    const ok = `Watching ${typeof spec === "string" ? name : windowWords(spec, bandNamer(bands))} with Receiver ${rxName(dev)}`;
    const said = made ? addedToast(made, ok) : { text: ok, bad: false };
    toast(said.text, said.bad);
    await onDone();
  };
  const other = free || others[0] || null;
  const width = +(band.stop_mhz - band.start_mhz).toFixed(1);
  const busy = byHand || holders.length > 0;
  const unconfirmed = !!ackFor && !ack;
  return (
    <div ref={box} className="m-panel rd-retune" role="group" aria-labelledby="rd-watch-h"
         onKeyDown={(e) => {
           // Escape closes the panel; the page's own Escape (back to reading) must not run too.
           if (e.key === "Escape") { e.stopPropagation(); onCancel(); }
         }}>
      <p id="rd-watch-h" className="m-line">{WHAT_IT_DOES}</p>
      {busy && (
        <p className="m-line">
          Receiver {rxName(device)} is {and([...(byHand ? ["tuned by hand"] : []), ...(holders.length ? [`following ${and(holders)}`] : [])])}.
          {byHand && " Watching with it ends listening by hand."}
          {holders.length > 0 && ` ${holders.length === 1 ? "It keeps running" : "They keep running"} beside the watch; if the receiver has to move away from ${holders.length === 1 ? "it" : "them"}, you're asked first.`}
        </p>
      )}
      {ackFor && (
        <p className="m-line">
          <Check checked={ack} onChange={setAck}>
            I've checked that I may receive this where I am, and I'm responsible for how I use what's heard.
            Laws on radio that isn't broadcast differ by country (<a className="rd-link" href={docUrl("RADIO.md")} target="_blank" rel="noreferrer">docs/RADIO.md</a>).
          </Check>
        </p>
      )}
      {asked && <p className="m-line" role="status">{asked.question}</p>}
      {trouble && (
        trouble.slice ? (
          <Refusal raw={trouble.text} said={`${name} is ${width} MHz wide, more than one receiver hears at once. Watch a slice of it instead:`}>
            <ActButton size="sm" variant="secondary" disabled={unconfirmed} run={() => start({ dev: device, spec: trouble.slice! })}>
              Watch {trouble.slice[0].toFixed(3)}–{trouble.slice[1].toFixed(3)} MHz
            </ActButton>
          </Refusal>
        ) : (
          <Refusal raw={trouble.text}>
            {other && trouble.code !== "acknowledge" && (
              <ActButton size="sm" variant="secondary" disabled={unconfirmed} run={() => start({ dev: other, spec: band.id })}>Use Receiver {rxName(other)}</ActButton>
            )}
          </Refusal>
        )
      )}
      <div ref={actions} className="m-actions">
        {asked ? (
          <>
            <ActButton variant="primary" icon="antenna" run={() => start(asked, true)}>
              {asked.stops.length ? `Stop ${and(asked.stops)} and watch` : "Move it and watch"}
            </ActButton>
            <Button variant="ghost" onClick={() => setAsked(null)}>Keep it as it is</Button>
          </>
        ) : (
          <>
            {!trouble?.slice && (
              <ActButton variant="primary" icon="antenna" disabled={unconfirmed} run={() => start({ dev: device, spec: band.id })}>
                {byHand ? "Let it go and watch" : "Start watching"}
              </ActButton>
            )}
            {free && busy && (
              <ActButton variant="secondary" disabled={unconfirmed} run={() => start({ dev: free, spec: band.id })}>Watch with Receiver {rxName(free)}, which is free</ActButton>
            )}
            <Button variant="ghost" onClick={onCancel}>Cancel</Button>
          </>
        )}
      </div>
    </div>
  );
}

/** Find, On the air: what watches caught. `keptNothing` says why some of it isn't listed. */
export function Caught({ bands, watches, keptNothing, onAdd, onChooseBand }: {
  bands: Band[];
  watches: Any[];
  keptNothing: ReactNode;
  onAdd: (t: Any) => void;
  /** Shows a band to watch on the dial, for a first watch. */
  onChooseBand: () => void;
}) {
  const [hours, setHours] = useState(24);
  const [band, setBand] = useState("");
  const [speech, setSpeech] = useState(false);
  const { data, error, reload } = useLoad(() => loadCatches(hours), [hours]);
  const { data: notes, reload: reloadNotes } = useLoad(() => get<{ notes: Note[] }>("/radio/notes").then((x) => x.notes).catch(() => [] as Note[]));
  useEvery(10000, reload);
  const all = data || [];
  const name = bandNamer(bands);
  const spots = groupCatches(filterCatches(all, { band: band || null, speech, hours }, Date.now() / 1000), notes || []);
  const running = watches.filter((w) => w.running);
  const range = RANGES.find(([h]) => h === hours)?.[1].toLowerCase() || "";
  return (
    <Section id="rd-caught" title="Caught here"
             description={`What a watch heard on channels that are usually silent, newest first, by frequency. ${CAN_MISS}`}>
      {keptNothing && <div className="m-note" role="status"><p>{keptNothing}</p></div>}
      {(all.length > 0 || watches.length > 0) && (
        <div className="m-row cg-filters">
          <Segmented label="How far back" value={String(hours)} onChange={(x) => setHours(+x)} options={RANGES.map(([h, l]) => ({ value: String(h), label: l }))} />
          {bandsIn(all).length > 1 && (
            <Select aria-label="Band" value={band} onChange={(e) => setBand(e.target.value)}
                    options={[["", "Every band"], ...bandsIn(all).map((id) => [id, name(id) || id] as [string, string])]} />
          )}
          <Check checked={speech} onChange={setSpeech}>With speech only</Check>
        </div>
      )}
      {!data ? (
        <p className="m-loading">{error ? `Couldn't load what was caught: ${error}` : "Loading…"}</p>
      ) : spots.length ? (
        <ul className="m-list cg-list">
          {spots.map((s) => <SpotRow key={s.freq_hz} s={s} band={s.band ? name(s.band) : null} onAdd={onAdd} onNoted={reloadNotes} />)}
        </ul>
      ) : watches.length || all.length ? (
        <p className="m-empty">
          Nothing caught in the {range}{band || speech ? " that the filters let through" : ""}.
          {running.length > 0 && ` ${and(running.map((w) => watchWords(w, bands)))} ${running.length === 1 ? "is" : "are"} being watched.`}
        </p>
      ) : (
        <div className="m-empty">
          <p>
            Nothing caught yet. A watch listens to every channel of a band at once (walkie-talkies, radio amateurs, boats,
            aircraft) and turns whatever is said into text, with its channel, so a channel that is usually silent shows here
            the moment someone speaks on it.
          </p>
          <div className="m-actions">
            <Button variant="secondary" icon="antenna" onClick={onChooseBand}>Choose a band to watch</Button>
          </div>
        </div>
      )}
    </Section>
  );
}

const FIRST = 3;

function SpotRow({ s, band, onAdd, onNoted }: { s: Spot; band: string | null; onAdd: (t: Any) => void; onNoted: () => Promise<void> }) {
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState(false);
  const edit = useRef<HTMLButtonElement>(null);
  const closed = useRef(false);
  // The form closes back onto the button that opened it.
  useEffect(() => { if (!editing && closed.current) edit.current?.focus(); closed.current = editing; }, [editing]);
  const f = fmtMhz(s.mhz, s.mode);
  const n = s.catches.length;
  const shown = all ? s.catches : s.catches.slice(0, FIRST);
  const noted = !!(s.name || s.note || s.tags.length);
  return (
    <li className="m-item cg-spot">
      <div className="m-item-head">
        <strong className="mono">{f} MHz</strong>
        {s.name && <strong dir="auto">{s.name}</strong>}
        {s.label && <span className="m-muted">{s.label}</span>}
        {s.tags.map((t) => <Tag key={t}>{t}</Tag>)}
        <span className="m-spacer" />
        <Button size="sm" variant="ghost" icon="speaker" aria-label={`Listen there, ${f} MHz`} onClick={() => listenTo(s.mhz, s.mode)}>Listen there</Button>
        <Button size="sm" icon="plus" aria-label={`Add ${f} MHz as a station`}
                onClick={() => onAdd({ mhz: s.mhz, mode: s.mode, name: s.name, privacy: s.privacy, t: Date.now() })}>
          Add as a station
        </Button>
      </div>
      <p className="m-item-sub">{[band, `caught ${n === 1 ? "once" : `${n} times`}`, `last ${whenLabel(s.last)}`].filter(Boolean).join(" · ")}</p>
      {editing ? (
        <NoteForm s={s} f={f} onDone={async (saved) => { setEditing(false); if (saved) await onNoted(); }} />
      ) : (
        <p className="cg-about">
          {s.note && <span dir="auto">{s.note} </span>}
          <button ref={edit} type="button" className="rd-link" onClick={() => setEditing(true)}>
            {noted ? "Edit name, tags and note" : "Add a name, tags or a note"}
          </button>
        </p>
      )}
      <ol className="cg-catches">
        {shown.map((c) => <CatchRow key={c.id} c={c} />)}
      </ol>
      {n > FIRST && (
        <button type="button" className="rd-link cg-more" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? "Show the last three" : `Show all ${n}`}
        </button>
      )}
    </li>
  );
}

const REASONS: Record<string, string> = { carrier: "on for minutes, then taken as the channel's noise" };

function CatchRow({ c }: { c: Catch }) {
  return (
    <li className="cg-catch">
      <p className="cg-when num">
        <time dateTime={isoTime(c.t_start)}>{whenLabel(c.t_start)}</time>
        {[secs(c.duration), `${c.snr_db.toFixed(0)} dB above noise`, voiceWord(c.voice), c.reason && (REASONS[c.reason] || c.reason)].filter(Boolean).map((x) => ` · ${x}`)}
      </p>
      {c.utterances.map((u) => {
        // In the reader's language where the store has it; nothing otherwise.
        const tr = P.lang && !sameLang(u.language, P.lang) ? Object.entries(u.translations || {}).find(([l]) => sameLang(l, P.lang))?.[1] : null;
        return (
          <div key={u.id} className="cg-said">
            <p dir="auto" lang={u.language}><span className="u-lang">{langNameEn(u.language)}</span>{u.text}</p>
            {tr && <p className="cg-tr" dir="auto" lang={P.lang || undefined}>{tr}</p>}
          </div>
        );
      })}
    </li>
  );
}

function NoteForm({ s, f, onDone }: { s: Spot; f: string; onDone: (saved: boolean) => Promise<void> }) {
  const [name, setName] = useState(s.name);
  const [tags, setTags] = useState(s.tags.join(", "));
  const [note, setNote] = useState(s.note);
  const id = `cg-${s.freq_hz}`;
  const save = () => act(async () => {
    await post(`/radio/notes/${s.freq_hz}`, { name: name.trim(), note: note.trim(), tags: tags.split(",").map((t) => t.trim()).filter(Boolean) });
    await onDone(true);
  }, "Saved");
  return (
    <form className="m-panel cg-note" aria-label={`About ${f} MHz`} onSubmit={(e) => { e.preventDefault(); save(); }}
          onKeyDown={(e) => {
            // Escape closes the form; the page's own Escape (back to reading) must not run too.
            if (e.key === "Escape") { e.stopPropagation(); onDone(false); }
          }}>
      <div className="m-form">
        <Field id={`${id}-name`} label="Name" hint="What you call this frequency; the heard list uses it too.">
          <Input id={`${id}-name`} autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field id={`${id}-tags`} label="Tags" hint="Separated by commas, such as family, building site.">
          <Input id={`${id}-tags`} value={tags} onChange={(e) => setTags(e.target.value)} />
        </Field>
        <Field id={`${id}-note`} label="Note" wide hint="What is said here, and when: for the next time something is caught on it.">
          <Textarea id={`${id}-note`} rows={2} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
        </Field>
      </div>
      <div className="m-actions">
        <Button type="submit" variant="primary">Save</Button>
        <Button variant="ghost" onClick={() => onDone(false)}>Cancel</Button>
      </div>
    </form>
  );
}
