// A station its receiver doesn't hear where it is tuned now ("out_of_reach" in the receiver's
// view): the button that moves the receiver back where it hears it and starts it. Moving it may
// leave other stations out; the server then asks, and the same press is sent again on yes.
import { useState } from "react";
import { ActButton, Button, toast } from "../components/ui";
import { type Any, ApiError, post } from "./api";
import { and } from "./bands";
import { watchCentre, watchWindow } from "./catches";
import { bandAt, centreBelow } from "./dial";
import { rxName } from "./Receivers";

/** Where receiver `rx` hears station `s` again: a watch's window where the server tunes a watch,
 * else just below the frequency, off the dongle's own spike at its centre. */
function centreFor(rx: Any, s: Any, bands: Any[], watch: unknown, mode?: string | null): number {
  const spec = watch as string | number[] | null | undefined;
  const b = spec == null ? null : typeof spec === "string" ? bands.find((x) => x.id === spec) : bandAt(bands, spec[0]);
  return b && spec != null ? watchCentre(watchWindow(b, spec), rx.span_mhz) : centreBelow(s.mhz, mode || bandAt(bands, s.mhz)?.mode);
}

/** `rx`, `s`: the receiver and its station as /radio lists them. `watch`: the station's watch,
 * from the stations view, when it is one. */
export function MoveThere({ rx, s, bands, watch, mode, onDone }: {
  rx: Any; s: Any; bands: Any[]; watch?: unknown; mode?: string | null; onDone: () => Promise<unknown> | void;
}) {
  const [asked, setAsked] = useState<{ question: string; stops: string[] } | null>(null);
  const move = async (confirmed: boolean) => {
    setAsked(null);
    try {
      await post(`/receivers/${rx.id}`, { center_mhz: centreFor(rx, s, bands, watch, mode), retune: confirmed });
    } catch (e) {
      if (e instanceof ApiError && e.code === "retune" && !confirmed) { setAsked({ question: e.message, stops: e.data.holders || [] }); return; }
      throw e;
    }
    await post(`/stations/${s.id}/start`);
    toast(`Receiver ${rxName(rx.id)} is back where it hears ${s.name}`);
    await onDone();
  };
  if (!asked) return <div className="m-actions"><ActButton size="sm" variant="secondary" run={() => move(false)}>Move Receiver {rxName(rx.id)} there</ActButton></div>;
  return (
    <>
      <p role="status">{asked.question}</p>
      <div className="m-actions">
        <ActButton size="sm" variant="secondary" run={() => move(true)}>{asked.stops.length ? `Stop ${and(asked.stops)} and move it` : "Move it"}</ActButton>
        <Button size="sm" variant="ghost" onClick={() => setAsked(null)}>Keep it as it is</Button>
      </div>
    </>
  );
}
