// Until its speech model is downloaded, a station plays without text. The reading area says so
// once, at its top, with the size of the download and the way to it: for every station while
// none can be written down, else by name for the ones that wait, with the model they wait for
// and that the others are ready.
import { useEffect, useRef, useState } from "react";
import { S, loadState } from "../lib/live";
import { langNameEn } from "../lib/lang";
import { type Need, type ModelsView, awaitedModels, neededModels, totalGb } from "../lib/models";
import { get } from "../manage/api";
import { go } from "../lib/router";
import { noText } from "../lib/station";
import { useTopics } from "../lib/store";
import { Icon } from "../components/Icon";
import { Button } from "../components/ui";
import { T } from "./strings";

const LOOK_MS = 5000;

export function FirstRun() {
  useTopics("state");
  // Only this machine can download; readers on other devices see "no text yet" per station.
  const without = S.stations.filter(noText);
  const waiting = S.manage && without.length > 0;
  // Written down already: a station whose speech model is here (another machine's comes as text).
  const ready = S.stations.filter((s) => s.transcribe && !!s.asr_model && s.kind !== "remote" && s.available && !s.disabled).length;
  const [needs, setNeeds] = useState<Need[] | null>(null);
  const seen = useRef("");
  useEffect(() => {
    if (!waiting) return;
    let on = true;
    const look = async () => {
      if (document.hidden) return;
      try {
        const m = await get<ModelsView>("/models");
        if (!on) return;
        const needs = neededModels(m);
        setNeeds(needs);
        // The stations learn of a download only when the page loads them again: do that when
        // what is installed or planned changes, and once at the start if nothing is missing
        // any more (the models arrived while this page showed something else).
        const now = m.models.map((e) => `${e.id}:${e.installed}:${e.in_use.length}`).join(" ");
        if (now !== seen.current && (seen.current || !needs.length)) loadState().catch(() => {});
        seen.current = now;
      } catch {
        /* the next look tries again */
      }
    };
    look();
    const t = setInterval(look, LOOK_MS);
    return () => { on = false; clearInterval(t); };
  }, [waiting]);
  if (!waiting) return null;
  // Some stations are written down already: only the ones that wait are named.
  const some = ready > 0;
  const models = awaitedModels(without.map((s) => s.language), needs || []);
  const gb = !needs?.length ? null : some && models.length ? models.reduce((sum, m) => sum + (m.gb || 0), 0) : totalGb(needs);
  return (
    <section className="firstrun" aria-labelledby="firstrun-h">
      <Icon name="download" className="firstrun-icon" />
      <div className="firstrun-text">
        <h2 id="firstrun-h" dir="auto">{some ? T.firstRunSome(without.map((s) => s.name)) : T.firstRunTitle}</h2>
        <p>
          {some
            ? T.firstRunSomeBody(without.length, models.map((m) => T.speechModelFor(m.languages.map(langNameEn), m.id)), gb, ready)
            : T.firstRunBody(gb)}
        </p>
      </div>
      <Button variant="primary" icon="right" onClick={() => go("#/settings/languages")}>{T.getModels}</Button>
      <p className="firstrun-note">{T.byMachine}</p>
    </section>
  );
}
