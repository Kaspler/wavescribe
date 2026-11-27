// Health: is every station heard and keeping up, and is this machine coping? Storage: how much
// is kept, and for how long.
import { useRef, useState } from "react";
import { columnLag, headerLag, skippedChecks } from "../lib/behind";
import { S } from "../lib/live";
import { type Lamp as LampState } from "../lib/station";
import { useTopics } from "../lib/store";
import { ago, day, hm } from "../lib/time";
import { ActButton, Advanced, Field, Input, Lamp, LinkButton, Section, Table, Tag, cx } from "../components/ui";
import { T } from "../reading/strings";
import { type Any, bytes, get, mb, num, post, secs, useEvery, useLoad } from "./api";

const kept = (v: string, old: number | null) => (v.trim() === "" ? old : +v);

export function Status() {
  const { data: s, error, reload } = useLoad(() => get("/status"));
  useEvery(5000, reload);
  if (!s) return <p className="m-loading">{error ? `Couldn't load the status: ${error}` : "Loading…"}</p>;
  return (
    <>
      <StationHealth s={s} />
      <Machine s={s} />
      {Object.keys(s.outputs || {}).length > 0 && <OutputHealth outs={s.outputs} />}
      <Advanced id="health-pipeline" label="Models and queues">
        <Pipeline s={s} />
      </Advanced>
    </>
  );
}

export function Storage() {
  const { data: s, error } = useLoad(() => get("/status"));
  if (!s) return <p className="m-loading">{error ? `Couldn't load the storage: ${error}` : "Loading…"}</p>;
  return <Retention s={s} />;
}

function stationLamp(st: Any): [LampState, string] {
  if (st.remote) return st.connected ? ["on", `following, ${st.received || 0} received`] : ["trouble", `reconnecting${st.error ? `: ${st.error}` : ""}`];
  if (st.stopping) return ["wait", "stopping"];
  if (!st.connected && st.error) {
    const next = st.retry_in_s >= 1 ? `next try in ${Math.ceil(st.retry_in_s)} s` : "trying again now";
    return ["trouble", `down: ${st.error} · ${next}`];
  }
  if (!st.connected) return ["trouble", "reconnecting"];
  if (st.music) return ["dim", "music"];
  if (st.in_speech) return ["speech", "speech"];
  return ["on", `quiet ${ago(st.quiet_s || 0)}`];
}

/** Whether the machine keeps up, live from the status the page already receives. */
function KeepingUp() {
  useTopics("status");
  const lag = headerLag(S.status);
  const skipped = skippedChecks(S.status);
  if (!lag.behind && !skipped) return null;
  return (
    <div className="m-note" role="status">
      {lag.behind && (
        <p>
          <strong>{T.behindBy(lag.minutes)}.</strong> {T.behindWhat(lag.minutes)}{S.status?.load?.reason ? ` ${S.status.load.reason}` : ""} {T.behindDo}
        </p>
      )}
      {skipped > 0 && <p className="is-trouble">{T.checksSkipped}</p>}
    </div>
  );
}

function StationHealth({ s }: { s: Any }) {
  useTopics("status");
  const pl = s.pipeline || {};
  const rows = Object.entries(pl.stations || {}) as [string, Any][];
  const signal = rows.some(([, st]) => st.signal_db != null); // only radios report one
  return (
    <Section id="st-health" title="Stations"
             description="Lag runs from the end of an utterance to its text and to its translation, over the last 10 minutes. A stream's own delay behind the broadcast, often 5–30 s, comes on top.">
      <KeepingUp />
      <div className="m-lag">
      <Table caption="Running stations" empty="No station is running."
             cols={[{ label: "Station" }, { label: "State" }, ...(signal ? [{ label: "Signal" }] : []), { label: "Text p50", num: true }, { label: "p95", num: true },
                    { label: "Translated p50", num: true }, { label: "p95", num: true }, { label: "Utterances", num: true }]}>
        {rows.map(([id, st]) => {
          const [lamp, state] = stationLamp(st);
          const lag = (pl.lag || {})[id] || {};
          const late = columnLag(S.live[id]);
          const over = st.clipped > 0.001;
          return (
            <tr key={id}>
              <td><span className="m-station"><Lamp state={lamp} />{S.byId[id]?.name || id}</span></td>
              <td className={cx(lamp === "trouble" && "is-trouble")}>
                {state}{st.reconnects ? ` · ${st.reconnects} reconnects` : ""}
                {late.text && <span className="is-trouble"> · {T.colBehind(late.text)}</span>}
                {late.translation && ` · ${T.colTranslationBehind(late.translation)}`}
              </td>
              {signal && (
                <td className="num-cell">
                  {st.signal_db != null ? <>{st.signal_db} dBFS{over && <Tag tone="warn">overload</Tag>}</> : "–"}
                </td>
              )}
              <td className="num">{secs(lag.text_p50)}</td>
              <td className="num">{secs(lag.text_p95)}</td>
              <td className="num">{secs(lag.translated_p50)}</td>
              <td className="num">{secs(lag.translated_p95)}</td>
              <td className="num">{lag.n ?? 0}</td>
            </tr>
          );
        })}
      </Table>
      </div>
    </Section>
  );
}

interface Meter {
  label: string;
  value: string;
  detail: string;
  key: string;
  max?: number;
  unit: (v: number) => string;
  trouble?: boolean;
}

function Machine({ s }: { s: Any }) {
  const sys = s.system || {};
  const p = sys.process || {}, mem = sys.memory || {}, cpu = sys.cpu || {}, gpu = sys.gpu || {};
  const pow = sys.power || {}, disk = sys.disk || {}, net = sys.network || {};
  const pressure = mem.pressure && mem.pressure !== "normal";
  const battery = pow.battery_percent != null
    ? `battery ${Math.round(pow.battery_percent)}%${pow.on_ac ? ", on power" : pow.minutes_remaining ? `, ${num(pow.minutes_remaining / 60)} h left` : ""}`
    : "no battery reading";
  const meters: Meter[] = [
    { label: "Memory", key: "footprint_mb", value: p.footprint_mb != null ? mb(p.footprint_mb) : "–", unit: mb, trouble: pressure,
      detail: `Wavescribe's footprint · system ${num(mem.used_gb)} of ${num(mem.total_gb, 0)} GB used${mem.swap_used_gb ? `, swap ${num(mem.swap_used_gb)} GB` : ""}${pressure ? `, pressure ${mem.pressure}` : ""}` },
    { label: "CPU", key: "process_cpu", value: p.cpu_percent == null ? "measuring…" : `${num(p.cpu_percent, 0)}%`, unit: (v) => `${num(v, 0)}%`,
      detail: `of one core · whole machine ${num(cpu.total_percent, 0)}% of ${cpu.cores || "?"} cores` },
    { label: "GPU", key: "gpu", max: 100, value: `${num(gpu.utilization_percent, 0)}%`, unit: (v) => `${num(v, 0)}%`,
      detail: `whole GPU at that instant, so it's spiky · ${num(gpu.in_use_memory_gb ?? gpu.allocated_memory_gb)} GB in use` },
    { label: "Power", key: "watts", value: pow.system_watts != null ? `${num(pow.system_watts)} W` : "–", unit: (v) => `${num(v)} W`, detail: `whole machine · ${battery}` },
    { label: "Disk free", key: "disk_free_gb", value: disk.free_gb != null ? `${num(disk.free_gb, 0)} GB` : "–", unit: (v) => `${num(v, 0)} GB`,
      trouble: disk.free_percent != null && disk.free_percent < 10,
      detail: `${num(disk.free_percent, 0)}% · database ${mb(disk.db_mb)} · audio ${mb(disk.audio_mb)}` },
    { label: "Network", key: "rx_kbps", value: net.rx_kbps == null ? "measuring…" : `${num(net.rx_kbps, 0)} kbit/s in`, unit: (v) => `${num(v, 0)} kbit/s`,
      detail: `${num(net.tx_kbps, 0)} kbit/s out: streams, outputs and the feed` },
  ];
  const hist: Any[] = s.history || [];
  return (
    <Section id="st-mac" title="This machine" description={`The last hour, sampled every 15 s. Up ${ago(s.uptime_s)}.`}>
      <ul className="meters">
        {meters.map((m) => (
          <li key={m.key} className={cx("meter", m.trouble && "is-trouble")}>
            <span className="meter-label">{m.label}</span>
            <span className="meter-value num">{m.value}</span>
            <Trace label={m.label} times={hist.map((x) => x.t)} values={hist.map((x) => x[m.key])} max={m.max} unit={m.unit} />
            <span className="meter-detail">{m.detail}</span>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** One measurement over the last hour: a thin line from zero, with a readout under the pointer. */
function Trace({ values, times, max, unit, label }: { values: (number | null)[]; times: number[]; max?: number; unit: (v: number) => string; label: string }) {
  const [at, setAt] = useState<number | null>(null);
  const box = useRef<SVGSVGElement>(null);
  const W = 240, H = 32;
  const pts = values.map((v, i) => [i, v] as const).filter((x): x is readonly [number, number] => x[1] != null);
  if (pts.length < 2) return <span className="trace trace-empty">collecting…</span>;
  const top = max ?? (Math.max(...pts.map(([, v]) => v)) * 1.15 || 1);
  const n = values.length - 1 || 1;
  const x = (i: number) => (i / n) * W;
  const y = (v: number) => H - 1 - Math.min(1, v / top) * (H - 3);
  const line = pts.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const hit = at != null ? pts.reduce((a, b) => (Math.abs(b[0] - at) < Math.abs(a[0] - at) ? b : a)) : null;
  const lo = Math.min(...pts.map(([, v]) => v)), hi = Math.max(...pts.map(([, v]) => v));
  return (
    <span className="trace">
      <svg ref={box} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
           aria-label={`${label}, last hour: ${unit(lo)} to ${unit(hi)}`}
           onPointerMove={(e) => {
             const r = box.current!.getBoundingClientRect();
             setAt(Math.round(((e.clientX - r.left) / r.width) * n));
           }}
           onPointerLeave={() => setAt(null)}>
        <line className="trace-base" x1="0" x2={W} y1={H - 0.5} y2={H - 0.5} />
        <polyline points={line} />
        {hit && <line className="trace-cursor" x1={x(hit[0])} x2={x(hit[0])} y1="0" y2={H} />}
      </svg>
      {hit && (
        <span className="trace-tip num" style={{ left: `${(hit[0] / n) * 100}%` }}>
          {hm(times[hit[0]])} · {unit(hit[1])}
        </span>
      )}
    </span>
  );
}

function Pipeline({ s }: { s: Any }) {
  const pl = s.pipeline || {};
  const models = pl.models || {};
  const errors = Object.entries(models.errors || {});
  const dropped = Object.entries(pl.dropped || {});
  const llm = s.llm_rules;
  return (
    <Section id="st-pipe" title="What's loaded"
             description={`Speech: ${(models.asr || []).join(", ") || "none loaded"} · translation: ${models.mt || "loads when first needed"}`}>
      {models.loading && <p className="m-line"><Tag tone="warn">loading {models.loading}</Tag></p>}
      {errors.length > 0 && <p className="m-trouble">{errors.map(([k, v]) => `${k}: ${v}`).join("; ")}</p>}
      <Table caption="Queues" cols={[{ label: "Queue" }, { label: "Waiting", num: true }, { label: "Busy, last minute", num: true }]}>
        <tr><td>Speech recognition</td><td className="num">{pl.asr_queue ?? "–"}</td><td className="num">{Math.round((pl.asr_busy || 0) * 100)}%</td></tr>
        <tr><td>Translation and described rules</td><td className="num">{pl.mt_queue ?? "–"}</td><td className="num">{Math.round((pl.mt_busy || 0) * 100)}%</td></tr>
      </Table>
      <p className="m-hint">
        {dropped.length ? `Dropped since start: ${dropped.map(([k, v]) => `${k} ${v}`).join(", ")}. ` : "Nothing dropped since start. "}
        {llm && `Described rules: ${llm.checks} checks, ${llm.matches} matches${llm.last_ms ? `, the last took ${(llm.last_ms / 1000).toFixed(1)} s` : ""}${llm.error ? `, error: ${llm.error}` : ""}. `}
        This node: {s.node?.name} ({s.node?.url}).
      </p>
    </Section>
  );
}

function OutputHealth({ outs }: { outs: Record<string, Any> }) {
  return (
    <Section id="st-outs" title="Connections">
      <Table caption="Connections" cols={[{ label: "Connection" }, { label: "Sent", num: true }, { label: "Failed", num: true }, { label: "Dropped", num: true }, { label: "Queued", num: true }, { label: "Last error" }]}>
        {Object.entries(outs).map(([id, o]) => (
          <tr key={id}>
            <td>{id}</td><td className="num">{o.sent}</td><td className="num">{o.failed}</td><td className="num">{o.dropped}</td><td className="num">{o.queued}</td>
            <td className={o.last_error ? "is-trouble" : "m-muted"}>{o.last_error || "–"}</td>
          </tr>
        ))}
      </Table>
    </Section>
  );
}

function Retention({ s }: { s: Any }) {
  const st = s.store;
  const r = st?.retention || {};
  const [audioDays, setAudioDays] = useState(String(r.audio_days ?? 0));
  const [audioGb, setAudioGb] = useState(String(r.audio_max_gb ?? 0));
  const [textDays, setTextDays] = useState(String(r.text_days ?? 0));
  if (!st) {
    return (
      <Section id="st-keep" title="Nothing is kept">
        <p className="m-line">This run keeps nothing (it was started with --no-store), so there's no history, search, replay or Ask.</p>
      </Section>
    );
  }
  const since = Math.round(Date.now() / 1000) - 86400;
  const recOff = r.audio_days == null;
  return (
    <Section id="st-keep" title="How long it's kept"
             description={`${st.utterances.toLocaleString()} utterances since ${day(st.oldest)} · database ${bytes(st.db_bytes)} · speech audio ${bytes(st.audio_bytes)} in ${st.audio_files.toLocaleString()} files`}
             actions={r.recording ? <Tag tone="good">recording speech</Tag> : <Tag tone="muted">not recording</Tag>}>
      <div className="m-form m-form-3">
        <Field id="ret-audio" label="Keep speech audio (days)" hint={recOff ? "Recording is off for this run (--audio-days 0)." : "0 stops recording. Each utterance's audio replays from its time in the transcript."}>
          <Input id="ret-audio" type="number" min={0} step={0.5} value={audioDays} disabled={recOff} onChange={(e) => setAudioDays(e.target.value)} />
        </Field>
        <Field id="ret-text" label="Keep text (days)" hint="0 keeps text with no age limit. Four talk stations add roughly 11–14 GB a year.">
          <Input id="ret-text" type="number" min={0} step={1} value={textDays} onChange={(e) => setTextDays(e.target.value)} />
        </Field>
      </div>
      <Advanced id="storage">
        <div className="m-form m-form-3">
          <Field id="ret-cap" label="Audio cap (GB)" hint="Above this, the oldest audio goes first.">
            <Input id="ret-cap" type="number" min={0} step={0.5} value={audioGb} disabled={r.audio_max_gb == null} onChange={(e) => setAudioGb(e.target.value)} />
          </Field>
        </div>
        <p className="m-line">
          <LinkButton href={`/feed/v1/utterances?format=ndjson&since=${since}&limit=10000`} variant="ghost" icon="download">Last 24 h as NDJSON</LinkButton>
          <span className="m-hint rd-inline">One JSON object per line, for scripts.</span>
        </p>
      </Advanced>
      <div className="m-actions">
        <ActButton variant="primary" done="Saved"
                   run={() => post("/retention", {
                     // an emptied field keeps its value: +"" would be 0, which stops recording
                     audio_days: recOff ? null : kept(audioDays, r.audio_days), audio_max_gb: r.audio_max_gb == null ? null : kept(audioGb, r.audio_max_gb), text_days: kept(textDays, r.text_days),
                   })}>
          Save
        </ActButton>
        <LinkButton href={`/feed/v1/utterances?format=csv&since=${since}&limit=10000`} icon="download">Last 24 h as CSV</LinkButton>
      </div>
      <p className="m-hint">Only speech is kept: the music gate drops songs before transcription. Recorded broadcasts are copyrighted, so keep them for your own use.</p>
    </Section>
  );
}
