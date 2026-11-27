// A station's "more" menu, the same from the station list and from a column's head; a column's
// also asks what the station is talking about now, or for a summary of it.
import { useLayoutEffect, useRef, useState } from "react";
import { S, post, running } from "../lib/live";
import { go, setCols } from "../lib/router";
import type { Station } from "../lib/types";
import { Icon } from "../components/Icon";
import { Button, ConfirmButton, cx, useDismiss } from "../components/ui";
import { T } from "./strings";
import { Chooser } from "./Summary";
import { askNow } from "./summaries";

export function StationMenu({ st, onlyOne, side }: { st: Station; onlyOne?: boolean; side?: boolean }) {
  const [open, setOpen] = useState(false);
  // The summary's choice of window, in place of the menu.
  const [pick, setPick] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const shut = () => { setOpen(false); setPick(false); };
  useDismiss(open, shut, [btn, menu]);

  // From the station list the menu opens beside it, outside the list's scrolling box.
  useLayoutEffect(() => {
    if (!open) return setPos(null);
    if (side && btn.current && menu.current) {
      const r = btn.current.getBoundingClientRect();
      const h = menu.current.offsetHeight;
      setPos({ left: r.right + 6, top: Math.max(8, Math.min(innerHeight - h - 8, r.top - 4)) });
    }
    // In the summary's choice, the first window: the last summary's buttons come before it.
    menu.current?.querySelector<HTMLElement>(pick ? ".sum-pick-list :is(button, input):not(:disabled)" : "[role=menuitem], button")?.focus();
  }, [open, side, pick]);

  const close = () => { shut(); btn.current?.focus(); };
  const sums = !side && S.hasStore && !S.digest;
  if (onlyOne && !S.manage && !(S.canControl && running(st.id)) && !sums) return null; // nothing to offer
  return (
    <span className="menu-anchor">
      <Button ref={btn} size="sm" variant={side ? "band" : "ghost"} icon="more" className={cx(side && "ch-btn ch-more")}
              aria-label={`More for ${st.name}`} aria-expanded={open} aria-haspopup="menu"
              onClick={() => setOpen(!open)} />
      {open && pick ? (
        <div ref={menu} className="pop sum-pop menu-pick" role="dialog" aria-label={T.summaryOf(st.name)}>
          <Chooser stations={[st.id]} one onDone={close} />
        </div>
      ) : open && (
        <div ref={menu} className={cx("pop", "menu", side && "menu-side")} role="menu" aria-label={st.name}
             style={side ? { left: pos?.left ?? -9999, top: pos?.top ?? 0 } : undefined}>
          {sums && running(st.id) && (
            <button role="menuitem" type="button" onClick={() => { close(); askNow(st.id); }}>
              <Icon name="ask" /> {T.nowAsk}
            </button>
          )}
          {sums && (
            <button role="menuitem" type="button" onClick={() => setPick(true)}>
              <Icon name="summary" /> {T.summaryOfThis}
            </button>
          )}
          {!onlyOne && (
            <button role="menuitem" type="button" onClick={() => { close(); setCols([st.id]); }}>
              <Icon name="columns" /> {T.showOnly}
            </button>
          )}
          {S.manage && (
            <button role="menuitem" type="button" onClick={() => { setOpen(false); go("#/settings/stations"); }}>
              <Icon name="gear" /> {T.stationSettings}
            </button>
          )}
          {S.canControl && running(st.id) && (
            <div className="menu-confirm">
              <p>{T.stopStationWhat(st.name)}</p>
              <ConfirmButton size="sm" variant="secondary" icon="power" confirm={T.stopStationConfirm}
                             onConfirm={async () => { await post(`/api/stations/${st.id}/stop`); close(); }}>
                {T.stopStation}
              </ConfirmButton>
            </div>
          )}
        </div>
      )}
    </span>
  );
}
