// A receiver tuned by hand, as the page holds it: tuning it (with the questions the server may ask
// first), polling what it hears, and playing its sound. Nothing heard this way is written down,
// stored or checked for alerts (radio/live.py).
import { useCallback, useEffect, useRef, useState } from "react";
import { ActButton, Button, Check, act, toast } from "../components/ui";
import { A, stopAudio } from "../lib/audio";
import { S } from "../lib/live";
import { useTopics } from "../lib/store";
import { ApiError, type Any, del, get, post, useLoad } from "./api";

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
  const [ask, setAsk] = useState<{ code: string; message: string; body: Any; holders?: string[] } | null>(null);
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

/** A refusal the user can answer: confirm they may receive this class of radio, or pause the
 * stations that hold the dongle. */
export function Ask({ ask, onYes, onNo }: { ask: Any; onYes: (extra: Any) => Promise<void>; onNo: () => void }) {
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

