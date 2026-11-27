// Move Receiver A there, from the reading view: a station whose receiver is tuned where it doesn't
// hear it, by its id. Loads where the receivers are and what the band plan holds, then offers the
// same button as Settings, Stations (MoveThere).
import type { Station } from "../lib/types";
import { get, refreshPage, useLoad } from "./api";
import { MoveThere } from "./MoveThere";

export default function MoveStation({ st }: { st: Station }) {
  const { data: radio, reload } = useLoad(() => get("/radio"), [st.id, st.device]);
  const rx = radio?.receivers?.find((r: { id: string }) => r.id === st.device);
  const s = rx?.stations?.find((x: { id: string }) => x.id === st.id);
  if (!rx || !s) return null;
  return <MoveThere rx={rx} s={s} bands={radio.bands} watch={st.watch} onDone={async () => { await reload(); await refreshPage(); }} />;
}
