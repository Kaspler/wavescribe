// Outputs: webhooks and pushes that send alerts, and optionally every utterance, to other programs.
import { useEffect, useRef, useState } from "react";
import { hms } from "../lib/time";
import {
  ActButton, Advanced, Button, Check, ChoiceChips, ConfirmButton, Field, Input, LangChips, Section, Segmented, Select, Table, Tag, act, toast,
} from "../components/ui";
import { type Any, SEVERITIES, SEV_WORD, del, get, knownLangs, post, useEvery, useLoad } from "./api";

const SECRET_HEADER = /auth|token|key|secret|cookie|password/i; // values shown as dots

export function Outputs() {
  const { data: v, error, reload } = useLoad(async () => {
    const [o, st, rules] = await Promise.all([get("/outputs"), get("/stations"), get("/rules").catch(() => ({ rules: [] }))]);
    return { ...o, stations: st.stations as Any[], rules: rules.rules as Any[] };
  });
  useEvery(5000, reload);
  const [editing, setEditing] = useState<Any | null>(null);
  const [log, setLog] = useState<"failed" | "all">("failed");
  if (!v) return <p className="m-loading">{error ? `Couldn't load the connections: ${error}` : "Loading…"}</p>;
  const rows: Any[] = log === "all" ? v.deliveries : v.deliveries.filter((d: Any) => !d.ok);
  return (
    <>
      <Section id="out-h" title="Webhooks and live pushes"
               description="Send alerts, and optionally every utterance, to other programs: Home Assistant, Node-RED, n8n, a chat bot, ntfy, or a central Wavescribe. Text and metadata only; audio stays here."
               actions={!editing && <>
                 <Button variant="primary" icon="plus" onClick={() => setEditing({})}>New connection</Button>
                 <Button icon="send" onClick={() => setEditing({ events: ["utterance", "alert"], batch_s: 5, name: "central", url: "http://central:8765/feed/v1/ingest", headers: { Authorization: "" } })}>
                   Push to a central Wavescribe
                 </Button>
               </>}>
        {editing && <OutputEditor key={editing.id || "new"} o={editing} v={v} onDone={async () => { setEditing(null); await reload(); }} onCancel={() => setEditing(null)} />}
        {v.outputs.length ? (
          <ul className="m-list">
            {v.outputs.map((o: Any) => <OutputRow key={o.id} o={o} onEdit={() => setEditing(o)} reload={reload} />)}
          </ul>
        ) : !editing && (
          <p className="m-empty">No connections yet. Alerts still show on the page and as notifications on this machine; a connection also sends them somewhere else.</p>
        )}
      </Section>
      <Section id="out-log" title="Delivery log"
               actions={<Segmented label="Which deliveries" value={log} onChange={setLog}
                                   options={[{ value: "failed", label: "Failures" }, { value: "all", label: "All" }]} />}>
        <Table caption="Deliveries" empty={log === "failed" ? "No failed deliveries." : "Nothing delivered yet."}
               cols={[{ label: "Time" }, { label: "Connection" }, { label: "Event" }, { label: "Result" }, { label: "Tries", num: true }, { label: "ms", num: true }, { label: "Error" }]}>
          {rows.map((d) => (
            <tr key={d.id}>
              <td className="num">{hms(d.t)}</td>
              <td>{d.output_id || d.output}</td>
              <td>{d.event}{d.ref ? ` ${d.ref}` : ""}</td>
              <td>{d.ok ? <Tag tone="good">{d.status ?? "ok"}</Tag> : <Tag tone="warn">{d.status ?? "failed"}</Tag>}</td>
              <td className="num">{d.attempts}</td>
              <td className="num">{d.ms ?? "–"}</td>
              <td className="m-muted">{d.error || ""}</td>
            </tr>
          ))}
        </Table>
      </Section>
    </>
  );
}

function OutputRow({ o, onEdit, reload }: { o: Any; onEdit: () => void; reload: () => Promise<void> }) {
  const st = o.status || {};
  return (
    <li className={`m-item${o.enabled ? "" : " is-off"}`}>
      <div className="m-item-head">
        <strong>{o.name || o.id}</strong>
        {o.enabled ? <Tag tone="good">on</Tag> : <Tag tone="muted">off</Tag>}
        <Tag>{o.events.join(" + ")}</Tag>
        <Tag>{o.format}</Tag>
        {o.secret && <Tag>signed</Tag>}
        <span className="m-spacer" />
        <ActButton size="sm" icon="send" run={async () => {
          const r = await post(`/outputs/${o.id}/test`);
          toast(r.ok ? `Test delivered (${r.status})` : `Test failed: ${r.error || r.status}`, !r.ok);
        }}>Test</ActButton>
        <Button size="sm" variant="ghost" icon="edit" onClick={onEdit}>Edit</Button>
        <ConfirmButton size="sm" icon="trash" confirm={`Delete ${o.name || o.id}`} onConfirm={() => act(async () => { await del(`/outputs/${o.id}`); await reload(); }, "Deleted")}>
          Delete
        </ConfirmButton>
      </div>
      <p className="m-item-sub">
        <span className="mono">{o.url}</span> · alerts {SEV_WORD[o.min_severity]?.toLowerCase()} and up{o.rules?.length ? ` (${o.rules.join(", ")})` : ""}
        {o.stations?.length ? ` · ${o.stations.join(", ")}` : ""}{o.batch_s ? ` · batches of ${o.batch_s} s` : ""}
      </p>
      <p className="m-item-sub num">
        {st.sent ?? 0} sent · {st.failed ?? 0} failed · {st.dropped ?? 0} dropped · {st.queued ?? 0} queued
        {st.last_error && <span className="is-trouble"> · last error: {st.last_error}</span>}
      </p>
    </li>
  );
}

function OutputEditor({ o, v, onDone, onCancel }: { o: Any; v: Any; onDone: () => Promise<void>; onCancel: () => void }) {
  const [name, setName] = useState(o.name || "");
  const [url, setUrl] = useState(o.url || "");
  const [events, setEvents] = useState<string[]>(o.events || ["alert"]);
  const [sev, setSev] = useState(o.min_severity || "medium");
  const [rules, setRules] = useState<string[]>(o.rules || []);
  const [stations, setStations] = useState<string[]>(o.stations || []);
  const [langs, setLangs] = useState<string[]>(o.languages || []);
  const [fmt, setFmt] = useState(o.format || "json");
  const [batch, setBatch] = useState(String(o.batch_s ?? 0));
  const [ctx, setCtx] = useState(String(o.context ?? 2));
  const [secret, setSecret] = useState("");
  const [dropSecret, setDropSecret] = useState(false);
  const [enabled, setEnabled] = useState(o.enabled ?? true);
  const [headers, setHeaders] = useState<[string, string][]>(Object.entries(o.headers || {}) as [string, string][]);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { box.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, []);
  const save = async () => {
    const raw: Any = {
      id: o.id, name: name.trim(), url: url.trim(), events, min_severity: sev, rules, stations, languages: langs, format: fmt,
      batch_s: +batch, context: +ctx, headers: Object.fromEntries(headers.filter(([k]) => k.trim()).map(([k, x]) => [k.trim(), x])), enabled,
      secret: dropSecret ? "" : secret || (o.secret ? true : ""),
    };
    if (!raw.id) delete raw.id;
    await post("/outputs", raw);
    await onDone();
  };
  return (
    <div ref={box} className="m-panel">
      <div className="m-panel-head">
        <h4>{o.id ? `Connection ${o.name || o.id}` : "New connection"}</h4>
        <ButtonClose onClick={onCancel} />
      </div>
      <div className="m-form">
        <Field id="o-name" label="Name" hint="Yours for it, shown in the delivery log."><Input id="o-name" value={name} placeholder="home-assistant" onChange={(e) => setName(e.target.value)} /></Field>
        <Field id="o-url" label="URL" hint="Where to send: the other program's webhook address."><Input id="o-url" type="url" value={url} placeholder="https://…" className="mono" onChange={(e) => setUrl(e.target.value)} /></Field>
        <Field label="Send" hint="Every utterance too, if the other program wants all the text."><ChoiceChips label="Events" options={[["alert", "Alerts"], ["utterance", "Every utterance"]]} value={events} onChange={setEvents} /></Field>
        <Field id="o-sev" label="Alerts from" hint="This severity and up.">
          <Select id="o-sev" value={sev} onChange={(e) => setSev(e.target.value)} options={[...SEVERITIES].reverse().map((s) => [s, SEV_WORD[s]] as [string, string])} />
        </Field>
      </div>
      <Advanced id="output-editor">
        <div className="m-form">
            {v.rules.length > 0 && (
              <Field label="Only these rules" hint="None selected: every rule." wide>
                <ChoiceChips label="Rules" options={v.rules.map((r: Any) => r.id)} value={rules} onChange={setRules} />
              </Field>
            )}
            <Field label="Only these stations" hint="None selected: every station." wide>
              <ChoiceChips label="Stations" options={v.stations.map((s: Any) => [s.id, s.name] as [string, string])} value={stations} onChange={setStations} />
            </Field>
            <Field label="Translations to include" hint="Empty: all of them.">
              <LangChips value={langs} onChange={setLangs} known={knownLangs(v.stations)} label="Add a language" />
            </Field>
            <Field id="o-fmt" label="Format" hint="Plain text suits chat bots and ntfy.">
              <Select id="o-fmt" value={fmt} onChange={(e) => setFmt(e.target.value)} options={[["json", "JSON events"], ["text", "Plain text"], ["cap", "CAP 1.2 XML (alerts)"]]} />
            </Field>
            <Field id="o-batch" label="Send utterances every (s)" hint="0 sends each one as it comes; alerts always go at once.">
              <Input id="o-batch" type="number" min={0} max={3600} value={batch} onChange={(e) => setBatch(e.target.value)} className="input-short" />
            </Field>
            <Field id="o-ctx" label="Context before an alert" hint="Utterances before the alert's, from the same station.">
              <Input id="o-ctx" type="number" min={0} max={10} value={ctx} onChange={(e) => setCtx(e.target.value)} className="input-short" />
            </Field>
            <Field id="o-secret" label="Signing secret" hint="Each request then carries X-Wavescribe-Signature: an HMAC-SHA256 of timestamp.body (docs/FEED.md).">
              <Input id="o-secret" type="password" value={secret} autoComplete="new-password" placeholder={o.secret ? "Set; leave empty to keep it" : "Optional"} onChange={(e) => setSecret(e.target.value)} />
              {o.secret && <Check checked={dropSecret} onChange={setDropSecret}>Remove the secret</Check>}
            </Field>
            <div className="field field-wide">
              <span className="field-label">Extra headers</span>
              {headers.map(([k, x], i) => (
                <div key={i} className="m-row">
                  <Input value={k} placeholder="Header" aria-label="Header name" size={18} onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [e.target.value, h[1]] : h)))} />
                  <Input value={x} type={SECRET_HEADER.test(k) ? "password" : "text"} autoComplete="new-password" spellCheck={false} placeholder={/^authorization$/i.test(k) ? "Bearer <push token>" : "Value"} aria-label="Header value" className="m-grow mono" onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [h[0], e.target.value] : h)))} />
                  <Button size="sm" variant="ghost" icon="close" aria-label="Remove this header" onClick={() => setHeaders(headers.filter((_, j) => j !== i))} />
                </div>
              ))}
              <div><Button size="sm" variant="ghost" icon="plus" onClick={() => setHeaders([...headers, ["", ""]])}>Header</Button></div>
              <p className="field-hint">Stored values show as •••.</p>
            </div>
      
        </div>
      </Advanced>
      <div className="m-actions">
        <ActButton variant="primary" disabled={!url.trim()} run={save} done="Connection saved">Save connection</ActButton>
        <Check checked={enabled} onChange={setEnabled}>On</Check>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

const ButtonClose = ({ onClick }: { onClick: () => void }) => <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onClick} />;
