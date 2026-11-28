// Outputs: webhooks and pushes that send alerts, and optionally every utterance, to other programs.
import { useEffect, useRef, useState } from "react";
import { hm, hms } from "../lib/time";
import {
  ActButton, Advanced, Button, Check, ChoiceChips, ConfirmButton, Field, Input, LangChips, Section, Segmented, Select, Table, Tag, act, toast,
} from "../components/ui";
import { type Any, ApiError, SEVERITIES, SEV_WORD, del, get, knownLangs, post, useEvery, useLoad } from "./api";
import { type Kind, bearer, goodTopic, newTopic, nodeId, ntfyParts, ntfyUrl, resultWord } from "./connections";
import { alertsLine, deliveryKey, maxAge, meshLine } from "./deliveries";

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
               description="Send alerts, and optionally every utterance, to other programs: Home Assistant, Node-RED, n8n, a chat bot, ntfy, a mesh node, or a central Wavescribe. Text and metadata only; audio stays here."
               actions={!editing && <>
                 <Button variant="primary" icon="plus" onClick={() => setEditing({})}>New connection</Button>
                 <Button icon="bell" onClick={() => setEditing({ kind: "ntfy", name: "phone", format: "line" })}>Notify a phone (ntfy)</Button>
                 <Button icon="send" onClick={() => setEditing({ events: ["utterance"], batch_s: 5, name: "central", url: "http://central:8765/feed/v1/ingest", headers: { Authorization: "" } })}>
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
          {rows.map((d) => {
            const mesh = v.outputs.find((o: Any) => o.id === (d.output_id ?? d.output))?.kind === "mesh";
            return (
            <tr key={deliveryKey(d)}>
              <td className="num">{hms(d.t)}</td>
              <td>{v.outputs.find((o: Any) => o.id === (d.output_id ?? d.output))?.name || d.output_id || d.output}</td>
              <td title={d.delivery ? `Delivery ${d.delivery} (X-Wavescribe-Delivery)` : undefined}>
                {d.event}{d.ref ? ` ${d.ref}` : ""}
                {d.after_restart && <span className="m-cell-note is-one">after a restart</span>}
              </td>
              <td><Tag tone={d.ok ? "good" : "warn"}>{resultWord(d, mesh)}</Tag></td>
              <td className="num">{d.attempts}</td>
              <td className="num">{d.ms ?? "–"}</td>
              <td className="m-muted">{d.error || ""}</td>
            </tr>
            );
          })}
        </Table>
      </Section>
    </>
  );
}

function OutputRow({ o, onEdit, reload }: { o: Any; onEdit: () => void; reload: () => Promise<void> }) {
  const st = o.status || {};
  const waiting = alertsLine(st);
  const mesh = o.kind === "mesh";
  const share = meshLine(st.mesh, hm);
  return (
    <li className={`m-item${o.enabled ? "" : " is-off"}`}>
      <div className="m-item-head">
        <strong>{o.name || o.id}</strong>
        {o.enabled ? <Tag tone="good">on</Tag> : <Tag tone="muted">off</Tag>}
        {o.enabled && st.waiting && <Tag tone="warn">waiting</Tag>}
        {o.enabled && st.mesh?.cap_reached && <Tag tone="warn">daily cap reached</Tag>}
        {o.kind === "ntfy" || mesh ? <Tag>{mesh ? "mesh, experimental" : "ntfy"}</Tag> : <Tag>{o.events.join(" + ")}</Tag>}
        <Tag>{o.format === "line" ? `one line, ${o.max_bytes} bytes` : o.format}</Tag>
        {o.secret && <Tag>signed</Tag>}
        <span className="m-spacer" />
        <ActButton size="sm" icon="send" run={async () => {
          const r = await post(`/outputs/${o.id}/test`);
          // A mesh node answers with an acknowledgement or not, never an HTTP status.
          if (mesh) toast(r.ok ? `Test acknowledged by ${o.node}` : `Test ${r.error || "not acknowledged"}`, !r.ok);
          else toast(r.ok ? `Test delivered (${r.status})` : `Test failed: ${r.error || r.status}`, !r.ok);
        }}>Test</ActButton>
        <Button size="sm" variant="ghost" icon="edit" onClick={onEdit}>Edit</Button>
        <ConfirmButton size="sm" icon="trash" confirm={`Delete ${o.name || o.id}`} onConfirm={() => act(async () => { await del(`/outputs/${o.id}`); await reload(); }, "Deleted")}>
          Delete
        </ConfirmButton>
      </div>
      <p className="m-item-sub">
        {mesh ? <><span className="mono">{o.node}</span> through {o.device ? <span className="mono">{o.device}</span> : "the node on USB"}</> : <span className="mono">{o.url}</span>} · alerts {SEV_WORD[o.min_severity]?.toLowerCase()} and up{o.rules?.length ? ` (${o.rules.join(", ")})` : ""}
        {o.stations?.length ? ` · ${o.stations.join(", ")}` : ""}{o.batch_s ? ` · batches of ${o.batch_s} s` : ""}
      </p>
      <p className="m-item-sub num">
        {st.sent ?? 0} sent · {st.failed ?? 0} failed · {st.dropped ?? 0} dropped · {st.queued ?? 0} queued
        {waiting && ` · ${waiting}`}
        {share && !st.mesh?.cap_reached && ` · ${share}`}
        {st.last_error && <span className="is-trouble"> · last error: {st.last_error}</span>}
      </p>
      {o.enabled && st.mesh?.cap_reached && <p className="m-item-sub is-trouble">{share}</p>}
      {o.enabled && st.waiting && (
        <p className="m-item-sub">
          {mesh
            ? "Not acknowledged yet: the alert waits and goes again, never sooner than the floor allows, until the node acknowledges it or it is too old."
            : "The other end doesn't answer: events wait for it and go once it is back, the oldest lines dropped first if too many pile up."}
        </p>
      )}
    </li>
  );
}

const KIND_HINT: Record<Kind, string> = {
  webhook: "Any program that takes a POST: home automation, a chat bot, your own script, a central Wavescribe.",
  ntfy: "Alerts as notifications on a phone, through an ntfy server: one you run, or a public one. The ntfy app on the phone subscribes to the topic.",
  mesh: "Experimental, not yet tried on real nodes. Alerts as direct messages to one Meshtastic node, through a node attached to this Mac and the meshtastic program: no internet or mobile network on the way. The node acknowledges what it gets; an alert can still fail to arrive.",
};

function OutputEditor({ o, v, onDone, onCancel }: { o: Any; v: Any; onDone: () => Promise<void>; onCancel: () => void }) {
  const [kind, setKind] = useState<Kind>(o.kind === "ntfy" || o.kind === "mesh" ? o.kind : "webhook");
  const [name, setName] = useState(o.name || "");
  const [url, setUrl] = useState(o.kind === "ntfy" || o.kind === "mesh" ? "" : o.url || "");
  const [node, setNode] = useState(o.node || "");
  const [device, setDevice] = useState(o.device || "");
  const [gap, setGap] = useState(String(o.min_gap_s ?? 120));
  const [cap, setCap] = useState(String(o.daily_cap ?? 20));
  const parts = o.kind === "ntfy" ? ntfyParts(o.url) : null;
  const [server, setServer] = useState(parts?.server || "");
  const [topic, setTopic] = useState(() => parts?.topic || newTopic());
  // The token is the Authorization header, which the page only ever sees as •••.
  const hadToken = o.kind === "ntfy" && Object.keys(o.headers || {}).some((k) => /^authorization$/i.test(k));
  const [token, setToken] = useState("");
  const [dropToken, setDropToken] = useState(false);
  const [events, setEvents] = useState<string[]>(o.events || ["alert"]);
  const [sev, setSev] = useState(o.min_severity || "medium");
  const [rules, setRules] = useState<string[]>(o.rules || []);
  const [stations, setStations] = useState<string[]>(o.stations || []);
  const [langs, setLangs] = useState<string[]>(o.languages || []);
  const [fmt, setFmt] = useState(o.format || "json");
  const [batch, setBatch] = useState(String(o.batch_s ?? 0));
  const [maxBytes, setMaxBytes] = useState(String(o.max_bytes ?? 200));
  const [ctx, setCtx] = useState(String(o.context ?? 2));
  const [secret, setSecret] = useState("");
  const [dropSecret, setDropSecret] = useState(false);
  const [enabled, setEnabled] = useState(o.enabled ?? true);
  const [headers, setHeaders] = useState<[string, string][]>(
    (Object.entries(o.headers || {}) as [string, string][]).filter(([k]) => !(o.kind === "ntfy" && /^authorization$/i.test(k))),
  );
  const [why, setWhy] = useState<string | null>(null);
  const ntfy = kind === "ntfy";
  const mesh = kind === "mesh";
  const hook = kind === "webhook";
  // A central Wavescribe's ingest takes utterances only: it runs its own rules (docs/FEED.md).
  const central = hook && /\/feed\/v1\/ingest\/?$/.test(url.trim());
  const alertsOnly = !hook || fmt === "line" || fmt === "cap";
  const line = mesh || fmt === "line";
  const maxAgeText = maxAge(o.alert_max_age_hours);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { box.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, []);
  const choose = (k: Kind) => {
    setKind(k);
    if ((k === "ntfy" && fmt !== "text") || k === "mesh") setFmt("line");
    if (k === "mesh" && +maxBytes > 200) setMaxBytes("200");
    // A mesh is shared and slow: a new one starts with the serious alerts only.
    if (k === "mesh" && !o.id && sev === "medium") setSev("high");
  };
  const ready = ntfy ? server.trim() !== "" && goodTopic(topic) : mesh ? nodeId(node) !== null : url.trim() !== "";
  const save = async () => {
    const extra: Record<string, string> = Object.fromEntries(headers.filter(([k]) => k.trim()).map(([k, x]) => [k.trim(), x]));
    if (ntfy && token.trim()) extra.Authorization = bearer(token);
    else if (ntfy && hadToken && !dropToken) extra.Authorization = "•••"; // the server keeps the one it has
    const raw: Any = {
      id: o.id, name: name.trim(), kind, url: ntfy ? ntfyUrl(server, topic) : mesh ? "" : url.trim(), events: hook ? events : ["alert"],
      min_severity: sev, rules, stations, languages: langs, format: mesh ? "line" : fmt,
      batch_s: +batch, context: +ctx, ...(line ? { max_bytes: +maxBytes } : {}), headers: extra, enabled,
      secret: dropSecret ? "" : secret || (o.secret ? true : ""),
      ...(mesh ? { node: nodeId(node), device: device.trim(), min_gap_s: +gap, daily_cap: +cap } : {}),
    };
    if (!raw.id) delete raw.id;
    try {
      await post("/outputs", raw);
    } catch (e) {
      // The server's reason (a name already in use, a URL it can't send to) stays beside the form.
      if (!(e instanceof ApiError)) throw e;
      setWhy(e.message.replace(/^./, (c) => c.toUpperCase()));
      return;
    }
    setWhy(null);
    toast("Connection saved");
    await onDone();
  };
  return (
    <div ref={box} className="m-panel">
      <div className="m-panel-head">
        <h4>{o.id ? `Connection ${o.name || o.id}` : "New connection"}</h4>
        <ButtonClose onClick={onCancel} />
      </div>
      <div className="m-mode">
        <Segmented label="Kind of connection" value={kind} onChange={choose} options={[
          { value: "webhook", label: "Webhook" },
          { value: "ntfy", label: "ntfy" },
          { value: "mesh", label: "Mesh node" },
        ]} />
        <p className="m-hint">{KIND_HINT[kind]}</p>
      </div>
      <div className="m-form">
        <Field id="o-name" label="Name" hint="Yours for it, shown in the delivery log."><Input id="o-name" value={name} placeholder={ntfy ? "phone" : mesh ? "pocket-node" : "home-assistant"} onChange={(e) => setName(e.target.value)} /></Field>
        {mesh ? (
          <>
            <Field id="o-node" label="Node" hint={node.trim() && !nodeId(node) ? "A node's id is ! and 8 hexadecimal digits; !ffffffff is every node, never sent to." : "The node that gets the alerts, by its id (the Meshtastic app shows it). Each alert is a direct message to that node alone, never to a channel."}>
              <Input id="o-node" value={node} placeholder="!1a2b3c4d" spellCheck={false} className="mono" onChange={(e) => setNode(e.target.value)} />
            </Field>
            <Field id="o-device" label="Sent through" hint="The node attached to this Mac: empty finds it on USB; or its serial port (/dev/cu.…), or its address on your network. The meshtastic program must be installed (docs/FEED.md).">
              <Input id="o-device" value={device} placeholder="The node on USB" spellCheck={false} className="mono" onChange={(e) => setDevice(e.target.value)} />
            </Field>
          </>
        ) : ntfy ? (
          <>
            <Field id="o-server" label="Server" hint="Alerts go to the server you name. A public server is run by a third party, who sees them; one on your own network keeps them there.">
              <Input id="o-server" type="url" value={server} placeholder="https://ntfy.example.org" className="mono" onChange={(e) => setServer(e.target.value)} />
            </Field>
            <Field id="o-topic" label="Topic" hint={goodTopic(topic) ? "Subscribe to it in the ntfy app. Where anyone may read a topic they know the name of, a long random one keeps it to you." : "Letters, digits, - and _ only, at most 64."}>
              <Input id="o-topic" value={topic} spellCheck={false} className="mono" onChange={(e) => setTopic(e.target.value)} />
            </Field>
            <Field id="o-token" label="Access token" hint="Only if the server asks for one: it goes as Authorization: Bearer ….">
              <Input id="o-token" type="password" value={token} autoComplete="new-password" spellCheck={false} placeholder={hadToken ? "Set; leave empty to keep it" : "Optional"} onChange={(e) => setToken(e.target.value)} />
              {hadToken && <Check checked={dropToken} onChange={setDropToken}>Remove the token</Check>}
            </Field>
          </>
        ) : (
          <>
            <Field id="o-url" label="URL" hint="Where to send: the other program's webhook address."><Input id="o-url" type="url" value={url} placeholder="https://…" className="mono" onChange={(e) => setUrl(e.target.value)} /></Field>
            <Field label="Send" hint={central ? "Every utterance: a central Wavescribe raises its own alerts from the text, with its own rules, and ignores alerts pushed to it." : "Every utterance too, if the other program wants all the text."}><ChoiceChips label="Events" options={[["alert", "Alerts"], ["utterance", "Every utterance"]]} value={events} onChange={setEvents} /></Field>
          </>
        )}
        <Field id="o-sev" label="Alerts from" hint={ntfy ? "This severity and up. ntfy's priority follows it: low is quiet, critical is the most insistent." : "This severity and up."}>
          <Select id="o-sev" value={sev} onChange={(e) => setSev(e.target.value)} options={[...SEVERITIES].reverse().map((s) => [s, SEV_WORD[s]] as [string, string])} />
        </Field>
      </div>
      <Advanced id="output-editor">
        <div className="m-form">
            {v.rules.length > 0 && (
              <Field label="Only these rules" hint="None selected: every rule." wide>
                <ChoiceChips label="Rules" options={v.rules.map((r: Any) => [r.id, r.id.replace(/[-_]+/g, " ").replace(/^./, (c: string) => c.toUpperCase())] as [string, string])} value={rules} onChange={setRules} />
              </Field>
            )}
            <Field label="Only these stations" hint="None selected: every station." wide>
              <ChoiceChips label="Stations" options={v.stations.map((s: Any) => [s.id, s.name] as [string, string])} value={stations} onChange={setStations} />
            </Field>
            {mesh ? (
              <>
                <Field id="o-gap" label="At most one message every (s)" hint="The band is shared: in Europe's 868 MHz band a node may send 10% of any hour. 30 s at the least.">
                  <Input id="o-gap" type="number" min={30} max={3600} value={gap} onChange={(e) => setGap(e.target.value)} className="input-short" />
                </Field>
                <Field id="o-cap" label="At most per 24 hours" hint="Past it, alerts wait for the oldest message to be a day old; a retry counts too.">
                  <Input id="o-cap" type="number" min={1} max={200} value={cap} onChange={(e) => setCap(e.target.value)} className="input-short" />
                </Field>
              </>
            ) : ntfy ? (
              <Field id="o-fmt" label="Message" hint={fmt === "line" ? "The alert in one line: severity, rule, station, then the words as heard." : "The translation, then the words as heard."}>
                <Select id="o-fmt" value={fmt} onChange={(e) => setFmt(e.target.value)} options={[["line", "One line"], ["text", "Short text"]]} />
              </Field>
            ) : (
              <Field id="o-fmt" label="Format" hint="Plain text suits chat bots. One line suits channels that take small messages: an SMS gateway, a pager.">
                <Select id="o-fmt" value={fmt} onChange={(e) => setFmt(e.target.value)} options={[["json", "JSON events"], ["text", "Plain text"], ["line", "One line (alerts)"], ["cap", "CAP 1.2 XML (alerts)"]]} />
              </Field>
            )}
            {line && (
              <Field id="o-bytes" label={mesh ? "Longest message (bytes)" : "Longest line (bytes)"} hint={mesh ? "200 at most: the time and station first, then severity, rule and the words as heard, cut on a word." : "In UTF-8: 200 bytes hold about 200 Latin letters, 100 Cyrillic, 66 Chinese. A longer alert is cut between characters, with … at the end."}>
                <Input id="o-bytes" type="number" min={40} max={mesh ? 200 : 4096} value={maxBytes} onChange={(e) => setMaxBytes(e.target.value)} className="input-short" />
              </Field>
            )}
            {!line && (
              <Field label="Translations to include" hint="Empty: all of them.">
                <LangChips value={langs} onChange={setLangs} known={knownLangs(v.stations)} label="Add a language" />
              </Field>
            )}
            {!alertsOnly && (
              <Field id="o-batch" label="Send utterances every (s)" hint="0 sends each one as it comes. Alerts don't wait for this: one goes once its translations are ready, about 12 s, and later mentions of the same incident fold into it.">
                <Input id="o-batch" type="number" min={0} max={3600} value={batch} onChange={(e) => setBatch(e.target.value)} className="input-short" />
              </Field>
            )}
            {hook && fmt !== "line" && (
              <Field id="o-ctx" label="Context before an alert" hint="Utterances before the alert's, from the same station.">
                <Input id="o-ctx" type="number" min={0} max={10} value={ctx} onChange={(e) => setCtx(e.target.value)} className="input-short" />
              </Field>
            )}
            {maxAgeText && (!hook || events.includes("alert")) && (
              <p className="m-hint field-wide">
                Alerts older than {maxAgeText} aren't sent: one that hasn't got there by then is given up, not sent late.
                It is <code>alert_max_age_hours</code> in this output's <code>[[output]]</code> in config/ui.toml.
              </p>
            )}
            {hook && (
              <Field id="o-secret" label="Signing secret" hint="Each request then carries X-Wavescribe-Signature: an HMAC-SHA256 of timestamp.body (docs/FEED.md).">
                <Input id="o-secret" type="password" value={secret} autoComplete="new-password" placeholder={o.secret ? "Set; leave empty to keep it" : "Optional"} onChange={(e) => setSecret(e.target.value)} />
                {o.secret && <Check checked={dropSecret} onChange={setDropSecret}>Remove the secret</Check>}
              </Field>
            )}
            {!mesh && <div className="field field-wide">
              <span className="field-label">Extra headers</span>
              {headers.map(([k, x], i) => (
                <div key={i} className="m-row">
                  <Input value={k} placeholder="Header" aria-label="Header name" size={18} onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [e.target.value, h[1]] : h)))} />
                  <Input value={x} type={SECRET_HEADER.test(k) ? "password" : "text"} autoComplete="new-password" spellCheck={false} placeholder={/^authorization$/i.test(k) ? "Bearer <push token>" : "Value"} aria-label="Header value" className="m-grow mono" onChange={(e) => setHeaders(headers.map((h, j) => (j === i ? [h[0], e.target.value] : h)))} />
                  <Button size="sm" variant="ghost" icon="close" aria-label="Remove this header" onClick={() => setHeaders(headers.filter((_, j) => j !== i))} />
                </div>
              ))}
              <div><Button size="sm" variant="ghost" icon="plus" onClick={() => setHeaders([...headers, ["", ""]])}>Header</Button></div>
              <p className="field-hint">{ntfy ? "Any other header ntfy reads. " : ""}Stored values show as •••.</p>
            </div>}
        </div>
      </Advanced>
      <div className="m-actions">
        <ActButton variant="primary" disabled={!ready} run={save}>Save connection</ActButton>
        <Check checked={enabled} onChange={setEnabled}>On</Check>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
      {why && <p className="m-trouble" role="alert">{why}</p>}
    </div>
  );
}

const ButtonClose = ({ onClick }: { onClick: () => void }) => <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onClick} />;
