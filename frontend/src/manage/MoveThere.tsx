// A station its receiver doesn't hear where it is tuned now (`out_of_reach`): the button that
// starts it, the server moving the receiver first to where it hears it (`reach_center_mhz`).
// Moving it may leave other stations out; the server then asks, and the same press is sent again
// on yes. Start pressed for it in a station list opens its column and presses this button.
import { useEffect, useRef, useState } from "react";
import { ActButton, Button, act, toast } from "../components/ui";
import { S } from "../lib/live";
import { useTopics } from "../lib/store";
import { ApiError, post } from "./api";
import { and } from "./bands";
import { rxName } from "./Receivers";

/** What starting a station answers (`POST /api/manage/stations/<id>/start`): after a move, where
 * its receiver went, whether listening by hand on it ended, and why the station didn't start if
 * it didn't. */
interface Started {
  ok: boolean;
  receiver?: { id: string; center_mhz: number; moved: boolean };
  live_closed?: boolean;
  start_error?: string | null;
}

/** `s`: the station, by id and name. `device`: its receiver. `onLiveClosed`: listening by hand
 * on `device` ended for the move, for a page that shows it. */
export default function MoveThere({ s, device, onDone, onLiveClosed }: {
  s: { id: string; name: string }; device: string; onDone: () => Promise<unknown> | void; onLiveClosed?: (dev: string) => void;
}) {
  const [asked, setAsked] = useState<{ question: string; stops: string[] } | null>(null);
  // The question replaces the button, and the button the question: the focus goes along.
  const box = useRef<HTMLDivElement>(null);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) { first.current = false; return; }
    box.current?.querySelector<HTMLElement>("button")?.focus();
  }, [!!asked]);
  const move = async (confirmed: boolean) => {
    setAsked(null);
    let a: Started;
    try {
      a = await post<Started>(`/stations/${s.id}/start`, { retune: confirmed });
    } catch (e) {
      if (e instanceof ApiError && e.code === "retune" && !confirmed) { setAsked({ question: e.message, stops: e.data.holders || [] }); return; }
      throw e;
    }
    if (a.live_closed) onLiveClosed?.(device);
    const went = a.receiver?.moved ? `Receiver ${rxName(device)} moved to ${+a.receiver.center_mhz.toFixed(3)} MHz` : null;
    if (a.start_error) toast(`${went ? `${went}, but ` : ""}${s.name} didn't start: ${a.start_error}`, true);
    else toast(`${went ? `${went}: ` : ""}${s.name} is running${a.live_closed ? `, and listening by hand on Receiver ${rxName(device)} ended` : ""}`);
    await onDone();
  };
  // Start pressed for it in a station list (`startThere`): the same press.
  const pressed = useTopics("startThere");
  useEffect(() => {
    if (S.startThere !== s.id) return;
    S.startThere = null;
    act(() => move(false));
  }, [pressed, s.id]);
  return (
    <div ref={box}>
      {!asked ? (
        <div className="m-actions"><ActButton size="sm" variant="secondary" run={() => move(false)}>Move Receiver {rxName(device)} there</ActButton></div>
      ) : (
        <>
          <p role="status">{asked.question}</p>
          <div className="m-actions">
            <ActButton size="sm" variant="secondary" run={() => move(true)}>{asked.stops.length ? `Stop ${and(asked.stops)} and move it` : "Move it"}</ActButton>
            <Button size="sm" variant="ghost" onClick={() => setAsked(null)}>Keep it as it is</Button>
          </div>
        </>
      )}
    </div>
  );
}
