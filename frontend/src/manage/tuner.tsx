// A receiver tuned by hand, as the page holds it: tuning it (with the questions the server may ask
// first), polling what it hears, and playing its sound. Nothing heard this way is written down,
// stored or checked for alerts (radio/live.py).
import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { IconName } from "../components/Icon";
import { ActButton, Button, Check, Kbd, Lamp, Segmented, Select, act, toast } from "../components/ui";
import { A, stopAudio } from "../lib/audio";
import { S } from "../lib/live";
import { useTopics } from "../lib/store";
import { ApiError, type Any, del, get, post, useEvery, useLoad } from "./api";
import { and, bandLabel, bandLine, channelAt, fmtMhz } from "./bands";
import { type LiveReceiver, type Span, TUNER_MHZ, hears, usable } from "./dial";
import { rxName } from "./Receivers";
import { type Step, defaultStep, hasChannels, keyStep, rasterKhz, snap, stepChoices, stepFrom, stepLabel } from "./tuning";

/** The gains an RTL-SDR's tuner offers, in dB. */
export const GAINS = [0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48, 49.6];
/** A few gains named by what they are for, low to high: the rest are under "More". The tuner
 * has no automatic gain (a receiver's own gain is set under Settings → Receivers). */
const GAIN_WORDS: [number, string][] = [[20.7, "Low"], [29.7, "Medium"], [38.6, "High"], [49.6, "Highest"]];
/** At most one retune this often: a burst of steps (a key or a button held down) becomes a few
 * retunes, each to the latest frequency asked for, not one per step. */
const TUNE_GAP_MS = 150;
/** A step button held down: the first step at once, the next after HOLD_MS, then every REPEAT_MS. */
const HOLD_MS = 400;
const REPEAT_MS = 110;
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
 * is tuned to (`st`, while it is open: the views of an open receiver below, and those drawn from
 * them, are drawn only then), the question the server asked before it would tune (acknowledge,
 * busy), and the sound. Listen's tuner and the dial on Find → On the air share it, so a receiver tuned on one is
 * tuned on the other. `from`: which of the two this is. `first`: a frequency to tune and play as
 * soon as a receiver is chosen. */
export function useTuner(from: "find" | "listen", first: () => Any | null = () => null) {
  const { data: all, error, reload } = useLoad(() => get("/live"));
  const receivers: LiveReceiver[] = all?.receivers || [];
  const [dev, setDevState] = useState<string>("");
  const [st, setSt] = useState<LiveReceiver | null>(null);
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
    const holds = (r: LiveReceiver) => hears(r.span_mhz, asked.mhz, asked.mode);
    const pick = receivers.find((r) => r.id === asked.device) || (st?.open ? cur : null) || receivers.find(holds)
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
  // While a retune is on its way (and TUNE_GAP_MS after it), what was asked for: the next step
  // counts from here, and polls don't put back the frequency the receiver had before.
  const busy = useRef(false);
  const queued = useRef<{ body: Any; play: boolean } | null>(null);
  const aim = useRef<number | null>(null);
  const tune = async (body: Any, play = false): Promise<Any> => {
    gen.current++;
    if (body.mhz != null) aim.current = body.mhz;
    const shown = Object.fromEntries(Object.entries(body).filter(([, v]) => v !== undefined));
    setSt((old: Any) => old && { ...old, ...shown }); // show it now; the next step counts from here
    if (busy.current) {
      queued.current = { body, play }; // latest wins: a burst of steps is a few retunes, not fifty
      return null;
    }
    busy.current = true;
    const started = performance.now();
    let s: Any = null;
    let again = false;
    try {
      // Where the tuning was asked from, so a refusal names the place the person is in.
      s = await post(`/live/${dev}`, { ...body, from });
      setAsk(null);
      // A step asked for since this one went keeps showing until its own retune.
      const later = queued.current ? Object.fromEntries(Object.entries(queued.current.body).filter(([, v]) => v !== undefined)) : {};
      setSt((old: Any) => ({ ...s, ...later, spectrum: old?.spectrum }));
      // A receiver that stopped while playing plays again once a retune brings it back.
      if (play || (wanted.current && !player.current)) startPlayer(s.audio);
    } catch (e) {
      // Once someone has said a receiver's stations may pause for listening, they aren't asked
      // again for that receiver until the browser session ends.
      if (e instanceof ApiError && e.code === "busy" && !body.pause && pausesFine(dev)) again = true;
      else if (e instanceof ApiError && (e.code === "acknowledge" || e.code === "busy")) {
        setAsk({ code: e.code, message: e.message, body: { ...body, ...(play ? { _play: true } : {}) }, holders: e.data.holders, dev });
      } else toast((e as Error).message, true);
    }
    const release = () => {
      busy.current = false;
      gen.current++;
      const next = queued.current;
      queued.current = null;
      if (!next && !again) aim.current = null;
      return next;
    };
    if (again) {
      const next = release();
      return tune({ ...(next?.body ?? body), pause: true }, next ? next.play : play);
    }
    // The next retune waits out the gap; this one's answer doesn't.
    const wait = TUNE_GAP_MS - (performance.now() - started);
    const then = () => { const next = release(); if (next) void tune(next.body, next.play); };
    if (wait > 0) setTimeout(then, wait);
    else then();
    return s;
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
    playing, blocked, startPlayer, stopPlayer, gen, busy, aim };
}
export type Tuner = ReturnType<typeof useTuner>;

// -- tuning a receiver that is open ----------------------------------------------------------

/** Polls the receiver tuned by hand a few times a second for its spectrum, signal and RDS. An
 * answer to a poll that started before a retune is older than it, and dropped. `view`: a zoomed
 * part of what it hears, in MHz. */
export function useLivePoll(t: Tuner, view: [number, number] | null) {
  const polling = useRef(false);
  useEvery(250, async () => {
    // One poll at a time on a slow link, and none while a retune is on its way.
    if (polling.current || t.busy.current) return;
    polling.current = true;
    const g = t.gen.current;
    try {
      const q = view ? `?lo=${view[0].toFixed(5)}&hi=${view[1].toFixed(5)}` : "";
      const s = await get(`/live/${t.dev}${q}`);
      if (g === t.gen.current && !t.busy.current) t.setSt(s);
    } catch {
      /* the next poll tries again */
    } finally {
      polling.current = false;
    }
  });
}

/** Moving the tuning of an open receiver: to a frequency, by steps (a raster, or the band's
 * channels where it lists them; see tuning.ts), and to the next signal either way (`seek`), which
 * moves the dongle along at the edge of what it hears. */
export function useSteps(t: Tuner, view: [number, number] | null) {
  const st = t.st!;
  const band = st.band;
  // The step picked, for the band and demodulation it was picked in. It holds there and outside
  // every band (a run of steps past the band's edge keeps it); another band or demodulation
  // starts at its own (tuning.ts, defaultStep).
  const [pick, setPick] = useState<{ band?: string; mode: string; step: Step } | null>(null);
  const mine = pick && pick.mode === st.mode && (!band || pick.band === band.id) ? pick.step : defaultStep(band, st.mode);
  const step0: Step = mine === "channels" && !hasChannels(band) ? rasterKhz(band, st.mode) : mine;
  const choices = stepChoices(band, st.mode);
  if (!choices.includes(step0)) choices.push(step0);
  const setBy = (s: Step) => setPick({ band: band?.id, mode: st.mode, step: s });
  // The raster that clicks, seeking and scanning land on: the channels' own where stepping by channel.
  const stepKhz = typeof step0 === "number" ? step0 : rasterKhz(band, st.mode);
  const [lo, hi] = st.span_mhz;
  // Where a channel of the mode tuned is heard, for saying so: a little inside the edges.
  const heard: Span = usable(st.span_mhz, st.mode);
  const keep = (m: Any) => ({ mode: st.mode, squelch_db: st.squelch_db, ...m });
  const to = (mhz: number) => t.tune(keep({ mhz: +mhz.toFixed(5) }));
  /** `n` steps on, from the frequency last asked for (a retune still on its way counts), by
   * channel when `channel` (Page Up and Page Down). */
  const step = (n: number, channel = false) =>
    to(stepFrom(t.aim.current ?? st.mhz, n, channel ? "channels" : step0, band, TUNER_MHZ));
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

  return { by: step0, setBy, choices, stepKhz, band, keep, to, step, seek, lo, hi, heard };
}
export type Steps = ReturnType<typeof useSteps>;

// -- the keys and buttons that step ------------------------------------------------------------

/** Controls focused with a mouse or a finger, as against with Tab: a slider or a list picked with
 * the mouse gives the arrow keys back to the tuning, one reached with Tab keeps them. */
const byPointer = new WeakSet<Element>();
let pointerAt = -1e9;
if (typeof document !== "undefined") {
  document.addEventListener("pointerdown", () => {
    pointerAt = performance.now();
    // A control that already had the focus, used again with the mouse, counts as used with it.
    setTimeout(() => { if (document.activeElement) byPointer.add(document.activeElement); });
  }, true);
  document.addEventListener("focusin", (e) => {
    const el = e.target as Element;
    if (performance.now() - pointerAt < 600) byPointer.add(el);
    else byPointer.delete(el);
  }, true);
}

/** Where the arrow keys belong to what has the focus rather than to the tuning: typing, a radio
 * button, a dialog, menu or toolbar of its own, and a slider or list reached with Tab. */
const OWN_ARROWS = "dialog, [role=dialog], [role=alertdialog], [role=menu], [role=listbox], [role=toolbar], [role=radiogroup], [role=tablist], [role=grid], [role=tree]";
const NO_ARROWS = new Set(["button", "submit", "reset", "checkbox", "file", "color", "image"]);
function keepsArrows(el: Element | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  if (el.isContentEditable || el instanceof HTMLTextAreaElement) return true;
  if (el instanceof HTMLInputElement) return el.type === "range" ? !byPointer.has(el) : !NO_ARROWS.has(el.type);
  if (el instanceof HTMLSelectElement) return !byPointer.has(el);
  return !!el.closest(OWN_ARROWS);
}

/** The arrow keys step the tuning, as on a radio: up and right higher, down and left lower, ten
 * steps with Shift; Page Up and Page Down go channel by channel where the band has channels, else
 * ten steps. They are the tuning's anywhere on the screen (`box` absent), or inside `box`, except
 * where keepsArrows gives them to the control that has the focus. */
export function useTuningKeys(steps: Steps, box?: RefObject<HTMLElement | null>) {
  const latest = useRef(steps);
  useLayoutEffect(() => { latest.current = steps; });
  useEffect(() => {
    const on: HTMLElement | Window | null = box ? box.current : window;
    if (!on) return;
    const onKey = (e: Event) => {
      const k = e as KeyboardEvent;
      if (k.defaultPrevented || k.altKey || k.ctrlKey || k.metaKey || k.isComposing) return;
      const s = latest.current;
      const move = keyStep(k.key, k.shiftKey, hasChannels(s.band));
      if (!move || keepsArrows(document.activeElement)) return;
      k.preventDefault();
      void s.step(move.n, move.channel);
    };
    on.addEventListener("keydown", onKey);
    return () => on.removeEventListener("keydown", onKey);
  }, [box]);
}

/** A step button that goes on stepping while it is held, as a radio's does: one step at once,
 * then a run after a moment. Shift-click steps ten. From the keyboard, Enter or Space steps once. */
function HoldButton({ icon, label, onStep }: { icon: IconName; label: string; onStep: (shift: boolean) => void }) {
  const timer = useRef(0);
  const run = useRef(0);
  // Each step of a run counts from where the last one went: the latest render's onStep.
  const latest = useRef(onStep);
  useLayoutEffect(() => { latest.current = onStep; });
  const stop = () => { clearTimeout(timer.current); clearInterval(run.current); timer.current = run.current = 0; };
  useEffect(() => stop, []);
  return (
    <Button size="sm" variant="secondary" icon={icon} aria-label={label} title={`${label} (hold to go on, Shift for ten)`}
            onPointerDown={(e) => {
              if (e.button !== 0) return;
              const shift = e.shiftKey;
              stop();
              onStep(shift);
              timer.current = window.setTimeout(() => { run.current = window.setInterval(() => latest.current(shift), REPEAT_MS); }, HOLD_MS);
            }}
            onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop} onBlur={stop}
            onContextMenu={(e) => e.preventDefault()} // a long press on a touch screen is holding, not a menu
            onClick={(e) => { if (e.detail === 0) onStep(e.shiftKey); }} />
  );
}

/** The keys that step, in a line, for a screen with a keyboard. */
export function TuningKeys({ steps }: { steps: Steps }) {
  return (
    <p className="m-hint lv-keys">
      <Kbd>↑</Kbd> <Kbd>↓</Kbd> or <Kbd>←</Kbd> <Kbd>→</Kbd> step; with <Kbd>⇧</Kbd>, ten steps;{" "}
      <Kbd>Page Up</Kbd> and <Kbd>Page Down</Kbd>, {hasChannels(steps.band) ? "channel by channel" : "ten steps"}.
    </p>
  );
}

/** What the tuning is, said once it settles: "Channel 8, 446.09375 MHz". */
const tunedWords = (mhz: number, mode: string, ch: number | null) => `${ch != null ? `Channel ${ch}, ` : ""}${fmtMhz(mhz, mode)} MHz`;

/** The tuning in large type, editable (Enter tunes, the up and down keys step), the buttons and
 * the step beside it, what is there (RDS, the band and its channel) and the signal meter. */
export function TunedHead({ t, steps }: { t: Tuner; steps: Steps }) {
  const st = t.st!;
  const band = st.band;
  const [entry, setEntry] = useState(fmtMhz(st.mhz, st.mode));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setEntry(fmtMhz(st.mhz, st.mode)); }, [st.mhz, st.mode, editing]);
  const enter = () => {
    const f = +entry;
    setEditing(false);
    if (f > 0 && Math.abs(f - st.mhz) > 1e-6) t.tune({ mhz: f });
  };
  const ch = band ? channelAt(band, st.mhz) : null;
  // Said politely once a run of steps settles, not at every step on the way.
  const [said, setSaid] = useState("");
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    const id = setTimeout(() => setSaid(tunedWords(st.mhz, st.mode, ch)), 350);
    return () => clearTimeout(id);
  }, [st.mhz]);
  const one = steps.by === "channels" ? "one channel" : stepLabel(steps.by);
  const sig = st.signal_db;
  const isNarrow = st.mode !== "wfm";
  return (
    <div className="lv-head">
      <div className="lv-tune">
        {ch != null && <span className="lv-chan">Channel {ch}<span className="lv-dot" aria-hidden="true"> · </span></span>}
        <label className="lv-freq">
          <span className="sr-only">Frequency in MHz</span>
          <input value={entry} inputMode="decimal" className="lv-freq-input" aria-describedby="lv-freq-unit"
                 style={{ width: `${Math.max(5, entry.length) + 0.4}ch` }}
                 onFocus={() => setEditing(true)} onBlur={enter} onChange={(e) => { setEditing(true); setEntry(e.target.value); }}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                   if (e.key === "Escape") { setEditing(false); setEntry(fmtMhz(st.mhz, st.mode)); (e.target as HTMLInputElement).blur(); }
                   // Left and right move the caret; up and down, and the page keys, step.
                   const move = e.key.startsWith("Arrow") && e.key !== "ArrowUp" && e.key !== "ArrowDown" ? null : keyStep(e.key, e.shiftKey, hasChannels(band));
                   if (move) { e.preventDefault(); setEditing(false); void steps.step(move.n, move.channel); }
                 }} />
          <span id="lv-freq-unit" className="lv-unit">MHz</span>
        </label>
        <span className="lv-steppers" role="group" aria-label="Step the frequency">
          <HoldButton icon="up" label={`Up ${one}`} onStep={(shift) => void steps.step(shift ? 10 : 1)} />
          <HoldButton icon="down" label={`Down ${one}`} onStep={(shift) => void steps.step(shift ? -10 : -1)} />
        </span>
        <label className="lv-step">
          <span>Step</span>
          <Select value={String(steps.by)} onChange={(e) => steps.setBy(e.target.value === "channels" ? "channels" : +e.target.value)}
                  options={steps.choices.map((k) => [String(k), stepLabel(k)])} />
        </label>
      </div>
      <div className="lv-what">
        {st.rds?.ps ? <strong className="lv-ps" dir="auto">{st.rds.ps.trim()}</strong> : <strong>{band ? bandLabel(band) : "No band plan lists this frequency"}</strong>}
        {st.rds?.rt && <span className="lv-rt" dir="auto">{st.rds.rt}</span>}
        {!st.rds?.ps && bandLine(band?.id) && <span className="lv-line">{bandLine(band?.id)}</span>}
      </div>
      <div className="lv-meter-wrap" aria-live="off">
        {isNarrow && <Lamp state={st.squelch_open ? "speech" : "dim"} label={st.squelch_open ? "Signal: squelch open" : "Squelch closed"} />}
        <span className="lv-meter" role="meter" aria-label="Signal level" aria-valuemin={-100} aria-valuemax={0} aria-valuenow={sig ?? -100}>
          <span className="lv-meter-fill" style={{ width: `${Math.max(0, Math.min(100, ((sig ?? -100) + 90) / 0.8))}%` }} />
        </span>
        <span className="lv-meter-num mono">{sig != null ? `${sig.toFixed(0)} dBFS` : "–"}</span>
      </div>
      <span className="sr-only" aria-live="polite" aria-atomic="true">{said}</span>
    </div>
  );
}

/** Seek, demodulation, squelch, gain, and the sound. */
export function TuneControls({ t, steps, play }: { t: Tuner; steps: Steps; play: () => void }) {
  const st = t.st!;
  const { keep, seek } = steps;
  const tune = t.tune;
  const isNarrow = st.mode !== "wfm";
  return (
    <div className="lv-controls">
      <div className="lv-group" role="group" aria-label="Seek">
        <Button size="sm" variant="secondary" icon="left" aria-label="Seek down" title="Seek down: the next signal below" onClick={() => seek(-1)} />
        <span className="lv-seek">Seek</span>
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

