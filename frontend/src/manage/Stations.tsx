// Stations: what is listened to, in which language, translated into which.
import { useEffect, useState } from "react";
import { langNameEn, sameLang } from "../lib/lang";
import { Icon } from "../components/Icon";
import {
  ActButton, Advanced, Button, Check, ConfirmButton, Field, Input, LangChips, LangPicker, LinkButton, Section, Segmented, Select, Tag, act, cx,
} from "../components/ui";
import { spoken } from "../reading/Column";
import { type Any, docUrl, del, get, knownLangs, post, readerLang, refreshPage, useLoad } from "./api";
import { changedFields } from "./drafts";
import { watchWords } from "./Caught";
import MoveThere from "./MoveThere";
import { SpokenField } from "./SpokenField";

export function Stations() {
  const { data, error, reload: load } = useLoad(() => get("/stations"));
  // The band plan, for the band a watch is written with.
  const { data: radio } = useLoad(() => get("/radio").catch(() => null));
  if (!data) return <p className="m-loading">{error ? `Couldn't load the stations: ${error}` : "Loading…"}</p>;
  const stations: Any[] = data.stations;
  const devices: string[] = data.devices || [];
  const reload = async () => { await load(); refreshPage(); };
  const waiting = stations.filter((s) => s.listen_only && !s.disabled).length;
  return (
    <>
      <Section id="stations-h" title={`Your stations · ${stations.length}`}
               description="What each station does: its language, its translations, its place in the list. Start or stop one from the station list under Read. Stations added here can be deleted; those from your stations file can be switched off. Changes made here are kept by Wavescribe; your own stations file is never changed."
               actions={<LinkButton variant="primary" icon="plus" href="#/add">Add a station</LinkButton>}>
        {waiting > 0 && (
          <div className="m-note" role="status">
            <p>
              <strong>No text yet.</strong> {waiting === stations.length ? "Stations" : `${waiting} of ${stations.length} stations`} are
              written down once their speech models are downloaded; they play meanwhile.
            </p>
            <LinkButton href="#/settings/languages" size="sm" icon="right">Get the models</LinkButton>
          </div>
        )}
        <ul className="m-list">
          {stations.map((s) => <StationRow key={s.id} s={s} devices={devices} bands={radio?.bands || []} reload={reload} />)}
        </ul>
      </Section>
    </>
  );
}

/** The languages every station is translated into, changed for all stations at once. */
export function TranslationLanguages() {
  const { data, error, reload: load } = useLoad(() => get("/stations"));
  if (!data) return <p className="m-loading">{error ? `Couldn't load the stations: ${error}` : "Loading…"}</p>;
  return <Languages stations={data.stations} reload={async () => { await load(); refreshPage(); }} />;
}

const KIND: Record<string, string> = { stream: "stream", sdr: "radio", remote: "remote" };
/** A stream's site, so the row stays one readable line; the whole address is in the editor. */
function site(src: string) {
  try {
    return /^https?:\/\//.test(src) ? new URL(src).host : src;
  } catch {
    return src;
  }
}

/** Whether any of its translation languages differs from what it speaks (en-GB into en isn't). */
const translated = (s: Any) => (s.translate_to || []).some((t: string) => t.split("-")[0] !== s.language.split("-")[0]);

function StationRow({ s, devices, bands, reload }: { s: Any; devices: string[]; bands: Any[]; reload: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  // Its receiver is tuned where it doesn't hear it, and a centre hears it: the move back.
  const movable = !!s.out_of_reach && s.reach_center_mhz != null && !!s.device;
  const kind = s.kind === "remote" && s.remote === "push" ? "pushed" : KIND[s.kind] || s.kind;
  // A watch has a band or a range, not a frequency.
  const tuned = s.watch != null ? `watching ${watchWords(s, bands)}`
    : `${(s.mhz || s.fm_mhz || []).join(", ")} MHz`;
  const src = s.stream || s.stream_hls || (s.device ? `receiver ${s.device} · ${tuned}${s.mode ? ` ${s.mode.toUpperCase()}` : ""}`
    : s.remote ? `${s.remote}${s.remote_station ? ` → ${s.remote_station}` : ""}` : "no source");
  const state = s.disabled ? <Tag tone="muted">switched off</Tag> : s.running ? <Tag tone="good">running</Tag>
    : s.available ? <Tag tone="muted">stopped</Tag> : <Tag tone="warn">unavailable</Tag>;
  return (
    <li className={cx("m-item", open && "is-open", s.disabled && "is-off")}>
      <div className="m-item-head">
        <button type="button" className="m-item-title" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Icon name={open ? "down" : "right"} size={14} />
          <strong dir="auto">{s.name}</strong>
        </button>
        {state}
        {s.listen_only && !s.disabled && <Tag tone="warn" title={s.reason || undefined}>no text yet</Tag>}
        {s.translate_to_missing?.length > 0 && !s.listen_only && <Tag tone="muted">some translations missing</Tag>}
        <Tag>{kind}</Tag>
        {s.origin === "page" ? <Tag tone="accent">added here</Tag> : s.overridden.length > 0 && <Tag tone="accent" title={s.overridden.join(", ")}>changed here</Tag>}
      </div>
      <p className="m-item-sub">
        {spoken(s)}
        {s.transcribe === false ? " · listen only, not written down" : (
          <>
            {intoOthers(s).length ? ` → ${intoOthers(s).map(langNameEn).join(", ")}` : (s.translate_to || []).length ? "" : " · not translated"}
            {" · "}speech {s.asr_model || (s.remote ? "at the source" : "no model installed")}
            {s.translate !== false && translated(s) && s.mt_model ? ` · translation ${s.mt_model}` : ""}
          </>
        )}
        {" · "}<span className="mono" title={src}>{site(src)}</span>
      </p>
      {!s.available && s.reason && !s.disabled && (movable ? (
        <div className="m-trouble">
          <p>{s.reason}</p>
          <MoveThere s={s} device={s.device} onDone={reload} />
        </div>
      ) : <p className="m-trouble">{s.reason}</p>)}
      {s.available && s.translate_to_missing?.length > 0 && !s.listen_only && s.reason && <p className="m-item-sub">{s.reason}</p>}
      {/* Detected without a model that tells languages apart: unlike a missing model for one
          language, naming the language is a way out too, so the reason is spelled out. */}
      {s.available && s.listen_only && s.language === "auto" && !s.disabled && s.reason && <p className="m-item-sub">{s.reason}</p>}
      {s.error && <p className="m-trouble">A change from this page wasn't applied: {s.error}</p>}
      <RegionNote s={s} reload={reload} />
      {open && <StationEditor s={s} devices={devices} reload={reload} />}
    </li>
  );
}

/** A radio station whose region changed to one that counts it as a less strict kind of radio:
 * it stays as strict as it was until the operator confirms, in the words the tuner asks for. */
function RegionNote({ s, reload }: { s: Any; reload: () => Promise<void> }) {
  const [ok, setOk] = useState(false);
  if (!s.privacy_region || !s.privacy || s.privacy_region === s.privacy) return null;
  return (
    <div className="m-panel m-region" role="group" aria-labelledby={`region-${s.id}`}>
      <p id={`region-${s.id}`} className="m-line">
        Kept as {s.privacy} radio after the region change; in this region it is {s.privacy_region}.
      </p>
      <p className="m-line">
        <Check checked={ok} onChange={setOk}>
          I've checked that I may receive this where I am, and I'm responsible for how I use what's heard.
          Laws on radio that isn't broadcast differ by country (<a className="rd-link" href={docUrl("RADIO.md")} target="_blank" rel="noreferrer">docs/RADIO.md</a>).
        </Check>
      </p>
      <div className="m-actions">
        <ActButton size="sm" variant="primary" disabled={!ok} done={`${s.name} follows this region`}
                   run={async () => { await post(`/stations/${s.id}`, { acknowledge: true }); await reload(); }}>
          Confirm
        </ActButton>
      </div>
    </div>
  );
}

function StationEditor({ s, devices, reload }: { s: Any; devices: string[]; reload: () => Promise<void> }) {
  const [name, setName] = useState(s.name);
  const [lang, setLang] = useState(s.language);
  // The languages a detected station mostly is; a named language takes none.
  const [langs, setLangs] = useState<string[]>(s.languages || []);
  const [targets, setTargets] = useState<string[]>(s.translate_to || []);
  const [alerts, setAlerts] = useState<string[]>(s.alert_languages || []);
  const [prio, setPrio] = useState(String(s.priority));
  const [model, setModel] = useState(s.asr_choice || "");
  const [mtModel, setMtModel] = useState(s.mt_choice || "");
  const [off, setOff] = useState(!!s.disabled);
  const [stream, setStream] = useState(s.stream || s.stream_hls || "");
  const [dev, setDev] = useState(s.device || devices[0] || "");
  const freqKey = s.mhz ? "mhz" : "fm_mhz"; // narrowband stations keep theirs under `mhz`
  const [fm, setFm] = useState((s[freqKey] || []).join(", "));
  const [models, setModels] = useState<[string, string][]>([]);
  const [translators, setTranslators] = useState<[string, string][]>([]);
  // Detected: only a model that tells languages apart will do (the server refuses another,
  // `cannot_detect`). The other installed ones are shown, not to be chosen, under why.
  const detect = lang === "auto";
  const [cannot, setCannot] = useState<[string, string][]>([]);
  useEffect(() => {
    const tag = (x: string) => x.split("-")[0];
    const label = (e: Any) => `${e.id}${e.fit?.level === "too_big" ? " (won't fit)" : ""}`;
    const speaks = (e: Any) => (detect ? e.detects_languages : e.languages.includes("*") || e.languages.some((l: string) => tag(l) === tag(lang)));
    get("/models").then((m) => {
      const asr = m.models.filter((e: Any) => e.capability === "asr" && e.installed);
      const fit = asr.filter(speaks);
      setModels(fit.map((e: Any) => [e.id, label(e)]));
      setCannot(detect ? asr.filter((e: Any) => !e.detects_languages).map((e: Any) => [e.id, label(e)]) : []);
      // Detected now, with a model that can't: back to Automatic, which picks one that can.
      if (detect) setModel((was: string) => (was && !fit.some((e: Any) => e.id === was) ? "" : was));
      setTranslators(m.models.filter((e: Any) => e.capability === "translate" && e.installed).map((e: Any) => [e.id, label(e)]));
    }).catch(() => {});
  }, [lang]);
  const saved = s.transcribe === false ? "listen" : s.translate === false ? "transcribe" : "translate";
  const [mode, setMode] = useState(saved);
  const save = async () => {
    if (mode !== "listen" && s.kind !== "remote" && !lang.trim()) { document.getElementById(id("lang"))?.focus(); return; }
    // Only what changed is sent: each field sent becomes the station's own setting.
    // Named after being detected, it drops the languages it mostly was: the server takes them
    // with "auto" only, and an empty list clears them.
    const ch: Any = changedFields(
      { name: s.name, language: s.language, languages: s.languages || [], translate_to: s.translate_to || [], alert_languages: s.alert_languages || [],
        priority: s.priority, asr_model: s.asr_choice || null, mt_model: s.mt_choice || null, disabled: s.disabled || null },
      { name: name.trim(), language: lang.trim(), languages: lang.trim() === "auto" ? langs : [], translate_to: targets, alert_languages: alerts,
        priority: +prio, asr_model: model || null, mt_model: mtModel || null, disabled: off || null },
    );
    // Sent only when it changed: the server checks for models whenever transcription is in a change.
    if (mode !== saved) Object.assign(ch, { transcribe: mode === "listen" ? false : null, translate: mode === "transcribe" ? false : null });
    if (s.kind === "stream") {
      const u = stream.trim();
      if (u !== (s.stream || s.stream_hls)) Object.assign(ch, /\.m3u8(\?|$)/i.test(u) ? { stream_hls: u, stream: null } : { stream: u, stream_hls: null });
    }
    if (s.kind === "sdr") {
      Object.assign(ch, changedFields({ device: s.device, [freqKey]: s[freqKey] || [] },
                                      { device: dev, [freqKey]: fm.split(/[,\s]+/).filter(Boolean).map(Number) }));
    }
    if (!Object.keys(ch).length) return;
    await post(`/stations/${s.id}`, ch);
    await reload();
  };
  const id = (k: string) => `ed-${s.id}-${k}`;
  const transcribing = mode !== "listen";
  return (
    <div className="m-edit">
      {s.kind !== "remote" && (
        <div className="m-mode">
          <Segmented label="What Wavescribe does with this station" value={mode} onChange={setMode} options={[
            { value: "listen", label: "Listen only" },
            { value: "transcribe", label: "Transcribe" },
            { value: "translate", label: "Transcribe and translate" },
          ]} />
          <p className="m-hint">{MODE_HINT[mode]}{mode !== saved && " Save below to apply it."}</p>
        </div>
      )}
      <div className="m-form">
        <Field id={id("name")} label="Name" hint="How it's shown on the reading page."><Input id={id("name")} value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <SpokenField id={id("lang")} lang={lang} onLang={setLang} langs={langs} onLangs={setLangs} known={knownLangs()} />
        {mode === "translate" && (
          <Field label="Translate into" hint="Continuously, as the station speaks.">
            <LangChips value={targets} onChange={setTargets} known={knownLangs()} offer={readerLang(lang)} label="Add a translation language" />
          </Field>
        )}
        {transcribing && s.kind !== "remote" && (
          <Field label="Translate alerts into" hint="Alerts above Low from this station are also translated into these, for everyone reading the page or receiving webhooks.">
            <LangChips value={alerts} onChange={setAlerts} known={knownLangs()} offer={readerLang(lang)} label="Add a language for alerts" />
          </Field>
        )}
        {s.kind === "stream" && (
          <Field id={id("url")} label="Stream URL" wide hint="The address of the station's audio stream.">
            <Input id={id("url")} type="url" value={stream} onChange={(e) => setStream(e.target.value)} className="mono" />
          </Field>
        )}
        {s.kind === "sdr" && (
          <>
            <Field id={id("dev")} label="Receiver" hint="The receiver that hears it."><Select id={id("dev")} value={dev} onChange={(e) => setDev(e.target.value)} options={devices} /></Field>
            <Field id={id("fm")} label={s.mhz ? "Frequency (MHz)" : "FM frequency (MHz)"} hint="Several, comma-separated, if the station has relays: the one inside the receiver's band is used.">
              <Input id={id("fm")} value={fm} onChange={(e) => setFm(e.target.value)} />
            </Field>
          </>
        )}
      </div>
      {transcribing && s.kind !== "remote" && (
        <Advanced id="station-editor">
          <div className="m-form">
            <Field id={id("model")} label="Speech model" hint={
              detect ? (models.length ? "Automatic picks the first installed model that tells languages apart." : "None that tells languages apart is downloaded yet: see Languages and models.")
                : models.length ? "Automatic picks the first installed model that speaks its language and fits." : "None downloaded for this language yet: see Languages and models."}>
              <Select id={id("model")} value={model} onChange={(e) => setModel(e.target.value)} options={[["", "Automatic: the first that fits"], ...models]}
                      groups={[["Can't tell languages apart", cannot, { disabled: true }]]} />
            </Field>
            {mode === "translate" && (
              <Field id={id("mt")} label="Translation model" hint="Another model than the shared one takes its own memory while it's in use.">
                <Select id={id("mt")} value={mtModel} onChange={(e) => setMtModel(e.target.value)} options={[["", "Automatic: the shared translator"], ...translators]} />
              </Field>
            )}
            <Field id={id("prio")} label="Transcribe first when busy" hint="1 goes first: when the Mac falls behind, lower numbers are written down first.">
              <Input id={id("prio")} type="number" min={1} max={99} value={prio} onChange={(e) => setPrio(e.target.value)} className="input-short" />
            </Field>
          </div>
        </Advanced>
      )}
      <div className="m-actions">
        <ActButton variant="primary" run={save} done="Saved">Save</ActButton>
        <Check checked={off} onChange={setOff}>Switch off (stays in the list, never starts)</Check>
        <span className="m-spacer" />
        {s.origin === "file" && s.overridden.length > 0 && (
          <ActButton variant="ghost" icon="refresh" done="Changes from this page removed" title="Back to what stations.toml says"
                     run={async () => { await post(`/stations/${s.id}`, Object.fromEntries(s.overridden.map((k: string) => [k, null]))); await reload(); }}>
            Undo page changes
          </ActButton>
        )}
        {s.origin === "page" && (
          <ConfirmButton icon="trash" confirm={`Delete ${s.name}`}
                         onConfirm={() => act(async () => { await del(`/stations/${s.id}`); await reload(); }, `${s.name} deleted`)}>
            Delete…
          </ConfirmButton>
        )}
      </div>
    </div>
  );
}

export const MODE_HINT: Record<string, string> = {
  listen: "You can play it; nothing is written down, stored or checked for alerts, and no model is needed.",
  transcribe: "What's said is written down in its own language, searched and checked for alerts. Nothing is translated continuously.",
  translate: "Written down, checked for alerts, and translated as it speaks into the languages below.",
};

/** The languages a station is translated into, without its own: an English station translated
 * "into English" just keeps its words. */
const intoOthers = (s: Any): string[] => (s.translate === false ? [] : (s.translate_to || []).filter((t: string) => !sameLang(t, s.language)));

/** The languages most stations are translated into, the most common first. */
export function primaryTargets(stations: Any[]): string[] {
  const counts: Record<string, number> = {};
  stations.forEach((s) => (s.translate_to || []).forEach((t: string) => { counts[t] = (counts[t] || 0) + 1; }));
  return Object.keys(counts).sort((a, b) => counts[b] - counts[a]);
}

// -- translation languages --------------------------------------------------------------------
function Languages({ stations, reload }: { stations: Any[]; reload: () => Promise<void> }) {
  const targets = primaryTargets(stations);
  const [add, setAdd] = useState("");
  const [drop, setDrop] = useState<string | null>(null);
  const count = (t: string) => stations.filter((s) => (s.translate_to || []).includes(t)).length;
  return (
    <Section id="langs-h" title="Translation languages"
             description="Every station is translated continuously into the first language; each further language costs GPU time per utterance. Readers choose their own language in the page's display settings. Alerts always use the source-language text.">
      <ul className="m-langs">
        {targets.map((t) => (
          <li key={t}>
            <span className="m-lang-name">{langNameEn(t)}</span>
            <span className="m-muted num">{count(t)} of {stations.length} stations</span>
            {drop === t ? (
              <span className="confirm">
                <ActButton size="sm" variant="danger" done={`${langNameEn(t)} removed from every station`}
                           run={async () => {
                             for (const s of stations.filter((x) => (x.translate_to || []).includes(t))) {
                               await post(`/stations/${s.id}`, { translate_to: s.translate_to.filter((x: string) => x !== t) });
                             }
                             setDrop(null);
                             await reload();
                           }}>
                  Stop translating into {langNameEn(t)}
                </ActButton>
                <Button size="sm" variant="ghost" onClick={() => setDrop(null)}>Cancel</Button>
              </span>
            ) : (
              <Button size="sm" variant="ghost" icon="close" aria-label={`Stop translating into ${langNameEn(t)}`} onClick={() => setDrop(t)} />
            )}
          </li>
        ))}
      </ul>
      <div className="m-row">
        <LangPicker value={add} onChange={setAdd} options={knownLangs(stations).filter((t) => !targets.includes(t))} label="Language to add" placeholder="Add a language" />
        <ActButton disabled={!add.trim()} done="Done"
                   run={async () => {
                     const t = add.trim();
                     for (const s of stations.filter((x) => !(x.translate_to || []).includes(t) && x.language !== t)) {
                       await post(`/stations/${s.id}`, { translate_to: [...(s.translate_to || []), t] });
                     }
                     setAdd("");
                     await reload();
                   }}>
          Translate every station into it
        </ActButton>
      </div>
    </Section>
  );
}
