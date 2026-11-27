// The server streams raw 16-bit mono PCM; it is scheduled on a Web Audio clock. This avoids the
// browsers' media players, which treat endless streams differently (WebKit especially).
import { fetchRead } from "./auth";
import { S, post, setNotice, stationsChanged } from "./live";
import { emit } from "./store";

const CUSHION_S = 0.4; // playout delay that absorbs network and chunking jitter
const MAX_AHEAD_S = 3; // live audio never queues more than this: older audio is dropped instead

interface Player {
  id: string | null;
  ctx: AudioContext;
  gain: GainNode;
  abort: AbortController;
  next: number;
  rate: number;
}

// One volume for everything the page plays, the tuner included, remembered in this browser.
const VOLUME_KEY = "ws-listen-volume";
export const MAX_VOLUME = 1.5;
function savedVolume(): number {
  try {
    const v = localStorage.getItem(VOLUME_KEY);
    return v !== null && Number.isFinite(+v) ? Math.min(MAX_VOLUME, Math.max(0, +v)) : 0.8;
  } catch {
    return 0.8;
  }
}

export const A = { playing: null as string | null, label: "", replay: false, volume: savedVolume() };
let audio: Player | null = null;

export function setVolume(v: number) {
  A.volume = v;
  if (audio) audio.gain.gain.value = v;
  try { localStorage.setItem(VOLUME_KEY, String(v)); } catch { /* private mode: not remembered */ }
  emit("audio");
}

function newPlayer(id: string | null): Player {
  const rate = S.audioRate;
  let ctx: AudioContext;
  try {
    ctx = new AudioContext({ sampleRate: rate }); // no per-chunk resampling
  } catch {
    ctx = new AudioContext();
  }
  ctx.resume(); // must happen inside the click, or autoplay rules block it
  const gain = ctx.createGain();
  gain.gain.value = A.volume;
  gain.connect(ctx.destination);
  return { id, ctx, gain, abort: new AbortController(), next: 0, rate };
}

function schedule(a: Player, pcm: Int16Array) {
  const buf = a.ctx.createBuffer(1, pcm.length, a.rate);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
  const src = a.ctx.createBufferSource();
  src.buffer = buf;
  src.connect(a.gain);
  const now = a.ctx.currentTime;
  if (a.next < now + 0.02) a.next = now + CUSHION_S; // start, or recover after a gap
  // Live audio only (a replay queues its whole paragraph at once): when the context is suspended
  // (a phone asleep), drop what can't be played instead of queueing it.
  if (a.id !== null && a.next > now + MAX_AHEAD_S) return;
  src.start(a.next);
  a.next += buf.duration;
}

export function stopAudio() {
  if (audio) {
    audio.abort.abort();
    audio.ctx.close();
    audio = null;
  }
  A.playing = null;
  A.label = "";
  A.replay = false;
  emit("audio");
}

export function toggleAudio(id: string) {
  if (A.playing === id) stopAudio();
  else playLive(id);
}

export function playLive(id: string) {
  stopAudio();
  play(begin(id));
}

/** Starts a station that is off and plays it, in one click: the audio context is made inside the
 * click (autoplay rules), and the stream is asked for once the server has started the station.
 * Where the server can, it is started only to play: listening isn't asking for a transcript, and
 * Start writes it down. */
export async function startAndPlay(id: string) {
  stopAudio();
  const a = begin(id);
  const onlyPlay = S.manage && Object.hasOwn(S.byId[id] || {}, "listen_only_run");
  const ok = onlyPlay
    ? await post(`/api/manage/stations/${id}/start`, { listen_only: true })
    : await post(`/api/stations/${id}/start`);
  if (!ok) {
    if (audio === a) stopAudio();
    return;
  }
  if (onlyPlay) stationsChanged().catch(() => {});
  if (audio === a) play(a, true);
}

function begin(id: string): Player {
  const a = newPlayer(id);
  audio = a;
  A.playing = id;
  A.label = S.byId[id]?.name || id;
  emit("audio");
  return a;
}

function play(a: Player, justStarted = false) {
  pump(a, justStarted)
    .catch((e) => {
      if (e.name !== "AbortError" && audio === a) setNotice(`Audio stopped: ${e.message}`);
    })
    .finally(() => {
      if (audio === a) stopAudio();
    });
}

const JOIN_TRIES = 20; // a station just started joins the stream within a few seconds
const JOIN_WAIT_MS = 400;

async function pump(a: Player, justStarted: boolean) {
  let r = await fetchRead(`/audio/${a.id}.pcm`, { signal: a.abort.signal, cache: "no-store" });
  for (let i = 1; justStarted && r.status === 409 && i < JOIN_TRIES; i++) {
    await new Promise((ok) => setTimeout(ok, JOIN_WAIT_MS));
    r = await fetchRead(`/audio/${a.id}.pcm`, { signal: a.abort.signal, cache: "no-store" });
  }
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
    if (usable) schedule(a, new Int16Array(bytes.buffer.slice(0, usable)));
  }
}

/** Replays archived utterances back to back (a paragraph from its time, or one search hit). */
export function playReplay(ids: number[], label: string) {
  stopAudio();
  const a = newPlayer(null);
  audio = a;
  A.label = label;
  A.replay = true;
  emit("audio");
  (async () => {
    for (const id of ids) {
      const r = await fetchRead(`/api/segments/${id}/audio.pcm`, { signal: a.abort.signal });
      if (!r.ok) throw new Error(await r.text());
      const buf = await r.arrayBuffer();
      schedule(a, new Int16Array(buf.slice(0, buf.byteLength - (buf.byteLength % 2))));
    }
    await new Promise((done) => setTimeout(done, Math.max(0, a.next - a.ctx.currentTime) * 1000 + 150));
  })()
    .catch((e) => {
      if (e.name !== "AbortError" && audio === a) setNotice(`Replay stopped: ${e.message}`);
    })
    .finally(() => {
      if (audio === a) stopAudio();
    });
}
