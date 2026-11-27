// A receiver tuned by hand, as the page holds it: tuning it (with the questions the server may ask
// first), polling what it hears, and playing its sound. Nothing heard this way is written down,
// stored or checked for alerts (radio/live.py).
import { useCallback, useEffect, useRef, useState } from "react";
import { ActButton, Button, Check, Lamp, Segmented, Select, act, toast } from "../components/ui";
import { A, stopAudio } from "../lib/audio";
import { S } from "../lib/live";
import { useTopics } from "../lib/store";
import { ApiError, type Any, del, get, post, useEvery, useLoad } from "./api";
import { and, bandLine, fmtMhz } from "./bands";

import { rxName } from "./Receivers";
import { snap } from "./Scope";

/** The gains an RTL-SDR's tuner offers, in dB. */
export const GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6];
/** A few gains named by what they are for, low to high: the rest are under "More". The tuner
 * has no automatic gain (a receiver's own gain is set under Settings, Receivers). */
const GAIN_WORDS: [number, string][] = [[20.7, "Low"], [29.7, "Medium"], [38.6, "High"], [49.6, "Highest"]];
/** A step in kHz as read: at most two decimals ("8.33 kHz", not 8.333333). */
const khz = (k: number) => `${+k.toFixed(2)} kHz`;
/** Tuning steps for each demodulation, the usual one first, in kHz. */
export const STEPS: Record<string, number[]> = { wfm: [100, 200, 50], nfm: [12.5, 6.25, 25, 5], am: [25, 8.33, 9, 5] };
export const MODES = [
  { value: "wfm", label: "WFM", name: "WFM, broadcast FM" },
  { value: "nfm", label: "NFM", name: "NFM: walkie-talkies, marine, amateur" },
  { value: "am", label: "AM", name: "AM: aircraft, medium wave" },
] as const;

/** A choice this browser remembers (the receiver and frequency used last), when it may. */
export const remember = (k: string, v?: string) => {
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
export class Player {
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

/** A receiver tuned by hand from the page (radio/live.py): the receivers, the one in use, what it
 * is tuned to, the question the server asked before it would tune (acknowledge, busy), and the
 * sound. Listen's tuner and the dial on Find, On the air share it, so a receiver tuned on one is
 * tuned on the other. `first`: a frequency to tune and play as soon as a receiver is chosen. */
export function useTuner(first: () => Any | null = () => null) {
  const { data: all, error, reload } = useLoad(() => get("/live"));
  const receivers: Any[] = all?.receivers || [];
  const [dev, setDevState] = useState<string>("");
  const [st, setSt] = useState<Any | null>(null);
  const [ask, setAsk] = useState<{ code: string; message: string; body: Any; holders?: string[]; dev: string } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [blocked, setBlocked] = useState(false);
  useTopics("audio");
  const volume = A.volume;
  const player = useRef<Player | null>(null);
  const wanted = useRef(false); // the listener's own play or pause; a stream that ends by itself leaves it
  // A frequency asked for, until a receiver is chosen for it; then what to tune on that receiver.
  const [asked, setAsked] = useState<Any | null>(first);
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
      // Once someone has said a receiver's stations may pause for listening, they aren't asked
      // again for that receiver until the browser session ends.
      if (e instanceof ApiError && e.code === "busy" && !body.pause && pausesFine(dev)) {
        busy.current = false;
        return tune({ ...body, pause: true }, play);
      }
      if (e instanceof ApiError && (e.code === "acknowledge" || e.code === "busy")) {
        setAsk({ code: e.code, message: e.message, body: { ...body, ...(play ? { _play: true } : {}) }, holders: e.data.holders, dev });
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
    if (extra.pause) pausesFine(ask.dev, true);
    const { _play, ...body } = ask.body;
    await tune({ ...body, ...extra }, !!_play);
  };
  const close = async () => {
    stopPlayer();
    await act(async () => { await del(`/live/${dev}`); });
    setSt(null);
    await reload();
  };
  /** Moves the tuner to receiver `id`, letting go of the one it leaves, and tunes `then` there
   * (with whatever else it carries, such as `pause`); `play` false only looks, without sound. */
  const moveTo = (id: string, then: Any | null, play = true) => {
    if (id !== dev) {
      if (st?.open) {
        stopPlayer();
        del(`/live/${dev}`).then(reload).catch(() => {});
      }
      setSt(null);
      setDev(id);
    }
    if (then) setPre({ ...then, play });
  };
  // Tuned once the receiver is chosen: this render's `tune` posts to it.
  useEffect(() => {
    if (!pre || !dev) return;
    setPre(null);
    const { play, ...body } = pre;
    remember("ws-listen-mhz", String(body.mhz));
    void tune({ ...body, mode: body.mode || undefined }, play);
  }, [pre, dev]);

  return { loaded: !!all, receivers, error, reload, dev, st, setSt, ask, setAsk, resend, tune, close, moveTo, asked: setAsked,
    playing, blocked, startPlayer, stopPlayer, gen };
}
export type Tuner = ReturnType<typeof useTuner>;

// -- tuning a receiver that is open ----------------------------------------------------------

/** Polls the receiver tuned by hand a few times a second for its spectrum, signal and RDS. An
 * answer to a poll that started before a retune is older than it, and dropped. `view`: a zoomed
 * part of what it hears, in MHz. */
export function useLivePoll(t: Tuner, view: [number, number] | null) {
  const polling = useRef(false);
  useEvery(250, async () => {
    if (polling.current) return; // one poll at a time on a slow link
    polling.current = true;
    const g = t.gen.current;
    try {
      const q = view ? `?lo=${view[0].toFixed(5)}&hi=${view[1].toFixed(5)}` : "";
      const s = await get(`/live/${t.dev}${q}`);
      if (g === t.gen.current) t.setSt(s);
    } catch {
      /* the next poll tries again */
    } finally {
      polling.current = false;
    }
  });
}

/** Moving the tuning of an open receiver: to a frequency, by steps of the band's raster, and to
 * the next signal either way (`seek`), which moves the dongle along at the edge of what it hears. */
export function useSteps(t: Tuner, view: [number, number] | null) {
  const st = t.st;
  const band = st.band;
  const usual = () => (band?.step_khz && band.mode === st.mode ? band.step_khz : STEPS[st.mode][0]);
  const [stepKhz, setStepKhz] = useState<number>(usual);
  useEffect(() => { setStepKhz(usual()); }, [st.mode, band?.id]);
  const lo = st.center_mhz - st.span_mhz / 2, hi = st.center_mhz + st.span_mhz / 2;
  const keep = (m: Any) => ({ mode: st.mode, squelch_db: st.squelch_db, ...m });
  const to = (mhz: number) => t.tune(keep({ mhz: +mhz.toFixed(5) }));
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

  const steps = [...new Set([...(band?.step_khz && band.mode === st.mode ? [band.step_khz] : []), ...STEPS[st.mode]])];
  return { stepKhz, setStepKhz, steps, keep, to, step, seek, lo, hi };
}
export type Steps = ReturnType<typeof useSteps>;

/** The tuning in large type, editable (Enter tunes, the arrow keys step), what is there (RDS,
 * the band and its channel) and the signal meter. */
export function TunedHead({ t, steps }: { t: Tuner; steps: Steps }) {
  const st = t.st;
  const band = st.band;
  const [entry, setEntry] = useState(fmtMhz(st.mhz, st.mode));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setEntry(fmtMhz(st.mhz, st.mode)); }, [st.mhz, st.mode, editing]);
  const enter = () => {
    const f = +entry;
    setEditing(false);
    if (f > 0 && Math.abs(f - st.mhz) > 1e-6) t.tune({ mhz: f });
  };
  const step = steps.step;
  const sig = st.signal_db;
  const isNarrow = st.mode !== "wfm";
  return (
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
  );
}

/** Step and seek, demodulation, squelch, gain, and the sound. */
export function TuneControls({ t, steps, play }: { t: Tuner; steps: Steps; play: () => void }) {
  const st = t.st;
  const { stepKhz, setStepKhz, keep, step, seek } = steps;
  const tune = t.tune;
  const isNarrow = st.mode !== "wfm";
  return (
    <div className="lv-controls">
      <div className="lv-group" role="group" aria-label="Tuning">
        <Button size="sm" variant="secondary" icon="left" aria-label="Seek down" title="Seek down: the next signal below" onClick={() => seek(-1)} />
        <Button size="sm" variant="secondary" icon="minus" aria-label={`Down ${khz(stepKhz)}`} onClick={() => step(-1)} />
        <Select aria-label="Step" value={String(stepKhz)} onChange={(e) => setStepKhz(+e.target.value)}
                options={steps.steps.map((k) => [String(k), khz(k)])} />
        <Button size="sm" variant="secondary" icon="plus" aria-label={`Up ${khz(stepKhz)}`} onClick={() => step(1)} />
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
                options={[...GAIN_WORDS.map(([g, w]) => [String(g), `${w}, ${g} dB`] as [string, string]),
                          ...(GAIN_WORDS.some(([g]) => g === st.gain_db) ? [] : [[String(st.gain_db), `${st.gain_db} dB`] as [string, string]])]}
                groups={[["More", GAINS.filter((g) => g !== st.gain_db && !GAIN_WORDS.some(([w]) => w === g)).map((g) => [String(g), `${g} dB`] as [string, string])]]} />
      </label>
      <div className="lv-group lv-audio">
        {t.playing ? (
          <Button size="sm" variant="secondary" icon="mute" onClick={t.stopPlayer}>Mute</Button>
        ) : (
          <Button size="sm" variant="primary" icon="speaker" onClick={play}>Play</Button>
        )}
      </div>
    </div>
  );
}

const PAUSE_KEY = "ws-pause-ok";
/** Whether this session already agreed that receiver `dev`'s stations pause while listening by
 * hand; `yes` records that it did. */
function pausesFine(dev: string, yes = false): boolean {
  try {
    const ok: string[] = JSON.parse(sessionStorage.getItem(PAUSE_KEY) || "[]");
    if (yes && !ok.includes(dev)) sessionStorage.setItem(PAUSE_KEY, JSON.stringify([...ok, dev]));
    return yes || ok.includes(dev);
  } catch {
    return yes; // without storage it asks each time
  }
}

/** A refusal the user can answer: confirm they may receive this class of radio, or pause the
 * stations that hold the dongle. */
export function Ask({ ask, onYes, onNo }: { ask: Any; onYes: (extra: Any) => Promise<void>; onNo: () => void }) {
  const [ok, setOk] = useState(false);
  const panel = useRef<HTMLDivElement>(null);
  // Asked for from a row of On the air here, the question can sit out of view below it. Focus
  // goes to its first control, so a keyboard answers it where it is.
  useEffect(() => {
    panel.current?.scrollIntoView({ block: "nearest" });
    panel.current?.querySelector<HTMLElement>("input, button")?.focus({ preventScroll: true });
  }, []);
  return (
    <div ref={panel} className="m-panel lv-ask" role="alertdialog" aria-labelledby="lv-ask-t">
      <p id="lv-ask-t" className="m-line">
        {ask.code === "busy" && ask.holders?.length
          ? `Receiver ${rxName(ask.dev)} is following ${and(ask.holders)}. Listening here pauses ${ask.holders.length === 1 ? "it" : "them"} until you stop.`
          : ask.message}
      </p>
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
          <ActButton variant="primary" run={() => onYes({ pause: true })}>Pause {ask.holders?.length > 1 ? "them" : "it"} and listen</ActButton>
          <Button variant="ghost" onClick={onNo}>Cancel</Button>
        </div>
      )}
    </div>
  );
}

