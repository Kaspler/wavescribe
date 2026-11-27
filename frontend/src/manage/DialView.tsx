// The dial: one bar for everything a receiver tunes, printed on the instrument's dark band like
// the scale of a radio. Bands are its segments, named in everyday words; what was heard here is a
// fine tick under them. A band chosen on it is explained underneath (BandCard), receivers stand
// on it (ReceiverMarks), and one tuned by hand shows what it sees live (LiveView). The geometry
// lives in dial.ts.
import { type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ActButton, Advanced, Button, Lamp, cx } from "../components/ui";
import type { Any } from "./api";
import { type Band, and, bandWords } from "./bands";
import { type Segment, bandOfWindow, dragCentre, kindOf, labelRows, makeScale, marks, onDial, segments, ticks } from "./dial";
import { rxName } from "./Receivers";
import { type Marker, Scope } from "./Scope";
import { TuneControls, TunedHead, type Tuner, useLivePoll, useSteps } from "./tuner";

const NAME_FONT = 12; // px, as --t-sm
const MIN_INNER_PX = 14; // a band inside another stays this wide
const LIST_BELOW = 560; // px: narrower, the dial is a list of bands, one under another
const CARD_ID = "dl-card";

/** "87.5 to 108 MHz": a range said in words, for names a screen reader reads out. */
export const rangeWords = (b: Pick<Band, "start_mhz" | "stop_mhz">) => `${+b.start_mhz.toFixed(3)} to ${+b.stop_mhz.toFixed(3)} MHz`;

/** The band's kind, as the dial's legend says it. */
export const KIND_WORDS = { text: "voices Wavescribe can turn into text", shown: "digital: it shows, but Wavescribe has no decoder" } as const;

/** Heard frequencies by band id, not counting digital services or what became a station. */
export function heardIn(heard: Any[], b: Pick<Band, "start_mhz" | "stop_mhz">): Any[] {
  return heard.filter((e) => e.privacy !== "digital" && e.mhz >= b.start_mhz && e.mhz <= b.stop_mhz);
}

/** Measures names in the page's own type, once per name. */
function useMeasure() {
  const cache = useRef(new Map<string, number>());
  const ctx = useRef<CanvasRenderingContext2D | null>(null);
  return (text: string) => {
    const hit = cache.current.get(text);
    if (hit !== undefined) return hit;
    if (!ctx.current) {
      ctx.current = document.createElement("canvas").getContext("2d");
      if (ctx.current) ctx.current.font = `600 ${NAME_FONT}px ${getComputedStyle(document.body).fontFamily}`;
    }
    const w = Math.ceil(ctx.current?.measureText(text).width ?? text.length * 7);
    cache.current.set(text, w);
    return w;
  };
}

export function DialBar({ bands, heard, chosen, onChoose, overlay, says, onBand, after }: {
  bands: Band[];
  heard: Any[];
  /** The chosen band's id, or null when none is (a range of one's own, the whole tuner). */
  chosen: string | null;
  onChoose: (id: string) => void;
  /** Drawn over the dial, given its width in px and its scale (the receivers). */
  overlay?: (width: number, scale: ReturnType<typeof makeScale>) => ReactNode;
  /** More to say about a band in its button's name (which receivers are on it). */
  says?: (b: Band) => string;
  /** On a narrow screen, where the dial is a list: what stands on a band's row (its receivers),
   * and what follows the list (receivers outside every band). */
  onBand?: (b: Band) => ReactNode;
  after?: ReactNode;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const measure = useMeasure();
  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.round(e.contentRect.width)));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const scale = useMemo(() => makeScale(bands), [bands]);
  const segs = useMemo(() => segments(onDial(bands), scale, width ? MIN_INNER_PX / width : 0), [bands, scale, width]);
  // Names go where they fit, broadcast first, then voices, then the wider bands.
  const names = useMemo(() => {
    if (!width) return {};
    const order = segs.slice().sort((a, b) =>
      +(b.band.privacy === "broadcast" && b.kind === "text") - +(a.band.privacy === "broadcast" && a.kind === "text")
      || +(b.kind === "text") - +(a.kind === "text") || (b.x1 - b.x0) - (a.x1 - a.x0));
    return labelRows(order.map((s) => ({ id: s.band.id, x0: s.x0, x1: s.x1, width: measure(label(s.band)) })), width, 2);
  }, [segs, width]);
  const marks = useMemo(() => (width ? ticks(scale, width) : []), [scale, width]);
  const found: number[] = useMemo(() => heard.filter((e) => e.privacy !== "digital").map((e) => e.mhz), [heard]);

  const keys = (e: KeyboardEvent<HTMLDivElement>) => {
    const order = (list ? rows.map((r) => r.s) : segs).map((s) => s.band.id);
    const i = order.indexOf(chosen || "");
    const next = e.key === "ArrowRight" || e.key === "ArrowDown" ? Math.min(order.length - 1, i + 1)
      : e.key === "ArrowLeft" || e.key === "ArrowUp" ? Math.max(0, i < 0 ? 0 : i - 1)
      : e.key === "Home" ? 0 : e.key === "End" ? order.length - 1 : null;
    if (next === null) return;
    e.preventDefault();
    onChoose(order[next]);
    requestAnimationFrame(() => track.current?.querySelector<HTMLElement>(`[data-band="${order[next]}"]`)?.focus());
  };
  const roving = chosen && segs.some((s) => s.band.id === chosen) ? chosen : segs[0]?.band.id;

  const list = width > 0 && width < LIST_BELOW;
  // As a list: in frequency order, a band before those inside it, and two bands of one name
  // (the amateurs' 2 m and 70 cm) by their shorter, distinct labels.
  const rows = useMemo(() => {
    const byStart = segs.slice().sort((a, b) => a.band.start_mhz - b.band.start_mhz || b.band.stop_mhz - a.band.stop_mhz);
    const named = new Map<string, number>();
    for (const s of byStart) named.set(bandWords(s.band).name, (named.get(bandWords(s.band).name) || 0) + 1);
    return byStart.map((s) => ({ s, shown: (named.get(bandWords(s.band).name) || 0) > 1 ? label(s.band) : bandWords(s.band).name }));
  }, [segs]);
  // On a phone the card is below a long list: a tap brings it into view (keys keep their place).
  const tapped = (id: string) => {
    onChoose(id);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    requestAnimationFrame(() => document.getElementById(CARD_ID)?.scrollIntoView({ block: "start", behavior: still ? "auto" : "smooth" }));
  };
  return (
    <div className={cx("dl", list && "is-list")}>
     <div className="dl-in" ref={wrap}>
      {list && (
        <>
          <div className="dl-list" ref={track} role="toolbar" aria-label="Bands on the dial" aria-orientation="vertical" onKeyDown={keys}>
            {rows.map(({ s, shown }) => {
              const count = heardIn(heard, s.band).length;
              const w = bandWords(s.band);
              return (
                <div key={s.band.id} className="dl-row">
                  <button type="button" data-band={s.band.id} className={cx("dl-row-band", s.band.id === chosen && "is-chosen")}
                          tabIndex={s.band.id === roving ? 0 : -1} aria-pressed={s.band.id === chosen}
                          aria-label={[w.name, rangeWords(s.band), s.kind === "text" ? "voices to text" : "digital, shown only",
                            count ? `${count} heard here` : null, says?.(s.band)].filter(Boolean).join(", ")}
                          onClick={(e) => (e.detail ? tapped(s.band.id) : onChoose(s.band.id))}>
                    <i className={cx("dl-key", `is-${s.kind}`)} aria-hidden="true" />
                    <span className="dl-row-name">{shown}</span>
                    <span className="dl-row-range">{+s.band.start_mhz.toFixed(3)}–{+s.band.stop_mhz.toFixed(3)}</span>
                    {count > 0 && <span className="dl-row-count">{count} heard</span>}
                  </button>
                  {onBand?.(s.band)}
                </div>
              );
            })}
          </div>
          {after}
        </>
      )}
      {width >= LIST_BELOW && (
        <>
          {overlay?.(width, scale)}
          <div className="dl-names" aria-hidden="true">
            {segs.map((s) => {
              const at = names[s.band.id];
              return at ? (
                <span key={s.band.id} className={cx("dl-name", `is-row${at.row}`, s.band.id === chosen && "is-chosen")} style={{ left: at.left }}
                      onClick={() => onChoose(s.band.id)}>
                  {label(s.band)}
                </span>
              ) : null;
            })}
          </div>
          <div className="dl-track" ref={track} role="toolbar" aria-label="Bands on the dial" aria-orientation="horizontal" onKeyDown={keys}>
            {segs.map((s) => (
              <BandButton key={s.band.id} s={s} chosen={s.band.id === chosen} tab={s.band.id === roving}
                          count={heardIn(heard, s.band).length} extra={says?.(s.band)} onChoose={onChoose} />
            ))}
          </div>
          <svg className="dl-scale" width={width} height={24} aria-hidden="true">
            {found.map((f, i) => <rect key={i} className="dl-heard" x={Math.round(scale.x(f) * width)} y={1} width={1} height={5} />)}
            {marks.map((f) => {
              const x = Math.round(scale.x(f) * width);
              return (
                <g key={f}>
                  <rect className="dl-tick" x={x} y={7} width={1} height={4} />
                  <text className="dl-mhz" x={Math.min(width - 2, Math.max(2, x))} y={22} textAnchor={x < 16 ? "start" : x > width - 16 ? "end" : "middle"}>
                    {f === marks[0] ? `${f} MHz` : f}
                  </text>
                </g>
              );
            })}
          </svg>
        </>
      )}
     </div>
    </div>
  );
}

/** The name printed on the dial: a shorter one where the everyday name is long. */
const label = (b: Band) => {
  const w = bandWords(b);
  return w.label || w.name;
};

function BandButton({ s, chosen, tab, count, extra, onChoose }: { s: Segment<Band>; chosen: boolean; tab: boolean; count: number; extra?: string; onChoose: (id: string) => void }) {
  const b = s.band;
  const w = bandWords(b);
  const said = [w.name, rangeWords(b), s.kind === "text" ? "voices to text" : "digital, shown only",
    count ? `${count} heard here` : null, extra].filter(Boolean).join(", ");
  return (
    <button type="button" data-band={b.id} className={cx("dl-band", `is-${s.kind}`, s.row === 1 && "is-inner", chosen && "is-chosen")}
            style={{ left: `${s.x0 * 100}%`, width: `${(s.x1 - s.x0) * 100}%` }}
            tabIndex={tab ? 0 : -1} aria-pressed={chosen} aria-label={said}
            title={`${w.name} · ${b.name} · ${+b.start_mhz.toFixed(3)}–${+b.stop_mhz.toFixed(3)} MHz`}
            onClick={() => onChoose(b.id)} />
  );
}

/** What the dial's two kinds of segment mean, and its bare stretches; said in words beside a
 * drawing of each, so the shapes aren't the only way to tell them apart. */
export function DialLegend() {
  return (
    <p className="dl-legend">
      <span><i className="dl-key is-text" aria-hidden="true" />Voices Wavescribe can turn into text</span>
      <span><i className="dl-key is-shown" aria-hidden="true" />Digital: shown, not decoded</span>
      <span className="is-bar"><i className="dl-key is-none" aria-hidden="true" />Outside any band</span>
      <span className="is-bar"><i className="dl-key is-heard" aria-hidden="true" />Heard here</span>
    </p>
  );
}

/** What each receiver is doing, in words beside its lamp: the dial's tabs carry only the lamp
 * and the letter, and this is also where the lamps' meanings are learnt. */
export function ReceiverWords({ rxs }: { rxs: RxStatus[] }) {
  return (
    <p className="dl-legend dl-rx-words">
      {rxs.map((r) => <span key={r.id}><Lamp state={r.lamp} />Receiver {rxName(r.id)}: {r.words}</span>)}
    </p>
  );
}

/** A chosen band, explained: what it is, what you would hear and when, the antenna it wants, and
 * what Wavescribe does with it; then what can be done there (`children`). */
export function BandCard({ band, kept, policies, heard, children }: { band: Band; kept: string; policies: Any; heard: number; children?: ReactNode }) {
  const w = bandWords(band);
  const ask = !!policies?.[band.privacy]?.needs_ack;
  return (
    <div className="dl-card" id={CARD_ID}>
      <h4 className="dl-card-h">
        {w.name}
        <span className="dl-card-tech">{band.name} · {+band.start_mhz.toFixed(3)}–{+band.stop_mhz.toFixed(3)} MHz{band.mode ? ` · ${band.mode.toUpperCase()}` : ""}</span>
      </h4>
      <p className="dl-card-text">{[w.what, w.when, w.antenna].filter(Boolean).join(" ")}</p>
      <p className="m-hint dl-card-who">
        {band.privacy === "broadcast" ? "Broadcast radio: meant for everyone." : kept}
        {ask && " Before it plays or becomes a station, Wavescribe asks you to confirm you may receive it where you are."}
        {kindOf(band) === "text" && heard > 0 && ` ${heard === 1 ? "One frequency" : `${heard} frequencies`} heard here so far.`}
      </p>
      {children}
    </div>
  );
}

// -- receivers on the dial ---------------------------------------------------------------------

/** What a receiver is doing, as the dial draws it and says it. */
export interface RxStatus {
  id: string;
  /** Its window: what it hears now, or, while it scans, the range it sweeps. */
  lo: number;
  hi: number;
  state: "idle" | "hand" | "stations" | "scanning" | "trouble";
  lamp: "off" | "dim" | "on" | "wait" | "trouble";
  /** "following Valley and City News", "idle", … */
  words: string;
  /** Running stations a move would pause. */
  holders: string[];
}


/** A receiver's state from what the radio view (`r`: stations, scan) and the tuner (`live`: where
 * the dongle is tuned now, whether it is tuned by hand and answering) say about it. */
export function rxStatus(r: Any, live: Any | undefined, job: Any | null): RxStatus {
  const centre = live?.center_mhz ?? r.center_mhz;
  const span = live?.span_mhz ?? r.span_mhz;
  const running = (r.stations || []).filter((s: Any) => s.running).map((s: Any) => s.name);
  const base = { id: r.id, lo: centre - span / 2, hi: centre + span / 2, holders: running };
  const scanning = r.scanning || (job && job.device === r.id && !["done", "error"].includes(job.phase));
  if (scanning) {
    return { ...base, lo: job?.start_mhz ?? base.lo, hi: job?.stop_mhz ?? base.hi, state: "scanning", lamp: "wait",
      words: `scanning ${job?.what || "a band"}` };
  }
  if (live?.open) {
    if (live.error || (live.connected === false && live.reconnects > 0)) {
      return { ...base, state: "trouble", lamp: "trouble",
        words: live.error ? `stopped: ${live.error}` : "not answering: another program may be using it, or it was unplugged" };
    }
    const waiting: string[] = live.paused || [];
    return { ...base, state: "hand", lamp: "dim",
      words: `tuned by hand to ${mhzText(live.mhz, live.mode)}${waiting.length ? `; ${and(waiting)} ${waiting.length === 1 ? "waits" : "wait"} until it's let go` : running.length ? `, beside ${and(running)}` : ""}` };
  }
  if (running.length) {
    return { ...base, state: "stations", lamp: "on", words: `following ${and(running)}` };
  }
  // Another program holding the dongle (another Wavescribe, say) is why it can't be used now.
  if (r.held_elsewhere) {
    return { ...base, state: "trouble", lamp: "trouble", words: `in use elsewhere: ${r.held_elsewhere.reason || "another program holds it"}` };
  }
  return { ...base, state: "idle", lamp: "off", words: "idle" };
}

export const mhzText = (mhz: number, mode?: string | null) => `${mode === "wfm" ? mhz.toFixed(1) : +mhz.toFixed(5)} MHz`;

const TAB_ROW = 22; // px per row of receiver tabs above the band names
const TO_TRACK = 38; // px from the band names' top to the bar's (.dl-names and its margin)
const TRACK = 26; // the bar's height (.dl-track)

/** The receivers drawn over the dial: each one's window as a pale column down to the bar, hung
 * from a tab with its lamp and letter. A tab is a button (it chooses the receiver) and a handle:
 * dragged along the dial, it asks to send the receiver there. */
export function ReceiverMarks({ width, scale, rxs, chosen, bands, onChoose, onDrop }: {
  width: number;
  scale: ReturnType<typeof makeScale>;
  rxs: RxStatus[];
  chosen: string;
  bands: Band[];
  onChoose: (id: string) => void;
  /** A tab dropped at a centre frequency, in MHz. */
  onDrop: (id: string, centre: number) => void;
}) {
  const [drag, setDrag] = useState<{ id: string; from: number; x: number } | null>(null);
  const placed = marks(rxs.map((r) => ({ id: r.id, lo: r.lo, hi: r.hi })), scale, width, 40);
  const rows = Math.max(1, ...placed.map((m) => m.row + 1));
  const by = Object.fromEntries(rxs.map((r) => [r.id, r]));
  const span = (id: string) => by[id].state === "scanning" ? 2.4 : by[id].hi - by[id].lo;
  return (
    <div className="dl-rxs" style={{ height: rows * TAB_ROW }}>
      {placed.map((m) => {
        const r = by[m.id];
        const dragging = drag?.id === m.id && Math.abs(drag.x - drag.from) > 4;
        const centre = dragging ? dragCentre(drag.x / width, scale, bands, span(m.id)) : null;
        const ghost = centre != null ? { x0: scale.x(centre - span(m.id) / 2), x1: scale.x(centre + span(m.id) / 2) } : null;
        const left = (ghost ? (ghost.x0 + ghost.x1) / 2 : m.xc) * width;
        const band = bandOfWindow(bands, r.lo, r.hi);
        return (
          <div key={m.id} className={cx("dl-rx", `is-${r.state}`, m.id === chosen && "is-chosen", dragging && "is-dragging")}>
            <span className="dl-rx-stem" aria-hidden="true"
                  style={{ left: Math.round(left), top: (m.row + 1) * TAB_ROW - 4, height: rows * TAB_ROW + TO_TRACK - (m.row + 1) * TAB_ROW + 1 }} />
            <span className="dl-rx-win" aria-hidden="true"
                  style={{ left: (ghost?.x0 ?? m.x0) * width, width: Math.max(8, ((ghost?.x1 ?? m.x1) - (ghost?.x0 ?? m.x0)) * width),
                           top: rows * TAB_ROW + TO_TRACK - 3, height: TRACK + 6 }} />
            <button type="button" className="dl-rx-tab" style={{ left, top: m.row * TAB_ROW }}
                    aria-pressed={m.id === chosen}
                    aria-label={`Receiver ${rxName(m.id)}, ${r.words}, ${r.state === "scanning" ? "over" : "hearing"} ${+r.lo.toFixed(2)} to ${+r.hi.toFixed(2)} MHz${band ? ` in ${bandWords(band).name}` : ""}`}
                    title={centre != null ? `Look at ${mhzText(centre)} with Receiver ${rxName(m.id)}` : `Receiver ${rxName(m.id)}: ${r.words}. Drag along the dial to move it.`}
                    onClick={() => { if (!dragging) onChoose(m.id); }}
                    onPointerDown={(e) => {
                      // One pointer moves one receiver: a second finger doesn't take it over.
                      if (e.button !== 0 || !e.isPrimary || drag || r.state === "scanning") return;
                      e.currentTarget.setPointerCapture(e.pointerId);
                      const x = e.clientX - (e.currentTarget.parentElement!.parentElement!.getBoundingClientRect().left);
                      setDrag({ id: m.id, from: x, x });
                    }}
                    onPointerMove={(e) => {
                      if (drag?.id !== m.id) return;
                      setDrag({ ...drag, x: e.clientX - e.currentTarget.parentElement!.parentElement!.getBoundingClientRect().left });
                    }}
                    onPointerUp={() => {
                      if (drag?.id === m.id && centre != null) onDrop(m.id, centre);
                      setTimeout(() => setDrag(null)); // after the click this release also fires
                    }}
                    onPointerCancel={() => setDrag(null)}
                    onKeyDown={(e) => { if (e.key === "Escape" && drag) { e.stopPropagation(); setDrag(null); } }}>
              <Lamp state={r.lamp} onBand />
              {rxName(m.id)}
            </button>
          </div>
        );
      })}
    </div>
  );
}

// -- what a receiver sees ----------------------------------------------------------------------

/** What to mark on a receiver's spectrum between `lo` and `hi` MHz: what was heard there, by
 * name, a signal on all the time that isn't broadcast as steady, and from the scans (`signals`)
 * the receivers' own lines and other receivers' clock lines. */
export function dialMarkers(heard: Any[], signals: Any[], lo: number, hi: number): Marker[] {
  const inside = (f: number) => f >= lo && f <= hi;
  const out: Marker[] = heard.filter((e) => inside(e.mhz)).map((e) => ({
    mhz: e.mhz,
    name: e.user_name || e.name || e.rds?.ps || "",
    kind: e.passes >= 10 && e.seen === e.passes && e.privacy !== "broadcast" ? "steady" : "station",
  }));
  for (const s of signals) if (s.spur && inside(s.mhz)) out.push({ mhz: s.mhz, name: "", kind: s.clock_hz != null ? "clock" : "own" });
  return out;
}

/** A receiver tuned by hand, live: the tuning and what is there, the sound, and its window as
 * spectrum and waterfall, where a click listens. `signals`: what a scan found in the window, for
 * the lines that aren't transmissions. */
export function LiveView({ t, heard, signals, onAdd }: { t: Tuner; heard: Any[]; signals: Any[]; onAdd: (pre: Any) => void }) {
  const st = t.st;
  useLivePoll(t, null);
  const steps = useSteps(t, null);
  const name = rxName(t.dev);
  const play = () => { t.stopPlayer(); t.startPlayer(st.audio); };
  // Opened by a button that goes away with it (Look here, Pause and look): the focus goes on to
  // what can be done here, not back to the top of the page.
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const a = document.activeElement;
    if (!a || a === document.body) box.current?.querySelector<HTMLElement>(".m-actions button")?.focus();
  }, []);
  return (
    <div className="dl-view" role="group" aria-label={`What Receiver ${name} hears now`} ref={box}>
      <TunedHead t={t} steps={steps} />
      <div className="m-actions">
        {t.playing
          ? <Button variant="secondary" icon="mute" onClick={t.stopPlayer}>Mute</Button>
          : <Button variant="primary" icon="speaker" onClick={play}>Listen</Button>}
        <Button icon="plus" onClick={() => onAdd({ mhz: st.mhz, mode: st.mode, name: st.rds?.ps?.trim() || "", privacy: st.privacy, device: t.dev, t: Date.now() })}>
          Add as a station
        </Button>
        <ActButton variant="ghost" run={t.close}>Let Receiver {name} go</ActButton>
      </div>
      {t.blocked && <p className="m-trouble">The browser held the sound back: press Listen.</p>}
      {st.clipped > 0.001 && <p className="m-trouble">The receiver is overloaded (its converter clips): lower the gain under Tuning.</p>}
      {!st.connected && (st.error || st.reconnects > 0) && (
        <p className="m-trouble">{st.error ? `Receiver ${name} stopped: ${st.error}. Look here again to restart it.` : `Receiver ${name} has stopped sending: reconnecting…`}</p>
      )}
      <Scope spectrum={st.spectrum || null} centerMhz={st.center_mhz} spanMhz={st.span_mhz} view={null} tunedMhz={st.mhz} mode={st.mode}
             channelKhz={st.channel_khz} stepKhz={steps.stepKhz} band={st.band} markers={dialMarkers(heard, signals, steps.lo, steps.hi)}
             onTune={(mhz) => t.tune(steps.keep({ mhz: +mhz.toFixed(5) }), true)} onStep={steps.step}
             inWords="The table below lists what has been found in this band, strongest first." />
      <p className="m-hint">
        Receiver {name} hears {steps.lo.toFixed(2)} to {steps.hi.toFixed(2)} MHz. Click a signal, or anywhere, to listen there; the arrow
        keys step. Nothing heard here is kept until you add it as a station.
        {st.paused?.length > 0 && ` ${and(st.paused)} ${st.paused.length === 1 ? "waits" : "wait"} until you let the receiver go.`}
      </p>
      <Advanced id="dl-tune" label="Tuning">
        <TuneControls t={t} steps={steps} play={play} />
      </Advanced>
    </div>
  );
}

/** A receiver on a band's row of the dial as a list: its lamp and letter, a button that brings
 * it into view. */
export function ReceiverChip({ r, chosen, onChoose }: { r: RxStatus; chosen: boolean; onChoose: (id: string) => void }) {
  return (
    <button type="button" className={cx("dl-chip", chosen && "is-chosen")} aria-pressed={chosen}
            aria-label={`Receiver ${rxName(r.id)}, ${r.words}`} onClick={() => onChoose(r.id)}>
      <Lamp state={r.lamp} onBand />
      {rxName(r.id)}
    </button>
  );
}
