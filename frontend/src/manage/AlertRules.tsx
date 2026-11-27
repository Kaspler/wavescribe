// Alerts: the rules that listen for what matters, and what happens when one fires.
import { useEffect, useRef, useState } from "react";
import { S } from "../lib/live";
import { langName, langNameEn } from "../lib/lang";
import {
  ActButton, Advanced, Button, Check, ChoiceChips, ConfirmButton, Field, Input, LinkButton, Section, Segmented, Select, Switch, Table, Tag, Textarea, act, toast,
} from "../components/ui";
import { type Any, SEVERITIES, SEV_WORD, del, get, post, useLoad } from "./api";
import { lines, ruleBody } from "./drafts";

/** Said under the page's heading, whatever follows: what an alert can and can't be relied on for. */
const NOT_A_WARNING_SYSTEM = (
  <p className="m-standing">
    Alerts are a help, not a warning system. One may come late, be wrong, or not come at all, and no alert doesn't mean
    nothing is happening. Don't rely on Wavescribe where life, health or property is at stake: follow official warnings
    and the emergency services.
  </p>
);

export function AlertRules() {
  return <>{NOT_A_WARNING_SYSTEM}<RulesPage /></>;
}

function RulesPage() {
  const { data: v, error, reload, setData } = useLoad(async () => {
    const [rules, st] = await Promise.all([get("/rules"), get("/stations")]);
    return { ...rules, stations: st.stations as Any[] };
  });
  const [editing, setEditing] = useState<Any | null>(null);
  if (!v) return <p className="m-loading">{error ? `Couldn't load the rules: ${error}` : "Loading…"}</p>;
  if (!v.enabled) {
    return (
      <Section id="al-off" title="Alerts are off">
        <p className="m-line">This run was started with --no-alerts. Without config/alerts.toml the example rules in config/alerts.example.toml apply; copy it to make your own.</p>
      </Section>
    );
  }
  const redraw = (fresh: Any) => setData({ ...fresh, stations: v.stations });
  return (
    <>
      <Section id="al-rules" title="Rules"
               description={`From ${v.file || "this page only"} and this page. Words to listen for match in each station's own language. A situation described in your words is checked by the model against what each station said, about every 45 s.`}
               actions={<>
                 <Button icon="plus" onClick={() => setEditing({ kind: "keyword" })}>Words to listen for</Button>
                 <Button icon="plus" onClick={() => setEditing({ kind: "llm" })}>Describe it in your words</Button>
               </>}>
        {editing && <RuleEditor key={editing.id || editing.kind} r={editing} v={v} onSaved={(fresh) => { redraw(fresh); setEditing(null); }} onCancel={() => setEditing(null)} />}
        <ul className="m-list">
          {v.rules.map((r: Any) => <RuleRow key={r.id} r={r} packs={v.packs} redraw={redraw} onEdit={() => setEditing(r)} />)}
        </ul>
        <div className="m-row">
          <span className="m-hint rd-inline">About a station rather than what it says:</span>
          <Button size="sm" variant="ghost" icon="plus" onClick={() => setEditing({ kind: "silence" })}>Station silent</Button>
          <Button size="sm" variant="ghost" icon="plus" onClick={() => setEditing({ kind: "traffic" })}>Traffic announcement</Button>
        </div>
      </Section>
      <TryRules stations={v.stations} />
      <Delivery v={v} reload={reload} />
    </>
  );
}

/** What an alert of this severity does, from the actions set under What happens on an alert. */
function sevDoes(v: Any, sev: string): string {
  const acts: string[] = v.actions?.[sev] || [];
  const ways = [
    acts.includes("notify") && "as a notification",
    ...acts.filter((a) => a.startsWith("speak:")).map((a) => `read aloud in ${langNameEn(a.slice(6))}`),
  ].filter(Boolean);
  return `${SEV_WORD[sev]}: shown on the page${ways.length ? `, ${ways.join(" and ")}` : " only"}${acts.includes("log") ? ", and logged" : ""}.`;
}

function SevMark({ sev }: { sev: string }) {
  return <span className={`sev sev-${sev}`}><i aria-hidden="true" />{SEV_WORD[sev] || sev}</span>;
}

/** The languages that have a ready-made word list for a topic (config/keywords). */
const listsFor = (packs: Any, topic: string) => Object.keys(packs || {}).filter((l) => packs[l].topics?.[topic]);

/** "5 minutes", "30 seconds": how long a silence rule waits. */
const span = (min: number) => (min < 1 ? `${Math.round(min * 60)} seconds` : min === 1 ? "1 minute" : `${+min.toFixed(1)} minutes`);

/** The kinds of rule this page writes, in its own words; a rule of another kind is listed as it
 * is and left alone. */
const KINDS: Record<string, { tag: string; title: string }> = {
  keyword: { tag: "words", title: "New rule: words to listen for" },
  llm: { tag: "described", title: "New rule: described in your words" },
  silence: { tag: "station silent", title: "New rule: a station goes silent" },
  traffic: { tag: "traffic", title: "New rule: a traffic announcement" },
};
const SILENCE_MINUTES = [0.5, 1440] as const; // the server's limits (rules/__init__.py)
const SILENCE_DB = [-120, 0] as const;

function RuleRow({ r, packs, redraw, onEdit }: { r: Any; packs: Any; redraw: (v: Any) => void; onEdit: () => void }) {
  const known = !!KINDS[r.kind];
  const topic = (t: string) => {
    const langs = listsFor(packs, t);
    return `topic ${t.replace(/_/g, " ")} (${langs.length ? `word lists in ${langs.map(langNameEn).join(", ")}` : "no word list yet"})`;
  };
  const what = !known ? `This page can't edit a rule of kind ${r.kind}; it runs as alerts.toml says.`
    : r.kind === "llm" ? `“${r.prompt}”`
    : r.kind === "silence" ? `Station silent: tells you when a station sends no audio for ${span(r.after_minutes ?? 5)}.`
    : r.kind === "traffic" ? "Traffic announcement: tells you when an FM station starts one."
    : [...(r.topics || []).map(topic), ...Object.entries(r.terms || {}).map(([l, ws]) => `${langNameEn(l)}: ${(ws as string[]).join(", ")}`),
       ...(r.regex?.length ? [`${r.regex.length} ${r.regex.length === 1 ? "pattern" : "patterns"}`] : [])].join(" · ");
  return (
    <li className={`m-item${r.enabled ? "" : " is-off"}`}>
      <div className="m-item-head">
        <Switch checked={r.enabled} label={`Rule ${r.id}`} hideLabel
                onChange={(on) => act(async () => redraw(await post(`/rules/${r.id}/enable`, { on })), on ? "Rule on" : "Rule off")} />
        <strong className="mono">{r.id}</strong>
        <SevMark sev={r.severity} />
        <Tag tone={known ? "neutral" : "muted"}>{known ? KINDS[r.kind].tag : r.kind}</Tag>
        {r.origin === "page" ? <Tag tone="accent">added here</Tag> : <Tag tone="muted">alerts.toml</Tag>}
        {r.verify && <Tag>model confirms</Tag>}
        <span className="m-spacer" />
        {known && <Button size="sm" variant="ghost" icon="edit" onClick={onEdit}>{r.origin === "page" ? "Edit" : "Change here"}</Button>}
        {r.origin === "page" && (
          <ConfirmButton size="sm" icon="trash" confirm={`Delete ${r.id}`} onConfirm={() => act(async () => redraw(await del(`/rules/${r.id}`)), "Deleted")}>
            Delete
          </ConfirmButton>
        )}
      </div>
      <p className="m-item-sub">
        {what || "–"}
        {r.near?.length ? ` · more severe near ${r.near.join(", ")}` : ""}
        {r.min_mentions > 1 ? ` · after ${r.min_mentions} mentions` : ""}
        {r.stations?.length ? ` · only ${r.stations.map((id: string) => S.byId[id]?.name || id).join(", ")}` : ""}
      </p>
    </li>
  );
}

function RuleEditor({ r, v, onSaved, onCancel }: { r: Any; v: Any; onSaved: (v: Any) => void; onCancel: () => void }) {
  const kind: string = r.kind || "keyword";
  const llm = kind === "llm", silence = kind === "silence", traffic = kind === "traffic";
  const words = kind === "keyword";
  // An FM station's traffic flag comes with its RDS, so only receiver stations can raise one.
  const stations: Any[] = traffic ? v.stations.filter((s: Any) => s.kind === "sdr") : v.stations;
  const [id, setId] = useState(r.id || "");
  const [sev, setSev] = useState(r.severity || "medium");
  const [mentions, setMentions] = useState(String(r.min_mentions || 1));
  const [only, setOnly] = useState<string[]>(r.stations || []);
  const [prompt, setPrompt] = useState(r.prompt || "");
  const langs = [...new Set([...v.stations.map((s: Any) => s.language.split("-")[0]), ...Object.keys(r.terms || {})])] as string[];
  const [terms, setTerms] = useState<Record<string, string>>(Object.fromEntries(langs.map((l) => [l, (r.terms?.[l] || []).join("\n")])));
  const [topics, setTopics] = useState<string[]>(r.topics || []);
  const [near, setNear] = useState<string[]>(r.near || []);
  const [verify, setVerify] = useState(!!r.verify);
  const [regex, setRegex] = useState<string>((r.regex || []).join("\n"));
  const [regexErr, setRegexErr] = useState<string | null>(null);
  const [after, setAfter] = useState(String(r.after_minutes ?? 5));
  const [level, setLevel] = useState(String(r.level_db ?? -50));
  const [mine, setMine] = useState("");
  const [saving, setSaving] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => { box.current?.scrollIntoView({ behavior: "smooth", block: "nearest" }); }, []);
  const packs = Object.values(v.packs || {}) as Any[];
  const topicOpts = [...new Set(packs.flatMap((p) => Object.keys(p.topics)))].sort();
  const placeOpts = [...new Set(packs.flatMap((p) => p.places))].sort() as string[];
  const inRange = (x: string, [lo, hi]: readonly [number, number]) => x.trim() !== "" && +x >= lo && +x <= hi;
  const ready = !!id.trim() && (!llm || !!prompt.trim()) && (!silence || (inRange(after, SILENCE_MINUTES) && inRange(level, SILENCE_DB)));
  const fields = (): Any => {
    const f: Any = { id: id.trim(), severity: sev, stations: only };
    if (!r.id) f.kind = kind;
    if (llm || words) f.min_mentions = +mentions > 1 ? +mentions : 1;
    if (llm) f.prompt = prompt.trim();
    if (silence) Object.assign(f, { after_minutes: +after, level_db: +level });
    if (words) {
      const t: Record<string, string[]> = {};
      for (const l of langs) {
        const ws = lines(terms[l] || "");
        if (ws.length) t[l] = ws;
      }
      Object.assign(f, { terms: t, topics, near, verify, regex: lines(regex) });
    }
    return f;
  };
  // A bad pattern is the server's to judge (Python's regular expressions): its words go beside
  // the field rather than into a passing message.
  const save = async () => {
    setSaving(true);
    setRegexErr(null);
    try {
      onSaved(await post("/rules", ruleBody(r.id ? r : null, fields())));
      toast("Rule saved");
    } catch (e) {
      const msg = (e as Error).message;
      if (words && /: regex /.test(msg)) setRegexErr(msg.replace(/^rule [^:]+: /, ""));
      else toast(msg, true);
    } finally {
      setSaving(false);
    }
  };
  const suggest = async () => {
    const s = await post("/rules/suggest", { terms: mine.split(","), languages: langs });
    const next = { ...terms };
    for (const [l, ws] of Object.entries(s.terms || {}) as [string, string[]][]) {
      const have = new Set((next[l] || "").split("\n").filter(Boolean));
      ws.forEach((w) => have.add(w));
      next[l] = [...have].join("\n");
    }
    setTerms(next);
  };
  return (
    <div ref={box} className="m-panel">
      <div className="m-panel-head">
        <h4>{r.id ? `Rule ${r.id}` : KINDS[kind].title}</h4>
        <Button size="sm" variant="ghost" icon="close" aria-label="Close" onClick={onCancel} />
      </div>
      {r.origin === "file" && <p className="m-hint">Saving makes a page copy that replaces the alerts.toml rule; delete the copy to go back.</p>}
      <div className="m-form">
        <Field id="ru-id" label="Id" hint="Lowercase letters, digits, dashes and underscores.">
          <Input id="ru-id" value={id} placeholder="short-id" pattern="[a-z0-9][a-z0-9_\-]{0,40}" disabled={!!r.id} onChange={(e) => setId(e.target.value)} className="mono" />
        </Field>
        <Field label="Severity" hint={<>{sevDoes(v, sev)} Each level's actions are set under What happens on an alert.</>}>
          <Segmented label="Severity" value={sev} onChange={setSev}
                     options={SEVERITIES.map((x) => ({ value: x, label: SEV_WORD[x] }))} />
        </Field>
        {silence && (
          <Field id="ru-after" label="Silent for (minutes)"
                 hint={`Tells you when a running station sends no audio, or only dead air, for this long: from ${span(SILENCE_MINUTES[0])} to a day. Once its audio is back, the alert says so.`}>
            <Input id="ru-after" type="number" min={SILENCE_MINUTES[0]} max={SILENCE_MINUTES[1]} step={0.5} value={after}
                   onChange={(e) => setAfter(e.target.value)} className="input-short" />
          </Field>
        )}
        {traffic && (
          <p className="m-line field-wide">
            Tells you when an FM station starts a traffic announcement, which it flags in RDS, the data it sends with its name.
            Only stations on a receiver send it; more announcements within the cooldown join the same alert.
          </p>
        )}
        {llm ? (
          <Field id="ru-prompt" wide label="Alert when the radio says that…"
                 hint="In any language. The model on this machine reads what each station said, about every 45 s, and says why it matches; matches go through the same cooldown and actions. It decides alone, and can miss what you mean or misread what was said.">
            <Textarea id="ru-prompt" rows={3} value={prompt} placeholder="e.g. residents are told to leave their homes or to stay indoors" onChange={(e) => setPrompt(e.target.value)} />
          </Field>
        ) : words && (
          <>
            <div className="field field-wide">
              <span className="field-label">Words and phrases</span>
              <div className="m-row">
                <Input value={mine} placeholder="Words in your language, comma-separated" aria-label="Words to suggest from" className="m-grow" onChange={(e) => setMine(e.target.value)} />
                <ActButton icon="ask" disabled={!mine.trim()} run={suggest} done="Suggestions added: check them before saving">Suggest in each language</ActButton>
              </div>
              <p className="field-hint">The model proposes how broadcasters say it in each station's language. Check them before saving; matching itself never translates.</p>
            </div>
            {langs.map((l) => (
              <Field key={l} id={`ru-t-${l}`} label={`In ${langName(l)}`} hint="As the radio says it, one per line.">
                <Textarea id={`ru-t-${l}`} rows={3} lang={l} value={terms[l] || ""} placeholder="One word or phrase per line"
                          onChange={(e) => setTerms({ ...terms, [l]: e.target.value })} />
              </Field>
            ))}
            {topicOpts.length > 0 && (
              <Field label="Ready-made word lists" wide
                     hint={`Topics with words per language, from config/keywords. Lists exist in ${Object.keys(v.packs || {}).map(langNameEn).join(", ") || "no language yet"}; other stations need words of your own.`}>
                <ChoiceChips label="Topics" options={topicOpts.map((t) => [t, t.replace(/_/g, " ")] as [string, string])} value={topics} onChange={setTopics} />
              </Field>
            )}
            {placeOpts.length > 0 && (
              <Field label="More severe near" hint="One level more severe when a place from this group is named." wide>
                <ChoiceChips label="Places" options={placeOpts} value={near} onChange={setNear} />
              </Field>
            )}
          </>
        )}
      </div>
      <Advanced id="rule-editor">
        <div className="m-form">
          {(llm || words) && (
            <Field id="ru-min" label="Mentions before alerting" hint="2 or more ignores a passing reference; a real event gets repeated.">
              <Input id="ru-min" type="number" min={1} max={10} value={mentions} onChange={(e) => setMentions(e.target.value)} className="input-short" />
            </Field>
          )}
          {silence && (
            <Field id="ru-level" label="Dead air below (dBFS)" hint="Audio that never gets louder than this counts as silence: a transmitter on air with nothing on it. −50 suits most stations.">
              <Input id="ru-level" type="number" min={SILENCE_DB[0]} max={SILENCE_DB[1]} step={1} value={level} onChange={(e) => setLevel(e.target.value)} className="input-short" />
            </Field>
          )}
          <Field label="Only on these stations" hint={traffic && !stations.length ? "No station is on a receiver yet: every FM station counts once there is one." : "None selected: every station."} wide>
            <ChoiceChips label="Stations" options={stations.map((s) => [s.id, s.name] as [string, string])} value={only} onChange={setOnly} />
          </Field>
          {words && (
            <div className="field field-wide">
              <Check checked={verify} onChange={setVerify}>Let the model confirm each match in context before raising it above Low</Check>
              <p className="field-hint">Fewer false alarms, a few seconds later.</p>
            </div>
          )}
          {words && (
            <Field id="ru-regex" wide label="Patterns (regular expressions)"
                   hint={<>One per line, matched in any station's language, ignoring case: for words that change, such as numbers. For example <code>\broad (closed|blocked)\b</code>.</>}>
              <Textarea id="ru-regex" rows={3} value={regex} spellCheck={false} className="mono" aria-invalid={regexErr ? true : undefined}
                        aria-describedby={regexErr ? "ru-regex-err" : undefined}
                        onChange={(e) => { setRegex(e.target.value); setRegexErr(null); }} />
              {regexErr && <p className="m-trouble" id="ru-regex-err" role="alert">{regexErr}</p>}
            </Field>
          )}
        </div>
      </Advanced>
      <div className="m-actions">
        <Button variant="primary" busy={saving} disabled={!ready} onClick={save}>Save rule</Button>
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </div>
  );
}

function TryRules({ stations }: { stations: Any[] }) {
  const [text, setText] = useState("");
  const [lang, setLang] = useState(stations[0]?.language || "");
  const [hits, setHits] = useState<Any[] | null>(null);
  return (
    <Section id="al-try" title="Try a sentence" description="Nothing is raised: this only shows which word rules would match. Rules described in your words can't be tried here, because they need what was said around it.">
      <Textarea rows={2} value={text} lang={lang} placeholder="A sentence as the radio would say it" aria-label="Sentence to try" onChange={(e) => setText(e.target.value)} />
      <div className="m-row">
        <Input value={lang} list="m-langs" aria-label="Its language" title="Its language, as a tag such as pt-PT" size={9} onChange={(e) => setLang(e.target.value)} />
        <ActButton variant="primary" disabled={!text.trim()} run={async () => setHits((await post("/rules/try", { text, language: lang })).hits)}>Try</ActButton>
        {hits && !hits.length && <span className="m-hint">No keyword rule matches.</span>}
      </div>
      {hits && hits.length > 0 && (
        <Table caption="Matches" cols={[{ label: "Rule" }, { label: "Severity" }, { label: "Matched" }, { label: "Places" }]}>
          {hits.map((x, i) => (
            <tr key={i}><td className="mono">{x.rule}</td><td><SevMark sev={x.severity} /></td><td>{x.terms.join(", ")}</td><td>{(x.places || []).join(", ") || "–"}</td></tr>
          ))}
        </Table>
      )}
    </Section>
  );
}

function Delivery({ v, reload }: { v: Any; reload: () => Promise<void> }) {
  const voices = Object.keys(v.voices || {});
  const kinds = ["log", "notify", ...voices.map((l) => `speak:${l}`)];
  const label = (k: string) => (k === "log" ? "Log" : k === "notify" ? "Notification" : `Speak ${langNameEn(k.slice(6))}`);
  const [matrix, setMatrix] = useState<Record<string, string[]>>(Object.fromEntries(SEVERITIES.map((s) => [s, v.actions[s] || []])));
  const [notifyLang, setNotifyLang] = useState(v.delivery.notify_language || "");
  const [wait, setWait] = useState(String(v.delivery.translation_wait_s ?? 10));
  const [cool, setCool] = useState(String(v.delivery.cooldown_minutes ?? 15));
  const [vc, setVc] = useState<Record<string, Any>>(Object.fromEntries(voices.map((l) => [l, { ...v.voices[l] }])));
  const [nl, setNl] = useState({ lang: "", voice: "", fallback: "" });
  const cur = (l: string): Any => vc[l] ?? { ...v.voices[l] }; // a voice added since this form opened
  const inst: string[] = v.installed_voices || [];
  const toggle = (sev: string, k: string) =>
    setMatrix({ ...matrix, [sev]: matrix[sev].includes(k) ? matrix[sev].filter((x) => x !== k) : [...matrix[sev], k] });
  const save = async () => {
    const speak: Record<string, Any> = {};
    for (const [l, x] of Object.entries(vc)) speak[l] = { voice: x.voice, intro: x.intro, fallback: x.fallback };
    if (nl.lang.trim() && nl.voice && nl.fallback.trim()) speak[nl.lang.trim()] = { voice: nl.voice, fallback: nl.fallback.trim() };
    await post("/alerts/settings", { delivery: { notify_language: notifyLang.trim(), translation_wait_s: +wait, cooldown_minutes: +cool }, actions: matrix, speak });
    await reload();
  };
  const voiceInput = (value: string, onChange: (v: string) => void, name: string) =>
    inst.length ? (
      <Select value={value} aria-label={name} onChange={(e) => onChange(e.target.value)} options={["", ...(inst.includes(value) || !value ? inst : [value, ...inst])]} />
    ) : (
      <Input value={value} aria-label={name} placeholder="Voice name" onChange={(e) => onChange(e.target.value)} />
    );
  return (
    <Section id="al-do" title="What happens on an alert" description="Tick what each severity does. Every alert also shows on the reading page.">
      <div className="m-matrix">
      <Table caption="Actions per severity" cols={[{ label: "Severity" }, ...kinds.map((k) => ({ label: label(k) }))]}>
        {SEVERITIES.map((sev) => (
          <tr key={sev}>
            <td><SevMark sev={sev} /></td>
            {kinds.map((k) => (
              <td key={k} className="m-cell-check">
                <input type="checkbox" className="m-tick" checked={matrix[sev].includes(k)} onChange={() => toggle(sev, k)} aria-label={`${SEV_WORD[sev]}: ${label(k)}`} />
              </td>
            ))}
          </tr>
        ))}
      </Table>
      </div>
      <div className="m-form m-form-3">
        <Field id="al-nl" label="Notifications in" hint="The language notifications use, when its translation arrives in time.">
          <Input id="al-nl" value={notifyLang} list="m-langs" onChange={(e) => setNotifyLang(e.target.value)} />
        </Field>
      </div>
      <Advanced id="alert-delivery">
        <div className="m-form m-form-3">
          <Field id="al-wait" label="Wait for translations (s)" hint="After that, the alert goes out in the source language.">
            <Input id="al-wait" type="number" min={0} max={120} value={wait} onChange={(e) => setWait(e.target.value)} />
          </Field>
          <Field id="al-cool" label="Cooldown (min)" hint="Repeats of a rule within this window fold into one alert.">
            <Input id="al-cool" type="number" min={1} max={1440} value={cool} onChange={(e) => setCool(e.target.value)} />
          </Field>
        </div>
        <h4 className="m-sub-h">Voices for spoken alerts</h4>
        <Table caption="Voices" cols={[{ label: "Language", width: "18%" }, { label: "Voice", width: "160px" }, { label: "Intro", width: "28%" }, { label: "Fallback phrase" }]}>
          {voices.map((l) => (
            <tr key={l}>
              <td>{langName(l)}</td>
              <td>{voiceInput(cur(l).voice, (x) => setVc({ ...vc, [l]: { ...cur(l), voice: x } }), `Voice for ${langNameEn(l)}`)}</td>
              <td><Input value={cur(l).intro || ""} lang={l} aria-label={`Intro in ${langNameEn(l)}`} onChange={(e) => setVc({ ...vc, [l]: { ...cur(l), intro: e.target.value } })} /></td>
              <td><Input value={cur(l).fallback || ""} lang={l} aria-label={`Fallback in ${langNameEn(l)}`} className="m-grow" onChange={(e) => setVc({ ...vc, [l]: { ...cur(l), fallback: e.target.value } })} /></td>
            </tr>
          ))}
          <tr className="m-add-row">
            <td><Input value={nl.lang} list="m-langs" placeholder="Add a language" aria-label="New voice language" size={12} onChange={(e) => setNl({ ...nl, lang: e.target.value })} /></td>
            <td>{voiceInput(nl.voice, (x) => setNl({ ...nl, voice: x }), "New voice")}</td>
            <td className="m-muted">–</td>
            <td><Input value={nl.fallback} placeholder="A fixed phrase in that language, read when no checked translation exists" aria-label="New fallback phrase" className="m-grow" onChange={(e) => setNl({ ...nl, fallback: e.target.value })} /></td>
          </tr>
        </Table>
        <p className="m-hint">System voices (System Settings → Accessibility → Spoken Content) are for local playback only. Only translations checked to be in the right language are read aloud; otherwise the fixed fallback phrase is.</p>
      </Advanced>
      <div className="m-actions">
        <ActButton variant="primary" run={save} done="Alert settings saved">Save alert settings</ActButton>
        <LinkButton href="#/settings/connections" variant="ghost" icon="send">Webhooks and pushes</LinkButton>
      </div>
    </Section>
  );
}
