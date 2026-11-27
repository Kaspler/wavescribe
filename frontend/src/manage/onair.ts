// A frequency heard on the air becomes a running station in one step, whatever its receiver was
// doing: tuned by hand (on the dial or under Listen), following other stations, or idle. The
// receiver ends up following the new station beside every station it can still hear; one it
// can't is named first, and stops only once the person agrees.
import { type Any, ApiError, get, post } from "./api";
import { type Channel, centreFor } from "./dial";

/** A station a receiver follows now, or will once listening by hand lets it go, as the channels
 * it needs: one, or for a watch the two ends of its window. */
export interface Following {
  id: string;
  name: string;
  channels: Channel[];
}

export interface AddRequest {
  /** What POST /radio/tune takes: device, mhz, mode, name, language, translate_to, acknowledge… */
  body: Any;
  /** Its demodulation, as the band plan or the person chose it. */
  mode: string;
  /** The receiver's settings: its centre and how much it hears, in MHz. */
  centre: number;
  span: number;
  /** Tuned by hand now: listening that way ends first (`letGo`). */
  live: boolean;
  letGo: () => Promise<unknown>;
  following: Following[];
  /** The person agreed to stop the stations that can't stay. */
  confirmed: boolean;
}

/** Added and running (`trouble` says why it isn't, when it was added but wouldn't start), or the
 * stations it would stop, with nothing changed yet. */
export type AddResult = { station: Any; trouble?: string } | { wouldStop: string[] };

const near = (a: number, b: number) => Math.abs(a - b) <= 0.006; // within a channel, as Heard here matches

/** The page's own station on this receiver and frequency, made by an earlier press. */
async function madeAlready(device: string, mhz: number): Promise<Any | null> {
  const { stations } = await get("/stations");
  return stations.find((s: Any) => s.device === device && s.watch == null && [...(s.fm_mhz || []), ...(s.mhz || [])].some((f: number) => near(f, mhz))) || null;
}

/** Where a receiver following `following` is set to hear `wanted` too, and which of those
 * stations stop for it: none when there is a centre that hears them all, else every one, as the
 * server stops every station on a receiver it retunes. One already on that frequency is the
 * station itself, not one beside it. */
export function planFor(wanted: Channel, following: Following[], span: number, centre: number) {
  const keeps = following.filter((f) => !f.channels.every((c) => near(c.mhz, wanted.mhz)));
  const at = centreFor([wanted, ...keeps.flatMap((f) => f.channels)], span, centre);
  return { centre: at, keeps, stops: at == null ? keeps.map((f) => f.name) : [] };
}

/** Makes the station and starts it. Until the server takes this as one call, the steps are the
 * page's: name the stations that can't stay and stop there unless agreed; end listening by
 * hand; set the receiver's centre where the new station and the ones it keeps all fit; add and
 * start the station. A second press finds the station the first one made instead of making
 * another. */
export async function addOnAir(r: AddRequest): Promise<AddResult> {
  const { device, mhz } = r.body;
  const { centre, keeps, stops } = planFor({ mhz, mode: r.mode }, r.following, r.span, r.centre);
  if (stops.length && !r.confirmed) return { wouldStop: stops };
  if (r.live) await r.letGo();
  const before = await madeAlready(device, mhz);
  if (before) return start(before);
  // Moved for the stations it keeps; with none, the server moves it to the new station itself.
  if (centre != null && keeps.length && Math.abs(centre - r.centre) > 1e-6) await post(`/receivers/${device}`, { center_mhz: centre });
  try {
    return await start(await post("/radio/tune", { ...r.body, retune: r.confirmed }));
  } catch (e) {
    if (e instanceof ApiError && e.code === "retune" && !r.confirmed) return { wouldStop: e.data.holders || [] };
    // Made, but its start was refused: it is there all the same, and a second press would
    // only be told its name is taken.
    const made = await madeAlready(device, mhz).catch(() => null);
    if (made) return { station: made, trouble: (e as Error).message };
    throw e;
  }
}

async function start(station: Any): Promise<AddResult> {
  if (station.running) return { station };
  try {
    await post(`/stations/${station.id}/start`);
    return { station: { ...station, running: true } };
  } catch (e) {
    return { station, trouble: (e as Error).message };
  }
}
