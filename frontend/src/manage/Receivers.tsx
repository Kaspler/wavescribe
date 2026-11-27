// Receivers: the RTL-SDR dongles, their tuning, and what they hear; and the pieces the pages that
// use a receiver share: its name, the switch between receivers, and a dongle put to use in one step.
import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { S } from "../lib/live";
import { ActButton, Advanced, Button, ConfirmButton, Field, Fold, Input, LinkButton, Section, Segmented, Select, Table, Tag, act, toast } from "../components/ui";
import { type Any, ApiError, del, docUrl, get, post, useLoad } from "./api";
import { and } from "./bands";
import { type Receiver, type RxStation, type Span, usable } from "./dial";

// The reading view loads it on its own when a column offers the move: it stays its own piece.
const MoveThere = lazy(() => import("./MoveStation"));

/** A receiver as the page names it: receiver "c" is Receiver C, as on a front panel. */
export const rxName = (id: string) => (id.length === 1 ? id.toUpperCase() : id);

/** The first free short id for a new receiver: a, b, c… */
export const nextId = (taken: string[]) => "abcdefghijklmnopqrstuvwxyz".split("").find((c) => !taken.includes(c)) || `rx${taken.length + 1}`;

/** Makes a dongle a receiver as Settings, Receivers would: centred on FM, at the gain that suits
 * most antennas. Both can be changed there afterwards. */
export async function makeReceiver(serial: string, taken: string[]): Promise<string> {
  const id = nextId(taken);
  await post("/receivers", { id, serial, center_mhz: 98, gain_db: 30 });
  return id;
}

/** Names the receiver a scan or the tuner uses, and switches to another in place. */
export function RxSwitch({ ids, value, onChange, note }: { ids: string[]; value: string; onChange: (id: string) => void; note?: string | null }) {
  return (
    <div className="rx-switch">
      <span>Receiver</span>
      {ids.length > 1 ? (
        <Segmented label="Receiver" value={value} onChange={onChange}
                   options={ids.map((id) => ({ value: id, label: rxName(id), name: `Receiver ${rxName(id)}` }))} />
      ) : <strong>{rxName(value)}</strong>}
      {note && <span className="m-hint">{note}</span>}
    </div>
  );
}

/** Where a page that needs a receiver has none: a dongle that's plugged in becomes one and starts
 * on `what` in one press, without a trip to Settings first. Settings, Receivers stays the place
 * to choose among several dongles by serial. */
export function UseDongle({ dongles, tools, what, then, look }: { dongles: Any[]; tools: boolean; what: string; then: (id: string) => Promise<void>; look: () => Promise<unknown> }) {
  if (!tools || !dongles.length) {
    return (
      <>
        <p className="m-line">{tools ? PLUG_IN : NO_TOOLS}</p>
        <div className="m-actions"><ActButton icon="refresh" run={async () => { await look(); }}>Look again</ActButton></div>
        <NotWorking tools />
      </>
    );
  }
  const one = dongles.length === 1;
  return (
    <>
      <p className="m-line">
        {one ? "A dongle is" : `${dongles.length} dongles are`} plugged in, not in use yet. One press makes {one ? "it" : "one"} a
        receiver; its centre and gain can be changed later under <a className="rd-link" href="#/settings/receivers">Settings, Receivers</a>.
      </p>
      <div className="m-actions">
        <ActButton variant="primary" icon="antenna" run={async () => then(await makeReceiver(dongles[0].serial, []))}>
          {one ? "Use it" : "Use one"} and {what.replace(/^\w/, (c) => c.toLowerCase())}
        </ActButton>
        {!one && <LinkButton href="#/settings/receivers" variant="ghost">Choose by serial</LinkButton>}
      </div>
    </>
  );
}

/** What to plug in, when no dongle is in sight. The tools behind it wait under "Not working?". */
const PLUG_IN = (
  <>Plug in an RTL-SDR dongle with an antenna; the hardware guide (<a className="rd-link" href={docUrl("HARDWARE.md")} target="_blank" rel="noreferrer">docs/HARDWARE.md</a>) says which to buy.</>
);
/** Without the rtl-sdr tools no dongle can be seen, plugged in or not: that comes first. */
const NO_TOOLS = (
  <>Wavescribe can't see dongles yet: the rtl-sdr tools it uses aren't installed. Install them with <code>brew install librtlsdr</code>, then look again.</>
);

/** Why a plugged-in dongle may not show, and the tools Wavescribe needs to see one. */
export function NotWorking({ tools }: { tools: boolean }) {
  return (
    <Fold id="rx-trouble" summary="Not working?" open={!tools} className="adv">
      {!tools && (
        <p className="m-line">
          Wavescribe finds and tunes dongles with the rtl-sdr tools, which aren't installed. Install them with{" "}
          <code>brew install librtlsdr</code>, then look again.
        </p>
      )}
      <p className="m-hint">
        A dongle that doesn't show: try another USB port or cable, and allow it if macOS asks. <code>rtl_test</code> in
        a terminal lists the dongles the tools can see. A dongle another program holds is listed but can't be used
        until that program lets it go.
      </p>
    </Fold>
  );
}

/** Dongles on USB that nothing uses yet, each one click from being a receiver; or what to plug in. */
export function FirstReceiver({ dongles, tools, taken, look, onAdded }: { dongles: Any[]; tools: boolean; taken: string[]; look: () => unknown; onAdded: () => Promise<void> }) {
  if (!tools || !dongles.length) {
    return (
      <>
        <p className="m-line">{PLUG_IN}</p>
        <div className="m-actions"><ActButton icon="refresh" run={async () => { await look(); }}>Look again</ActButton></div>
        <NotWorking tools={tools} />
      </>
    );
  }
  return (
    <>
      <p className="m-line">{dongles.length === 1 ? "A dongle is" : `${dongles.length} dongles are`} plugged in. Use one as a receiver; its centre and gain can be changed afterwards.</p>
      <Table caption="Dongles on USB" cols={[{ label: "Dongle" }, { label: "Serial" }, { label: "", width: "1%" }]}>
        {dongles.map((d) => (
          <tr key={d.serial}>
            <td>
              {d.vendor || d.maker || ""} {d.product || d.model || ""}
              {d.held_elsewhere?.reason && <span className="rd-sub">In use: {d.held_elsewhere.reason}</span>}
            </td>
            <td className="mono">{d.serial}</td>
            <td className="rd-act">
              <ActButton size="sm" icon="plus" done="Receiver added"
                         run={async () => { await makeReceiver(d.serial, taken); await onAdded(); }}>
                Use it
              </ActButton>
            </td>
          </tr>
        ))}
      </Table>
      <NotWorking tools={tools} />
    </>
  );
}

export function Receivers({ onChange }: { onChange?: () => Promise<void> }) {
  const { data: v, error, reload: load } = useLoad(() => get("/receivers"));
  const [scan, setScan] = useState<Any[] | null>(null);
  const [form, setForm] = useState<Any | null>(null);
  const reload = async () => { await load(); setScan(null); await onChange?.(); };
  const look = async () => setScan((await get("/receivers?scan=1")).dongles || []);
  const none = !!v && !v.devices.length;
  useEffect(() => { if (none) look().catch(() => setScan([])); }, [none]);
  if (!v) return <p className="m-loading">{error ? `Couldn't load the receivers: ${error}` : "Loading…"}</p>;
  const ids: string[] = v.devices.map((d: Any) => d.id);
  const fresh = (scan || []).filter((d) => !v.devices.some((x: Any) => x.serial === d.serial));
  const bySerial = (
    <Advanced id="rx-add">
      {form ? <DeviceForm pre={form} onDone={async () => { setForm(null); await reload(); }} onCancel={() => setForm(null)} />
        : <Button size="sm" onClick={() => setForm({})}>Add by serial</Button>}
      <p className="m-hint">For a dongle the list doesn't show, or to choose its id and centre yourself.</p>
    </Advanced>
  );
  if (none) {
    return (
      <Section id="rx-h" title="Set up a receiver"
               description="A receiver is an RTL-SDR dongle on USB with an antenna. It hears FM radio without the internet, and other radio too.">
        {scan ? <FirstReceiver dongles={scan} tools={!!v.rtl_sdr} taken={[]} look={look} onAdded={reload} /> : <p className="m-loading">Looking for dongles…</p>}
        {bySerial}
      </Section>
    );
  }
  return (
    <>
      {v.devices.map((d: Any) => <Device key={d.id} d={d} reload={reload} />)}
      <Section id="rx-add-h" title="Add a receiver" description="Plug in another dongle, then look for it.">
        {scan ? (
          fresh.length ? <FirstReceiver dongles={fresh} tools={!!v.rtl_sdr} taken={ids} look={look} onAdded={reload} />
            : (
              <>
                <p className="m-line">No new dongle found.</p>
                <div className="m-actions"><ActButton icon="refresh" run={look}>Look again</ActButton></div>
                <NotWorking tools={!!v.rtl_sdr} />
              </>
            )
        ) : (
          <div className="m-actions"><ActButton icon="refresh" run={look}>Look for dongles</ActButton></div>
        )}
        {bySerial}
      </Section>
    </>
  );
}

function DeviceForm({ pre, onDone, onCancel }: { pre: Any; onDone: () => Promise<void>; onCancel: () => void }) {
  const [id, setId] = useState(pre.id || "");
  const [serial, setSerial] = useState(pre.serial || "");
  const [center, setCenter] = useState("");
  const [gain, setGain] = useState("30");
  return (
    <div className="m-panel">
      <div className="m-panel-head">
        <h4>Add a receiver by serial</h4>
        <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onCancel} />
      </div>
      <div className="m-form m-form-4">
        <Field id="dv-id" label="Id" hint="Short: a, b, c…"><Input id="dv-id" value={id} placeholder="a" onChange={(e) => setId(e.target.value)} /></Field>
        <Field id="dv-serial" label="Serial" hint="Unique per dongle (rtl_eeprom sets it)."><Input id="dv-serial" value={serial} className="mono" onChange={(e) => setSerial(e.target.value)} /></Field>
        <Field id="dv-center" label="Centre (MHz)" hint="Between the stations you want, so all lie within ±1 MHz.">
          <Input id="dv-center" type="number" step={0.1} value={center} placeholder="e.g. 95.0" onChange={(e) => setCenter(e.target.value)} />
        </Field>
        <Field id="dv-gain" label="Gain (dB)" hint="Start at 30; lower it if overload shows."><Input id="dv-gain" type="number" step={0.1} value={gain} onChange={(e) => setGain(e.target.value)} /></Field>
      </div>
      <div className="m-actions">
        <ActButton variant="primary" disabled={!id.trim() || !serial.trim() || !center} done="Receiver added"
                   run={async () => { await post("/receivers", { id: id.trim(), serial: serial.trim(), center_mhz: +center, gain_db: +gain }); await onDone(); }}>
          Save
        </ActButton>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

const RATES: [string, string][] = [["960000", "0.96 MS/s"], ["1200000", "1.2 MS/s"], ["1440000", "1.44 MS/s"], ["1920000", "1.92 MS/s"], ["2400000", "2.4 MS/s"]];

/** How far either side of its centre a receiver hears a channel, in MHz, for saying so. */
const reach = ([lo, hi]: Span) => +((hi - lo) / 2).toFixed(2);

function Device({ d, reload }: { d: Receiver; reload: () => Promise<void> }) {
  const [center, setCenter] = useState(String(d.center_mhz));
  const [gain, setGain] = useState(String(d.gain_db));
  const [rate, setRate] = useState(String(d.sample_rate));
  const [ppm, setPpm] = useState(String(d.ppm ?? 0));
  // Tuned elsewhere meanwhile (a move back to a station, from here or another page): the fields
  // follow, so Apply doesn't undo it.
  useEffect(() => { setCenter(String(d.center_mhz)); }, [d.center_mhz]);
  useEffect(() => { setGain(String(d.gain_db)); }, [d.gain_db]);
  // Its stations it doesn't hear where it is tuned, each with the move back where a centre hears
  // it (a switched-off one is left as it is: the move would start it).
  const outOfReach = (s: RxStation) => !!s.out_of_reach && !S.byId[s.id]?.disabled;
  const away = d.stations.filter(outOfReach);
  // The server's question before a retune leaves stations out; nothing has changed while it is asked.
  const [asked, setAsked] = useState<{ question: string; stops: string[] } | null>(null);
  // The question replaces Apply, and Apply the question: the focus goes along.
  const actions = useRef<HTMLDivElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || asked) actions.current?.querySelector<HTMLElement>("button")?.focus();
    opened.current = true;
  }, [!!asked]);
  const apply = async (confirmed = false) => {
    setAsked(null);
    let after: Any;
    try {
      after = await post(`/receivers/${d.id}`, { center_mhz: +center, gain_db: +gain, sample_rate: +rate, ppm: Math.round(+ppm) || 0, retune: confirmed });
    } catch (e) {
      if (e instanceof ApiError && e.code === "retune" && !confirmed) { setAsked({ question: e.message, stops: e.data.holders || [] }); return; }
      throw e;
    }
    const was = new Set(away.map((s) => s.id));
    const left = ((after?.stations || []) as RxStation[]).filter((s) => outOfReach(s) && !was.has(s.id)).map((s) => s.name);
    toast(left.length ? `Receiver ${rxName(d.id)} retuned; out of its reach now: ${and(left)}` : "Receiver retuned; its stations restarted");
    await reload();
  };
  // The move replaces its own button: the focus goes to Apply.
  const moved = async () => {
    await reload();
    requestAnimationFrame(() => actions.current?.querySelector<HTMLElement>("button")?.focus());
  };
  // No to the question: the fields go back to how the receiver is tuned.
  const keep = () => {
    setAsked(null);
    setCenter(String(d.center_mhz));
    setGain(String(d.gain_db));
    setRate(String(d.sample_rate));
    setPpm(String(d.ppm ?? 0));
  };
  const over = d.clipped > 0.001;
  return (
    <Section id={`dv-${d.id}`} title={`Receiver ${rxName(d.id)}`}
             description={`Takes in ${+d.span_mhz[0].toFixed(3)}–${+d.span_mhz[1].toFixed(3)} MHz around its centre.`}
             actions={<>
               {d.running ? <Tag tone="good">running</Tag> : <Tag tone="muted">idle</Tag>}
               {over && <Tag tone="warn">overload {(d.clipped * 100).toFixed(2)}%</Tag>}
               {d.reconnects > 0 && <Tag tone="warn">{d.reconnects} restarts</Tag>}
               {d.origin === "page" && <Tag tone="accent">added here</Tag>}
             </>}>
      <div className="m-form m-form-4">
        <Field id={`c-${d.id}`} label="Centre (MHz)"
               hint={`The middle of what it takes in: FM stations up to ${reach(usable(d.span_mhz, "wfm"))} MHz either side are heard, narrow channels up to ${reach(usable(d.span_mhz, "nfm"))} MHz.`}>
          <Input id={`c-${d.id}`} type="number" step={0.05} value={center} onChange={(e) => setCenter(e.target.value)} />
        </Field>
        <Field id={`g-${d.id}`} label="Gain (dB)" hint="How much it amplifies, 0–49.6. Too much overloads it and adds false signals; too little loses weak stations.">
          <Input id={`g-${d.id}`} type="number" step={0.1} value={gain} onChange={(e) => setGain(e.target.value)} />
        </Field>
      </div>
      <Advanced id={`rx-${d.id}`}>
        {(open) => (
          <>
            <div className="m-form m-form-4">
              <Field id={`r-${d.id}`} label="Sample rate" hint="Wider hears more of the band at once and costs more CPU.">
                <Select id={`r-${d.id}`} value={rate} onChange={(e) => setRate(e.target.value)} options={RATES} />
              </Field>
              <Field id={`p-${d.id}`} label="Frequency correction (ppm)" hint="The dongle's crystal error, as rtl_test -p measures it. 0 for a dongle with a TCXO.">
                <Input id={`p-${d.id}`} type="number" step={1} min={-500} max={500} value={ppm} onChange={(e) => setPpm(e.target.value)} />
              </Field>
            </div>
            <p className="m-hint">Serial <span className="mono">{d.serial}</span>. Apply, below, sends these with the centre and gain.</p>
            <div className="m-actions">
              <ActButton variant="secondary"
                         run={async () => { await post(`/receivers/${d.id}/monitor`, { on: !d.monitoring }); await reload(); }}>
                {d.monitoring ? "Stop the antenna test" : "Test the antenna"}
              </ActButton>
              <span className="m-hint rd-inline">Runs the receiver with no station, so the spectrum shows what the antenna picks up.</span>
              <span className="m-spacer" />
              {d.origin === "page" && (
                <ConfirmButton icon="trash" confirm={`Remove receiver ${d.id}`} onConfirm={() => act(async () => { await del(`/receivers/${d.id}`); await reload(); }, "Removed")}>
                  Remove
                </ConfirmButton>
              )}
            </div>
            {open && <Spectrum d={d} />}
            <Table caption={`Stations on receiver ${d.id}`} empty="No station uses this receiver yet."
                   cols={[{ label: "Station" }, { label: "MHz", num: true }, { label: "Running" }, { label: "Signal" }]}>
              {d.stations.map((s: Any) => (
                <tr key={s.name}>
                  <td dir="auto">{s.name}</td><td className="num">{s.mhz.toFixed(2)}</td><td>{s.running ? "yes" : "no"}</td>
                  <td className="num-cell">{s.signal_db != null ? <>{s.signal_db} dBFS{s.clipped > 0.001 && <Tag tone="warn">overload</Tag>}</> : "–"}</td>
                </tr>
              ))}
            </Table>
          </>
        )}
      </Advanced>
      {away.map((s) => (
        <div key={s.id} className="m-trouble">
          <p>{s.out_of_reach}</p>
          {s.reach_center_mhz != null && <Suspense fallback={null}><MoveThere s={s} device={d.id} onDone={moved} /></Suspense>}
        </div>
      ))}
      {asked && <p className="m-line" role="status">{asked.question}</p>}
      <div ref={actions} className="m-actions">
        {asked ? (
          <>
            <ActButton key="yes" variant="primary" run={() => apply(true)}>{asked.stops.length ? `Stop ${and(asked.stops)} and apply` : "Apply all the same"}</ActButton>
            <Button variant="ghost" onClick={keep}>Keep it as it is</Button>
          </>
        ) : (
          // Its own button, not the yes still busy with the retune: that one can't take the focus.
          <ActButton key="apply" variant="primary" run={() => apply()}>Apply</ActButton>
        )}
      </div>
    </Section>
  );
}

/** The live spectrum and its waterfall. Both sit on the instrument's dark surface in either theme. */
function Spectrum({ d }: { d: Any }) {
  const spec = useRef<HTMLCanvasElement>(null);
  const fall = useRef<HTMLCanvasElement>(null);
  const [note, setNote] = useState("The spectrum shows while the receiver runs: start one of its stations, or test the antenna.");
  useEffect(() => {
    let busy = false;
    const css = getComputedStyle(document.documentElement);
    const tok = (n: string) => css.getPropertyValue(n).trim() || "#888";
    const id = setInterval(async () => {
      if (busy || document.hidden) return;
      busy = true;
      try {
        const { spectrum: s } = await get(`/receivers/${d.id}/spectrum`);
        if (!s) { setNote("No spectrum: the receiver isn't running. Start one of its stations, or test the antenna."); return; }
        setNote(`Centre ${(s.center_hz / 1e6).toFixed(3)} MHz · gain ${s.gain_db} dB. Peaks are stations; a flat floor with no peaks means no antenna, or too little gain.`);
        drawSpectrum(spec.current!, s, d, tok);
        drawFall(fall.current!, s);
      } catch {
        /* the next poll tries again */
      } finally {
        busy = false;
      }
    }, 450);
    return () => clearInterval(id);
  }, [d]);
  return (
    <figure className="m-scope">
      <canvas ref={spec} className="m-spectrum" width={1024} height={180} role="img" aria-label={`Spectrum of dongle ${d.id}`} />
      <canvas ref={fall} className="m-fall" width={512} height={140} role="img" aria-label={`Waterfall of dongle ${d.id}`} />
      <figcaption className="m-hint">{note}</figcaption>
    </figure>
  );
}

function drawSpectrum(canvas: HTMLCanvasElement, s: Any, d: Any, tok: (n: string) => string) {
  const ctx = canvas.getContext("2d")!;
  const W = canvas.width, H = canvas.height, lo = -100, hi = -10;
  ctx.clearRect(0, 0, W, H);
  ctx.strokeStyle = tok("--band-tick");
  ctx.fillStyle = tok("--band-ink-2");
  ctx.font = "20px system-ui, sans-serif";
  ctx.lineWidth = 1;
  for (let db = -90; db <= -20; db += 20) {
    const y = H - ((db - lo) / (hi - lo)) * H;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    ctx.fillText(`${db} dB`, 6, y - 6);
  }
  const start = s.center_hz - s.sample_rate / 2;
  for (const st of d.stations) {
    const x = ((st.mhz * 1e6 - start) / s.sample_rate) * W;
    ctx.fillStyle = tok("--accent");
    ctx.fillRect(x - 1, 0, 2, H);
    ctx.fillText(`${st.name} ${st.mhz.toFixed(1)}`, Math.min(W - 180, x + 6), 24);
  }
  ctx.strokeStyle = s.clipped > 0.001 ? tok("--band-lamp-trouble") : tok("--band-ink");
  ctx.lineWidth = 2;
  ctx.beginPath();
  s.db.forEach((v: number, i: number) => {
    const x = (i / (s.db.length - 1)) * W;
    const y = H - ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * H;
    if (i) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
  });
  ctx.stroke();
}

/** One row per poll, scrolling down. Noise stays near the band's black; signal warms through the
 * accent to near white, one hue family so strength reads as brightness. */
function drawFall(canvas: HTMLCanvasElement, s: Any) {
  const ctx = canvas.getContext("2d")!;
  const W = canvas.width, H = canvas.height;
  ctx.drawImage(canvas, 0, 0, W, H - 1, 0, 1, W, H - 1);
  const row = ctx.createImageData(W, 1);
  const n = s.db.length;
  const stops: [number, number, number][] = [[12, 12, 11], [74, 40, 22], [210, 78, 20], [242, 150, 80], [255, 240, 225]];
  for (let x = 0; x < W; x++) {
    const t = Math.max(0, Math.min(1, (s.db[Math.floor((x / W) * n)] + 100) / 80)) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(t)), f = t - i;
    const c = stops[i].map((a, k) => a + (stops[i + 1][k] - a) * f);
    row.data.set([c[0], c[1], c[2], 255], x * 4);
  }
  ctx.putImageData(row, 0, 0);
}
