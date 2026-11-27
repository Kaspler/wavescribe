// Listen: every station, from the internet or a receiver, as one list to play, with the one volume
// for all; then what the receivers found here, and the receiver tuned by hand. One sound plays at a
// time.
import { A, MAX_VOLUME, playLive, setVolume, startAndPlay, stopAudio } from "../lib/audio";
import { langNameEn } from "../lib/lang";
import { S, running, startStation } from "../lib/live";
import { stateOf } from "../lib/station";
import { useTopics } from "../lib/store";
import type { Station } from "../lib/types";
import { Icon } from "../components/Icon";
import { Button, Lamp, Section, cx } from "../components/ui";
import { T } from "../reading/strings";
import { Listen } from "./Listen";

export function ListenPage() {
  useTopics("state", "status", "audio", "conn");
  return (
    <>
      <Section id="ls-h" title="My stations" description="One click plays a station; a stopped one starts only to play, and Start writes it down. Another click stops it."
               actions={<Volume />}>
        {S.stations.length ? (
          <ul className="ls-list">{S.stations.map((st) => <PlayRow key={st.id} st={st} />)}</ul>
        ) : (
          <p className="m-line">No station yet. <a className="rd-link" href="#/add">Find stations</a> on the air or on the internet.</p>
        )}
      </Section>
      <Listen />
    </>
  );
}

function Volume() {
  return (
    <label className="ls-volume">
      <Icon name="speaker" size={14} />
      <span>Volume</span>
      <input type="range" min={0} max={MAX_VOLUME} step={0.05} value={A.volume} onChange={(e) => setVolume(+e.target.value)} />
    </label>
  );
}

/** Where a station comes from, in a few words. */
function source(st: Station): string {
  if (st.kind === "sdr") return st.fm_mhz?.length ? `radio, ${st.fm_mhz.join(", ")} MHz` : "radio";
  if (st.kind === "remote") return "another Wavescribe, text only";
  return "internet";
}

function PlayRow({ st }: { st: Station }) {
  const on = running(st.id);
  const playing = A.playing === st.id;
  const live = S.conn === "live";
  // Another Wavescribe sends text, not sound; a station that can't start says why.
  const why = st.kind === "remote" ? "Text only: no sound crosses from another Wavescribe."
    : on ? (S.live[st.id]?.connected ? null : stateOf(st.id).detail || "Not connected yet.")
    : !S.canControl ? "Only the Wavescribe machine can start stations."
    : st.disabled ? "Switched off under Settings, Stations."
    : !st.available ? st.reason || "Unavailable."
    : !S.status?.ready ? "Wavescribe is still starting." : null;
  const state = on ? (st.listen_only_run ? T.playingOnly : stateOf(st.id).label) : "stopped";
  return (
    <li className={cx("ls-row", playing && "is-playing")}>
      <button type="button" className="ls-main" aria-pressed={playing} disabled={!playing && (!!why || !live)}
              onClick={() => (playing ? stopAudio() : on ? playLive(st.id) : startAndPlay(st.id))}
              title={why || undefined}
              aria-label={`${playing ? "Stop" : on ? "Play" : "Start and play"} ${st.name}`}>
        <Lamp state={on && live ? stateOf(st.id).lamp : "off"} />
        <span className="ls-text">
          <span className="ls-name" dir="auto">{st.name}</span>
          <span className="ls-sub">{langNameEn(st.language)} · {source(st)} · {playing ? "playing" : state}</span>
          {why && !playing && <span className="ls-why">{why}</span>}
        </span>
        <Icon name={playing ? "stop" : "play"} />
      </button>
      {on && st.listen_only_run && S.canControl && (
        <Button size="sm" variant="secondary" icon="power" className="ls-start" onClick={() => startStation(st.id)} aria-label={T.startWriting(st.name)}>
          {T.start}
        </Button>
      )}
    </li>
  );
}
