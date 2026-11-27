// Listen's radio: what the receivers found here, each a press from playing, and a receiver tuned by
// hand, like a radio or an SDR program. Nothing heard here is written down, stored or checked for
// alerts; "Keep as a station" makes it one (radio/live.py).
import { useEffect, useRef, useState } from "react";
import { S } from "../lib/live";
import { overloadOf } from "../lib/overload";
import { OverloadNote } from "../components/Overload";
import { ActButton, Button, Field, Input, Lamp, Section, Segmented, Select, Table, act, cx, toast } from "../components/ui";
import { ApiError, type Any, HANDOFF, del, knownLangs, post, refreshPage } from "./api";
import { addedToast } from "./added";
import { and, bandLabel, fmtMhz } from "./bands";
import { type LiveReceiver, bandAt, hears, usable } from "./dial";
import { OnAirHere, Responsibility, useRadio } from "./Radio";
import { RxSwitch, rxName } from "./Receivers";
import { Scope, snap } from "./Scope";
import { SpokenField } from "./SpokenField";
import { spokenFields, spokenLang, stationsOnAir } from "./spokenLang";
import { Ask, TuneControls, TunedHead, type Tuner, remember, useLivePoll, useSteps, useTuner } from "./tuner";

const ZOOMS = [
  { value: "full", label: "Whole band" },
  { value: "400", label: "400 kHz" },
  { value: "100", label: "100 kHz" },
  { value: "25", label: "25 kHz" },
] as const;
type Zoom = (typeof ZOOMS)[number]["value"];

// -- frequencies -----------------------------------------------------------------------------

/** Channels of a band, or a raster between two frequencies. */
function raster(lo: number, hi: number, stepKhz: number, band: Any | null): number[] {
  if (band?.channels_mhz?.length) return band.channels_mhz.filter((c: number) => c >= lo && c <= hi);
  const out: number[] = [];
  const step = stepKhz / 1000;
  for (let f = snap(lo, stepKhz, band); f <= hi + 1e-9 && out.length < 600; f = +(f + step).toFixed(5)) if (f >= lo) out.push(f);
  return out;
}

// -- the tab ---------------------------------------------------------------------------------

/** A frequency handed over from Find, On the air (`listenTo`), read once. */
function handedOver(): Any | null {
  try {
    const hand = JSON.parse(sessionStorage.getItem(HANDOFF) || "null");
    sessionStorage.removeItem(HANDOFF);
    return hand;
  } catch {
    return null; // nothing handed over
  }
}

/** On the air here, then the tuner. A frequency asked for from either, or handed over from Find,
 * is tuned and played on the receiver best placed for it. */
export function Listen() {
  const radioState = useRadio();
  const radio = radioState.v;
  const t = useTuner("listen", handedOver);
  const { receivers, error, reload, dev, st, setSt, ask, setAsk, resend, tune, close, moveTo, asked: setAsked,
    playing, startPlayer, stopPlayer, loaded } = t;
  const open = st?.open && st.device === dev;
  const air = (
    <OnAirHere radio={radioState} playingMhz={open && playing ? st.mhz : null} onPlay={setAsked} onStop={close} onReceiver={reload} />
  );
  if (!loaded) return <>{air}<p className="m-loading">{error ? `Couldn't load the receivers: ${error}` : "Loading…"}</p></>;
  // Without a receiver there is nothing to tune: On the air here offers a dongle to use.
  if (!receivers.length) return air;
  const ids = receivers.map((r) => r.id);
  return (
    <>
      {air}
      <Responsibility />
      {ask && <Ask ask={ask} onYes={resend} onNo={() => setAsk(null)} />}
      {open ? (
        <Tuned t={t} radio={radio} ids={ids} onRx={(id) => moveTo(id, { mhz: st.mhz, mode: st.mode })}
               play={() => { stopPlayer(); startPlayer(st.audio); }}
               onKept={async () => { stopPlayer(); setSt(null); await reload(); await refreshPage(); }} />
      ) : (
        <Start receivers={receivers} dev={dev} onRx={(id) => moveTo(id, null)} tune={(b) => tune(b, true)} />
      )}
      <BandChips radio={radio} onBand={(m, md) => { remember("ws-listen-mhz", String(m)); return tune({ mhz: m, mode: md }, true); }} />
    </>
  );
}

// -- before listening ------------------------------------------------------------------------

function Start({ receivers, dev, onRx, tune }: { receivers: Any[]; dev: string; onRx: (id: string) => void; tune: (b: Any) => Promise<Any> }) {
  const [mhz, setMhz] = useState(remember("ws-listen-mhz") || "");
  const [mode, setMode] = useState<string>("");
  const rx = receivers.find((r) => r.id === dev);
  const f = +mhz;
  const go = (m: number, md?: string) => { remember("ws-listen-mhz", String(m)); return tune({ mhz: m, mode: md || undefined }); };
  return (
    <>
      <Section id="lv-start" title="Tune by hand"
               description="Like a radio: pick a frequency and listen. Nothing you hear here is written down, kept or checked for alerts. When you find something worth following, keep it as a station.">
        <RxSwitch ids={receivers.map((r) => r.id)} value={dev} onChange={onRx}
                  note={rx?.holders.length ? `In use by ${rx.holders.join(", ")}: you can listen alongside within its band.` : receivers.length > 1 ? "The one with an antenna for the band." : null} />
        <div className="m-form m-form-4">
          <Field id="lv-f" label="Frequency (MHz)" hint="As on a radio's dial; Enter tunes.">
            <Input id="lv-f" type="number" step={0.00001} value={mhz} placeholder="e.g. 99.5 or 446.00625" className="mono"
                   onChange={(e) => setMhz(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && f > 0) go(f, mode); }} />
          </Field>
          <Field id="lv-mode" label="Demodulation" hint="How the signal carries sound; the band plan knows.">
            <Select id="lv-mode" value={mode} onChange={(e) => setMode(e.target.value)}
                    options={[["", "From the band plan"], ["wfm", "WFM (broadcast FM)"], ["nfm", "NFM (walkie-talkies, marine, amateur)"], ["am", "AM (aircraft, medium wave)"]]} />
          </Field>
        </div>
        <div className="m-actions">
          <ActButton variant="primary" icon="speaker" disabled={!(f > 0)} run={() => go(f, mode)}>Start listening</ActButton>
          <span className="m-hint rd-inline">Every band at once, and where each receiver is: <a className="rd-link" href="#/add/air">the dial</a> under Find.</span>
        </div>
      </Section>
    </>
  );
}

/** The bands where you are, one press to the start of each: before listening and while. */
function BandChips({ radio, onBand }: { radio: Any; onBand: (mhz: number, mode: string) => unknown }) {
  const bands: Any[] = (radio?.bands || []).filter((b: Any) => b.mode && b.tunable && b.privacy !== "digital");
  if (!bands.length) return null;
  return (
    <Section id="lv-bands" title="Bands where you are" description="Start at a band, then seek or scan through it.">
      <div className="lv-chips">
        {bands.map((b) => (
          <Button key={b.id} size="sm" variant="secondary" onClick={() => onBand(b.channels_mhz?.[0] ?? +(b.mode === "wfm" ? b.start_mhz + 0.1 : b.start_mhz).toFixed(5), b.mode)}>
            {bandLabel(b)}
          </Button>
        ))}
      </div>
    </Section>
  );
}

// -- listening -------------------------------------------------------------------------------

interface TunedProps {
  t: Tuner; radio: Any; ids: string[]; onRx: (id: string) => void; play: () => void; onKept: () => Promise<void>;
}

function Tuned({ t, radio, ids, onRx, play, onKept }: TunedProps) {
  const { dev, close, blocked, setSt: onPoll } = t;
  const st = t.st!;
  const band = st.band;
  const [zoom, setZoom] = useState<Zoom>(() => (st.mode === "wfm" ? "full" : "400"));
  const wide = st.mode === "wfm";
  useEffect(() => { setZoom(wide ? "full" : "400"); }, [wide]); // a narrow channel is lost in the whole band
  const [view, setView] = useState<[number, number] | null>(null);

  const [lo, hi] = st.span_mhz;
  // A zoomed view stays put while the tuning moves inside it, and follows when it leaves.
  useEffect(() => {
    if (zoom === "full") { setView(null); return; }
    const half = +zoom / 2000;
    setView((v) => {
      if (v && st.mhz > v[0] + half * 0.2 && st.mhz < v[1] - half * 0.2 && v[1] - v[0] === half * 2) return v;
      const c = Math.min(hi - half, Math.max(lo + half, st.mhz));
      return [c - half, c + half];
    });
  }, [zoom, st.mhz, lo, hi]);

  useLivePoll(t, view);

  const steps = useSteps(t, view);
  const markers: Any[] = (radio?.receivers || []).find((r: Any) => r.id === dev)?.stations || [];
  const isNarrow = st.mode !== "wfm";
  // A transmitter close by, as the server says it, while it lasts and 10 minutes after; lighter
  // clipping than it reports keeps the page's own line.
  const ovl = overloadOf(st.overload);
  return (
    <>
      <Section id="lv-tuned" title="Tune by hand" actions={<Button icon="stop" onClick={close}>Stop listening</Button>}>
        <RxSwitch ids={ids} value={dev} onChange={onRx}
                  note={st.holders?.length ? `Shared with ${st.holders.join(", ")}: a frequency outside its band, or another gain, needs it to itself.` : null} />
        <TunedHead t={t} steps={steps} />
        {ovl && <OverloadNote o={ovl} />}
        <TuneControls t={t} steps={steps} play={play} />
        {blocked && <p className="m-trouble">The browser held the sound back: press Play.</p>}
        {!ovl && st.clipped > 0.001 && <p className="m-trouble">The receiver is overloaded (its converter clips): lower the gain.</p>}
        {!st.connected && (st.error || st.reconnects > 0) && (
          <p className="m-trouble">{st.error ? `Receiver ${rxName(dev)} stopped: ${st.error}. Tune again to restart it.` : `Receiver ${rxName(dev)} has stopped sending: reconnecting…`}</p>
        )}

        <Scope spectrum={st.spectrum || null} span={st.span_mhz} view={view} tunedMhz={st.mhz} mode={st.mode}
               channelKhz={st.channel_khz} stepKhz={steps.stepKhz} band={band} markers={markers} onTune={steps.to} onStep={steps.step} />
        <div className="lv-under">
          <Segmented label="Zoom" value={zoom} onChange={setZoom} options={[...ZOOMS]} />
          <span className="m-hint">Click to tune, scroll or use the arrow keys to step. Receiver {rxName(dev)} hears {steps.heard[0].toFixed(2)} to {steps.heard[1].toFixed(2)} MHz.</span>
        </div>
        {st.paused?.length > 0 && (
          <p className="m-hint">
            Paused while you listen: {and(st.paused)}. {st.paused.length === 1 ? "It starts" : "They start"} again when you stop listening.
          </p>
        )}
      </Section>
      {isNarrow && <ScanPanel st={st} radio={radio} dev={dev} view={view} stepKhz={steps.stepKhz} onPoll={onPoll} />}
      <Keep st={st} radio={radio} dev={dev} onKept={onKept} />
    </>
  );
}

// -- scanning --------------------------------------------------------------------------------

function ScanPanel({ st, radio, dev, view, stepKhz, onPoll }: { st: LiveReceiver; radio: Any; dev: string; view: [number, number] | null; stepKhz: number; onPoll: (s: Any) => void }) {
  const band = st.band && st.band.mode === st.mode ? st.band : null;
  const [source, setSource] = useState<string>(band ? "band" : "view");
  const [threshold, setThreshold] = useState(10);
  const [hold, setHold] = useState(2);
  // Only channels the receiver hears where it is tuned: inside its edges and, being narrow, off
  // the spike at its centre, as the server scans them.
  const [lo, hi] = usable(st.span_mhz, st.mode);
  const heardHere = (f: number) => hears(st.span_mhz, f, st.mode, st.channel_khz);
  const heard: number[] = (radio?.heard || []).filter((e: Any) => e.mode === st.mode && heardHere(e.mhz)).map((e: Any) => e.mhz);
  const lists: Record<string, number[]> = {
    band: band ? raster(Math.max(lo, band.start_mhz), Math.min(hi, band.stop_mhz), band.step_khz || stepKhz, band).filter(heardHere) : [],
    heard,
    view: raster(view ? Math.max(lo, view[0]) : lo, view ? Math.min(hi, view[1]) : hi, stepKhz, null).filter(heardHere),
  };
  const freqs = lists[source] || [];
  const inBand = band ? raster(band.start_mhz, band.stop_mhz, band.step_khz || stepKhz, band).length : 0;
  const scan = st.scan;
  const start = () => act(async () => onPoll(await post(`/live/${dev}/scan`, { freqs_mhz: freqs, mode: st.mode, channel_khz: st.channel_khz, threshold_db: threshold, hold_s: hold })));
  const stopScan = () => act(async () => onPoll(await del(`/live/${dev}/scan`)));
  const lock = (mhz: number, on: boolean) => act(async () => onPoll(await post(`/live/${dev}/lockout`, { mhz, on })));
  const rows = scan ? scan.freqs_mhz.filter((f: number) => (scan.hits[f.toFixed(5)] || 0) > 0 || f === scan.holding_mhz || scan.locked_out_mhz.includes(f)) : [];
  return (
    <Section id="lv-scan" title="Scan"
             description="Watches every channel of the list at once and stops on the strongest one that comes alive, then moves on a moment after it goes quiet. Only channels in what the receiver hears now are scanned.">
      {!scan ? (
        <>
          <div className="m-form m-form-4">
            <Field id="lv-src" label="Channels"
                   hint={source === "band" && inBand > lists.band.length ? `${lists.band.length} of the band's ${inBand} are in what the receiver hears now.` : undefined}>
              <Select id="lv-src" value={source} onChange={(e) => setSource(e.target.value)} options={[
                ...(band ? [["band", `${bandLabel(band)} · ${lists.band.length}`] as [string, string]] : []),
                ["heard", `Heard here · ${heard.length}`],
                ["view", `Every ${stepKhz} kHz ${view ? "in view" : "it hears"} · ${lists.view.length}`],
              ]} />
            </Field>
            <label className="lv-slider lv-field">
              <span>Stops above the noise by</span>
              <input type="range" min={6} max={30} value={threshold} onChange={(e) => setThreshold(+e.target.value)} />
              <span className="mono lv-slider-v">{threshold} dB</span>
            </label>
            <label className="lv-slider lv-field">
              <span>Waits after a signal</span>
              <input type="range" min={0.5} max={10} step={0.5} value={hold} onChange={(e) => setHold(+e.target.value)} />
              <span className="mono lv-slider-v">{hold} s</span>
            </label>
          </div>
          <div className="m-actions">
            <ActButton variant="primary" icon="scan" disabled={!freqs.length} run={start}>Scan {freqs.length} channels</ActButton>
          </div>
        </>
      ) : (
        <>
          <p className="m-line lv-scan-state">
            {scan.holding_mhz ? <><Lamp state="speech" /> Stopped on <strong className="mono">{fmtMhz(scan.holding_mhz, st.mode)}</strong> MHz</> : <><Lamp state="wait" /> Scanning {scan.freqs_mhz.length} channels…</>}
            <span className="m-spacer" />
            {scan.holding_mhz && <Button size="sm" variant="ghost" onClick={() => lock(scan.holding_mhz, true)}>Skip this channel</Button>}
            <ActButton size="sm" icon="stop" run={stopScan}>Stop scanning</ActButton>
          </p>
          <Table cols={[{ label: "MHz" }, { label: "Now" }, { label: "Times", num: true }, { label: "" }]} empty="Nothing has come alive yet.">
            {rows.map((f: number) => {
              const k = f.toFixed(5), level = scan.levels[k] ?? 0, locked = scan.locked_out_mhz.includes(f);
              return (
                <tr key={k} className={cx(f === scan.holding_mhz && "is-current", locked && "is-off")}>
                  <td className="mono">{fmtMhz(f, st.mode)}</td>
                  <td><span className="lv-bar" aria-label={`${level.toFixed(0)} dB above the noise`}><span style={{ width: `${Math.max(0, Math.min(100, level * 2.5))}%` }} /></span></td>
                  <td className="num">{scan.hits[k] || 0}</td>
                  <td className="num"><Button size="sm" variant="ghost" onClick={() => lock(f, !locked)}>{locked ? "Scan it again" : "Skip"}</Button></td>
                </tr>
              );
            })}
          </Table>
        </>
      )}
    </Section>
  );
}

// -- keeping it ------------------------------------------------------------------------------

/** Keeps what is tuned as a station. Its language is named, or detected, among the languages it
 * mostly is where the person gives them. */
function Keep({ st, radio, dev, onKept }: { st: LiveReceiver; radio: Any; dev: string; onKept: () => Promise<void> }) {
  const [does, setDoes] = useState<string>("translate");
  // Until the person chooses, the language follows what is tuned, once the receivers are known.
  const [chosen, setLang] = useState<string | null>(null);
  const lang = chosen ?? spokenLang(stationsOnAir(radio?.receivers || [], S.stations), { mhz: st.mhz, device: dev }, (m) => bandAt(radio?.bands || [], m)?.id ?? null,
                                    radio?.defaults?.language);
  const [langs, setLangs] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [retune, setRetune] = useState<string | null>(null);
  // The question takes the focus while it is asked.
  const question = useRef<HTMLDivElement>(null);
  useEffect(() => { if (retune) question.current?.querySelector<HTMLElement>("button")?.focus(); }, [!!retune]);
  const save = async (force = false) => {
    if (does !== "listen" && !lang.trim()) { document.getElementById("lv-lang")?.focus(); return; }
    try {
      const v = await post(`/live/${dev}/station`, { does, ...spokenFields(lang, langs), name: name.trim() || st.rds?.ps?.trim() || undefined, translate_to: radio?.defaults?.translate_to, retune: force });
      const said = addedToast(v, `${v.name} is a station now${does === "listen" ? "" : ": it's on the reading page"}`);
      toast(said.text, said.bad);
      setRetune(null);
      await onKept();
      // Listening ended with it: the focus goes where the next frequency is typed.
      requestAnimationFrame(() => document.getElementById("lv-f")?.focus());
    } catch (e) {
      if (e instanceof ApiError && e.code === "retune") setRetune(e.message);
      else throw e;
    }
  };
  return (
    <Section id="lv-keep" title="Keep it as a station"
             description="A station runs all the time, beside the others. Listening here stops when you keep it.">
      <div className="m-mode">
        <Segmented label="What Wavescribe does with it" value={does} onChange={setDoes} options={[
          { value: "listen", label: "Listen only" },
          { value: "transcribe", label: "Transcribe" },
          { value: "translate", label: "Transcribe and translate" },
        ]} />
      </div>
      <div className="m-form m-form-4">
        <Field id="lv-name" label="Name" hint={st.mode === "wfm" ? "Optional: an FM station names itself from RDS." : "Optional"}>
          <Input id="lv-name" value={name} placeholder={st.rds?.ps?.trim() || `${fmtMhz(st.mhz, st.mode)} MHz`} onChange={(e) => setName(e.target.value)} />
        </Field>
        {does !== "listen" && <SpokenField id="lv-lang" lang={lang} onLang={setLang} langs={langs} onLangs={setLangs} known={knownLangs()} />}
      </div>
      {retune && (
        <div ref={question} className="m-panel rd-retune">
          <p className="m-line">{retune}</p>
          <div className="m-actions">
            <ActButton variant="primary" run={() => save(true)}>Retune and keep it</ActButton>
            <Button variant="ghost" onClick={() => { setRetune(null); requestAnimationFrame(() => document.getElementById("lv-keep-add")?.focus()); }}>Cancel</Button>
          </div>
        </div>
      )}
      <div className="m-actions">
        <ActButton id="lv-keep-add" variant="primary" icon="plus" run={() => save(false)}>Keep as a station</ActButton>
        {st.privacy !== "broadcast" && (
          <span className="m-hint">This is {st.privacy} radio: what's kept of it follows <a className="rd-link" href="#/settings/storage">What's kept</a>.</span>
        )}
      </div>
    </Section>
  );
}
