// Listen's radio: what the receivers found here, each a press from playing, and a receiver tuned by
// hand, like a radio or an SDR program. Nothing heard here is written down, stored or checked for
// alerts; "Keep as a station" makes it one (radio/live.py).
import { useCallback, useEffect, useRef, useState } from "react";
import { ActButton, Button, Check, Field, Input, Lamp, Section, Segmented, Select, Table, act, cx, toast } from "../components/ui";
import { langName } from "../lib/lang";
import { A, stopAudio } from "../lib/audio";
import { S } from "../lib/live";
import { useTopics } from "../lib/store";
import { ApiError, type Any, HANDOFF, del, get, post, refreshPage, useEvery, useLoad } from "./api";
import { bandLine, fmtMhz } from "./bands";
import { OnAirHere, Responsibility, useRadio } from "./Radio";
import { RxSwitch, rxName } from "./Receivers";
import { Scope, snap } from "./Scope";

const GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6];
const STEPS: Record<string, number[]> = { wfm: [100, 200, 50], nfm: [12.5, 6.25, 25, 5], am: [25, 8.33, 9, 5] };
const ZOOMS = [
  { value: "full", label: "Whole band" },
  { value: "400", label: "400 kHz" },
  { value: "100", label: "100 kHz" },
  { value: "25", label: "25 kHz" },
] as const;
type Zoom = (typeof ZOOMS)[number]["value"];
const MODES = [
  { value: "wfm", label: "WFM", name: "WFM, broadcast FM" },
  { value: "nfm", label: "NFM", name: "NFM: walkie-talkies, marine, amateur" },
  { value: "am", label: "AM", name: "AM: aircraft, medium wave" },
] as const;

const remember = (k: string, v?: string) => {
  try {
    if (v === undefined) return localStorage.getItem(k);
    localStorage.setItem(k, v);
  } catch {
    /* private window: not remembered */
  }
  return null;
};

// -- the player ------------------------------------------------------------------------------

/** Plays the receiver's raw 16-bit PCM on a Web Audio clock, through a volume control. */
class Player {
  ctx: AudioContext;
  gain: GainNode;
  abort = new AbortController();
  next = 0;
  rate: number;
  constructor(url: string, volume: number, onEnd: () => void) {
    this.rate = S.audioRate || 16000;
    try {
      this.ctx = new AudioContext({ sampleRate: this.rate });
    } catch {
      this.ctx = new AudioContext();
    }
    this.gain = this.ctx.createGain();
    this.gain.gain.value = volume;
    this.gain.connect(this.ctx.destination);
    this.ctx.resume();
    this.pump(url).catch(() => {}).finally(onEnd);
  }
  set volume(v: number) { this.gain.gain.value = v; }
  get blocked() { return this.ctx.state === "suspended"; }
  private async pump(url: string) {
    const r = await fetch(url, { signal: this.abort.signal, cache: "no-store" });
    if (!r.ok || !r.body) throw new Error(await r.text());
    const reader = r.body.getReader();
    let carry = new Uint8Array(0);
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      const bytes = new Uint8Array(carry.length + value.length);
      bytes.set(carry);
      bytes.set(value, carry.length);
      const usable = bytes.length - (bytes.length % 2);
      carry = bytes.slice(usable);
      if (usable) this.schedule(new Int16Array(bytes.buffer.slice(0, usable)));
    }
  }
  private schedule(pcm: Int16Array) {
    const buf = this.ctx.createBuffer(1, pcm.length, this.rate);
    const ch = buf.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(this.gain);
    const now = this.ctx.currentTime;
    if (this.next < now + 0.02) this.next = now + 0.3; // start, or recover after a gap
    src.start(this.next);
    this.next += buf.duration;
  }
  stop() {
    this.abort.abort();
    this.ctx.close().catch(() => {});
  }
}

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
  const { data: all, error, reload } = useLoad(() => get("/live"));
  const radioState = useRadio();
  const radio = radioState.v;
  const receivers: Any[] = all?.receivers || [];
  const [dev, setDevState] = useState<string>("");
  const [st, setSt] = useState<Any | null>(null);
  const [ask, setAsk] = useState<{ code: string; message: string; body: Any; holders?: string[] } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [blocked, setBlocked] = useState(false);
  useTopics("audio");
  const volume = A.volume;
  const player = useRef<Player | null>(null);
  const wanted = useRef(false); // the listener's own play or pause; a stream that ends by itself leaves it
  // A frequency asked for, until a receiver is chosen for it; then what to tune on that receiver.
  const [asked, setAsked] = useState<Any | null>(handedOver);
  const [pre, setPre] = useState<Any | null>(null);

  const setDev = (d: string) => { setDevState(d); remember("ws-listen-device", d); };
  // For a frequency asked for: the receiver named, the one already listening, one whose window
  // holds it, a free one. With nothing asked: the one open, else the one used last.
  useEffect(() => {
    if (!receivers.length || (dev && !asked)) return;
    if (!asked) {
      const pick = receivers.find((r) => r.open) || receivers.find((r) => r.id === remember("ws-listen-device")) || receivers[0];
      setDev(pick.id);
      if (pick.open) setSt(pick);
      return;
    }
    const cur = receivers.find((r) => r.id === dev);
    const hears = (r: Any) => Math.abs(asked.mhz - r.center_mhz) < r.span_mhz / 2 - 0.06;
    const pick = receivers.find((r) => r.id === asked.device) || (st?.open ? cur : null) || receivers.find(hears)
      || receivers.find((r) => !r.holders.length) || cur || receivers[0];
    setAsked(null);
    moveTo(pick.id, { mhz: asked.mhz, mode: asked.mode });
  }, [receivers, asked]);

  const stopPlayer = () => { wanted.current = false; player.current?.stop(); player.current = null; setPlaying(false); setBlocked(false); };
  const startPlayer = useCallback((audio: string) => {
    wanted.current = true;
    if (player.current) return;
    stopAudio(); // one sound at a time: a station playing gives way to the tuner
    const p = new Player(`/audio/${audio}.pcm`, volume, () => { if (player.current === p) { player.current = null; setPlaying(false); } });
    player.current = p;
    setPlaying(true);
    setTimeout(() => setBlocked(p.blocked), 300);
  }, [volume]);
  useEffect(() => () => stopPlayer(), []); // leaving the tab stops the sound; the receiver closes when idle
  useEffect(() => { if (player.current) player.current.volume = volume; }, [volume]);
  useEffect(() => { if (A.playing && player.current) stopPlayer(); }, [A.playing]); // and the tuner to a station

  const gen = useRef(0); // bumps around every retune: a poll that started before one is stale
  const busy = useRef(false);
  const queued = useRef<{ body: Any; play: boolean } | null>(null);
  const tune = async (body: Any, play = false): Promise<Any> => {
    gen.current++;
    const shown = Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined));
    setSt((old: Any) => old && { ...old, ...shown }); // show it now; the next step counts from here
    if (busy.current) {
      queued.current = { body, play }; // latest wins: a wheel or slider burst is one retune, not fifty
      return null;
    }
    busy.current = true;
    try {
      const s = await post(`/live/${dev}`, body);
      setAsk(null);
      setSt((old: Any) => ({ ...s, spectrum: old?.spectrum }));
      // A receiver that stopped while playing plays again once a retune brings it back.
      if (play || (wanted.current && !player.current)) startPlayer(s.audio);
      return s;
    } catch (e) {
      if (e instanceof ApiError && (e.code === "acknowledge" || e.code === "busy")) {
        setAsk({ code: e.code, message: e.message, body: { ...body, ...(play ? { _play: true } : {}) }, holders: e.data.holders });
        return null;
      }
      toast((e as Error).message, true);
      return null;
    } finally {
      busy.current = false;
      gen.current++;
      const next = queued.current;
      queued.current = null;
      if (next) void tune(next.body, next.play);
    }
  };
  const resend = async (extra: Any) => {
    if (!ask) return;
    const { _play, ...body } = ask.body;
    await tune({ ...body, ...extra }, !!_play);
  };
  const close = async () => {
    stopPlayer();
    await act(async () => { await del(`/live/${dev}`); });
    setSt(null);
    await reload();
  };
  /** Moves the tuner to receiver `id`, letting go of the one it leaves, and tunes `then` there. */
  const moveTo = (id: string, then: Any | null) => {
    if (id !== dev) {
      if (st?.open) {
        stopPlayer();
        del(`/live/${dev}`).then(reload).catch(() => {});
      }
      setSt(null);
      setDev(id);
    }
    if (then) setPre(then);
  };
  // Tuned once the receiver is chosen: this render's `tune` posts to it.
  useEffect(() => {
    if (!pre || !dev) return;
    setPre(null);
    remember("ws-listen-mhz", String(pre.mhz));
    void tune({ mhz: pre.mhz, mode: pre.mode || undefined }, true);
  }, [pre, dev]);

  const open = st?.open && st.device === dev;
  const air = (
    <OnAirHere radio={radioState} playingMhz={open && playing ? st.mhz : null} onPlay={setAsked} onStop={close} onReceiver={reload} />
  );
  if (!all) return <>{air}<p className="m-loading">{error ? `Couldn't load the receivers: ${error}` : "Loading…"}</p></>;
  // Without a receiver there is nothing to tune: On the air here offers a dongle to use.
  if (!receivers.length) return air;
  const ids = receivers.map((r) => r.id);
  return (
    <>
      {air}
      <Responsibility />
      {ask && <Ask ask={ask} onYes={resend} onNo={() => setAsk(null)} />}
      {open ? (
        <Tuned st={st} radio={radio} dev={dev} ids={ids} onRx={(id) => moveTo(id, { mhz: st.mhz, mode: st.mode })} tune={tune} close={close}
               playing={playing} blocked={blocked} play={() => { stopPlayer(); startPlayer(st.audio); }} stop={stopPlayer}
               gen={gen} onPoll={setSt} onKept={async () => { stopPlayer(); setSt(null); await reload(); await refreshPage(); }} />
      ) : (
        <Start receivers={receivers} radio={radio} dev={dev} onRx={(id) => moveTo(id, null)} tune={(b) => tune(b, true)} />
      )}
    </>
  );
}

/** A refusal the user can answer: confirm they may receive this class of radio, or pause the
 * stations that hold the dongle. */
function Ask({ ask, onYes, onNo }: { ask: Any; onYes: (extra: Any) => Promise<void>; onNo: () => void }) {
  const [ok, setOk] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  // Asked for from a row of On the air here, the question can sit out of view below it.
  useEffect(() => { panel.current?.scrollIntoView({ block: "nearest" }); }, []);
  return (
    <div ref={panel} className="m-panel lv-ask" role="alertdialog" aria-labelledby="lv-ask-t">
      <p id="lv-ask-t" className="m-line">{ask.message}</p>
      {ask.code === "acknowledge" ? (
        <>
          <p className="m-line">
            <Check checked={ok} onChange={setOk}>
              I've checked that I may receive this where I am, and I'm responsible for how I use what I hear.
            </Check>
          </p>
          <div className="m-actions">
            <ActButton variant="primary" icon="speaker" disabled={!ok} run={() => onYes({ acknowledge: true })}>Listen</ActButton>
            <Button variant="ghost" onClick={onNo}>Cancel</Button>
          </div>
        </>
      ) : (
        <div className="m-actions">
          <ActButton variant="primary" run={() => onYes({ pause: true })}>Pause {ask.holders?.length === 1 ? ask.holders[0] : "them"} while I listen</ActButton>
          <Button variant="ghost" onClick={onNo}>Cancel</Button>
        </div>
      )}
    </div>
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
  st: Any; radio: Any; dev: string; ids: string[]; onRx: (id: string) => void;
  tune: (b: Any, play?: boolean) => Promise<Any>; close: () => Promise<void>;
  playing: boolean; blocked: boolean; play: () => void; stop: () => void;
  gen: { current: number }; onPoll: (s: Any) => void; onKept: () => Promise<void>;
}

function Tuned({ st, radio, dev, ids, onRx, tune, close, playing, blocked, play, stop, gen, onPoll, onKept }: TunedProps) {
  const polling = useRef(false);
  const band = st.band;
  const [stepKhz, setStepKhz] = useState<number>(() => band?.step_khz && band.mode === st.mode ? band.step_khz : STEPS[st.mode][0]);
  useEffect(() => { setStepKhz(band?.step_khz && band.mode === st.mode ? band.step_khz : STEPS[st.mode][0]); }, [st.mode, band?.id]);
  const [zoom, setZoom] = useState<Zoom>(() => (st.mode === "wfm" ? "full" : "400"));
  const wide = st.mode === "wfm";
  useEffect(() => { setZoom(wide ? "full" : "400"); }, [wide]); // a narrow channel is lost in the whole band
  const [view, setView] = useState<[number, number] | null>(null);
  const [entry, setEntry] = useState(fmtMhz(st.mhz, st.mode));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setEntry(fmtMhz(st.mhz, st.mode)); }, [st.mhz, st.mode, editing]);

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

  useEvery(250, async () => {
    if (polling.current) return; // one poll at a time on a slow link
    polling.current = true;
    const g = gen.current;
    try {
      const q = view ? `?lo=${view[0].toFixed(5)}&hi=${view[1].toFixed(5)}` : "";
      const s = await get(`/live/${dev}${q}`);
      if (g === gen.current) onPoll(s); // a retune happened meanwhile: this answer is older than it
    } catch {
      /* the next poll tries again */
    } finally {
      polling.current = false;
    }
  });

  const keep = (m: Any) => ({ mode: st.mode, squelch_db: st.squelch_db, ...m });
  const to = (mhz: number) => tune(keep({ mhz: +mhz.toFixed(5) }));
  const step = (n: number) => to(snap(st.mhz + (n * stepKhz) / 1000, stepKhz, band));
  const seek = (dir: 1 | -1) => {
    const s = st.spectrum;
    if (!s) return;
    const n = s.db.length, f0 = s.start_hz / 1e6, df = (s.stop_hz - s.start_hz) / 1e6 / n;
    const level = s.noise_db + (st.mode === "wfm" ? 15 : 10);
    const groups: number[] = [];
    let start = -1;
    for (let i = 0; i <= n; i++) {
      const on = i < n && s.db[i] >= level;
      if (on && start < 0) start = i;
      if (!on && start >= 0) { groups.push(f0 + ((start + i - 1) / 2 + 0.5) * df); start = -1; }
    }
    const min = (stepKhz / 1000) * 0.6;
    const ahead = groups.filter((g) => (g - st.mhz) * dir > min).sort((a, b) => (a - b) * dir);
    if (ahead.length) return to(snap(ahead[0], stepKhz, band));
    if (view) return toast("Nothing stronger that way in this view: zoom out to seek further.");
    // Nothing more in the band the dongle hears: move it along and look again.
    const edge = dir > 0 ? hi : lo;
    return to(snap(edge + dir * (st.mode === "wfm" ? 0.3 : 0.1), stepKhz, band));
  };
  const enter = () => {
    const f = +entry;
    setEditing(false);
    if (f > 0 && Math.abs(f - st.mhz) > 1e-6) tune({ mhz: f });
  };

  const markers: Any[] = (radio?.receivers || []).find((r: Any) => r.id === dev)?.stations || [];
  const sig = st.signal_db;
  const isNarrow = st.mode !== "wfm";
  return (
    <>
      <Section id="lv-tuned" title="Tune by hand" actions={<Button icon="stop" onClick={close}>Stop listening</Button>}>
        <RxSwitch ids={ids} value={dev} onChange={onRx}
                  note={st.holders?.length ? `Shared with ${st.holders.join(", ")}: a frequency outside its band, or another gain, needs it to itself.` : null} />
        <div className="lv-head">
          <label className="lv-freq">
            <span className="sr-only">Frequency in MHz</span>
            <input value={entry} inputMode="decimal" className="lv-freq-input" aria-describedby="lv-freq-unit"
                   style={{ width: `${Math.max(5, entry.length) + 0.4}ch` }}
                   onFocus={() => setEditing(true)} onBlur={enter} onChange={(e) => setEntry(e.target.value)}
                   onKeyDown={(e) => {
                     if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                     if (e.key === "Escape") { setEditing(false); setEntry(fmtMhz(st.mhz, st.mode)); (e.target as HTMLInputElement).blur(); }
                     if (e.key === "ArrowUp" || e.key === "ArrowDown") { e.preventDefault(); step(e.key === "ArrowUp" ? 1 : -1); }
                   }} />
            <span id="lv-freq-unit" className="lv-unit">MHz</span>
          </label>
          <div className="lv-what">
            {st.rds?.ps ? <strong className="lv-ps" dir="auto">{st.rds.ps.trim()}</strong> : <strong>{band ? band.name : "No band plan lists this frequency"}</strong>}
            {st.rds?.rt && <span className="lv-rt" dir="auto">{st.rds.rt}</span>}
            {!st.rds?.ps && bandLine(band?.id) && <span className="lv-rt">{bandLine(band?.id)}</span>}
            {!st.rds?.ps && band?.channels_mhz?.length > 0 && (() => {
              const i = band.channels_mhz.findIndex((c: number) => Math.abs(c - st.mhz) < 0.002);
              return i >= 0 ? <span className="lv-rt">Channel {i + 1}</span> : null;
            })()}
          </div>
          <div className="lv-meter-wrap" aria-live="off">
            {isNarrow && <Lamp state={st.squelch_open ? "speech" : "dim"} label={st.squelch_open ? "Signal: squelch open" : "Squelch closed"} />}
            <span className="lv-meter" role="meter" aria-label="Signal level" aria-valuemin={-100} aria-valuemax={0} aria-valuenow={sig ?? -100}>
              <span className="lv-meter-fill" style={{ width: `${Math.max(0, Math.min(100, ((sig ?? -100) + 90) / 0.8))}%` }} />
            </span>
            <span className="lv-meter-num mono">{sig != null ? `${sig.toFixed(0)} dBFS` : "–"}</span>
          </div>
        </div>

        <div className="lv-controls">
          <div className="lv-group" role="group" aria-label="Tuning">
            <Button size="sm" variant="secondary" icon="left" aria-label="Seek down" title="Seek down: the next signal below" onClick={() => seek(-1)} />
            <Button size="sm" variant="secondary" icon="minus" aria-label={`Down ${stepKhz} kHz`} onClick={() => step(-1)} />
            <Select aria-label="Step" value={String(stepKhz)} onChange={(e) => setStepKhz(+e.target.value)}
                    options={[...new Set([...(band?.step_khz && band.mode === st.mode ? [band.step_khz] : []), ...STEPS[st.mode]])].map((k) => [String(k), `${k} kHz`])} />
            <Button size="sm" variant="secondary" icon="plus" aria-label={`Up ${stepKhz} kHz`} onClick={() => step(1)} />
            <Button size="sm" variant="secondary" icon="right" aria-label="Seek up" title="Seek up: the next signal above" onClick={() => seek(1)} />
          </div>
          <Segmented label="Demodulation" value={st.mode} onChange={(m) => tune({ mhz: st.mhz, mode: m })} options={[...MODES]} />
          {isNarrow && (
            <label className="lv-slider">
              <span>Squelch</span>
              <input type="range" min={-100} max={-20} step={1} value={st.squelch_db ?? -100}
                     aria-valuetext={st.squelch_db == null ? "automatic" : `${st.squelch_db} dBFS`}
                     onChange={(e) => tune(keep({ mhz: st.mhz, squelch_db: +e.target.value <= -100 ? null : +e.target.value }))} />
              <span className="mono lv-slider-v">{st.squelch_db == null ? "auto" : st.squelch_db}</span>
            </label>
          )}
          <label className="lv-slider">
            <span>Gain</span>
            <Select aria-label="Receiver gain" value={String(st.gain_db)} onChange={(e) => tune(keep({ mhz: st.mhz, gain_db: +e.target.value }))}
                    options={[...new Set([st.gain_db, ...GAINS])].sort((a, b) => a - b).map((g) => [String(g), `${g} dB`])} />
          </label>
          <div className="lv-group lv-audio">
            {playing ? (
              <Button size="sm" variant="secondary" icon="mute" onClick={stop}>Mute</Button>
            ) : (
              <Button size="sm" variant="primary" icon="speaker" onClick={play}>Play</Button>
            )}
          </div>
        </div>
        {blocked && <p className="m-trouble">The browser held the sound back: press Play.</p>}
        {st.clipped > 0.001 && <p className="m-trouble">The receiver is overloaded (its converter clips): lower the gain.</p>}
        {!st.connected && (st.error || st.reconnects > 0) && (
          <p className="m-trouble">{st.error ? `Receiver ${rxName(dev)} stopped: ${st.error}. Tune again to restart it.` : `Receiver ${rxName(dev)} has stopped sending: reconnecting…`}</p>
        )}

        <Scope spectrum={st.spectrum || null} centerMhz={st.center_mhz} spanMhz={st.span_mhz} view={view} tunedMhz={st.mhz} mode={st.mode}
               channelKhz={st.channel_khz} stepKhz={stepKhz} band={band} markers={markers} onTune={to} onStep={step} />
        <div className="lv-under">
          <Segmented label="Zoom" value={zoom} onChange={setZoom} options={[...ZOOMS]} />
          <span className="m-hint">Click to tune, scroll or use the arrow keys to step. Receiver {rxName(dev)} hears {lo.toFixed(2)} to {hi.toFixed(2)} MHz.</span>
        </div>
        {st.paused?.length > 0 && <p className="m-hint">Paused while you listen: {st.paused.join(", ")}. They start again when you stop listening.</p>}
      </Section>
      {isNarrow && <ScanPanel st={st} radio={radio} dev={dev} view={view} stepKhz={stepKhz} onPoll={onPoll} />}
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
