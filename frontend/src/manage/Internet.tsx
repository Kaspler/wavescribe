// Find stations, On the internet: the station directory and a stream's own address, with a
// playlist and another Wavescribe as the rarer ways in. Each opens the same form for the new station.
import { useEffect, useRef, useState } from "react";
import { langNameEn } from "../lib/lang";
import { day } from "../lib/time";
import { ActButton, Advanced, Button, Check, Field, Input, LangChips, LangPicker, Section, Segmented, Table, Tag, Textarea, act, toast } from "../components/ui";
import { type Any, get, knownLangs, post, readerLang, refreshPage, useLoad } from "./api";
import { addedToast } from "./added";
import { SpokenField } from "./SpokenField";
import { spokenFields } from "./spokenLang";
import { MODE_HINT, primaryTargets } from "./Stations";

export function OnTheInternet() {
  const { data, error, reload } = useLoad(() => get("/stations"));
  const [pre, setOpen] = useState<{ from: string; p: Pre } | null>(null);
  // What opened the form: the focus goes back there when it closes, while it is still there.
  const opener = useRef<HTMLElement | null>(null);
  const setPre = (next: { from: string; p: Pre } | null) => {
    if (next) opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    else requestAnimationFrame(() => { if (opener.current?.isConnected) opener.current.focus(); });
    setOpen(next);
  };
  if (!data) return <p className="m-loading">{error ? `Couldn't load the stations: ${error}` : "Loading…"}</p>;
  const stations: Any[] = data.stations;
  const useFrom = (from: string) => (url: string, extra: Partial<Pre> & { hls?: boolean } = {}) => {
    const hls = /\.m3u8(\?|$)/i.test(url) || extra.hls;
    setPre({ from, p: { ...extra, raw: hls ? { stream_hls: url } : { stream: url }, summary: url } });
  };
  // The new station's form opens under the way it was found.
  const form = (from: string) => pre?.from === from && (
    <StationForm key={JSON.stringify(pre.p.raw)} pre={pre.p} stations={stations}
                 onDone={async () => { setPre(null); await reload(); await refreshPage(); }} onCancel={() => setPre(null)} />
  );
  return (
    <>
      <Section id="in-dir" title="Station directory" description="Internet stations by name, place, tag or language.">
        <DirectoryMode use={useFrom("dir")} />
        {form("dir")}
      </Section>
      <Section id="in-url" title="By address (URL)" description="A station's own stream address, as its website gives it.">
        <UrlMode use={useFrom("url")} />
        {form("url")}
      </Section>
      <Advanced id="add-more" label="Other ways to add">
        <Section id="in-list" title="From a playlist" description="An .m3u or .pls file, pasted or by its address.">
          <PlaylistMode use={useFrom("list")} />
          {form("list")}
        </Section>
        <Section id="in-remote" title="From another Wavescribe" description="Follow another machine's stations as text.">
          <RemoteMode setPre={(p) => setPre({ from: "remote", p })} />
          {form("remote")}
        </Section>
      </Advanced>
    </>
  );
}

type Pre = { name?: string; language?: string; detected?: string; translate_to?: string[]; raw: Any; summary?: string };

function StationForm({ pre, stations, onDone, onCancel }: { pre: Pre; stations: Any[]; onDone: () => Promise<void>; onCancel: () => void }) {
  const [name, setName] = useState(pre.name || "");
  const [lang, setLang] = useState(pre.language || "");
  const [langs, setLangs] = useState<string[]>([]);
  const [targets, setTargets] = useState<string[]>(pre.translate_to || primaryTargets(stations).slice(0, 2));
  const [prio, setPrio] = useState("5");
  const [start, setStart] = useState(true);
  const [does, setDoes] = useState<string>("translate");
  const [noModels, setNoModels] = useState(false);
  const first = useRef<HTMLInputElement>(null);
  useEffect(() => { first.current?.focus(); }, []);
  useEffect(() => {
    // With no speech model downloaded yet, start by listening: transcription can follow.
    get("/models").then((m) => {
      const has = (cap: string) => m.models.some((e: Any) => e.capability === cap && e.installed);
      if (!has("vad") || !has("asr")) { setNoModels(true); setDoes("listen"); }
    }).catch(() => {});
  }, []);
  return (
    <div className="m-new">
      <h4>New station</h4>
      {pre.summary && <p className="m-hint mono">{pre.summary}</p>}
      <div className="m-mode">
        <Segmented label="What Wavescribe does with this station" value={does} onChange={setDoes} options={[
          { value: "listen", label: "Listen only" },
          { value: "transcribe", label: "Transcribe" },
          { value: "translate", label: "Transcribe and translate" },
        ]} />
        <p className="m-hint">
          {noModels && does !== "listen" ? "No speech model is downloaded yet: download one under Languages and models first, or listen now and turn transcription on later." : MODE_HINT[does]}
        </p>
      </div>
      <div className="m-form">
        <Field id="new-name" label="Name" hint="How it's shown on the reading page."><Input ref={first} id="new-name" value={name} placeholder="Station name" onChange={(e) => setName(e.target.value)} /></Field>
        <SpokenField id="new-lang" lang={lang} onLang={setLang} langs={langs} onLangs={setLangs} known={knownLangs(stations)}
                     named={pre.detected && lang === pre.language ? `Detected: ${pre.detected}` : undefined} />
        {does === "translate" && <Field label="Translate into" hint="Continuously, as the station speaks."><LangChips value={targets} onChange={setTargets} known={knownLangs(stations)} offer={readerLang(lang)} label="Add a translation language" /></Field>}
        {does !== "listen" && (
          <Field id="new-prio" label="Priority" hint="1 is transcribed first when the machine falls behind.">
            <Input id="new-prio" type="number" min={1} max={99} value={prio} onChange={(e) => setPrio(e.target.value)} className="input-short" />
          </Field>
        )}
      </div>
      <div className="m-actions">
        <ActButton variant="primary" icon="plus" disabled={!name.trim() || !lang.trim()}
                   run={async () => {
                     const added = await post("/stations", {
                       ...pre.raw, name: name.trim(), ...spokenFields(lang, langs), translate_to: targets, priority: +prio, start,
                       ...(does === "listen" ? { transcribe: false } : does === "transcribe" ? { translate: false } : {}),
                     });
                     const said = addedToast(added, `${added.name} added${start ? " and starting" : ""}`);
                     toast(said.text, said.bad);
                     await onDone();
                   }}>
          Add as a station
        </ActButton>
        <Check checked={start} onChange={setStart}>Start now</Check>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function UrlMode({ use }: { use: (url: string, extra?: Any) => void }) {
  const [url, setUrl] = useState("");
  const [detect, setDetect] = useState(false);
  const [res, setRes] = useState<React.ReactNode>(null);
  const check = async () => {
    setRes(<p className="m-hint">{detect ? "Listening for 20 s to identify the language…" : "Checking…"}</p>);
    let r;
    try {
      r = await post("/probe", { url: url.trim(), detect });
    } catch (e) {
      setRes(<p className="m-trouble">Couldn't check it: {(e as Error).message}</p>);
      return;
    }
    const p = r.probe;
    if (!p.ok) {
      setRes(
        <div className="m-trouble">
          <p>Couldn't play this address. Check it's a stream link (often ending .mp3, .aac or .m3u8), or search the directory instead.</p>
          {p.error && (
            <details className="m-why">
              <summary>Details</summary>
              <p className="mono">{p.error}</p>
            </details>
          )}
        </div>,
      );
      return;
    }
    const d = r.detected || {};
    setRes(
      <>
        <p className="m-line">
          <Tag tone="good">plays</Tag> {p.codec || "audio"}{p.bitrate_kbps ? ` ${p.bitrate_kbps} kbit/s` : ""}{p.title ? ` · “${p.title}”` : ""}
          {d.language ? ` · sounds like ${langNameEn(d.language)}` : ""}{d.music_skipped_s ? ` (ignoring ${Math.round(d.music_skipped_s)} s of music)` : ""}
          {d.error ? ` · ${d.error}` : ""}
        </p>
        {d.text && <p className="m-hint">Heard: {d.text}</p>}
      </>,
    );
    use(url.trim(), { name: p.title, hls: p.is_hls, language: d.language, detected: d.language ? langNameEn(d.language) : undefined });
  };
  return (
    <>
      <div className="m-row">
        <Input type="url" value={url} placeholder="https://…/stream.mp3 or .m3u8" aria-label="Stream URL" className="m-grow mono"
               onChange={(e) => setUrl(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && url.trim()) act(check); }} />
        <ActButton variant="primary" disabled={!url.trim()} run={check}>Check</ActButton>
        <Button variant="ghost" disabled={!url.trim()} onClick={() => use(url.trim())}>Skip the check</Button>
      </div>
      <Check checked={detect} onChange={setDetect}>Also identify the language (needs Whisper; listens for 20 s)</Check>
      {res}
    </>
  );
}

function DirectoryMode({ use }: { use: (url: string, extra?: Any) => void }) {
  const [q, setQ] = useState("");
  const [lang, setLang] = useState("");
  const [country, setCountry] = useState("");
  const [online, setOnline] = useState(false);
  const [info, setInfo] = useState("");
  const [rows, setRows] = useState<Any[] | null>(null);
  const search = async () => {
    const qs = new URLSearchParams({ q, language: lang, country: country.toUpperCase(), online: online ? "1" : "0" });
    const r = await get(`/catalog/stations?${qs}`);
    setInfo(r.source === "none" ? "No offline copy of the directory yet: download it (about 10 MB), or search online."
      : r.source === "snapshot" ? `Offline copy from ${day(r.snapshot_at)}.` : "Searched online.");
    setRows(r.source === "none" ? null : r.results);
  };
  // The first look stays on this machine. Without an offline copy there is nothing to search
  // locally, so Search goes online until the copy is downloaded or the box is cleared.
  useEffect(() => {
    act(async () => {
      const r = await get("/catalog/stations?q=&online=0");
      if (r.source === "none") {
        setOnline(true);
        setInfo("No offline copy of the directory yet, so Search asks Radio Browser online. Download the directory (about 10 MB) to search without a connection.");
      } else {
        setInfo(`Offline copy from ${day(r.snapshot_at)}.`);
        setRows(r.results);
      }
    });
  }, []);
  const refresh = async () => {
    let r = await post("/catalog/stations/refresh");
    setInfo("Downloading the Radio Browser directory…");
    while (r.refresh?.state === "running" || r.state === "running") {
      await new Promise((ok) => setTimeout(ok, 2000));
      r = await get("/catalog/stations?q=");
    }
    const st = r.refresh || r;
    setInfo(st.state === "failed" ? `Download failed: ${st.error}` : `Directory saved: ${st.count ?? "?"} stations.`);
  };
  return (
    <>
      <div className="m-row">
        <Input type="search" value={q} placeholder="Name, city or tag" aria-label="Search the directory" className="m-grow"
               onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") act(search); }} />
        <LangPicker value={lang} onChange={setLang} options={knownLangs()} label="Language" placeholder="Any language" className="m-lang-filter" />
        <Input value={country} placeholder="Country" aria-label="Country code, two letters" size={9} maxLength={2} onChange={(e) => setCountry(e.target.value)} />
        <ActButton variant="primary" icon="search" run={search}>Search</ActButton>
      </div>
      <div className="m-row">
        <Check checked={online} onChange={setOnline}>Search online</Check>
        <ActButton size="sm" variant="ghost" icon="download" run={refresh}>Download the directory</ActButton>
        {info && <span className="m-hint">{info}</span>}
      </div>
      {rows && (
        <Table caption="Directory results" empty="No station matches."
               cols={[{ label: "Station" }, { label: "Country" }, { label: "Languages" }, { label: "Stream" }, { label: "", width: "1%" }]}>
          {rows.map((st, i) => (
            <tr key={`${st.url}-${i}`}>
              <td>{st.name}{!st.ok && <> <Tag tone="warn">last check failed</Tag></>}</td>
              <td>{st.countrycode || st.country || "–"}</td>
              <td>{(st.languages || []).join(", ") || "–"}</td>
              <td className="m-muted">{st.codec || ""} {st.bitrate_kbps ? `${st.bitrate_kbps}k` : ""}{st.hls ? " HLS" : ""}</td>
              <td><Button size="sm" onClick={() => use(st.url, { name: st.name, hls: st.hls, language: (st.languages || [])[0] })}>Use</Button></td>
            </tr>
          ))}
        </Table>
      )}
      <p className="m-hint">Radio Browser is a public-domain directory of about 54,000 internet stations. Searches use the offline copy unless you search online.</p>
    </>
  );
}

function PlaylistMode({ use }: { use: (url: string, extra?: Any) => void }) {
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [rows, setRows] = useState<Any[] | null>(null);
  return (
    <>
      <Textarea rows={4} value={text} placeholder="Paste an .m3u or .pls file here, or give its URL below" aria-label="Playlist text" onChange={(e) => setText(e.target.value)} />
      <div className="m-row">
        <Input type="url" value={url} placeholder="https://…/stations.m3u" aria-label="Playlist URL" className="m-grow mono" onChange={(e) => setUrl(e.target.value)} />
        <ActButton variant="primary" disabled={!text.trim() && !url.trim()}
                   run={async () => setRows((await post("/playlist", { text: text || null, url: url.trim() || null })).entries)}>
          Read
        </ActButton>
      </div>
      {rows && (
        <Table caption="Playlist entries" cols={[{ label: "Name" }, { label: "Stream" }, { label: "", width: "1%" }]}>
          {rows.map((e, i) => (
            <tr key={i}>
              <td>{e.name || "–"}</td><td className="mono m-break">{e.url}</td>
              <td><Button size="sm" onClick={() => use(e.url, { name: e.name, hls: e.hls })}>Use</Button></td>
            </tr>
          ))}
        </Table>
      )}
    </>
  );
}

function RemoteMode({ setPre }: { setPre: (p: Pre) => void }) {
  const [url, setUrl] = useState("");
  const [token, setToken] = useState("");
  const [r, setR] = useState<Any>(null);
  return (
    <>
      <div className="m-row">
        <Input type="url" value={url} placeholder="http://other-machine.local:8765" aria-label="Its address" className="m-grow mono" onChange={(e) => setUrl(e.target.value)} />
        <Input value={token} type="password" autoComplete="new-password" spellCheck={false} placeholder="Feed token, if it has any" aria-label="Feed token" size={24} onChange={(e) => setToken(e.target.value)} />
        <ActButton variant="primary" disabled={!url.trim()} run={async () => setR(await post("/remote/probe", { url: url.trim(), token: token.trim() || null }))}>Look</ActButton>
      </div>
      {r && (
        <>
          <p className="m-hint">{r.node.name} · Wavescribe {r.node.version} · {r.stations.length} stations</p>
          <Table caption="Its stations" cols={[{ label: "Station" }, { label: "Language" }, { label: "Running" }, { label: "", width: "1%" }]}>
            {r.stations.map((st: Any) => (
              <tr key={st.id}>
                <td dir="auto">{st.name}</td><td>{langNameEn(st.language)}</td><td>{st.running ? "yes" : "no"}</td>
                <td>
                  <Button size="sm" onClick={() => setPre({
                    name: `${st.name} (${r.node.name})`, language: st.language, translate_to: st.translate_to,
                    raw: { remote: url.trim().replace(/\/$/, ""), remote_station: st.id, ...(token.trim() ? { remote_token: token.trim() } : {}) },
                    summary: `Text from ${r.node.name}; no audio crosses. This machine's rules and translations apply.`,
                  })}>
                    Follow
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        </>
      )}
      <p className="m-hint">For a machine this one can't reach (behind NAT), let it push instead: see Connections.</p>
    </>
  );
}
