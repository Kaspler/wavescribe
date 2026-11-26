// For other programs, under Connections: this node's name and address, its feed tokens, and how
// other programs read it.
import { useState } from "react";
import { day } from "../lib/time";
import { ActButton, Button, ChoiceChips, ConfirmButton, Field, Input, LinkButton, Section, Table, act, toast } from "../components/ui";
import { type Any, del, get, post, useLoad } from "./api";

export function FeedApi() {
  const { data: v, error, reload } = useLoad(async () => {
    const [node, outs] = await Promise.all([get("/node"), get("/outputs")]);
    return { node, tokens: outs.tokens as Any[] };
  });
  if (!v) return <p className="m-loading">{error ? `Couldn't load: ${error}` : "Loading…"}</p>;
  return (
    <>
      <Node node={v.node} />
      <Tokens tokens={v.tokens} reload={reload} />
      <Feed base={v.node.url} />
      <Central />
    </>
  );
}

function Node({ node }: { node: Any }) {
  const [name, setName] = useState(node.settings.name || "");
  const [pub, setPub] = useState(node.settings.public_url || "");
  return (
    <Section id="api-node" title="This node"
             description={node.lan
               ? `Listening on the network (--lan), port ${node.port}. ${node.tokens_required ? "Other machines need a feed token." : "No feed tokens yet, so anyone on the network can read the feed, as they can read the page."}`
               : `Only this machine can connect; start with --lan to let other machines read the feed. Port ${node.port}.`}>
      <div className="m-form">
        <Field id="n-name" label="Name" hint="Shown on a central server and in every event. “wavescribe” until you name it.">
          <Input id="n-name" value={name} placeholder={node.name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field id="n-url" label="Public URL" hint="How other machines reach this one; audio and CAP links in events point here. Under --lan it defaults to this machine's .local name, which then goes out in events too.">
          <Input id="n-url" type="url" value={pub} placeholder={node.url} className="mono" onChange={(e) => setPub(e.target.value)} />
        </Field>
      </div>
      <div className="m-actions">
        <ActButton variant="primary" done="Saved" run={() => post("/node", { name, public_url: pub })}>Save</ActButton>
      </div>
    </Section>
  );
}

function Tokens({ tokens, reload }: { tokens: Any[]; reload: () => Promise<void> }) {
  const [label, setLabel] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [made, setMade] = useState<string | null>(null);
  return (
    <Section id="api-tokens" title="Feed tokens"
             description="Once the first token exists, other machines need one: Authorization: Bearer <token>, or ?token= for EventSource. Only a hash is kept, so a token is shown once.">
      <Table caption="Feed tokens" empty="No tokens."
             cols={[{ label: "Label" }, { label: "Scopes" }, { label: "Made" }, { label: "", width: "1%" }]}>
        {tokens.map((t) => (
          <tr key={t.id}>
            <td>{t.label || "–"}</td><td>{t.scopes.join(", ")}</td><td>{day(t.created)}</td>
            <td>
              <ConfirmButton size="sm" confirm="Revoke it" onConfirm={() => act(async () => { await del(`/tokens/${t.id}`); await reload(); }, "Revoked")}>
                Revoke
              </ConfirmButton>
            </td>
          </tr>
        ))}
      </Table>
      <div className="m-row">
        <Input value={label} placeholder="Who uses it" aria-label="Token label" size={20} onChange={(e) => setLabel(e.target.value)} />
        <ChoiceChips label="Scopes" options={[["read", "Read the feed"], ["push", "Push to ingest"]]} value={scopes} onChange={setScopes} />
        <ActButton variant="primary" icon="plus" disabled={!scopes.length}
                   run={async () => { setMade((await post("/tokens", { label, scopes })).token); setLabel(""); await reload(); }}>
          Make a token
        </ActButton>
      </div>
      {made && (
        <div className="m-panel m-secret" role="status">
          <h4>Copy it now: it won't be shown again</h4>
          <pre className="m-code">{made}</pre>
          <div className="m-actions">
            <Button icon="copy" onClick={() => navigator.clipboard?.writeText(made).then(() => toast("Copied"), () => toast("Couldn't copy: select it and copy by hand"))}>Copy</Button>
            <Button variant="ghost" onClick={() => setMade(null)}>Done</Button>
          </div>
        </div>
      )}
    </Section>
  );
}

function Feed({ base }: { base: string }) {
  const ex = (t: string) => <pre className="m-code">{t}</pre>;
  return (
    <Section id="api-feed" title="Read the feed" description="Text and metadata only. docs/FEED.md describes every field; pushes to webhooks are set up above.">
      <p className="m-line">Live events, as Server-Sent Events; resume with Last-Event-ID:</p>
      {ex(`curl -N ${base}/feed/v1/events`)}
      <p className="m-line">What was said in the last hour, as CSV or NDJSON:</p>
      {ex(`curl "${base}/feed/v1/utterances?since=$(($(date +%s)-3600))&format=csv"`)}
      <p className="m-line">Stations, this node, health and Prometheus metrics:</p>
      {ex(`curl ${base}/feed/v1/stations\ncurl ${base}/feed/v1/node\ncurl ${base}/healthz\ncurl ${base}/metrics`)}
      <div className="m-actions">
        {["stations", "node", "alerts"].map((k) => (
          <LinkButton key={k} href={`/feed/v1/${k}`} target="_blank" size="sm" icon="external">/feed/v1/{k}</LinkButton>
        ))}
        <LinkButton href="/metrics" target="_blank" size="sm" icon="external">/metrics</LinkButton>
      </div>
    </Section>
  );
}

function Central() {
  return (
    <Section id="api-central" title="Many places, one central server"
             description="Either way the central server stores, searches and alerts on the text with its own rules and languages; no audio crosses.">
      <dl className="m-defs">
        <dt>Pull</dt>
        <dd>On the central Wavescribe, open Stations, Add a station, Another Wavescribe, and give this node's URL and a read token.</dd>
        <dt>Push, from behind NAT</dt>
        <dd>
          Make a push token on the central server. Then on each place add an output to <code>http://central:8765/feed/v1/ingest</code> with
          events Every utterance and Alerts, a 5 s batch and the header <code>Authorization: Bearer &lt;token&gt;</code>.
        </dd>
      </dl>
    </Section>
  );
}
