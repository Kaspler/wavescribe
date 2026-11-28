// The reading area: transcript columns, or search results, or Ask; on phones the station list.
// A summary opens beside the columns, at their left (on phones it is a page of its own).
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
import { FocusedAlert } from "./Alerts";
import { PhoneList, StationStrip } from "./PhoneList";
import { NoneRunning } from "./Rack";
import { T } from "./strings";
import { SummaryPanel } from "./Summary";
import { Sm } from "./summaries";
import { cx } from "../components/ui";

/** How many columns fit: each needs about 22 em of transcript text plus its margin, so larger
 * text means fewer, still readable, columns. Beside a summary 18 em will do, so that opening one
 * usually leaves the columns being read where they were. */
function useFit(ref: React.RefObject<HTMLElement | null>, beside: boolean) {
  const [fit, setFit] = useState(2);
  useTopics("prefs");
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const read = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--read")) || 17;
      setFit(Math.max(1, Math.min(MAX_COLS, Math.floor(el.clientWidth / (read * (beside ? 18 : 22) + 64)))));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, P.size, beside]);
  return fit;
}

export function Reading({ route }: { route: Route }) {
  useTopics("state", "status", "prefs", "narrow", "summary", "focus");
  // Beside the columns, a summary's own link is All live with the summary open.
  const alone = route.kind === "summary" && V.narrow;
  const allLive = route.kind === "live" || (route.kind === "summary" && !V.narrow);
  const beside = !V.narrow && !!Sm.job && (Sm.open || route.kind === "summary") && (allLive || route.kind === "cols");
  const area = useRef<HTMLDivElement>(null);
  const fit = useFit(area, beside);
  // All live shows what fits at a readable width, and says so when that isn't every station;
  // "Show all" lays every one out side by side, to scroll across.
  const [all, setAll] = useState(false);
  const live = S.stations.filter((s) => visible(s.id) && running(s.id)).map((s) => s.id);
  const more = allLive && !V.narrow && live.length > fit;

  let ids: string[] = [];
  if (route.kind === "cols") ids = route.ids.filter((id) => Object.hasOwn(S.byId, id)); // not "constructor"
  else if (allLive && !V.narrow) ids = all ? live : live.slice(0, fit);
  if (V.narrow) ids = ids.slice(0, 1);

  useEffect(() => setShown(route.kind === "find" || route.kind === "ask" || alone ? [] : ids));

  let content: React.ReactNode;
  if (route.kind === "find") content = <Search q={route.q} />;
  else if (route.kind === "ask") content = <Ask />;
  else if (alone) content = <SummaryPanel phone />;
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
        {V.narrow && <FocusedAlert station={ids[0]} />}
        {more && (
          <p className="cols-note">
            {all ? T.showingAll(live.length) : T.showingOf(fit, live.length)} ·{" "}
            <button type="button" onClick={() => setAll(!all)}>{all ? T.fitScreen : T.showAll}</button>
          </p>
        )}
        <div className={cx("cols", more && all && "is-all")} style={{ "--n": ids.length } as React.CSSProperties}>
          {ids.map((id) => <Column key={id} id={id} onlyOne={ids.length === 1 && V.narrow} />)}
        </div>
      </>
    );
  return (
    <main className={cx("reading", beside && "has-sum")} id="main" tabIndex={-1}>
      {beside && <SummaryPanel />}
      <div ref={area} className="reading-area">
        {route.kind !== "find" && route.kind !== "ask" && !alone && <FirstRun />}
        {content}
      </div>
    </main>
  );
}

