// Radio: find what's on the air with an idle receiver, see what was heard here and turn a frequency
// into a station (Find stations, On the air); what was found, to play (Listen, On the air here); set
// up the receivers and the region, and decide what is kept of radio that isn't broadcast (Settings)
// (src/wavescribe/radio/service.py).
import { useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../components/Icon";
import { ActButton, Advanced, Button, Check, ConfirmButton, Field, Fold, Input, LangChips, Section, Segmented, Select, Switch, Table, Tag, act, cx, toast } from "../components/ui";
import { langName } from "../lib/lang";
import { type Any, ApiError, del, get, knownLangs, listenTo, post, refreshPage, useEvery, useLoad } from "./api";
import { bandLine, fmtMhz } from "./bands";
import { Receivers, RxSwitch, UseDongle, makeReceiver, rxName } from "./Receivers";
import { MODE_HINT } from "./Stations";

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
const WATCH: [string, string][] = [["0", "One sweep"], ["1", "Watch 1 min"], ["5", "Watch 5 min"], ["15", "Watch 15 min"]];

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
function snapToBand(bands: Any[], mhz: number): number {
  const b = bandAt(bands, mhz);
  if (b?.channels_mhz?.length) return b.channels_mhz.reduce((p: number, c: number) => (Math.abs(c - mhz) < Math.abs(p - mhz) ? c : p));
  if (b?.step_khz) {
    const step = b.step_khz / 1000;
    return +(b.start_mhz + Math.round((mhz - b.start_mhz) / step) * step).toFixed(5);
  }
  return +mhz.toFixed(3);
}

/** The narrowest band of the plan holding `mhz` (the server decides; this is for hints). */
function bandAt(bands: Any[], mhz: number): Any | null {
  const here = bands.filter((b) => b.start_mhz <= mhz && mhz <= b.stop_mhz);
  return here.sort((a, b) => (a.stop_mhz - a.start_mhz) - (b.stop_mhz - b.start_mhz))[0] || null;
}

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
const fmBand = (v: Any): Any | undefined => v.bands.find((b: Any) => b.mode === "wfm" && b.privacy === "broadcast" && b.tunable);

/** The receiver a scan starts on: the one that scanned last (most likely the one with an antenna;
 * if it's busy, the button says what the scan pauses), else a free one. Switching stays one press. */
function pickReceiver(v: Any): string {
  const last = v.receivers.find((r: Any) => r.serial === v.last_scan?.serial);
  return (last || v.receivers.find((r: Any) => !r.busy.length && !r.scanning) || v.receivers[0])?.id || "";
}

/** Anything worth listening to: not the receivers' own lines, not a digital service's noise. */
const real = (s: Signal) => !s.spur && s.privacy !== "digital";

const loading = (error: string | null) => <p className="m-loading">{error ? `Couldn't load the radio: ${error}` : "Loading…"}</p>;

/** Find stations, On the air: sweep a band, keep a frequency as a station, and what was heard here. */
export function OnTheAir() {
  const { v, error, reload, job, setJob, running } = useRadio();
  const [add, setAdd] = useState<Any | null>(null);
  if (!v) return loading(error);
  const openAdd = (t: Any) => {
    setAdd(t);
    requestAnimationFrame(() => document.getElementById("rd-tune")?.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" }));
  };
  return (
    <>
      <Finder v={v} job={job} running={running} onJob={setJob} onAdd={openAdd} reload={reload} />
      {add && <AddFromAir v={v} pre={add} key={`${add.mhz}-${add.t}`} onDone={async () => { setAdd(null); await reload(); }} />}
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

function Finder({ v, job, running, onJob, onAdd, reload }: { v: Any; job: Any; running: boolean; onJob: (j: Any) => void; onAdd: (t: Any) => void; reload: () => Promise<void> }) {
  const [device, setDevice] = useState<string>(() => pickReceiver(v));
  const fm = fmBand(v);
  const [band, setBand] = useState<string>(fm?.id || v.bands[0]?.id || "all");
  const [lo, setLo] = useState("");
  const [hi, setHi] = useState("");
  const [watch, setWatch] = useState("0");
  const [rds, setRds] = useState(true);
  const [showAll, setShowAll] = useState(false);
  const rx = v.receivers.find((r: Any) => r.id === device);
  const chosen = v.bands.find((b: Any) => b.id === band);
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
  const scanWhat = band === fm?.id ? "Find FM stations" : chosen ? `Scan ${chosen.name}` : band === "all" ? "Scan everything" : "Scan this range";
  const start = async (on = device) => {
    const body: Any = { device: on, rds, watch_min: +watch, pause: !!v.receivers.find((r: Any) => r.id === on)?.busy.length };
    if (band === "custom") Object.assign(body, { start_mhz: +lo, stop_mhz: +hi });
    else body.band = band;
    const { scan } = await post("/radio/scan", body);
    onJob(scan);
  };
  // The same scan on another receiver, or on a dongle just made one.
  const tryOn = async (id: string) => { await reload(); setDevice(id); await start(id); };
  return (
    <Section id="rd-find" title="Scan for stations"
             description="The receiver sweeps the band and lists every signal above the noise, with what the band plan says it is; FM stations name themselves. What's found is kept under Heard here.">
      {!v.receivers.length ? (
        <>
          <UseDongle dongles={v.dongles || []} tools={!!v.tools.rtl_sdr} what={scanWhat} look={reload} then={tryOn} />
          <p className="m-hint">Internet stations need no receiver: they're added under <a className="rd-link" href="#/add/internet">On the internet</a>.</p>
        </>
      ) : (
        <>
          <RxSwitch ids={v.receivers.map((r: Any) => r.id)} value={device} onChange={setDevice}
                    note={v.receivers.length > 1 ? "The one with an antenna for this band." : null} />
          {!v.tools.rtl_power && <p className="m-trouble">Scanning needs the rtl-sdr tools: <code>brew install librtlsdr</code>.</p>}
          <div className="m-actions">
            {running && !job.learn_spurs ? (
              <ActButton icon="stop" run={async () => onJob((await del("/radio/scan")).scan)}>Stop scanning</ActButton>
            ) : (
              <ActButton variant="primary" icon="search" disabled={!rx || running || !v.tools.rtl_power || (band === "custom" && !(+lo < +hi))}
                         run={() => start()}>
                {pause ? `Pause and ${scanWhat.replace(/^\w/, (c) => c.toLowerCase())}` : scanWhat}
              </ActButton>
            )}
            {pause && !running && (
              <span className="m-hint rd-inline">Receiver {rxName(rx.id)} is running {rx.busy.join(", ")}. The scan pauses {rx.busy.length === 1 ? "it" : "them"} and starts {rx.busy.length === 1 ? "it" : "them"} again when it ends.</span>
            )}
          </div>
          {chosen && chosen.privacy !== "broadcast" && <p className="m-hint">{policyLine(chosen.privacy, v.policies)}</p>}
          <Advanced id="rd-find">
            <div className="m-form rd-form">
              <Field id="rd-band" label="What to scan" wide
                     hint={chosen ? bandLine(chosen.id) || undefined : band === "all" ? "The whole tuner, in coarse steps: good for strong signals, too coarse for narrow channels." : "From and to, in MHz."}>
                <Select id="rd-band" value={band} onChange={(e) => setBand(e.target.value)} options={bandOptions} />
              </Field>
              {band === "custom" && (
                <>
                  <Field id="rd-lo" label="From (MHz)" hint="The lower end."><Input id="rd-lo" type="number" step={0.001} value={lo} placeholder="e.g. 150" onChange={(e) => setLo(e.target.value)} /></Field>
                  <Field id="rd-hi" label="To (MHz)" hint="The upper end, up to 1766."><Input id="rd-hi" type="number" step={0.001} value={hi} placeholder="e.g. 175" onChange={(e) => setHi(e.target.value)} /></Field>
                </>
              )}
              <Field id="rd-watch" label="How long"
                     hint={chosen && chosen.privacy !== "broadcast" ? "These channels talk now and then: watch a few minutes." : "One sweep finds broadcast stations; watching also catches channels that talk now and then."}>
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
          {result?.spectrum && <ScanSpectrum spectrum={result.spectrum} signals={shown} bands={v.bands} />}
          {(failed || (result && !running && !found.length)) && <NothingHeard v={v} result={failed ? scanJob : result} onTry={tryOn} />}
          {result && (
            <>
              {(shown.length > 0 || running) && (
                <Table caption="Signals found" empty="Nothing yet…"
                       cols={[{ label: "MHz", num: true }, { label: "Name" }, { label: "What it is" }, { label: "Above noise", num: true }, { label: "", width: "1%" }]}>
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
    </Section>
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
  const device: string | undefined = result.device ?? v.receivers.find((r: Any) => r.serial === result.serial)?.id;
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

function SignalRow({ s, station, onAdd, common }: { s: Signal; station?: string | null; onAdd: (t: Any) => void; common?: string | null }) {
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
          : clock ? <Button size="sm" variant="ghost" icon="speaker" onClick={() => listenTo(s.mhz, s.mode)}>Listen</Button>
          : s.privacy === "digital" ? <Button size="sm" variant="ghost" onClick={() => onAdd({ mhz: s.mhz, mode: null, name, privacy: s.privacy, t: Date.now() })}>Try anyway</Button>
          : (
            <>
              <Button size="sm" variant="ghost" icon="speaker" onClick={() => listenTo(s.mhz, s.mode)}>Listen</Button>
              <Button size="sm" icon="plus" onClick={() => onAdd({ mhz: s.mhz, mode: s.mode, name, privacy: s.privacy, t: Date.now() })}>Add as a station</Button>
            </>
          )}
      </td>
    </tr>
  );
}

/** The scan's spectrum on the instrument's dark band; signals ticked, FM names printed, hover reads out. */
function ScanSpectrum({ spectrum, signals, bands }: { spectrum: Any; signals: Any[]; bands: Any[] }) {
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
                  if (listenable(sig)) listenTo(sig.mhz, sig.mode);
                } else if (hover) {
                  // anywhere else: the nearest channel of the band there, to listen for what comes and goes
                  listenTo(snapToBand(bands, hover.mhz), bandAt(bands, hover.mhz)?.mode || null);
                }
              }}
              style={{ cursor: "pointer" }} />
      {hover && (
        <span className="trace-tip rd-tip" style={{ left: `clamp(60px, ${hover.x}px, calc(100% - 60px))` }}>
          {sig ? `${fmtMhz(sig.mhz, sig.mode)} MHz${sig.rds?.ps ? ` · ${sig.rds.ps}` : ""} · ${sig.snr_db.toFixed(0)} dB above noise` : `${fmtMhz(snapToBand(bands, hover.mhz), bandAt(bands, hover.mhz)?.mode)} MHz · click to listen`}
        </span>
      )}
      <figcaption className="m-hint">Peaks are signals: click one, or anywhere, to listen there under Listen. The line is the strongest each frequency reached during the scan.</figcaption>
    </figure>
  );
}

// -- adding: a frequency found on the air becomes a station ----------------------------------------

const DOES = [
  { value: "listen", label: "Listen only" },
  { value: "transcribe", label: "Transcribe" },
  { value: "translate", label: "Transcribe and translate" },
];

function AddFromAir({ v, pre, onDone }: { v: Any; pre: Any; onDone: () => Promise<void> }) {
  const fits = (r: Any, mhz: number) => Math.abs(mhz - r.center_mhz) <= r.span_mhz / 2 - 0.12;
  const f: number = pre.mhz;
  const band = bandAt(v.bands, f);
  const [mode, setMode] = useState<string>(pre.mode && band && pre.mode !== band.mode ? pre.mode : "");
  const pickRx = () =>
    (v.receivers.find((r: Any) => fits(r, f) && !r.scanning) || v.receivers.find((r: Any) => !r.busy.length && !r.scanning) || v.receivers[0])?.id || "";
  const [device, setDevice] = useState<string>(pickRx);
  const [name, setName] = useState(pre.name || "");
  const [does, setDoes] = useState("translate");
  const [lang, setLang] = useState<string>(v.defaults.language || "");
  const [to, setTo] = useState<string[]>(v.defaults.translate_to || []);
  const [ack, setAck] = useState(false);
  const [retune, setRetune] = useState<Any | null>(null);
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
  const effMode = mode || band?.mode || "nfm";
  const submit = async (force = false) => {
    try {
      const st = await post("/radio/tune", {
        device, mhz: f, mode: mode || undefined, name: name.trim() || undefined, language: lang.trim(), translate_to: does === "translate" ? to : [],
        acknowledge: ack, retune: force, ...(does === "listen" ? { transcribe: false } : does === "transcribe" ? { translate: false } : {}),
      });
      setRetune(null);
      await refreshPage();
      toast(does === "listen" ? `${st.name} added: play it under Listen` : `Transcribing ${st.name}: it's on the reading page`);
      await onDone();
    } catch (e) {
      if (e instanceof ApiError && e.code === "retune") setRetune(e.message);
      else throw e;
    }
  };
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
          <Field id="rd-lang" label="Spoken language" hint={lang ? langName(lang) : "As a BCP-47 tag, e.g. es-ES"}>
            <Input id="rd-lang" list="m-langs" value={lang} placeholder="e.g. es-ES" onChange={(e) => setLang(e.target.value)} />
          </Field>
        )}
        {does === "translate" && (
          <Field id="rd-to" label="Translate into" wide hint="Continuously, as it speaks."><LangChips value={to} onChange={setTo} known={knownLangs()} label="Translate into" /></Field>
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
              <Select id="rd-rx" value={device} onChange={(e) => { setDevice(e.target.value); setRetune(null); }}
                      options={v.receivers.map((r: Any) => [r.id, `${rxName(r.id)} · ${r.center_mhz.toFixed(2)} MHz${r.busy.length ? ` · ${r.busy.join(", ")}` : ""}`])} />
            </Field>
          )}
        </div>
      </Advanced>
      {needsAck && (
        <p className="m-line">
          <Check checked={ack} onChange={setAck}>
            I've checked that I may receive this where I am, and I'm responsible for how I use what's heard.
            Laws on radio that isn't broadcast differ by country (<code>docs/RADIO.md</code>).
          </Check>
        </p>
      )}
      {retune && (
        <div className="m-panel rd-retune">
          <p className="m-line">{retune}</p>
          <div className="m-actions">
            <ActButton variant="primary" run={() => submit(true)}>Retune and add</ActButton>
            <Button variant="ghost" onClick={() => setRetune(null)}>Keep it as it is</Button>
          </div>
        </div>
      )}
      <div className="m-actions">
        <ActButton variant="primary" icon="plus" disabled={(does !== "listen" && !lang.trim()) || (needsAck && !ack) || !device || !!rx?.scanning} run={() => submit(false)}>
          Add station
        </ActButton>
        <Button variant="ghost" onClick={() => onDone()}>Cancel</Button>
      </div>
    </Section>
  );
}

// -- what this machine has heard ---------------------------------------------------------------

const ago = (t: number) => {
  const h = (Date.now() / 1000 - t) / 3600;
  return h < 1 ? `${Math.max(1, Math.round(h * 60))} min ago` : h < 48 ? `${Math.round(h)} h ago` : `${Math.round(h / 24)} days ago`;
};

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
                          playing ? "playing" : e.missing ? "not in the last scan" : `heard ${ago(e.last_heard)}`].filter(Boolean).join(" · ")}
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
              {ago(e.last_heard)}{e.missing && <span className="m-muted"> · not in the last scan</span>}
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
    </Section>
  );
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
              <td>
                <Select aria-label={`${label}: text kept`} value={value} options={options}
                        onChange={(e) => set(cls, { text_days: e.target.value === "keep" ? "keep" : +e.target.value })} />
              </td>
              <td><Switch label={`${label}: keep audio`} hideLabel checked={p.audio} onChange={(on) => set(cls, { audio: on })} /></td>
              <td><Switch label={`${label}: may leave this machine`} hideLabel checked={p.export} onChange={(on) => set(cls, { export: on })} /></td>
              <td><Switch label={`${label}: words in alerts`} hideLabel checked={p.quote_in_alerts} onChange={(on) => set(cls, { quote_in_alerts: on })} /></td>
            </tr>
          );
        })}
      </Table>
      <p className="m-hint">
        Changes apply at once, also to what's already kept: a shorter time removes older text within a minute.
        "Leaves this machine" covers the text feed, webhooks and other Wavescribes following this one. Without
        "Words in alerts", an alert says which rule matched, not what was said.
      </p>
    </Section>
  );
}
