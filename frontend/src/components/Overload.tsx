// The note a receiver overloaded by a transmitter close by leaves, the same wherever it is said:
// a watch's column, the dial's receiver line, the hand tuner and the dial's live view. Quiet on
// purpose: it is about the reception, so it never takes an alert's colour or sound.
import { S } from "../lib/live";
import { channelOf, overloadOf, warningBeside } from "../lib/overload";
import { useTopics } from "../lib/store";
import { hm, isoTime } from "../lib/time";
import type { Overload } from "../lib/types";
import { placeWords } from "../manage/bands";
import { channelText } from "../reading/channels";
import { Icon } from "./Icon";

/** When it was last seen, the channel it was caught on, if it was, and what to do, in the server's
 * words. */
export function OverloadNote({ o }: { o: Overload }) {
  const where = channelText({ freq_hz: channelOf(o) }, placeWords);
  return (
    <p className="ovl-note">
      <Icon name="info" size={14} />
      <span>
        <span className="num"><time dateTime={isoTime(o.t)}>{hm(o.t)}</time>{where && ` · ${where}`}</span> · {o.message}
      </span>
    </p>
  );
}

/** A watch's notes from its status: what keeps it deaf, while that lasts, and its last overload. */
export function WatchNotes({ id }: { id: string }) {
  useTopics("status");
  const live = S.live[id];
  const o = overloadOf(live?.overload);
  const warning = warningBeside(live?.warning, o);
  if (!o && !warning) return null;
  return (
    <div className="ovl">
      {warning && <p className="ovl-warning">{warning}</p>}
      {o && <OverloadNote o={o} />}
    </div>
  );
}
