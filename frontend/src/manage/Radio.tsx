// Radio: find what's on the air with an idle receiver, see what was heard here and turn a frequency
// into a station (Find stations, On the air); what was found, to play (Listen, On the air here); set
// up the receivers and the region, and decide what is kept of radio that isn't broadcast (Settings)
// (src/wavescribe/radio/service.py).
import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { playLive } from "../lib/audio";
import { Icon } from "../components/Icon";
import { agoWords } from "../lib/time";
import { ActButton, Advanced, Button, Check, ConfirmButton, Field, Fold, Input, LangChips, LangPicker, Section, Segmented, Select, Switch, Table, Tag, act, cx, toast } from "../components/ui";
import { type Any, docUrl, del, get, knownLangs, listenTo, localLang, post, readerLang, refreshPage, useEvery, useLoad } from "./api";
import { type Band, and, bandLine, bandWords, fmtMhz } from "./bands";
import { Caught, WatchBand, useWatchesHere, watchWords } from "./Caught";
import { talksNowAndThen, technical } from "./catches";
import { BandCard, DialBar, DialLegend, LiveView, ReceiverChip, ReceiverMarks, ReceiverWords, type RxStatus, heardIn, mhzText, rxStatus } from "./DialView";
import { bandAt as narrowestAt, bandOfWindow, kindOf, lookAt, lookTune, onDial, onRaster } from "./dial";
import { Receivers, RxSwitch, UseDongle, makeReceiver, rxName } from "./Receivers";
import { MODE_HINT } from "./Stations";
import { type AddResult, addOnAir } from "./onair";
import { MoveThere } from "./MoveThere";
import { Refusal } from "./Refusal";
import { Ask, type Tuner, useTuner } from "./tuner";

const REGION_NAMES: Record<string, string> = {
  cept: "Europe (CEPT)",
  "1": "Africa, Middle East, rest of Region 1",
  "2": "The Americas (Region 2)",
  "3": "Asia-Pacific (Region 3)",
  jp: "Japan",
};
const REGION_SOURCE: Record<string, string> = {
  page: "chosen here",
  config: "from the stations file",
  locale: "guessed from this computer's language settings: until you choose, each frequency counts as the strictest kind any region gives it",
};
const CLASS_WORD: Record<string, string> = {
  broadcast: "broadcast",
  amateur: "amateur",
  utility: "utility",
  private: "private",
  digital: "digital",
};
const WATCH: [string, string][] = [["0", "One sweep"], ["1", "Sweeps for 1 min"], ["5", "Sweeps for 5 min"], ["15", "Sweeps for 15 min"]];

/** The kinds of radio a band plan knows (radio/bands.toml), in the order the page lists them. */
const CLASSES: Record<string, { name: string; what: string; examples: string }> = {
  broadcast: { name: "Broadcast", what: "Broadcast radio", examples: "FM, medium wave, weather radio" },
  amateur: { name: "Amateur", what: "Amateur radio", examples: "2 m, 70 cm" },
  utility: { name: "Utility", what: "Utility radio (aircraft, ships)", examples: "Aircraft, ships" },
  private: { name: "Private", what: "Private radio (walkie-talkies, anything unlisted)", examples: "Walkie-talkies (PMR446, FRS), CB, 433 MHz, anything no band plan lists" },
  digital: { name: "Digital", what: "A digital service", examples: "TETRA, DAB, mobile networks. There's no digital decoder: tuning one gives noise" },
};
const days = (n: number) => (n === 1 ? "1 day" : `${+n.toFixed(1)} days`);

/** What happens to what's heard on a channel of a class, as the user has set it (What's kept). */
function policyLine(cls: string, policies: Any): string {
  if (cls === "broadcast") return "Broadcast radio: kept like any station.";
  const p = policies?.[cls];
  const what = cls === "digital" ? "A digital service: Wavescribe has no digital decoder, so you'll hear noise. It's handled" : CLASSES[cls]?.what || cls;
  if (!p) return `${what}.`;
  const parts = [
    p.text_days === null ? "text kept like a broadcast station's" : p.text_days === 0 ? "shown live, text never stored" : `text kept ${days(p.text_days)}`,
    p.audio ? "audio kept" : "no audio",
    p.export ? "it may leave this machine" : "it stays on this machine",
  ];
  if (!p.quote_in_alerts) parts.push("alerts don't quote it");
  return `${what}${cls === "digital" ? " like private radio" : ""}: ${parts.join(", ")}. Settings, Storage and privacy changes this.`;
}

/** The nearest channel of the band at `mhz` (its channel list or raster), else `mhz` to the kHz. */
const snapToBand = (bands: Any[], mhz: number): number => onRaster(bandAt(bands, mhz), mhz);

/** The narrowest band of the plan holding `mhz` (the server decides; this is for hints). */
const bandAt = (bands: Any[], mhz: number): Any | null => narrowestAt(bands as Band[], mhz);

/** The radio view and the scan in progress, shared by the pages about radio. */
export function useRadio() {
  const { data: v, error, reload } = useLoad(() => get("/radio"));
  const [job, setJob] = useState<Any | null>(null);
  const running = !!job && !["done", "error"].includes(job.phase);
  useEffect(() => { if (v?.scan) setJob(v.scan); }, [v]);
  useEvery(800, async () => {
    const { scan } = await get("/radio/scan");
    setJob(scan);
    if (scan && ["done", "error"].includes(scan.phase)) reload();
  }, running);
  useEvery(10000, reload, !running); // receivers change elsewhere too (stations started, retuned)
  return { v, error, reload, job, setJob, running };
}
export type RadioState = ReturnType<typeof useRadio>;

/** What a first scan sweeps: broadcast FM, whose band differs by region (fm, fm-americas…). */
/** After a first band, the next worth a try, in order; a region's plan has some of them. */
const NEXT_BANDS = ["airband", "airband-25", "marine", "weather-us", "pmr446", "frs", "2m", "2m-wide", "70cm", "70cm-wide", "cb"];

const fmBand = (v: Any): Any | undefined => v.bands.find((b: Any) => b.mode === "wfm" && b.privacy === "broadcast" && b.tunable);

/** The receiver that made a scan: by the receiver it names, else by its dongle's serial (a scan
 * saved before scans named their receiver). */
const scanRx = (v: Any, scan: Any): Any | undefined =>
  scan && (v.receivers.find((r: Any) => scan.device && r.id === scan.device) || v.receivers.find((r: Any) => r.serial === scan.serial));

/** When a scan was made: a saved scan carries its time (`t`); the scan this page started is the
 * scan job, which has when it started and, once done, how long it took. Null when neither says. */
const scanTime = (scan: Any): number | null =>
  scan.t ?? (scan.started != null && scan.phase === "done" ? scan.started + (scan.seconds || 0) : null);

/** The receiver a scan starts on: the one that scanned last (most likely the one with an antenna;
 * if it's busy, the button says what the scan pauses), else a free one. Switching stays one press. */
function pickReceiver(v: Any): string {
  const last = scanRx(v, v.last_scan);
  return (last || v.receivers.find((r: Any) => !r.busy.length && !r.scanning) || v.receivers[0])?.id || "";
}

/** Anything worth listening to: not the receivers' own lines, not a digital service's noise. */
const real = (s: Signal) => !s.spur && s.privacy !== "digital";

const loading = (error: string | null) => <p className="m-loading">{error ? `Couldn't load the radio: ${error}` : "Loading…"}</p>;

/** Find stations, On the air: sweep a band, keep a frequency as a station, and what was heard here. */
export function OnTheAir() {
  const { v, error, reload, job, setJob, running } = useRadio();
  const tuner = useTuner("find");
  useEvery(3000, tuner.reload); // where each receiver is tuned now, and whether it answers
  const [add, setAdd] = useState<Any | null>(null);
  const [want, setWant] = useState<{ id: string; t: number } | null>(null);
  const canWatch = useWatchesHere();
  const { data: st, reload: reloadStations } = useLoad(() => (canWatch ? get("/stations") : Promise.resolve(null)), [canWatch]);
  useEvery(10000, reloadStations, canWatch);
  if (!v) return loading(error);
  const watches: Any[] = ((st?.stations || []) as Any[]).filter((s) => s.watch != null);
  // The form is further down the page: it is brought into view, and the focus goes to its first
  // field, so the keyboard continues where the eyes go.
  const openAdd = (t: Any) => {
    setAdd(t);
    requestAnimationFrame(() => {
      const h = document.getElementById("rd-tune");
      h?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      h?.closest("section")?.querySelector<HTMLElement>("input:not([disabled]), select, textarea")?.focus({ preventScroll: true });
    });
  };
  // A first watch: the narrowest band that can be watched, which one receiver hears whole.
  const firstWatch = () => {
    const b = onDial(v.bands as Band[]).filter((x) => talksNowAndThen(x)).sort((x, y) => (x.stop_mhz - x.start_mhz) - (y.stop_mhz - y.start_mhz))[0];
    if (b) setWant({ id: b.id, t: Date.now() });
  };
  // A watch whose kind of radio keeps nothing lists nothing here; say so, and where that is set.
  const bare = watches.filter((w) => w.running && v.policies?.[w.privacy]?.text_days === 0);
  const keptNothing = bare.length > 0 && (
    <>
      {[...new Set(bare.map((w) => w.privacy))].map((c) => CLASSES[c]?.name || c).join(" and ")} radio is shown as it's caught and not kept,
      so what {and(bare.map((w) => watchWords(w, v.bands)))} {bare.length === 1 ? "catches" : "catch"} shows only in Read while it runs.
      Change what's kept under <a className="rd-link" href="#/settings/storage">Settings, Storage and privacy</a>.
    </>
  );
  return (
    <>
      <Finder v={v} job={job} running={running} onJob={setJob} onAdd={openAdd} reload={reload} tuner={tuner}
              watches={canWatch ? watches : null} want={want} onWatched={async () => { await reloadStations(); await reload(); await refreshPage(); }} />
      {add && <AddFromAir v={v} pre={add} key={`${add.mhz}-${add.t}`} tuner={tuner}
                          onDone={async () => { setAdd(null); await Promise.all([reload(), reloadStations(), tuner.reload()]); }} />}
      {canWatch && <Caught bands={v.bands} watches={watches} keptNothing={keptNothing} onAdd={openAdd} onChooseBand={firstWatch} />}
      <Heard v={v} onAdd={openAdd} reload={reload} />
    </>
  );
}

/** Settings, Receivers: the dongles, set up and tuned, the region whose band plan names what they
 * hear, and the signals each makes itself. */
export function ReceiversPage() {
  const { v, error, job, setJob, running, reload } = useRadio();
  const { data: st, reload: reloadStations } = useLoad(() => get("/stations"));
  if (!v) return loading(error);
  const held = ((st?.stations || []) as Any[]).filter((s) => s.privacy_region && s.privacy && s.privacy_region !== s.privacy);
  return (
    <>
      <Receivers onChange={reload} />
      <Region v={v} reload={async () => { await reload(); await reloadStations(); }} held={held.length} />
      {v.receivers.length > 0 && (
        <Advanced id="rd-spurs" label="Each receiver's own signals">
          <Spurs v={v} job={job} running={running} onJob={setJob} />
        </Advanced>
      )}
    </>
  );
}

/** Settings, Storage and privacy: what's kept of each kind of radio. */
export function RadioKept() {
  const { v, error, reload } = useRadio();
  if (!v) return loading(error);
  return <Kept v={v} reload={reload} />;
}

/** Said at the top of the pages that tune a receiver: the law is the user's to follow; what
 * Wavescribe does and doesn't do. */
export function Responsibility() {
  return (
    <Fold id="radio-responsibility" open className="rd-notice" summary="You're responsible for how you use radio">
      <p>
        Broadcast radio is meant for everyone. Other
        radio, such as walkie-talkies, aircraft, ships and radio amateurs, is often protected by law: where you
        are, listening to it, keeping what's said or passing it on may be restricted or forbidden. Check your
        local rules before you tune it. Wavescribe only receives. It has no digital decoder and never decrypts.
      </p>
    </Fold>
  );
}

// -- finding: sweep a band, list what's there ---------------------------------------------------

function Finder({ v, job, running, onJob, onAdd, reload, tuner, watches, want, onWatched }: {
  v: Any; job: Any; running: boolean; onJob: (j: Any) => void; onAdd: (t: Any) => void; reload: () => Promise<void>; tuner: Tuner;
  /** The watch stations, or null where this Wavescribe can't watch a band. */
  watches: Any[] | null;
  /** A band asked for from elsewhere on the page. */
  want: { id: string; t: number } | null;
  onWatched: () => Promise<void>;
}) {
  const [device, setDevice] = useState<string>(() => tuner.receivers.find((r) => r.open)?.id || pickReceiver(v));
  const fm = fmBand(v);
  const [band, setBand] = useState<string>(fm?.id || v.bands[0]?.id || "all");
  const [lo, setLo] = useState("");
  const [hi, setHi] = useState("");
  const [watch, setWatch] = useState("0");
  const [rds, setRds] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const rx = v.receivers.find((r: Any) => r.id === device);
  const chosen: Band | undefined = v.bands.find((b: Any) => b.id === band);
  const onTheDial = !!chosen && onDial([chosen]).length > 0;
  // Channels that talk now and then are watched for a while; broadcast is found in one sweep.
  const choose = (id: string) => {
    setBand(id);
    const b = v.bands.find((x: Any) => x.id === id);
    if (b) setWatch(b.privacy === "broadcast" || b.privacy === "digital" ? "0" : "5");
  };
  const scanJob = job && !job.learn_spurs ? job : null;
  const failed = scanJob?.phase === "error" && scanJob.error !== "cancelled";
  // Until a scan has something to show, the previous one stays (no jump as a sweep starts).
  const result = scanJob?.phase === "done" || scanJob?.signals?.length ? scanJob : v.last_scan || null;
  const signals: Signal[] = result?.signals || [];
  const hidden = signals.filter((s) => !real(s));
  const found = signals.filter(real);
  const shown = showAll ? signals : found;
  // A label every row shares ("FM broadcast") is said once, in the line above the table.
  const common = shown.length > 1 && shown.every((s) => s.label === shown[0].label) ? shown[0].label : null;
  const stationAt = (s: Any) => v.heard.find((h: Any) => Math.abs(h.freq_hz - s.freq_hz) <= 6000 && h.station)?.station;

  const bandOptions: [string, string][] = [
    ...v.bands.filter((b: Any) => b.mode && b.tunable).map((b: Any) => [b.id, `${b.name} · ${+b.start_mhz.toFixed(3)}–${+b.stop_mhz.toFixed(3)} MHz`] as [string, string]),
    ["all", "Everything the tuner reaches · 24–1766 MHz"],
    ["custom", "A range of my own…"],
  ];
  const pause = !!rx?.busy.length;
  // What a scan on it interrupts, in words: listening by hand ends, stations and the spectrum
  // monitor wait for it (the tuner's view leaves listening by hand out of what holds it).
  const byHand = !!tuner.receivers.find((l) => l.id === device && l.open);
  const holding: string[] = tuner.receivers.find((l) => l.id === device)?.holders ?? rx?.busy ?? [];
  const scanWhat = chosen && kindOf(chosen) === "text" ? "Find stations here" : chosen ? `Scan ${bandWords(chosen).name}` : band === "all" ? "Scan everything" : "Scan this range";
  /** Scans on receiver `on`: the band chosen, or `only` in one sweep. */
  const start = async (on = device, only?: string) => {
    const body: Any = { device: on, rds, watch_min: only ? 0 : +watch, pause: !!v.receivers.find((r: Any) => r.id === on)?.busy.length };
    if (only) body.band = only;
    else if (band === "custom") Object.assign(body, { start_mhz: +lo, stop_mhz: +hi });
    else body.band = band;
    const { scan } = await post("/radio/scan", body);
    onJob(scan);
  };
  // The same scan on another receiver, or on a dongle just made one.
  const tryOn = async (id: string) => { await reload(); setDevice(id); await start(id); };
  const minutes = +watch;
  const watchOn = (id: string) => (watches || []).find((w) => w.device === id && w.running);
  // Stations their receiver doesn't hear where it is tuned now (moved for another station):
  // listed, unavailable, and one button away from running again.
  const unheard: { rx: Any; s: Any }[] = v.receivers.flatMap((r: Any) => r.stations.filter((x: Any) => x.out_of_reach).map((x: Any) => ({ rx: r, s: x })));
  /** The band a station is in: its watch's band, or the band at its frequency. */
  const bandOfStation = (x: Any): Any | undefined => {
    const w = (watches || []).find((y) => y.id === x.id);
    return w ? (typeof w.watch === "string" ? v.bands.find((b: Any) => b.id === w.watch) : bandAt(v.bands, w.watch[0])) : bandAt(v.bands, x.mhz);
  };
  /** Moves a receiver back where it hears `s`, asking first if that leaves others out, and
   * starts `s` there. */
  const rxs: RxStatus[] = v.receivers.map((r: Any) => {
    const s = rxStatus(r, tuner.receivers.find((l) => l.id === r.id), scanJob);
    const w = watchOn(r.id);
    const t = w && s.state === "stations" ? { ...s, words: `watching ${watchWords(w, v.bands)}` } : s;
    // Its stations it doesn't hear where it is tuned now are counted here, and named with what
    // fixes it on their band's card.
    const lost = unheard.filter((x) => x.rx.id === r.id);
    return lost.length ? { ...t, words: `${t.words}; ${lost.length === 1 ? lost[0].s.name : `${lost.length} of its stations`} can't run where it is tuned now` } : t;
  });
  const me = rxs.find((r) => r.id === device);
  const [moving, setMoving] = useState<{ id: string; mhz: number; mode: string | null; play: boolean } | null>(null);
  const question = useRef<HTMLDivElement>(null);
  // A question takes the focus while it is asked, and gives it back to where it was asked from.
  const asked = !!moving;
  useEffect(() => {
    if (!asked) return;
    const from = document.activeElement as HTMLElement | null;
    question.current?.querySelector<HTMLElement>("button")?.focus();
    return () => { if (from?.isConnected) from.focus(); };
  }, [asked]);
  /** Tunes receiver `id` to `mhz`, with sound when `play`. One following stations asks first if
   * that moves it, saying which stations wait, and a free receiver is offered instead. */
  const send = (id: string, mhz: number, mode: string | null, play: boolean, pause = false) => {
    const r = rxs.find((x) => x.id === id);
    if (!r) return listenTo(mhz, mode);
    const inside = mhz > r.lo + 0.12 && mhz < r.hi - 0.12;
    if (r.holders.length && !inside && !pause) { setMoving({ id, mhz, mode, play }); return; }
    setMoving(null);
    setDevice(id);
    tuner.moveTo(id, { mhz, mode: mode || undefined, ...(pause ? { pause: true } : {}) }, play);
  };
  /** Looks around `centre` with a receiver, without sound until something is chosen: tuned to
   * the channel there, so what the page shows is what it is on. */
  const look = (id: string, centre: number, mode: string | null) => send(id, snapToBand(v.bands, lookTune(centre, mode)), mode, false);
  const listen = (mhz: number, mode: string | null) => send(device, mhz, mode, true);
  /** The strongest station heard in the band; a receiver already there following stations looks
   * inside what it hears, which pauses nothing. */
  const lookHere = (b: Band) => {
    const there = me && me.holders.length > 0 && bandOfWindow(v.bands, me.lo, me.hi)?.id === b.id;
    send(device, lookAt(b, heardIn(v.heard, b), there ? me : null), b.mode, false);
  };
  // A free receiver to offer instead: one already on the band first.
  const free = rxs.filter((r) => r.id !== moving?.id && r.state === "idle")
    .sort((a, b) => +(bandOfWindow(v.bands, b.lo, b.hi)?.id === band) - +(bandOfWindow(v.bands, a.lo, a.hi)?.id === band))[0];
  // What the receiver in view sees: live while it is tuned by hand on this band; else a still
  // picture of the band from the newest scan that covered it, or the scan sweeping it now.
  const liveHere = !!me && me.state === "hand" && tuner.dev === device && !!tuner.st?.open && onTheDial && bandOfWindow(v.bands, me.lo, me.hi)?.id === band;
  // Letting the receiver go takes the live view away with the button pressed: the focus goes to
  // Look here, which takes its place, not back to the top of the page.
  const wasLive = useRef(liveHere);
  useEffect(() => {
    const a = document.activeElement;
    if (wasLive.current && !liveHere && (!a || a === document.body)) document.getElementById("rd-look")?.focus();
    wasLive.current = liveHere;
  }, [liveHere]);
  const stretch: [number, number] | null = liveHere && me ? [me.lo, me.hi] : onTheDial && chosen ? [chosen.start_mhz, chosen.stop_mhz] : null;
  const { data: still } = useLoad<Any | null>(
    () => (stretch ? get(`/radio/scans?lo=${stretch[0].toFixed(3)}&hi=${stretch[1].toFixed(3)}`).then((x) => x.scan) : Promise.resolve(null)),
    [stretch?.[0].toFixed(1), stretch?.[1].toFixed(1), v.last_scan?.t],
  );
  const sweeping = scanJob && chosen && scanJob.band === chosen.id ? scanJob : null;
  // Found in the band, strongest first: the scan sweeping it now, else everything heard there.
  /** Chooses a band, bringing a receiver already there into view. */
  const pick = (id: string) => {
    choose(id);
    const onIt = (r: RxStatus) => bandOfWindow(v.bands, r.lo, r.hi)?.id === id;
    if (me && !onIt(me)) { const there = rxs.find(onIt); if (there) setDevice(there.id); }
  };
  useEffect(() => {
    if (!want) return;
    pick(want.id);
    requestAnimationFrame(() => document.getElementById("rd-find")?.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" }));
  }, [want?.t]);
  // Watching: every channel of a band whose channels talk now and then, written down as it comes.
  const [watchAsk, setWatchAsk] = useState(false);
  useEffect(() => setWatchAsk(false), [band, device]);
  const unheardHere = chosen ? unheard.filter((x) => bandOfStation(x.s)?.id === chosen.id) : [];
  const watchable = !!watches && !!chosen && onTheDial && talksNowAndThen(chosen) && !!rx;
  // A watch of the band, or of a slice of it.
  const ofBand = (w: Any) => w.watch === chosen?.id || (Array.isArray(w.watch) && !!chosen && w.watch[0] >= chosen.start_mhz && w.watch[1] <= chosen.stop_mhz);
  const watched = watchable ? watches.find((w) => w.running && ofBand(w)) : undefined;
  const following = ((rx?.stations || []) as Any[]).filter((x) => x.running).map((x) => ({ id: x.id as string, name: x.name as string }));
  // With nothing found yet, one way in: FM, the easiest to hear, on a free receiver.
  const fresh = v.receivers.length > 0 && !!fm && !v.heard.length && !scanJob;
  const firstRx = v.receivers.find((r: Any) => !r.busy.length && !r.scanning) || v.receivers[0];
  // Once a band has finds, the next one worth a try in this region, quietly.
  const next = chosen && heardIn(v.heard, chosen).length > 0
    ? NEXT_BANDS.map((id) => v.bands.find((b: Any) => b.id === id) as Band | undefined).find((b) => b && b.tunable && b.id !== chosen.id && !heardIn(v.heard, b).length)
    : undefined;
  const inBand: Signal[] = onTheDial && chosen ? (sweeping ? sweeping.signals || [] : heardIn(v.heard, chosen)).slice().sort((a: Any, b: Any) => b.snr_db - a.snr_db) : [];
  const actions = !v.receivers.length ? (
    <>
      <UseDongle dongles={v.dongles || []} tools={!!v.tools.rtl_sdr} what={scanWhat} look={reload} then={tryOn} />
      <p className="m-hint">Internet stations need no receiver: they're added under <a className="rd-link" href="#/add/internet">On the internet</a>.</p>
    </>
  ) : (
    <>
      <RxSwitch ids={v.receivers.map((r: Any) => r.id)} value={device} onChange={setDevice}
                note={me ? `${me.words.replace(/^\w/, (c) => c.toUpperCase())}.` : null} />
      {!v.tools.rtl_power && <p className="m-trouble">Scanning needs the rtl-sdr tools: <code>brew install librtlsdr</code>.</p>}
      <div className="m-actions">
        {running && !job.learn_spurs ? (
          <ActButton icon="stop" run={async () => onJob((await del("/radio/scan")).scan)}>Stop scanning</ActButton>
        ) : (chosen ? kindOf(chosen) === "text" : true) && (
          <ActButton variant={fresh ? "secondary" : "primary"} icon="search" disabled={!rx || running || !v.tools.rtl_power || (band === "custom" && !(+lo < +hi))}
                     run={() => start()}>
            {holding.length ? `Pause and ${scanWhat.replace(/^\w/, (c) => c.toLowerCase())}` : scanWhat}
          </ActButton>
        )}
        {chosen && onTheDial && me && me.state !== "scanning" && !liveHere && (
          <Button id="rd-look" variant={kindOf(chosen) === "text" ? "secondary" : "primary"} icon="eye" onClick={() => lookHere(chosen)}>
            Look here with Receiver {rxName(device)}
          </Button>
        )}
        {watched ? (
          <ActButton icon="stop" run={async () => { await post(`/stations/${watched.id}/stop`); await onWatched(); }}>
            Stop watching{Array.isArray(watched.watch) ? ` ${watchWords(watched, v.bands)}` : ""}{watched.device !== device ? ` with Receiver ${rxName(watched.device)}` : ""}
          </ActButton>
        ) : watchable && !watchAsk && (
          <Button id="rd-watch-open" variant="secondary" icon="antenna" disabled={me?.state === "scanning"} onClick={() => setWatchAsk(true)}>
            Watch this band with Receiver {rxName(device)}
          </Button>
        )}
        {!running && !pause && chosen && kindOf(chosen) === "text" && (
          <span className="m-hint rd-inline">
            {minutes ? `Sweeps the band for ${minutes} minutes: these channels talk now and then.` : "One sweep, a minute or two."}
            {onTheDial && me && me.state !== "scanning" && !liveHere && " Look here shows the band live, without sound."}
          </span>
        )}
        {pause && !running && !watchAsk && (
          <span className="m-hint rd-inline">
            {byHand && `Receiver ${rxName(rx.id)} is tuned by hand: the scan lets it go.`}
            {byHand && holding.length > 0 && " "}
            {holding.length > 0 && `${byHand ? "It is also running" : `Receiver ${rxName(rx.id)} is running`} ${and(holding)}. The scan pauses ${holding.length === 1 ? "it" : "them"} and starts ${holding.length === 1 ? "it" : "them"} again when it ends.`}
          </span>
        )}
      </div>
      {unheardHere.map(({ rx, s }) => (
        <div key={s.id} className="m-trouble dl-unheard">
          <p>{s.name} can't run where Receiver {rxName(rx.id)} is tuned now.</p>
          <MoveThere rx={rx} s={s} bands={v.bands} watch={(watches || []).find((w) => w.id === s.id)?.watch} onDone={onWatched} />
        </div>
      ))}
      {next && !running && (
        <p className="m-hint">
          Next, try <button type="button" className="rd-link" onClick={() => pick(next.id)}>{bandWords(next).name}</button>: {bandWords(next).what.replace(/[:.].*$/, "").replace(/^\w/, (c) => c.toLowerCase())}.
        </p>
      )}
      {moving && (
        <div ref={question} className="m-panel rd-retune" role="alertdialog" aria-labelledby="dl-move-q"
             onKeyDown={(e) => {
               // Escape answers the question; the page's own Escape (back to reading) must not run too.
               if (e.key === "Escape") { e.stopPropagation(); setMoving(null); }
             }}>
          <p id="dl-move-q" className="m-line">
            Receiver {rxName(moving.id)} is following {and(rxs.find((r) => r.id === moving.id)?.holders || [])}. {moving.play ? "Listening to" : "Looking at"}{" "}
            {mhzText(moving.mhz, moving.mode)} pauses {rxs.find((r) => r.id === moving.id)?.holders.length === 1 ? "it" : "them"} until you let the receiver go.
          </p>
          <div className="m-actions">
            {free && (
              <Button variant="primary" icon={moving.play ? "speaker" : "eye"} onClick={() => send(free.id, moving.mhz, moving.mode, moving.play)}>
                {moving.play ? "Listen" : "Look"} with Receiver {rxName(free.id)}, which is free
              </Button>
            )}
            <Button variant={free ? "secondary" : "primary"} onClick={() => send(moving.id, moving.mhz, moving.mode, moving.play, true)}>
              Pause and {moving.play ? "listen" : "look"} with Receiver {rxName(moving.id)}
            </Button>
            <Button variant="ghost" onClick={() => setMoving(null)}>Cancel</Button>
          </div>
        </div>
      )}
      {tuner.ask && <Ask ask={tuner.ask} onYes={tuner.resend} onNo={() => tuner.setAsk(null)} />}
      {watchAsk && watchable && !watched && (
        <WatchBand band={chosen} device={device} holders={following} needsAck={!!v.policies?.[chosen.privacy]?.needs_ack}
                   free={rxs.find((r) => r.id !== device && r.state === "idle")?.id || null} others={rxs.filter((r) => r.id !== device).map((r) => r.id)}
                   byHand={byHand} letGo={(id) => (tuner.dev === id ? tuner.close() : del(`/live/${id}`))}
                   watches={watches} receivers={v.receivers} bands={v.bands}
                   onDone={async () => { setWatchAsk(false); await onWatched(); }} onCancel={() => {
                     setWatchAsk(false);
                     // The panel closes back onto the button that opened it.
                     requestAnimationFrame(() => document.getElementById("rd-watch-open")?.focus());
                   }} />
      )}
    </>
  );
  return (
    <Section id="rd-find" title="The dial"
             description="Everything a receiver can tune, from 24 to 1766 MHz. Choose a band to see what it is and find what's on the air there; what's found is kept under Heard here.">
      {fresh && (
        <div className="dl-first">
          <p className="dl-first-line">
            {v.receivers.length === 1 ? "One receiver" : `${v.receivers.length} receivers`}, nothing found yet. Start with FM radio: it is the easiest to hear.
          </p>
          <ActButton variant="primary" icon="search" disabled={!v.tools.rtl_power}
                     run={async () => { pick(fm.id); setDevice(firstRx.id); await start(firstRx.id, fm.id); }}>
            Find FM stations with Receiver {rxName(firstRx.id)}
          </ActButton>
        </div>
      )}
      <DialBar bands={v.bands} heard={v.heard} chosen={onTheDial ? band : null} onChoose={pick}
               says={(b) => rxs.filter((r) => bandOfWindow(v.bands, r.lo, r.hi)?.id === b.id).map((r) => `Receiver ${rxName(r.id)} is here`).join(", ")}
               onBand={(b) => rxs.filter((r) => bandOfWindow(v.bands, r.lo, r.hi)?.id === b.id).map((r) => (
                 <ReceiverChip key={r.id} r={r} chosen={r.id === device} onChoose={(id) => { setDevice(id); choose(b.id); }} />
               ))}
               after={rxs.some((r) => !bandOfWindow(v.bands, r.lo, r.hi)) && (
                 <p className="dl-outside">
                   Outside the bands:
                   {rxs.filter((r) => !bandOfWindow(v.bands, r.lo, r.hi)).map((r) => (
                     <span key={r.id}><ReceiverChip r={r} chosen={r.id === device} onChoose={setDevice} /> {mhzText((r.lo + r.hi) / 2)}</span>
                   ))}
                 </p>
               )}
               overlay={(width, scale) => v.receivers.length > 0 && (
                 <ReceiverMarks width={width} scale={scale} rxs={rxs} chosen={device} bands={v.bands}
                                onChoose={(id) => {
                                  setDevice(id);
                                  const r = rxs.find((x) => x.id === id);
                                  const on = r && bandOfWindow(v.bands, r.lo, r.hi);
                                  if (on) choose(on.id);
                                }}
                                onDrop={(id, centre) => {
                                  const on = bandAt(v.bands, centre);
                                  if (on && onDial([on]).length) choose(on.id);
                                  look(id, centre, on?.mode || null);
                                }} />
               )} />
      <DialLegend />
      {rxs.length > 0 && <ReceiverWords rxs={rxs} />}
      {onTheDial && chosen ? (
        <BandCard band={chosen} kept={policyLine(chosen.privacy, v.policies)} policies={v.policies} heard={heardIn(v.heard, chosen).length}>
          {actions}
        </BandCard>
      ) : (
        <div className="dl-card">
          <h4 className="dl-card-h">{band === "all" ? "Everything the tuner reaches" : band === "custom" ? "A range of your own" : chosen?.name}</h4>
          <p className="dl-card-text">
            {band === "all" ? "The whole tuner in coarse steps: good for strong signals, too coarse for narrow channels."
              : band === "custom" ? "From and to, in MHz, under Advanced." : bandLine(band)}
          </p>
          {actions}
        </div>
      )}
      {v.receivers.length > 0 && (
        <>
          <Advanced id="rd-find">
            <div className="m-form rd-form">
              <Field id="rd-band" label="What to scan" wide
                     hint={chosen ? bandLine(chosen.id) || undefined : band === "all" ? "The whole tuner, in coarse steps: good for strong signals, too coarse for narrow channels." : "From and to, in MHz."}>
                <Select id="rd-band" value={band} onChange={(e) => choose(e.target.value)} options={bandOptions} />
              </Field>
              {band === "custom" && (
                <>
                  <Field id="rd-lo" label="From (MHz)" hint="The lower end."><Input id="rd-lo" type="number" step={0.001} value={lo} placeholder="e.g. 150" onChange={(e) => setLo(e.target.value)} /></Field>
                  <Field id="rd-hi" label="To (MHz)" hint="The upper end, up to 1766."><Input id="rd-hi" type="number" step={0.001} value={hi} placeholder="e.g. 175" onChange={(e) => setHi(e.target.value)} /></Field>
                </>
              )}
              <Field id="rd-watch" label="How long"
                     hint={chosen && chosen.privacy !== "broadcast" ? "These channels talk now and then: sweep for a few minutes." : "One sweep finds broadcast stations; sweeping for longer also catches channels that talk now and then."}>
                <Select id="rd-watch" value={watch} onChange={(e) => setWatch(e.target.value)} options={WATCH} />
              </Field>
            </div>
            {(band === "all" || band.startsWith("fm")) && (
              <p className="m-line">
                <Check checked={rds} onChange={setRds}>Read station names (RDS)</Check>
                <span className="m-hint rd-inline">FM stations send their name and a line of text; reading them adds a few seconds per station.</span>
              </p>
            )}
          </Advanced>
          {scanJob && !failed && !(scanJob.phase === "done" && !found.length) && <ScanProgress job={scanJob} />}
          {onTheDial && chosen ? (
            <BandView v={v} band={chosen} me={me} live={liveHere} tuner={tuner} still={still} sweeping={sweeping} rows={inBand}
                      onAdd={onAdd} onListen={listen} onTry={tryOn} stationAt={stationAt} />
          ) : (
            <>
          {result?.spectrum && <ScanSpectrum spectrum={result.spectrum} signals={shown} bands={v.bands} />}
          {(failed || (result && !running && !found.length)) && <NothingHeard v={v} result={failed ? scanJob : result} onTry={tryOn} />}
          {result && (
            <>
              {(shown.length > 0 || running) && (
                <Table caption="Signals found" empty="Nothing yet…"
                       cols={[{ label: "MHz", num: true }, { label: "Name" }, { label: "What it is" }, { label: "Above noise" }, { label: "", width: "1%" }]}>
                  {shown.map((s) => <SignalRow key={s.freq_hz} s={s} station={stationAt(s)} onAdd={onAdd} common={common} />)}
                </Table>
              )}
              {hidden.length > 0 && (
                <p className="m-hint">
                  <button type="button" className="rd-link" onClick={() => setShowAll(!showAll)}>
                    {showAll ? "Hide" : "Show"} {hidden.length} more
                  </button>: {foldedKinds(hidden)}.
                </p>
              )}
              {result === v.last_scan && v.last_scan?.t && <p className="m-hint">From the last scan, {new Date(v.last_scan.t * 1000).toLocaleString()}.</p>}
            </>
          )}
            </>
          )}
        </>
      )}
    </Section>
  );
}

/** Under a band chosen on the dial: what the receiver in view sees there, live or as a still
 * picture with its age, then everything found in the band, strongest first (the text
 * alternative to the pictures). */
function BandView({ v, band, me, live, tuner, still, sweeping, rows, onAdd, onListen, onTry, stationAt }: {
  v: Any; band: Band; me: RxStatus | undefined; live: boolean; tuner: Tuner; still: Any | null; sweeping: Any | null; rows: Signal[];
  onAdd: (t: Any) => void; onListen: (mhz: number, mode: string | null) => void; onTry: (device: string) => Promise<void>; stationAt: (s: Any) => string | null;
}) {
  const [showAll, setShowAll] = useState(false);
  const name = bandWords(band).name;
  const who = me ? `Receiver ${rxName(me.id)}` : "a receiver";
  const hidden = rows.filter((s) => !real(s));
  const shown = showAll ? rows : rows.filter(real);
  const common = shown.length > 1 && shown.every((s) => s.label === shown[0].label) ? shown[0].label : null;
  const failed = sweeping?.phase === "error" && sweeping.error !== "cancelled";
  const picture = sweeping?.spectrum ? sweeping : still?.spectrum ? still : null;
  const scanner = picture && scanRx(v, picture);
  const running = !!sweeping && !["done", "error"].includes(sweeping.phase);
  const age = picture ? agoWords(scanTime(picture)) : null;
  const heading = live ? `What Receiver ${rxName(tuner.dev)} hears, live`
    : picture === sweeping && running ? `${scanner ? `Receiver ${rxName(scanner.id)}` : "A receiver"} is sweeping ${name}`
    : picture ? `${name} at the last scan${age ? `, ${age}` : ""}` : null;
  return (
    <>
      {heading && <h4 className="m-sub-h">{heading}</h4>}
      {live ? (
        <LiveView t={tuner} heard={v.heard} signals={still?.signals || []} onAdd={onAdd} />
      ) : picture ? (
        <ScanSpectrum spectrum={picture.spectrum} signals={picture === sweeping ? shown : (picture.signals || []).filter(real)} bands={v.bands} onPick={onListen}
                      caption={picture === sweeping && running
                        ? `The line fills in as it sweeps. Click a peak, or anywhere, to listen there with ${who}.`
                        : `A still picture: the strongest each frequency reached in ${scanner ? `Receiver ${rxName(scanner.id)}'s` : "that"} scan. Click a peak, or anywhere, to listen there with ${who}; Look here shows it live.`} />
      ) : null}
      {(failed || (sweeping?.phase === "done" && !rows.some(real))) && <NothingHeard v={v} result={sweeping} onTry={onTry} />}
      {(shown.length > 0 || running) && <h4 id="dl-found-h" className="m-sub-h">Found in {name}, strongest first</h4>}
      {(shown.length > 0 || running) && (
       <div className="dl-found">
        <Table labelledBy="dl-found-h" empty="Nothing yet…"
               cols={[{ label: "MHz", num: true }, { label: "Name" }, { label: "What it is" }, { label: "Above noise" }, { label: "", width: "1%" }]}>
          {shown.map((s) => <SignalRow key={s.freq_hz} s={s} station={stationAt(s)} onAdd={onAdd} common={common} onListen={onListen} />)}
        </Table>
       </div>
      )}
      {hidden.length > 0 && (
        <p className="m-hint">
          <button type="button" className="rd-link" onClick={() => setShowAll(!showAll)}>{showAll ? "Hide" : "Show"} {hidden.length} more</button>: {foldedKinds(hidden)}.
        </p>
      )}
    </>
  );
}

/** `held`: radio stations the region change would make less strict, waiting for the operator's
 * word in Settings, Stations, where each station is managed. */
function Region({ v, reload, held }: { v: Any; reload: () => Promise<void>; held: number }) {
  return (
    <Section id="rd-region-h" title="Your region"
             description="Decides which band plan names each frequency and what kind of radio it counts as, so what is kept of it and whether it asks first.">
      <div className="m-form">
        <Field id="rd-region" label="Band plan" hint={REGION_SOURCE[v.region_source] ? `Now ${REGION_SOURCE[v.region_source]}.` : undefined}>
          <Select id="rd-region" value={v.region}
                  onChange={(e) => act(async () => { await post("/radio/region", { region: e.target.value }); await reload(); }, "Region changed")}
                  options={v.regions.map((r: string) => [r, REGION_NAMES[r] || r])} />
        </Field>
      </div>
      {held > 0 && (
        <div className="m-note" role="status">
          <p>
            {held === 1 ? "One radio station keeps its" : `${held} radio stations keep their`} earlier, stricter kind of radio
            until you confirm the change under <a className="rd-link" href="#/settings/stations">Settings, Stations</a>.
          </p>
        </div>
      )}
    </Section>
  );
}

function ScanProgress({ job }: { job: Any }) {
  const mmss = (s: number) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  let text: string;
  switch (job.phase) {
    case "starting": text = "Starting…"; break;
    case "sweep": text = `Sweeping ${job.what}: step ${job.done} of ${job.total}`; break;
    case "watch": text = `Watching ${job.what}: ${mmss(job.done)} of ${mmss(job.total)} (${job.note || "sweeping"}), ${job.signals?.length || 0} found so far`; break;
    case "rds": text = `Reading station names: group ${job.done + 1} of ${job.total}`; break;
    case "done": text = job.learn_spurs ? `Learnt ${job.spurs} of the receiver's own signals in ${job.seconds} s` : `${job.what}: ${job.signals.length} signals in ${job.seconds} s`; break;
    default: text = job.error === "cancelled" ? "Scan stopped." : `The scan failed: ${job.error}`;
  }
  const busy = !["done", "error"].includes(job.phase);
  if (busy && job.paused?.length) text += `. ${job.paused.join(", ")} paused until the scan ends.`;
  return (
    <p className={cx("m-line rd-progress", job.phase === "error" && job.error !== "cancelled" && "is-trouble")} role="status">
      {busy && <progress max={job.total || 1} value={job.phase === "starting" ? undefined : job.done} aria-hidden="true" />}
      <span>{text}</span>
    </p>
  );
}

/** After a scan that found nothing, or couldn't run: which receiver it was, the likely cause, and
 * the other receivers and a dongle not in use yet, each one press from the same scan again. */
function NothingHeard({ v, result, onTry }: { v: Any; result: Any; onTry: (device: string) => Promise<void> }) {
  const device: string | undefined = result.device ?? scanRx(v, result)?.id;
  const others: Any[] = v.receivers.filter((r: Any) => r.id !== device);
  // Listing the dongles opens none of them, so asking costs a running station nothing.
  const { data: spare, reload: look } = useLoad(() => get("/radio?dongles=1").then((x) => (x.dongles || []) as Any[]));
  const who = device ? <>Receiver <strong>{rxName(device)}</strong></> : "The receiver";
  const choices = others.length > 0 || !!spare?.length;
  return (
    <div className="m-note rd-nothing" role="status">
      <p>
        {result.phase === "error"
          ? <>{who} couldn't scan: {result.error}.</>
          : <>{who} heard nothing {result.what ? `on ${result.what}` : "in its last scan"}. Is its antenna connected?</>}
        {choices ? " Try another receiver:" : " Check the antenna and its cable, or raise the gain under Settings, Receivers."}
      </p>
      {choices && (
        <div className="m-actions">
          {others.map((r) => (
            <ActButton key={r.id} size="sm" variant="secondary" run={() => onTry(r.id)}>
              Receiver {rxName(r.id)}{r.busy.length ? `, pausing ${r.busy.join(", ")}` : ""}
            </ActButton>
          ))}
          {!!spare?.length && (
            <ActButton size="sm" variant="secondary" icon="plus"
                       run={async () => { const id = await makeReceiver(spare[0].serial, v.receivers.map((r: Any) => r.id)); await look(); await onTry(id); }}>
              {spare.length === 1 ? "The spare dongle" : "A spare dongle"}
            </ActButton>
          )}
        </div>
      )}
    </div>
  );
}

/** One signal a scan found, as far as this page decides on it; its other fields pass through. */
interface Signal {
  spur?: boolean;
  /** Set with `spur` on a narrow line at a multiple of 2.4 MHz: another receiver's clock. */
  clock_hz?: number | null;
  [field: string]: Any;
}

/** A spur is the receivers' own and never a station, except a clock line: a real station can share
 * that frequency, so it may still be listened to. */
const listenable = (s: Signal) => !s.spur || s.clock_hz != null;

/** What "Show N more" folds away, naming only the kinds this scan found. */
function foldedKinds(hidden: Signal[]): string {
  const kinds = [
    hidden.some((s) => !s.spur && s.privacy === "digital") && "digital services, which give only noise without a decoder",
    hidden.some((s) => s.spur && s.clock_hz == null) && "the receiver's own signals",
    hidden.some((s) => s.spur && s.clock_hz != null) && "lines of another receiver's clock",
  ].filter((k): k is string => !!k);
  return kinds.length > 1 ? `${kinds.slice(0, -1).join(", ")} and ${kinds[kinds.length - 1]}` : kinds[0] || "";
}

function SignalRow({ s, station, onAdd, common, onListen = listenTo }: {
  s: Signal; station?: string | null; onAdd: (t: Any) => void; common?: string | null; onListen?: (mhz: number, mode: string | null) => void;
}) {
  const rds = s.rds || {};
  const name = s.user_name || rds.ps || "";
  const what = [s.label !== common && (s.label || "Unlisted"), rds.pty && rds.pty_name].filter(Boolean).join(" · ");
  const clock = s.spur && s.clock_hz != null;
  return (
    <tr>
      <td className="num">{fmtMhz(s.mhz, s.mode)}</td>
      <td><strong className="rd-name" dir="auto">{name || <span className="m-muted">–</span>}</strong></td>
      <td>
        <span className="rd-what">{what || (rds.rt || s.passes > 1 || clock ? null : <span className="m-muted">–</span>)}</span>
        {rds.rt && <span className="rd-rt" dir="auto" title="Radiotext: what the station sends about itself">{rds.rt}</span>}
        {clock && <span className="rd-rt">Probably another receiver's clock, not a transmission</span>}
        {s.passes > 1 && (
          <span className="rd-rt">
            {s.passes >= 10 && s.seen === s.passes && s.privacy !== "broadcast"
              ? "On all the time: a beacon, a recorded weather loop, or interference"
              : `Busy in ${s.seen} of ${s.passes} sweeps`}
          </span>
        )}
      </td>
      <td className="num-cell">{s.snr_db.toFixed(0)} dB{s.privacy !== "broadcast" && <Tag tone="muted">{CLASS_WORD[s.privacy] || s.privacy}</Tag>}</td>
      <td className="rd-act">
        {!listenable(s) ? <span className="m-muted">receiver's own</span>
          : station ? <a href="#/" className="rd-link">On the page</a>
          : clock ? <Button size="sm" variant="ghost" icon="speaker" onClick={() => onListen(s.mhz, s.mode)}>Listen</Button>
          : s.privacy === "digital" ? <Button size="sm" variant="ghost" onClick={() => onAdd({ mhz: s.mhz, mode: null, name, privacy: s.privacy, t: Date.now() })}>Try anyway</Button>
          : (
            <>
              <Button size="sm" variant="ghost" icon="speaker" onClick={() => onListen(s.mhz, s.mode)}>Listen</Button>
              <Button size="sm" icon="plus" onClick={() => onAdd({ mhz: s.mhz, mode: s.mode, name, privacy: s.privacy, t: Date.now() })}>Add as a station</Button>
            </>
          )}
      </td>
    </tr>
  );
}

/** The scan's spectrum on the instrument's dark band; signals ticked, FM names printed, hover reads
 * out. A click listens under Listen, or calls `onPick` (the dial listens in place). */
function ScanSpectrum({ spectrum, signals, bands, onPick = listenTo, caption }: {
  spectrum: Any; signals: Any[]; bands: Any[]; onPick?: (mhz: number, mode: string | null) => void; caption?: string;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<{ x: number; mhz: number; db: number; sig: Any | null } | null>(null);
  const { start_mhz: a, stop_mhz: b, db } = spectrum;
  const [lo, hi] = useMemo(() => {
    const sorted = [...db].sort((p: number, q: number) => p - q);
    return [sorted[Math.floor(sorted.length * 0.02)] - 3, sorted[sorted.length - 1] + 4];
  }, [db]);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const css = getComputedStyle(document.documentElement);
    const tok = (n: string) => css.getPropertyValue(n).trim() || "#888";
    const dpr = window.devicePixelRatio || 1;
    const W = (c.width = Math.round(c.clientWidth * dpr));
    const H = (c.height = Math.round(c.clientHeight * dpr));
    const ctx = c.getContext("2d")!;
    const x = (mhz: number) => ((mhz - a) / (b - a)) * W;
    const y = (v: number) => H - 6 * dpr - ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (H - 34 * dpr);
    ctx.clearRect(0, 0, W, H);
    // frequency scale: about six ticks at round numbers
    const span = b - a;
    const step = [0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 25, 50, 100, 250].find((s) => span / s <= 7) || 500;
    ctx.font = `${11 * dpr}px system-ui, sans-serif`;
    ctx.fillStyle = tok("--band-ink-2");
    ctx.strokeStyle = tok("--band-tick");
    ctx.lineWidth = dpr;
    for (let f = Math.ceil(a / step) * step; f <= b; f += step) {
      const px = x(f);
      ctx.beginPath(); ctx.moveTo(px, 24 * dpr); ctx.lineTo(px, H); ctx.stroke();
      ctx.fillText(`${+f.toFixed(3)}`, px + 3 * dpr, H - 6 * dpr);
    }
    // the spectrum
    ctx.strokeStyle = tok("--band-ink");
    ctx.lineWidth = 1.5 * dpr;
    ctx.lineJoin = "round";
    ctx.beginPath();
    db.forEach((v: number, i: number) => {
      const px = (i / (db.length - 1)) * W;
      if (i) ctx.lineTo(px, y(v)); else ctx.moveTo(px, y(v));
    });
    ctx.stroke();
    // signals: a tick each, names where they fit
    let lastLabel = -Infinity;
    for (const s of signals) {
      const px = x(s.mhz);
      ctx.fillStyle = tok("--band-ink-2");
      ctx.fillRect(px - dpr / 2, 4 * dpr, dpr, 8 * dpr);
      const name = s.user_name || s.rds?.ps;
      if (name && px - lastLabel > 70 * dpr) {
        ctx.fillStyle = tok("--band-ink");
        ctx.fillText(name, Math.min(W - 70 * dpr, px + 3 * dpr), 20 * dpr);
        lastLabel = px;
      }
    }
    if (hover) {
      ctx.fillStyle = tok("--accent");
      ctx.fillRect(hover.x * dpr - dpr / 2, 0, dpr, H);
    }
  }, [a, b, db, lo, hi, signals, hover]);
  const move = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - r.left;
    const mhz = a + (px / r.width) * (b - a);
    const near = signals.reduce<Any | null>((best, s) => {
      const d = Math.abs(((s.mhz - a) / (b - a)) * r.width - px);
      return d <= 8 && (!best || d < best.d) ? { s, d } : best;
    }, null);
    const i = Math.max(0, Math.min(db.length - 1, Math.round((px / r.width) * (db.length - 1))));
    setHover({ x: near ? ((near.s.mhz - a) / (b - a)) * r.width : px, mhz: near ? near.s.mhz : mhz, db: db[i], sig: near?.s || null });
  };
  const sig = hover?.sig;
  return (
    <figure className="m-scope rd-scope">
      <canvas ref={canvas} className="m-spectrum rd-spectrum" role="img"
              aria-label={`Spectrum from ${a} to ${b} MHz with ${signals.length} signals; the table below lists them`}
              onMouseMove={move} onMouseLeave={() => setHover(null)}
              onClick={() => {
                if (sig) {
                  if (listenable(sig)) onPick(sig.mhz, sig.mode);
                } else if (hover) {
                  // anywhere else: the nearest channel of the band there, to listen for what comes and goes
                  onPick(snapToBand(bands, hover.mhz), bandAt(bands, hover.mhz)?.mode || null);
                }
              }}
              style={{ cursor: "pointer" }} />
      {hover && (
        <span className="trace-tip rd-tip" style={{ left: `clamp(60px, ${hover.x}px, calc(100% - 60px))` }}>
          {sig ? `${fmtMhz(sig.mhz, sig.mode)} MHz${sig.rds?.ps ? ` · ${sig.rds.ps}` : ""} · ${sig.snr_db.toFixed(0)} dB above noise` : `${fmtMhz(snapToBand(bands, hover.mhz), bandAt(bands, hover.mhz)?.mode)} MHz · click to listen`}
        </span>
      )}
      <figcaption className="m-hint">{caption || "Peaks are signals: click one, or anywhere, to listen there under Listen. The line is the strongest each frequency reached during the scan."}</figcaption>
    </figure>
  );
}

// -- adding: a frequency found on the air becomes a station ----------------------------------------

const DOES = [
  { value: "listen", label: "Listen only" },
  { value: "transcribe", label: "Transcribe" },
  { value: "translate", label: "Transcribe and translate" },
];

function AddFromAir({ v, pre, tuner, onDone }: { v: Any; pre: Any; tuner: Tuner; onDone: () => Promise<void> }) {
  // Where a receiver is tuned now: by hand, where the tuner has it; else where it is set.
  const centreOf = (r: Any) => tuner.receivers.find((l) => l.id === r.id && l.open)?.center_mhz ?? r.center_mhz;
  const fits = (r: Any, mhz: number) => Math.abs(mhz - centreOf(r)) <= r.span_mhz / 2 - 0.12;
  const f: number = pre.mhz;
  const band = bandAt(v.bands, f);
  const [mode, setMode] = useState<string>(pre.mode && band && pre.mode !== band.mode ? pre.mode : "");
  const pickRx = () =>
    (v.receivers.find((r: Any) => fits(r, f) && !r.scanning) || v.receivers.find((r: Any) => !r.busy.length && !r.scanning) || v.receivers[0])?.id || "";
  const [device, setDevice] = useState<string>(() => pre.device || pickRx());
  const [name, setName] = useState(pre.name || "");
  const [does, setDoes] = useState("translate");
  const [lang, setLang] = useState<string>(() => localLang(v));
  const [to, setTo] = useState<string[]>(v.defaults.translate_to || []);
  const [ack, setAck] = useState(false);
  // The server's question when adding it moves the receiver away from stations it has: its
  // sentence, and the running ones that would stop. Nothing has changed while it is asked.
  const [asked, setAsked] = useState<{ question: string; stops: string[] } | null>(null);
  const [trouble, setTrouble] = useState<string | null>(null);
  useEffect(() => {
    // With no speech model downloaded yet, a new station starts by listening: transcription can follow.
    get("/models").then((m) => {
      const has = (cap: string) => m.models.some((e: Any) => e.capability === cap && e.installed);
      if (!has("vad") || !has("asr")) setDoes("listen");
    }).catch(() => {});
  }, []);
  const cls = band ? band.privacy : "private";
  const needsAck = !!v.policies[cls]?.needs_ack;
  const rx = v.receivers.find((r: Any) => r.id === device);
  const live = tuner.receivers.find((r) => r.id === device && r.open);
  const effMode = mode || band?.mode || "nfm";
  const called = name.trim() || pre.name || `${fmtMhz(f, effMode)} MHz`;
  const submit = async (confirmed = false) => {
    setTrouble(null);
    setAsked(null);
    let r: AddResult;
    try {
      r = await addOnAir({
        device, mhz: f, mode: mode || undefined, name: name.trim() || undefined, language: lang.trim(), translate_to: does === "translate" ? to : [],
        acknowledge: ack, ...(does === "listen" ? { transcribe: false } : does === "transcribe" ? { translate: false } : {}),
      }, confirmed);
    } catch (e) {
      setTrouble((e as Error).message);
      return;
    }
    if ("question" in r) { setAsked(r); return; }
    setAsked(null);
    const st = r.station;
    if (st.live_closed) {
      // Listening by hand ended with the add: what was playing goes on as the station.
      const playing = tuner.dev === device && tuner.playing;
      if (tuner.dev === device) { tuner.stopPlayer(); tuner.setSt(null); }
      if (playing && st.running) playLive(st.id);
    }
    await refreshPage();
    if (r.trouble) toast(`${st.name} was added and didn't start: ${r.trouble}`, true);
    else toast(does === "listen" ? `${st.name} added: play it under Listen` : `Transcribing ${st.name}: it's on the reading page`);
    await onDone();
  };
  const other = v.receivers.find((r: Any) => r.id !== device)?.id as string | undefined;
  // Already one of the stations: adding it again would only be told its name is taken.
  const already = v.receivers.flatMap((r: Any) => r.stations.map((x: Any) => ({ ...x, rx: r.id }))).find((x: Any) => Math.abs(x.mhz - f) <= 0.006);
  return (
    <Section id="rd-tune" title={`Add ${fmtMhz(f, effMode)} MHz as a station`}
             description="It runs all the time, beside the other stations, within what its kind of radio allows.">
      <p className="m-hint">
        {band ? <><strong>{band.name}</strong>{band.channels_mhz?.length ? `, channel ${band.channels_mhz.findIndex((c: number) => Math.abs(c - f) < 0.002) + 1 || "off-raster"}` : ""}. {bandLine(band.id)} </> : "No band plan lists this frequency. "}
        {policyLine(cls, v.policies)}
      </p>
      <div className="m-mode">
        <Segmented label="What Wavescribe does with it" value={does} onChange={setDoes} options={DOES} />
        <p className="m-hint">{MODE_HINT[does]}</p>
      </div>
      <div className="m-form rd-form">
        <Field id="rd-name" label="Name" hint={effMode === "wfm" ? "Optional: an FM station names itself from RDS." : "Optional: a channel label."}><Input id="rd-name" value={name} placeholder={band ? band.name : ""} onChange={(e) => setName(e.target.value)} /></Field>
        {does !== "listen" && (
          <Field id="rd-lang" label="Language spoken" hint="Its name in English or in the language itself, or a tag.">
            <LangPicker id="rd-lang" value={lang} onChange={setLang} options={knownLangs()} />
          </Field>
        )}
        {does === "translate" && (
          <Field id="rd-to" label="Translate into" wide hint="Continuously, as it speaks."><LangChips value={to} onChange={setTo} known={knownLangs()} offer={readerLang(lang)} label="Translate into" /></Field>
        )}
      </div>
      <Advanced id="rd-add">
        <div className="m-form rd-form">
          <Field id="rd-mode" label="Demodulation" hint="How the signal carries sound. The band plan knows; change it only if the sound is garbled.">
            <Select id="rd-mode" value={mode} onChange={(e) => setMode(e.target.value)}
                    options={[["", band?.mode ? `From the band plan (${band.mode.toUpperCase()})` : "From the band plan"], ["wfm", "WFM (broadcast FM)"], ["nfm", "NFM (walkie-talkies, marine, amateur)"], ["am", "AM (aircraft, medium wave)"]]} />
          </Field>
          {v.receivers.length > 1 && (
            <Field id="rd-rx" label="Receiver" hint={rx && !fits(rx, f) ? `Moves receiver ${rxName(rx.id)} to ${fmtMhz(f, effMode)} MHz.` : "The receiver that tunes it."}>
              <Select id="rd-rx" value={device} onChange={(e) => { setDevice(e.target.value); setAsked(null); }}
                      options={v.receivers.map((r: Any) => {
                        const by = tuner.receivers.find((l) => l.id === r.id);
                        const doing = by?.open ? ["tuned by hand"] : (by?.holders || []);
                        return [r.id, `${rxName(r.id)} · ${centreOf(r).toFixed(2)} MHz${doing.length ? ` · ${doing.join(", ")}` : ""}`];
                      })} />
            </Field>
          )}
        </div>
      </Advanced>
      {needsAck && (
        <p className="m-line">
          <Check checked={ack} onChange={setAck}>
            I've checked that I may receive this where I am, and I'm responsible for how I use what's heard.
            Laws on radio that isn't broadcast differ by country (<a className="rd-link" href={docUrl("RADIO.md")} target="_blank" rel="noreferrer">docs/RADIO.md</a>).
          </Check>
        </p>
      )}
      {already ? (
        <p className="m-line" role="status">
          {already.name} is one of your stations already{already.out_of_reach
            ? `, and Receiver ${rxName(already.rx)} isn't tuned where it hears it: the band's card above moves the receiver back to it.`
            : already.running ? `, and Receiver ${rxName(already.rx)} follows it.` : ": start it from the station list under Read."}
        </p>
      ) : asked ? (
        <p className="m-line" role="status">{asked.question}</p>
      ) : live && (
        <p className="m-line" role="status">Receiver {rxName(device)} will follow {called} from now; listening by hand stops.</p>
      )}
      {trouble && (
        <Refusal raw={trouble} said={technical(trouble) ? `Receiver ${rxName(device)} couldn't take ${called}.` : undefined}>
          {other && <Button size="sm" variant="secondary" onClick={() => { setDevice(other); setTrouble(null); setAsked(null); }}>Use Receiver {rxName(other)}</Button>}
        </Refusal>
      )}
      <div className="m-actions">
        <ActButton variant="primary" icon="plus" disabled={(does !== "listen" && !lang.trim()) || (needsAck && !ack) || !device || !!rx?.scanning || !!already} run={() => submit(!!asked)}>
          {asked ? (asked.stops.length ? `Stop ${and(asked.stops)} and add` : "Move it and add") : "Add as a station"}
        </ActButton>
        <Button variant="ghost" onClick={() => (asked ? setAsked(null) : onDone())}>{asked ? "Keep it as it is" : "Cancel"}</Button>
      </div>
    </Section>
  );
}

// -- what this machine has heard ---------------------------------------------------------------

/** Broadcast first, then the named, then what the last scan heard again, then by frequency. */
const byInterest = (a: Any, b: Any) =>
  +(a.privacy !== "broadcast") - +(b.privacy !== "broadcast")
  || +!(a.user_name || a.name) - +!(b.user_name || b.name)
  || +!!a.missing - +!!b.missing
  || a.mhz - b.mhz;

const FIRST_ROWS = 12;

/** Listen, On the air here: what scans found from this place, broadcast first, each one press from
 * playing through a receiver (the tuner below). Before the first scan, that scan in one press; with
 * no receiver yet, a plugged-in dongle put to use for it. */
export function OnAirHere({ radio, playingMhz, onPlay, onStop, onReceiver }: {
  radio: RadioState;
  /** What the tuner is playing, to mark its row; null when it plays nothing. */
  playingMhz: number | null;
  onPlay: (t: { mhz: number; mode: string | null; device?: string }) => void;
  onStop: () => void;
  /** A dongle became a receiver here: the tuner's list of receivers is out of date. */
  onReceiver: () => Promise<void>;
}) {
  const { v, error, reload, job, setJob, running } = radio;
  const [all, setAll] = useState(false);
  const [device, setDevice] = useState("");
  if (!v) return loading(error);
  const fm = fmBand(v);
  const heard: Any[] = v.heard.filter((e: Any) => !e.station && e.privacy !== "digital").sort(byInterest);
  const dev = v.receivers.some((r: Any) => r.id === device) ? device : pickReceiver(v);
  const rx = v.receivers.find((r: Any) => r.id === dev);
  const start = async (on: string) => {
    const pause = !!v.receivers.find((r: Any) => r.id === on)?.busy.length;
    setJob((await post("/radio/scan", { device: on, band: fm?.id || "all", rds: true, watch_min: 0, pause })).scan);
  };
  const tryOn = async (id: string) => { await reload(); await onReceiver(); setDevice(id); await start(id); };
  const scanJob = job && !job.learn_spurs ? job : null;
  const failed = scanJob?.phase === "error" && scanJob.error !== "cancelled";
  // Nothing found: by the scan just run, or by the last one this machine kept.
  const empty = scanJob ? scanJob.phase === "done" && !scanJob.signals.some(real) : !!v.last_scan && !v.last_scan.signals?.some(real);
  const shown = all ? heard : heard.slice(0, FIRST_ROWS);
  const serialOf = (e: Any) => v.receivers.find((r: Any) => r.serial === e.serial)?.id;
  return (
    <Section id="ls-air" title="On the air here"
             description="What your receivers have found from where you are, broadcast stations first. One press tunes one in and plays it; nothing is written down.">
      {!v.receivers.length ? (
        <UseDongle dongles={v.dongles || []} tools={!!v.tools.rtl_sdr} what="Find what's on the air" look={reload} then={tryOn} />
      ) : heard.length ? (
        <>
          <ul className="ls-list">
            {shown.map((e) => {
              const playing = playingMhz != null && Math.abs(playingMhz - e.mhz) < 0.003;
              const f = fmtMhz(e.mhz, e.mode);
              const name = e.user_name || e.name;
              const label = e.label || "Unlisted";
              const kind = e.privacy !== "broadcast" && (CLASS_WORD[e.privacy] || e.privacy);
              return (
                <li key={e.freq_hz} className={cx("ls-row", playing && "is-playing")}>
                  <button type="button" className="ls-main" aria-pressed={playing}
                          aria-label={`${playing ? "Stop" : "Play"} ${name ? `${name}, ` : ""}${f} MHz`}
                          onClick={() => (playing ? onStop() : onPlay({ mhz: e.mhz, mode: e.mode, device: serialOf(e) }))}>
                    <span className="ls-freq mono">{f}</span>
                    <span className="ls-text">
                      {/* Without a name, what the band plan calls it stands in; the frequency is already beside it. */}
                      <span className={cx("ls-name", !name && "is-unnamed")} dir="auto">{name || label}</span>
                      <span className="ls-sub">
                        {[name && label, kind && !label.toLowerCase().includes(kind) && kind,
                          playing ? "playing" : e.missing ? "not in the last scan" : agoWords(e.last_heard) && `heard ${agoWords(e.last_heard)}`].filter(Boolean).join(" · ")}
                      </span>
                    </span>
                    <Icon name={playing ? "stop" : "play"} />
                  </button>
                </li>
              );
            })}
          </ul>
          <p className="m-hint">
            {heard.length > shown.length && (
              <><button type="button" className="rd-link" onClick={() => setAll(true)}>Show all {heard.length}</button>. </>
            )}
            Other bands, and adding any of these as a station, under <a className="rd-link" href="#/add/air">Find, On the air</a>.
          </p>
        </>
      ) : v.heard.length ? (
        <p className="m-line">
          Everything found here so far is under My stations. <a className="rd-link" href="#/add/air">Find, On the air</a> scans
          other bands.
        </p>
      ) : (
        <>
          <RxSwitch ids={v.receivers.map((r: Any) => r.id)} value={dev} onChange={setDevice}
                    note={v.receivers.length > 1 ? "The one with an antenna for FM." : null} />
          {!v.tools.rtl_power && <p className="m-trouble">Scanning needs the rtl-sdr tools: <code>brew install librtlsdr</code>.</p>}
          <div className="m-actions">
            {running && scanJob ? (
              <ActButton icon="stop" run={async () => setJob((await del("/radio/scan")).scan)}>Stop scanning</ActButton>
            ) : (
              <ActButton variant="primary" icon="search" disabled={!rx || running || !v.tools.rtl_power} run={() => start(dev)}>
                {rx?.busy.length ? `Pause ${rx.busy.join(", ")} and find what's on the air` : "Find what's on the air"}
              </ActButton>
            )}
            {!scanJob && <span className="m-hint">Broadcast FM, in about two minutes; stations name themselves.</span>}
          </div>
          {scanJob && !failed && !(empty && !running) && <ScanProgress job={scanJob} />}
          {(failed || (empty && !running)) && <NothingHeard v={v} result={failed ? scanJob : scanJob || v.last_scan} onTry={tryOn} />}
        </>
      )}
    </Section>
  );
}

function Heard({ v, onAdd, reload }: { v: Any; onAdd: (t: Any) => void; reload: () => Promise<void> }) {
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const save = (freq: number) => act(async () => { await post(`/radio/heard/${freq}`, { name: draft }); setEditing(null); await reload(); });
  return (
    <Section id="rd-heard" title="Heard here"
             description="Everything scans found from this place, kept across scans; nothing comes from outside. Names come from the stations' own RDS, or from you."
             actions={v.heard.length > 0 && (
               <ConfirmButton icon="trash" size="sm" confirm="Forget everything heard" onConfirm={() => act(async () => { await del("/radio/heard"); await reload(); }, "Forgotten")}>
                 Forget all
               </ConfirmButton>
             )}>
      <HeardFold n={v.heard.length}>
      <Table caption="Heard here" empty="Nothing yet: scan a band above."
             cols={[{ label: "MHz", num: true }, { label: "Name" }, { label: "What it is" }, { label: "Last heard" }, { label: "", width: "1%" }]}>
        {v.heard.map((e: Any) => (
          <tr key={e.freq_hz} className={cx(e.missing && "rd-missing")}>
            <td className="num">{fmtMhz(e.mhz, e.mode)}</td>
            <td>
              {editing === e.freq_hz ? (
                <form className="rd-rename" onSubmit={(ev) => { ev.preventDefault(); save(e.freq_hz); }}>
                  <Input aria-label="Name" autoFocus value={draft} onChange={(ev) => setDraft(ev.target.value)} onKeyDown={(ev) => ev.key === "Escape" && setEditing(null)} />
                  <Button size="sm" type="submit">Save</Button>
                </form>
              ) : (
                <span className="rd-name-cell">
                  <strong className="rd-name" dir="auto">{e.user_name || e.name || <span className="m-muted">–</span>}</strong>
                  <Button size="sm" variant="ghost" icon="edit" aria-label={`Rename ${fmtMhz(e.mhz, e.mode)} MHz`} onClick={() => { setEditing(e.freq_hz); setDraft(e.user_name || e.name || ""); }} />
                </span>
              )}
            </td>
            <td>
              <span className="rd-what">{[e.label || "Unlisted", e.rds?.pty && e.rds?.pty_name].filter(Boolean).join(" · ")}</span>
              {e.privacy !== "broadcast" && <Tag tone="muted">{CLASS_WORD[e.privacy] || e.privacy}</Tag>}
              {bandLine(e.band) && <span className="rd-sub">{bandLine(e.band)}</span>}
            </td>
            <td className="num-cell" title={`Heard in ${e.times} ${e.times === 1 ? "scan" : "scans"}`}>
              {agoWords(e.last_heard) || <span className="m-muted">–</span>}{e.missing && <span className="m-muted"> · not in the last scan</span>}
            </td>
            <td className="rd-act">
              {e.station ? <a href="#/" className="rd-link">On the page</a> : (
                <>
                  <Button size="sm" variant="ghost" icon="speaker" onClick={() => listenTo(e.mhz, e.mode)}>Listen</Button>
                  <Button size="sm" icon="plus" onClick={() => onAdd({ mhz: e.mhz, mode: e.mode, name: e.user_name || e.name || "", privacy: e.privacy, t: Date.now() })}>Add as a station</Button>
                </>
              )}
            </td>
          </tr>
        ))}
      </Table>
      </HeardFold>
    </Section>
  );
}

/** Every frequency heard here, to rename or forget: folded, since each band's finds are already
 * listed under the dial; with nothing heard yet, the table's own words say so. */
function HeardFold({ n, children }: { n: number; children: ReactNode }) {
  if (!n) return <>{children}</>;
  return <Fold id="rd-heard-all" summary={`All ${n === 1 ? "1 frequency" : `${n} frequencies`} heard here`}>{() => children}</Fold>;
}

// -- each receiver's own signals ----------------------------------------------------------------

function Spurs({ v, job, running, onJob }: { v: Any; job: Any; running: boolean; onJob: (j: Any) => void }) {
  if (!v.receivers.length) return null;
  const learning = job?.learn_spurs ? job : null;
  return (
    <Section id="rd-spurs" title="Learn them once"
             description="A receiver picks up a few signals of its own: its crystal's harmonics, the computer's USB noise. Disconnect the antenna and let Wavescribe learn them once; scans then mark them and leave them out.">
      {learning && <ScanProgress job={learning} />}
      <Table caption="Learnt spurs" cols={[{ label: "Receiver" }, { label: "Learnt" }, { label: "", width: "1%" }]}>
        {v.receivers.map((r: Any) => {
          const s = v.spurs[r.serial];
          return (
            <tr key={r.id}>
              <td><strong>{rxName(r.id)}</strong> <span className="m-muted mono">{r.serial}</span></td>
              <td>{s ? `${s.count} ${s.count === 1 ? "line" : "lines"}, ${new Date(s.t * 1000).toLocaleDateString()}` : <span className="m-muted">not yet</span>}</td>
              <td className="rd-act">
                <ConfirmButton size="sm" variant="secondary" confirm="The antenna is disconnected: learn"
                               onConfirm={() => act(async () => onJob((await post("/radio/scan", { device: r.id, band: "all", learn_spurs: true })).scan))}>
                  {s ? "Learn again" : "Learn"}
                </ConfirmButton>
                {running && learning?.device === r.id && <span className="m-muted"> learning…</span>}
              </td>
            </tr>
          );
        })}
      </Table>
    </Section>
  );
}

// -- what is kept of each kind of radio: the user decides -------------------------------------------

const KEEP: [string, string][] = [["0", "Never: shown live only"], ["1", "1 day"], ["3", "3 days"], ["7", "7 days"], ["30", "30 days"], ["keep", "Like broadcast stations"]];

function Kept({ v, reload }: { v: Any; reload: () => Promise<void> }) {
  const classes = ["amateur", "utility", "private", "digital"].filter((c) => v.policies[c]);
  const changed = classes.some((c) => v.policies[c].changed?.length);
  const set = (cls: string, changes: Any) => act(async () => { await post(`/radio/policy/${cls}`, changes); await reload(); }, "Saved");
  return (
    <Section id="rd-kept" title="What's kept of each kind of radio"
             description="You decide what Wavescribe keeps of radio that isn't broadcast, and whether it may leave this machine. The defaults are cautious, because in many countries such radio may be heard but not recorded or passed on. Choose what the law where you are allows: that's your responsibility, not Wavescribe's."
             actions={changed && (
               <ConfirmButton size="sm" confirm="Back to the cautious defaults" onConfirm={() => act(async () => { await del("/radio/policy"); await reload(); }, "Back to the defaults")}>
                 Defaults
               </ConfirmButton>
             )}>
      <div className="rd-kept-table">
      <Table caption="What's kept of each kind of radio"
             cols={[{ label: "Kind of radio" }, { label: "Text kept" }, { label: "Audio kept" }, { label: "Leaves this machine" }, { label: "Words in alerts" }]}>
        <tr>
          <td><strong>{CLASSES.broadcast.name}</strong><span className="rd-sub">{CLASSES.broadcast.examples}</span></td>
          <td colSpan={4} className="m-muted">Like any station: How long it's kept, above, applies.</td>
        </tr>
        {classes.map((cls) => {
          const p = v.policies[cls];
          const value = p.text_days === null ? "keep" : String(p.text_days);
          const options = KEEP.some(([k]) => k === value) ? KEEP : [...KEEP, [value, days(p.text_days)] as [string, string]];
          const label = CLASSES[cls]?.name || cls;
          return (
            <tr key={cls}>
              <td>
                <span className="rd-kind">
                  <strong>{label}</strong>
                  {p.needs_ack && <Tag tone="muted" title="Each station asks you to confirm you may receive it">asks first</Tag>}
                  {p.changed?.length > 0 && <Tag tone="muted" title={`Changed from the default: ${p.changed.join(", ")}`}>changed</Tag>}
                </span>
                <span className="rd-sub">{CLASSES[cls]?.examples}</span>
              </td>
              <td data-label="Text kept">
                <Select aria-label={`${label}: text kept`} value={value} options={options}
                        onChange={(e) => set(cls, { text_days: e.target.value === "keep" ? "keep" : +e.target.value })} />
              </td>
              <td data-label="Audio kept"><Switch label={`${label}: keep audio`} hideLabel checked={p.audio} onChange={(on) => set(cls, { audio: on })} /></td>
              <td data-label="Leaves this machine"><Switch label={`${label}: may leave this machine`} hideLabel checked={p.export} onChange={(on) => set(cls, { export: on })} /></td>
              <td data-label="Words in alerts"><Switch label={`${label}: words in alerts`} hideLabel checked={p.quote_in_alerts} onChange={(on) => set(cls, { quote_in_alerts: on })} /></td>
            </tr>
          );
        })}
      </Table>
      </div>
      <p className="m-hint">
        Changes apply at once, also to what's already kept: a shorter time removes older text within a minute.
        "Leaves this machine" covers the text feed, webhooks and other Wavescribes following this one. Without
        "Words in alerts", an alert says which rule matched, not what was said.
      </p>
    </Section>
  );
}
