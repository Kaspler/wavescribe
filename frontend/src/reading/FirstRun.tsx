// Until the speech models are downloaded, every station plays without text. The reading area
// says so once, at its top, with the size of the download and the way to it.
import { useEffect, useRef, useState } from "react";
import { S, loadState } from "../lib/live";
import { type ModelsView, neededModels, totalGb } from "../lib/models";
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
  const waiting = S.manage && S.stations.some(noText);
  const [gb, setGb] = useState<number | null>(null);
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
        setGb(needs.length ? totalGb(needs) : null);
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
  return (
    <section className="firstrun" aria-labelledby="firstrun-h">
      <Icon name="download" className="firstrun-icon" />
      <div className="firstrun-text">
        <h2 id="firstrun-h">{T.firstRunTitle}</h2>
        <p>{T.firstRunBody(gb)}</p>
      </div>
      <Button variant="primary" icon="right" onClick={() => go("#/settings/languages")}>{T.getModels}</Button>
      <p className="firstrun-note">{T.byMachine}</p>
    </section>
  );
}
