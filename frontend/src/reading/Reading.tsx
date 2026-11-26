// The reading area: transcript columns, or search results, or Ask; on phones the station list.
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { S, running } from "../lib/live";
import { P, visible } from "../lib/prefs";
import { MAX_COLS, type Route } from "../lib/router";
import { useTopics } from "../lib/store";
import { V, setShown } from "../lib/view";
import { Column } from "./Column";
import { Ask } from "./Ask";
import { FirstRun } from "./FirstRun";
import { Search } from "./Search";
import { PhoneList, StationStrip } from "./PhoneList";
import { NoneRunning } from "./Rack";
import { T } from "./strings";

/** How many columns fit: each needs about 22 em of transcript text plus its margin, so larger
 * text means fewer, still readable, columns. */
function useFit(ref: React.RefObject<HTMLElement | null>) {
  const [fit, setFit] = useState(2);
  useTopics("prefs");
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const read = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--read")) || 17;
      setFit(Math.max(1, Math.min(MAX_COLS, Math.floor(el.clientWidth / (read * 22 + 64)))));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, P.size]);
  return fit;
}

export function Reading({ route }: { route: Route }) {
  useTopics("state", "status", "prefs", "narrow");
  const main = useRef<HTMLDivElement>(null);
  const fit = useFit(main);

  let ids: string[] = [];
  if (route.kind === "cols") ids = route.ids.filter((id) => Object.hasOwn(S.byId, id)); // not "constructor"
  else if (route.kind === "live" && !V.narrow)
    ids = S.stations.filter((s) => visible(s.id) && running(s.id)).slice(0, fit).map((s) => s.id);
  if (V.narrow) ids = ids.slice(0, 1);

  useEffect(() => setShown(route.kind === "find" || route.kind === "ask" ? [] : ids));

  let content: React.ReactNode;
  if (route.kind === "find") content = <Search q={route.q} />;
  else if (route.kind === "ask") content = <Ask />;
  else if (V.narrow && !ids.length) content = <PhoneList />;
  else if (!ids.length)
    content = S.loaded ? (
      <p className="reading-empty">
        {S.stations.some((s) => visible(s.id) && running(s.id)) ? T.chooseStation
          : <NoneRunning off={S.stations.filter((s) => visible(s.id) && !running(s.id)).length} />}
      </p>
    ) : null;
  else
    content = (
      <>
        {V.narrow && <StationStrip current={ids[0]} />}
        <div className="cols" style={{ "--n": ids.length } as React.CSSProperties}>
          {ids.map((id) => <Column key={id} id={id} onlyOne={ids.length === 1 && V.narrow} />)}
        </div>
      </>
    );
  return (
    <main ref={main} className="reading" id="main" tabIndex={-1}>
      {route.kind !== "find" && route.kind !== "ask" && <FirstRun />}
      {content}
    </main>
  );
}

