// The dial: one bar for everything a receiver tunes, printed on the instrument's dark band like
// the scale of a radio. Bands are its segments, named in everyday words; what was heard here is a
// fine tick under them. A band chosen on it is explained underneath (BandCard). The geometry lives
// in dial.ts.
import { type KeyboardEvent, type ReactNode, useLayoutEffect, useMemo, useRef, useState } from "react";
import { cx } from "../components/ui";
import type { Any } from "./api";
import { type Band, bandWords } from "./bands";
import { type Segment, kindOf, labelRows, makeScale, onDial, segments, ticks } from "./dial";

const NAME_FONT = 12; // px, as --t-sm
const MIN_INNER_PX = 14; // a band inside another stays this wide

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

export function DialBar({ bands, heard, chosen, onChoose, overlay, says }: {
  bands: Band[];
  heard: Any[];
  /** The chosen band's id, or null when none is (a range of one's own, the whole tuner). */
  chosen: string | null;
  onChoose: (id: string) => void;
  /** Drawn over the dial, given its width in px and its scale (the receivers). */
  overlay?: (width: number, scale: ReturnType<typeof makeScale>) => ReactNode;
  /** More to say about a band in its button's name (which receivers are on it). */
  says?: (b: Band) => string;
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
    const order = segs.map((s) => s.band.id);
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

  return (
    <div className="dl">
     <div className="dl-in" ref={wrap}>
      {width > 0 && (
        <>
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
                  <text className="dl-mhz" x={Math.min(width - 2, Math.max(2, x))} y={22} textAnchor={x < 16 ? "start" : x > width - 16 ? "end" : "middle"}>{f}</text>
                </g>
              );
            })}
          </svg>
          {overlay?.(width, scale)}
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
      <span><i className="dl-key is-none" aria-hidden="true" />Outside any band</span>
      <span><i className="dl-key is-heard" aria-hidden="true" />Heard here</span>
    </p>
  );
}

/** A chosen band, explained: what it is, what you would hear and when, the antenna it wants, and
 * what Wavescribe does with it; then what can be done there (`children`). */
export function BandCard({ band, kept, policies, heard, children }: { band: Band; kept: string; policies: Any; heard: number; children?: ReactNode }) {
  const w = bandWords(band);
  const ask = !!policies?.[band.privacy]?.needs_ack;
  return (
    <div className="dl-card">
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
