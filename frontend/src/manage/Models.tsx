// Models: what this machine can run, in which order, and where more come from.
import { useEffect, useRef, useState } from "react";
import { langNameEn } from "../lib/lang";
import { NEED_LABEL, downloadState, neededModels, needsLicence, totalGb } from "../lib/models";
import { Icon } from "../components/Icon";
import { ActButton, Advanced, Button, Check, Input, Section, Select, Table, Tag, type Tone, act, cx, toast } from "../components/ui";
import { type Any, bytes, gbSize, get, post, refreshPage, useEvery, useLoad } from "./api";

const CAPS: [string, string, string][] = [
  ["asr", "Speech recognition", "Each station uses the first installed model in this order that speaks its language and fits. A station can also name its own under Stations."],
  ["translate", "Translation", "The first installed model in this order is used."],
  ["vad", "Speech detection (voice activity)", "Finds where someone speaks, so only speech is transcribed. The first installed model in this order is used."],
  ["gate", "Music filter (music gate)", "Drops songs before they are transcribed. The first installed model in this order is used."],
  ["langid", "Language identification", "The first installed model in this order is used."],
];
const TIER: Record<string, [string, Tone, string]> = {
  default: ["permissive", "good", "a permissive licence (conditions such as attribution may apply)"],
  "opt-in": ["extra terms", "warn", "extra terms: read and accept them before downloading"],
  restricted: ["non-commercial", "warn", "non-commercial only: accept with a non-commercial acknowledgement"],
  byo: ["your own", "muted", "your own model: you're responsible for its licence"],
};
const FIT: Record<string, [string, Tone]> = { good: ["fits", "good"], tight: ["tight", "warn"], too_big: ["won't fit", "warn"], unknown: ["size unknown", "muted"] };

/** `after` goes under the first section (what this machine can run, and what to download). */
export function Models({ after }: { after?: React.ReactNode }) {
  const { data: m, error, reload } = useLoad(() => get("/models"));
  const downloading = !!m && Object.values(m.downloads || {}).some((d: Any) => d.state === "running" || d.state === "stopping");
  useEvery(2500, reload, downloading);
  // When a download finishes, the reading view loads the stations again: they have text now.
  const was = useRef<Record<string, string>>({});
  useEffect(() => {
    const now: Record<string, string> = Object.fromEntries(Object.values(m?.downloads || {}).map((d: Any) => [d.model, d.state]));
    if (Object.entries(now).some(([id, st]) => st === "done" && was.current[id] === "running")) refreshPage();
    was.current = now;
  }, [m]);
  if (!m) return <p className="m-loading">{error ? `Couldn't load the models: ${error}` : "Loading…"}</p>;
  const mc = m.machine;
  return (
    <>
      <Section id="mod-mac" title="This machine"
               description={`Models can use about ${mc.budget_gb} GB: memory minus 6 GB for the system and other apps, within the GPU's limit of ${mc.gpu_limit_gb} GB. “Tight” means a model fits but leaves little room for the others.`}>
        <p className="m-spec num">
          {mc.chip} · {mc.memory_gb} GB unified memory · {mc.gpu_cores ?? "?"}-core GPU · {mc.performance_cores ?? "?"}+{mc.efficiency_cores ?? "?"} CPU cores
          {mc.bandwidth_gbs ? ` · ${mc.bandwidth_gbs} GB/s` : ""}
        </p>
        <Recommended m={m} reload={reload} />
      </Section>
      {after}
      <Downloads d={m.downloads || {}} reload={reload} />
      {CAPS.filter(([c]) => m.models.some((e: Any) => e.capability === c)).map(([cap, label, desc]) => (
        <ModelSection key={cap} cap={cap} label={label} desc={desc} m={m} reload={reload} />
      ))}
      <Advanced id="models-more" label="Find more models">
        <Catalogue reload={reload} />
      </Advanced>
    </>
  );
}

/** What the stations need, one row per job, each with its models in order and whether one of
 * them is installed; the missing first choices download with one click. */
function Recommended({ m, reload }: { m: Any; reload: () => Promise<void> }) {
  const byId = new Map<string, Any>(m.models.map((e: Any) => [e.id, e]));
  const rows: [string, string, string[]][] = []; // key, label, model ids in order
  if (m.chains.vad?.length) rows.push(["vad", NEED_LABEL.vad, m.chains.vad]);
  if (m.chains.gate?.length) rows.push(["gate", NEED_LABEL.gate, m.chains.gate]);
  const byChain = new Map<string, string[]>();
  const none: string[] = [];
  for (const [lang, list] of Object.entries(m.recommended) as [string, Any[]][]) {
    if (lang === "translation") continue;
    if (!list.length) { none.push(lang); continue; }
    const key = list.map((r) => r.id).join(" ");
    byChain.set(key, [...(byChain.get(key) || []), lang]);
  }
  for (const [ids, langs] of byChain) rows.push([ids, `Speech in ${[...new Set(langs.map(langNameEn))].join(", ")}`, ids.split(" ")]);
  const tr: Any[] = m.recommended.translation || [];
  rows.push(["translation", NEED_LABEL.translation, tr.map((r) => r.id)]);
  const job = (id: string) => (m.downloads || {})[id];
  const needs = neededModels(m).filter((n) => job(n.entry.id)?.state !== "running");
  const ready = needs.filter((n) => !needsLicence(n.entry));
  const gb = totalGb(ready);
  const chain = (ids: string[]) => ids.map((id, i) => <span key={id}>{i > 0 && <span className="m-then">then</span>}<code>{id}</code></span>);
  const state = (ids: string[]) => {
    const have = ids.find((id) => byId.get(id)?.installed);
    if (have) return <Tag tone="good">{have === ids[0] ? "installed" : `${have} installed`}</Tag>;
    const running = ids.map(job).find((j) => j?.state === "running");
    if (running) return <span className="num-cell">downloading{running.percent != null ? ` ${Math.round(running.percent)}%` : "…"}</span>;
    const first = byId.get(ids[0]);
    if (!first) return <span className="is-trouble">nothing fits this machine</span>;
    if (needsLicence(first)) return <span className="m-muted">accept its licence below first</span>;
    return <span className="m-muted">not downloaded{first.size_gb ? ` · ${gbSize(first.size_gb)}` : ""}</span>;
  };
  return (
    <div className="m-rec" id="mod-rec">
      <h4>Recommended for your stations</h4>
      <Table caption="Recommended models" cols={[{ label: "For" }, { label: "Models, in order" }, { label: "State" }]}>
        {rows.map(([key, label, ids]) => (
          <tr key={key}>
            <td>{label}</td>
            <td className="m-chain">{ids.length ? chain(ids) : <span className="m-muted">–</span>}</td>
            <td>{state(ids)}</td>
          </tr>
        ))}
      </Table>
      {none.length > 0 && <p className="m-trouble">Nothing installed speaks {none.map(langNameEn).join(", ")}: search the catalogue below.</p>}
      {ready.length > 0 && (
        <div className="m-actions">
          <ActButton variant="primary" icon="download" done="Downloads started"
                     run={async () => { for (const n of ready) await post(`/models/${n.entry.id}/fetch`); await reload(); }}>
            Download what's missing{gb >= 0.1 ? ` · ${gbSize(gb)}` : ""}
          </ActButton>
        </div>
      )}
      <p className="m-hint">Each job uses the first model in its order that is installed and fits this machine. Speech detection is needed for any text at all; the music filter keeps songs out of the transcripts.</p>
    </div>
  );
}

function Downloads({ d, reload }: { d: Record<string, Any>; reload: () => Promise<void> }) {
  const rows = Object.values(d);
  if (!rows.length) return null;
  return (
    <Section id="mod-dl" title="Downloads">
      <Table caption="Downloads" cols={[{ label: "Model" }, { label: "State" }, { label: "Progress" }, { label: "", width: "1%" }]}>
        {rows.map((j) => {
          const [word, detail] = downloadState(j);
          const busy = j.state === "running" || j.state === "stopping";
          return (
            <tr key={j.model}>
              <td className="mono">{j.model}</td>
              <td className={cx(j.state === "failed" && "is-trouble")}>
                {word}
                {detail && <div className="m-hint">{detail}</div>}
              </td>
              <td className="num-cell">
                {busy && <progress max={100} value={j.percent ?? 0} aria-label={`${j.model} downloaded`} />}
                {j.bytes != null && ` ${bytes(j.bytes)}`}
              </td>
              <td>
                {j.state === "running" && (
                  <ActButton size="sm" icon="stop" run={async () => { await post(`/models/${j.model}/stop`); await reload(); }}>Stop</ActButton>
                )}
                {(j.state === "stopped" || j.state === "failed") && (
                  <ActButton size="sm" icon="download" done="Download started"
                             run={async () => { await post(`/models/${j.model}/fetch`); await reload(); }}>Start again</ActButton>
                )}
              </td>
            </tr>
          );
        })}
      </Table>
    </Section>
  );
}

function ModelSection({ cap, label, desc, m, reload }: { cap: string; label: string; desc: string; m: Any; reload: () => Promise<void> }) {
  const chain: string[] = m.chains[cap] || [];
  const entries: Any[] = m.models.filter((e: Any) => e.capability === cap).sort((a: Any, b: Any) => (a.chain_position ?? 999) - (b.chain_position ?? 999));
  const setChain = async (ids: string[], done: string) => { await act(() => post("/models/chain", { capability: cap, ids }), done); await reload(); };
  const move = (id: string, delta: number) => {
    const ids = [...chain];
    const i = ids.indexOf(id), j = i + delta;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    return setChain(ids, "Order saved; stations switch model at their next utterance");
  };
  return (
    <Section id={`mod-${cap}`} title={label} description={desc}>
      <ol className="m-list">
        {entries.map((e) => <ModelRow key={e.id} e={e} chain={chain} move={move} setChain={setChain} reload={reload} />)}
      </ol>
    </Section>
  );
}

function ModelRow({ e, chain, move, setChain, reload }: {
  e: Any; chain: string[]; move: (id: string, d: number) => Promise<void> | undefined;
  setChain: (ids: string[], done: string) => Promise<void>; reload: () => Promise<void>;
}) {
  const [accepting, setAccepting] = useState(false);
  const [nc, setNc] = useState(false);
  const pos = chain.indexOf(e.id);
  const [tierWord, tierTone, tierText] = TIER[e.tier] || [e.tier, "neutral", e.tier];
  const [fitWord, fitTone] = FIT[e.fit.level] || FIT.unknown;
  const langs = e.languages.includes("*") ? "any language" : e.languages.length > 8 ? `${e.languages.length} languages` : e.languages.map(langNameEn).join(", ");
  const needsAccept = (e.tier === "opt-in" || e.tier === "restricted") && !e.accepted;
  return (
    <li className={cx("m-item", pos < 0 && "is-off")}>
      <div className="m-item-head">
        <span className={cx("m-pos", pos < 0 && "is-out")} aria-label={pos >= 0 ? `Number ${pos + 1} in the order` : "Not in the order"}>{pos >= 0 ? pos + 1 : "–"}</span>
        <strong className="mono">{e.id}</strong>
        {e.loaded ? <Tag tone="good">loaded</Tag> : e.in_use.length ? <Tag tone="accent">planned: {e.in_use.join(", ")}</Tag> : e.installed ? <Tag tone="muted">installed</Tag> : <Tag tone="muted">not downloaded</Tag>}
        <Tag tone={fitTone}>{fitWord}</Tag>
        <Tag tone={tierTone} title={tierText}>{tierWord}</Tag>
        {e.tested ? <Tag tone="good" title={e.tested}>tested</Tag> : <Tag tone="muted">untested</Tag>}
        {e.origin === "catalog" && <Tag>from the catalogue</Tag>}
        <span className="m-spacer" />
        {!e.installed && e.origin !== "local" && (needsAccept ? (
          <Button size="sm" aria-expanded={accepting} onClick={() => setAccepting(!accepting)}>Accept the licence…</Button>
        ) : (
          <ActButton size="sm" variant="primary" icon="download" done="Download started" run={async () => { await post(`/models/${e.id}/fetch`); await reload(); }}>
            Download{e.size_gb ? ` · ${gbSize(e.size_gb)}` : ""}
          </ActButton>
        ))}
        {pos >= 0 ? (
          <span className="m-order">
            <Button size="sm" variant="ghost" icon="up" aria-label={`Move ${e.id} earlier`} title="Earlier in the order" disabled={pos === 0} onClick={() => move(e.id, -1)} />
            <Button size="sm" variant="ghost" icon="down" aria-label={`Move ${e.id} later`} title="Later in the order" disabled={pos === chain.length - 1} onClick={() => move(e.id, 1)} />
            {chain.length > 1 && (
              <Button size="sm" variant="ghost" icon="minus" aria-label={`Leave ${e.id} out of the order`} title="Leave out of the order"
                      onClick={() => setChain(chain.filter((x) => x !== e.id), `${e.id} left out of the order`)} />
            )}
          </span>
        ) : (e.installed || e.origin === "catalog") && (
          <Button size="sm" icon="plus" onClick={() => setChain([...chain, e.id], `${e.id} added at the end of the order`)}>Add to the order</Button>
        )}
      </div>
      <p className="m-item-sub">
        {e.backend} · {langs} · {gbSize(e.size_gb)} · <a href={e.license_url} target="_blank" rel="noopener">{e.license}</a>
        {e.upstream && <> · <a href={e.upstream} target="_blank" rel="noopener">source</a></>}
      </p>
      <p className="m-item-sub">{e.fit.reason}{e.speed ? ` · ${e.speed}` : ""}</p>
      {e.tested && <p className="m-hint">Tested: {e.tested}</p>}
      {e.notes && <p className="m-hint">{e.notes}</p>}
      {e.unsafe_targets.length > 0 && <p className="m-trouble">Unsafe into {e.unsafe_targets.map(langNameEn).join(", ")}: it invents text there.</p>}
      {e.error && <p className="m-trouble">Failed to load: {e.error}</p>}
      {e.attribution && <p className="m-hint">{e.attribution}</p>}
      {accepting && (
        <div className="m-edit">
          <p className="m-line">{e.id} is under {e.license}. <a href={e.license_url} target="_blank" rel="noopener">Read the licence <Icon name="external" size={12} /></a> before accepting.</p>
          {e.tier === "restricted" && <Check checked={nc} onChange={setNc}>I will use it for non-commercial purposes only</Check>}
          <div className="m-actions">
            <ActButton variant="primary" done="Accepted" disabled={e.tier === "restricted" && !nc}
                       run={async () => { await post(`/models/${e.id}/accept`, { license: e.license, non_commercial: nc }); setAccepting(false); await reload(); }}>
              I accept {e.license}
            </ActButton>
            <Button variant="ghost" onClick={() => setAccepting(false)}>Cancel</Button>
          </div>
        </div>
      )}
    </li>
  );
}

function Catalogue({ reload }: { reload: () => Promise<void> }) {
  const [q, setQ] = useState("");
  const [cap, setCap] = useState("asr");
  const [rows, setRows] = useState<Any[] | null>(null);
  const search = async () => setRows((await get(`/catalog/models?${new URLSearchParams({ q, capability: cap })}`)).results);
  return (
    <Section id="mod-cat" title="Search online"
             description="Searches MLX models on Hugging Face: the only network use on this page, and only when you search. Licences are sorted into the same tiers as above. Models with no licence, or one that excludes the EU, are never offered. Added models are pinned to the version found and marked untested.">
      <div className="m-row">
        <Input type="search" value={q} placeholder="e.g. whisper, parakeet, qwen" aria-label="Search models" className="m-grow"
               onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") act(search); }} />
        <Select value={cap} aria-label="Capability" onChange={(e) => setCap(e.target.value)} options={[["asr", "Speech recognition"], ["translate", "Translation"]]} />
        <ActButton variant="primary" icon="search" run={search}>Search online</ActButton>
      </div>
      {rows && (
        <Table caption="Catalogue results" empty="Nothing found."
               cols={[{ label: "Model" }, { label: "Licence" }, { label: "Size and fit" }, { label: "Downloads", num: true }, { label: "", width: "1%" }]}>
          {rows.map((x) => {
            const [fitWord, fitTone] = FIT[x.fit.level] || FIT.unknown;
            const [tierWord, tierTone] = TIER[x.tier] || [x.tier, "neutral"];
            return (
              <tr key={x.repo}>
                <td>
                  <a href={`https://huggingface.co/${x.repo}`} target="_blank" rel="noopener" className="mono">{x.repo}</a>
                  {x.upstream && <div className="m-hint">conversion of {x.upstream}</div>}
                  {(x.caveats || []).map((c: string) => <div key={c} className="m-hint is-trouble">{c}</div>)}
                </td>
                <td><Tag tone={tierTone}>{tierWord}</Tag> <span className="m-muted">{x.license || "none"}</span></td>
                <td>{x.weights_gb ? `${x.weights_gb.toFixed(1)} GB ` : "? "}<Tag tone={fitTone}>{fitWord}</Tag></td>
                <td className="num">{x.downloads.toLocaleString()} · {x.likes} likes</td>
                <td>
                  {x.offered ? (
                    <ActButton size="sm" run={async () => {
                      const added = await post("/catalog/models", { model: x, capability: cap });
                      toast(`${added.id} added (${added.tier}); download it from its section`);
                      await reload();
                    }}>Add</ActButton>
                  ) : <span className="m-muted">{x.reason || "not offered"}</span>}
                </td>
              </tr>
            );
          })}
        </Table>
      )}
    </Section>
  );
}
