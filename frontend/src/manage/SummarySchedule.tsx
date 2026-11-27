// Summaries on a schedule, under Settings, Alerts: one switch that works as it is (a summary every
// hour, of every station), then, folded, how often, which stations, the language, reading aloud
// and quiet hours. The page's schedule takes the place of alerts.toml's [digest], if the file has
// one, until the file's is chosen again (GET/POST/DELETE /api/manage/digest/schedule); the view
// doesn't say whether the file has one meanwhile. Refusals name the field they are about.
import { useEffect, useState } from "react";
import { langNameEn } from "../lib/lang";
import { whenLabel } from "../lib/time";
import { asSentence } from "../reading/digest";
import { Advanced, Button, Check, ChoiceChips, Field, Input, LangPicker, LinkButton, Section, Select, Switch, toast } from "../components/ui";
import { type Any, ApiError, del, get, knownLangs, post, useEvery, useLoad } from "./api";
import { type Form, type View, type Where, bodyOf, everyWords, formOf, hasVoice, whereOf } from "./schedule";

const fileName = (v: View) => (v.file ? v.file.split(/[\\/]/).pop() : "alerts.toml");
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

export function SummarySchedule({ stations }: { stations: Any[] }) {
  const { data: v, error, reload, setData } = useLoad(() => get<View>("/digest/schedule"));
  // When the next one comes moves on after each: the view is asked for again now and then.
  useEvery(30_000, reload);
  const [form, setForm] = useState<Form | null>(null);
  const [dirty, setDirty] = useState(false);
  const [pick, setPick] = useState(false);
  const [trouble, setTrouble] = useState<{ where: Where | "switch"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!v || dirty) return;
    const f = formOf(v);
    setForm(f);
    setPick(f.stations.length > 0);
  }, [v, dirty]);

  if (!v || !form) {
    return (
      <Section id="al-sum" title="Summaries on a schedule">
        <p className="m-loading">{error ? `Couldn't load the schedule: ${error}` : "Loading…"}</p>
      </Section>
    );
  }
  const file = fileName(v);
  const edit = (f: Partial<Form>) => { setForm({ ...form, ...f }); setDirty(true); setTrouble(null); };
  const language = form.language || v.language;
  const voiced = hasVoice(v.voices, language);
  const send = async (enabled: boolean, from: "switch" | "form") => {
    setBusy(true);
    setTrouble(null);
    try {
      const fresh = await post<View>("/digest/schedule", bodyOf({ ...form, speak: form.speak && voiced }, enabled));
      setDirty(false);
      setData(fresh);
      toast(!enabled ? "Summaries on a schedule are off" : from === "switch" ? "Summaries on a schedule are on" : "Schedule saved");
    } catch (e) {
      const where = e instanceof ApiError ? whereOf(e.data) : "form";
      setTrouble({ where: from === "switch" ? "switch" : where, text: asSentence((e as Error).message) });
    } finally {
      setBusy(false);
    }
  };
  const giveBack = async () => {
    setBusy(true);
    try {
      const fresh = await del<View>("/digest/schedule");
      setDirty(false);
      setData(fresh);
      toast(fresh.source === "file" ? `${file}'s [digest] applies` : `${file} has no [digest]: summaries on a schedule are off`);
    } catch (e) {
      toast(asSentence((e as Error).message), true);
    } finally {
      setBusy(false);
    }
  };
  const fieldTrouble = (where: Where, id: string) =>
    trouble?.where === where ? <p className="m-trouble" id={`${id}-err`} role="alert">{trouble.text}</p> : null;
  const invalid = (where: Where, id: string) =>
    trouble?.where === where ? { "aria-invalid": true, "aria-describedby": `${id}-err` } : {};
  const every = (v.schedule && !dirty ? v.schedule.every_minutes : form.every);
  const noneChosen = pick && !form.stations.length;

  return (
    <Section id="al-sum" title="Summaries on a schedule"
             description="A few points the local model picks out of what was said since the last summary, as a notification without the alerts' sound. Experimental: it can miss things or get them wrong, and it is no warning.">
      {!v.alerts ? (
        <p className="m-line">They go out the way alerts do, and alerts are off in this run: start Wavescribe without --no-alerts to have them.</p>
      ) : (
        <>
          <div className="m-row">
            <Switch label={`A summary ${everyWords(every)}`} checked={v.enabled} onChange={(on) => { if (!busy) send(on, "switch"); }} />
          </div>
          {trouble?.where === "switch" && <p className="m-trouble" role="alert">{trouble.text}</p>}
          <Status v={v} />
          {v.digest && (
            <div className="m-trouble">
              <p>Summaries can't be made in this run: {v.digest}. {v.digest_code === "no_store" ? "Start Wavescribe without --no-store." : v.enabled ? "Each one due says so instead." : ""}</p>
              {v.digest_code === "no_llm" && <LinkButton size="sm" variant="secondary" icon="right" href="#/settings/languages">Languages and models</LinkButton>}
            </div>
          )}
          {v.error && (
            <p className="m-trouble">
              {v.source === "file" ? `${file}'s [digest] can't be used: ${v.error}. Setting it here takes its place.` : asSentence(v.error)}
            </p>
          )}
          {v.source === "file" && <p className="m-hint">Set in {file}, under [digest]. A change here takes the place of the file's.</p>}
          {v.source === "page" && (
            <div className="m-row">
              <span className="m-hint rd-inline">Set on this page, in place of anything under [digest] in {file}.</span>
              <Button size="sm" variant="ghost" icon="refresh" disabled={busy} onClick={giveBack}>Use {file}'s instead</Button>
            </div>
          )}
          <Advanced id="digest-schedule" label="How often, which stations, language, reading aloud, quiet hours">
            <div className="m-form m-form-3">
              <Field id="ds-every" label="How often" hint="At whole periods of the clock: every 15 minutes is at :00, :15, :30 and :45.">
                <Select id="ds-every" value={String(form.every)} onChange={(e) => edit({ every: +e.target.value })} {...invalid("every", "ds-every")}
                        options={v.every_minutes_choices.map((n) => [String(n), cap(everyWords(n))] as [string, string])} />
                {fieldTrouble("every", "ds-every")}
              </Field>
              <Field id="ds-lang" label="Language" hint="The notifications' language, unless another is chosen.">
                <LangPicker id="ds-lang" value={language} options={knownLangs()}
                            onChange={(tag) => edit({ language: tag === v.language ? "" : tag })} />
                {fieldTrouble("language", "ds-lang")}
              </Field>
              <div className="field">
                <span className="field-label">Reading aloud</span>
                <Check checked={form.speak && voiced} disabled={!voiced} onChange={(on) => edit({ speak: on })}>Read it aloud on this Mac</Check>
                <p className="field-hint">
                  {voiced ? "In the voice for spoken alerts. An alert's voice goes first and cuts it short."
                    : `No voice reads ${langNameEn(language)}: add one under Voices for spoken alerts, above.`}
                </p>
                {fieldTrouble("speak", "ds-speak")}
              </div>
            </div>
            <div className="m-form">
            <div className="field field-wide">
              <span className="field-label" id="ds-st">Stations</span>
              <Check checked={!pick} onChange={(all) => { setPick(!all); edit({ stations: all ? [] : form.stations }); }}>Every station</Check>
              {pick && (
                <ChoiceChips label="Stations to summarise" value={form.stations} onChange={(ids) => edit({ stations: ids })}
                             options={stations.map((s) => [s.id, s.name || s.id] as [string, string])} />
              )}
              <p className="field-hint">{noneChosen ? "Choose at least one, or every station." : "Radio set to stay on this machine is left out."}</p>
              {fieldTrouble("stations", "ds-st")}
            </div>
            <div className="field field-wide">
              <span className="field-label">Quiet hours</span>
              <Check checked={!!form.quiet} onChange={(on) => edit({ quiet: on ? ["22:00", "07:00"] : null })}>None at night</Check>
              {form.quiet && (
                <div className="m-row">
                  <label htmlFor="ds-q0">From</label>
                  <Input id="ds-q0" type="time" className="num" value={form.quiet[0]} required {...invalid("quiet", "ds-q")}
                         onChange={(e) => edit({ quiet: [e.target.value, form.quiet![1]] })} />
                  <label htmlFor="ds-q1">to</label>
                  <Input id="ds-q1" type="time" className="num" value={form.quiet[1]} required {...invalid("quiet", "ds-q")}
                         onChange={(e) => edit({ quiet: [form.quiet![0], e.target.value] })} />
                </div>
              )}
              <p className="field-hint">The first one after them covers the hours it was quiet.</p>
              {fieldTrouble("quiet", "ds-q")}
            </div>
            </div>
            <div className="m-actions">
              <Button variant="primary" disabled={busy || noneChosen || (!!form.quiet && (!form.quiet[0] || !form.quiet[1]))} onClick={() => send(true, "form")}>
                {v.enabled ? "Save" : "Save and turn on"}
              </Button>
              {dirty && <Button variant="ghost" disabled={busy} onClick={() => { setDirty(false); setTrouble(null); }}>Undo changes</Button>}
            </div>
            {trouble?.where === "form" && <p className="m-trouble" role="alert">{trouble.text}</p>}
          </Advanced>
        </>
      )}
    </Section>
  );
}

/** When the next summary comes and what it is, or why none does. */
function Status({ v }: { v: View }) {
  if (!v.enabled) return null;
  const t = v.schedule;
  if (!v.running || v.next_at == null) return <p className="m-hint">On, but not running.</p>;
  const what = [
    t && t.stations.length ? `${t.stations.length} station${t.stations.length === 1 ? "" : "s"}` : "every station",
    t ? `in ${langNameEn(t.language)}` : null,
    t?.speak ? "read aloud" : null,
  ].filter(Boolean).join(", ");
  const quiet = t?.quiet_hours ? ` None from ${t.quiet_hours[0]} to ${t.quiet_hours[1]}.` : "";
  return <p className="m-hint">Next at {whenLabel(v.next_at)}: what was said since the one before, {what}.{quiet}</p>;
}
