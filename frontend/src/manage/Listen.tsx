// Listen's radio: what the receivers found here, each a press from playing, and a receiver tuned by
// hand, like a radio or an SDR program. Nothing heard here is written down, stored or checked for
// alerts; "Keep as a station" makes it one (radio/live.py).
import { useEffect, useState } from "react";
import { ActButton, Button, Field, Input, Lamp, Section, Segmented, Select, Table, act, cx, toast } from "../components/ui";
import { langName } from "../lib/lang";
import { ApiError, type Any, HANDOFF, del, post, refreshPage } from "./api";
import { fmtMhz } from "./bands";
import { OnAirHere, Responsibility, useRadio } from "./Radio";
import { RxSwitch, rxName } from "./Receivers";
import { Scope, snap } from "./Scope";
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
  const t = useTuner(handedOver);
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
        <Start receivers={receivers} radio={radio} dev={dev} onRx={(id) => moveTo(id, null)} tune={(b) => tune(b, true)} />
      )}
    </>
  );
}

// -- before listening ------------------------------------------------------------------------

function Start({ receivers, radio, dev, onRx, tune }: { receivers: Any[]; radio: Any; dev: string; onRx: (id: string) => void; tune: (b: Any) => Promise<Any> }) {
  const [mhz, setMhz] = useState(remember("ws-listen-mhz") || "");
  const [mode, setMode] = useState<string>("");
  const rx = receivers.find((r) => r.id === dev);
  const f = +mhz;
  const go = (m: number, md?: string) => { remember("ws-listen-mhz", String(m)); return tune({ mhz: m, mode: md || undefined }); };
  const bands: Any[] = (radio?.bands || []).filter((b: Any) => b.mode && b.tunable && b.privacy !== "digital");
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
      {bands.length > 0 && (
        <Section id="lv-bands" title="Bands where you are" description="Start at a band, then seek or scan through it.">
          <div className="lv-chips">
            {bands.map((b) => (
              <Button key={b.id} size="sm" variant="secondary" onClick={() => go(b.channels_mhz?.[0] ?? +(b.mode === "wfm" ? b.start_mhz + 0.1 : b.start_mhz).toFixed(5), b.mode)}>
                {b.name}
              </Button>
            ))}
          </div>
        </Section>
      )}
    </>
  );
}

// -- listening -------------------------------------------------------------------------------

interface TunedProps {
  t: Tuner; radio: Any; ids: string[]; onRx: (id: string) => void; play: () => void; onKept: () => Promise<void>;
}

function Tuned({ t, radio, ids, onRx, play, onKept }: TunedProps) {
  const { st, dev, close, blocked, setSt: onPoll } = t;
  const band = st.band;
  const [zoom, setZoom] = useState<Zoom>(() => (st.mode === "wfm" ? "full" : "400"));
  const wide = st.mode === "wfm";
  useEffect(() => { setZoom(wide ? "full" : "400"); }, [wide]); // a narrow channel is lost in the whole band
  const [view, setView] = useState<[number, number] | null>(null);

  const lo = st.center_mhz - st.span_mhz / 2, hi = st.center_mhz + st.span_mhz / 2;
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
  return (
    <>
      <Section id="lv-tuned" title="Tune by hand" actions={<Button icon="stop" onClick={close}>Stop listening</Button>}>
        <RxSwitch ids={ids} value={dev} onChange={onRx}
                  note={st.holders?.length ? `Shared with ${st.holders.join(", ")}: a frequency outside its band, or another gain, needs it to itself.` : null} />
        <TunedHead t={t} steps={steps} />
        <TuneControls t={t} steps={steps} play={play} />
        {blocked && <p className="m-trouble">The browser held the sound back: press Play.</p>}
        {st.clipped > 0.001 && <p className="m-trouble">The receiver is overloaded (its converter clips): lower the gain.</p>}
        {!st.connected && (st.error || st.reconnects > 0) && (
          <p className="m-trouble">{st.error ? `Receiver ${rxName(dev)} stopped: ${st.error}. Tune again to restart it.` : `Receiver ${rxName(dev)} has stopped sending: reconnecting…`}</p>
        )}

        <Scope spectrum={st.spectrum || null} centerMhz={st.center_mhz} spanMhz={st.span_mhz} view={view} tunedMhz={st.mhz} mode={st.mode}
               channelKhz={st.channel_khz} stepKhz={steps.stepKhz} band={band} markers={markers} onTune={steps.to} onStep={steps.step} />
        <div className="lv-under">
          <Segmented label="Zoom" value={zoom} onChange={setZoom} options={[...ZOOMS]} />
          <span className="m-hint">Click to tune, scroll or use the arrow keys to step. Receiver {rxName(dev)} hears {lo.toFixed(2)} to {hi.toFixed(2)} MHz.</span>
        </div>
        {st.paused?.length > 0 && <p className="m-hint">Paused while you listen: {st.paused.join(", ")}. They start again when you stop listening.</p>}
      </Section>
      {isNarrow && <ScanPanel st={st} radio={radio} dev={dev} view={view} stepKhz={steps.stepKhz} onPoll={onPoll} />}
      <Keep st={st} radio={radio} dev={dev} onKept={onKept} />
    </>
  );
}

// -- scanning --------------------------------------------------------------------------------

function ScanPanel({ st, radio, dev, view, stepKhz, onPoll }: { st: Any; radio: Any; dev: string; view: [number, number] | null; stepKhz: number; onPoll: (s: Any) => void }) {
  const band = st.band && st.band.mode === st.mode ? st.band : null;
  const [source, setSource] = useState<string>(band ? "band" : "view");
  const [threshold, setThreshold] = useState(10);
  const [hold, setHold] = useState(2);
  const margin = 0.06;
  const lo = st.center_mhz - st.span_mhz / 2 + margin, hi = st.center_mhz + st.span_mhz / 2 - margin;
  const heard: number[] = (radio?.heard || []).filter((e: Any) => e.mode === st.mode && e.mhz >= lo && e.mhz <= hi).map((e: Any) => e.mhz);
  const lists: Record<string, number[]> = {
    band: band ? raster(Math.max(lo, band.start_mhz), Math.min(hi, band.stop_mhz), band.step_khz || stepKhz, band) : [],
    heard,
    view: raster(view ? view[0] : lo, view ? view[1] : hi, stepKhz, null),
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
                ...(band ? [["band", `${band.name} · ${lists.band.length}`] as [string, string]] : []),
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

function Keep({ st, radio, dev, onKept }: { st: Any; radio: Any; dev: string; onKept: () => Promise<void> }) {
  const [does, setDoes] = useState<string>("translate");
  const [lang, setLang] = useState<string>(radio?.defaults?.language || "");
  const [name, setName] = useState("");
  const [retune, setRetune] = useState<string | null>(null);
  const save = async (force = false) => {
    try {
      const v = await post(`/live/${dev}/station`, { does, language: lang.trim(), name: name.trim() || st.rds?.ps?.trim() || undefined, translate_to: radio?.defaults?.translate_to, retune: force });
      toast(`${v.name} is a station now${does === "listen" ? "" : ": it's on the reading page"}`);
      setRetune(null);
      await onKept();
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
        {does !== "listen" && (
          <Field id="lv-lang" label="Spoken language" hint={lang ? langName(lang) : "A BCP-47 tag, e.g. es-ES"}>
            <Input id="lv-lang" list="m-langs" value={lang} placeholder="e.g. es-ES" onChange={(e) => setLang(e.target.value)} />
          </Field>
        )}
      </div>
      {retune && (
        <div className="m-panel rd-retune">
          <p className="m-line">{retune}</p>
          <div className="m-actions">
            <ActButton variant="primary" run={() => save(true)}>Retune and keep it</ActButton>
            <Button variant="ghost" onClick={() => setRetune(null)}>Cancel</Button>
          </div>
        </div>
      )}
      <div className="m-actions">
        <ActButton variant="primary" icon="plus" disabled={does !== "listen" && !lang.trim()} run={() => save(false)}>Keep as a station</ActButton>
        {st.privacy !== "broadcast" && (
          <span className="m-hint">This is {st.privacy} radio: what's kept of it follows <a className="rd-link" href="#/settings/storage">What's kept</a>.</span>
        )}
      </div>
    </Section>
  );
}
