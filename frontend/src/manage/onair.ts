// A frequency heard on the air becomes a running station in one call (POST /radio/tune): the
// server lets a receiver tuned by hand go, keeps its centre if it hears the frequency there,
// else moves it where it hears the new station beside every station it has, then makes the
// station and starts it. Where no centre hears them all, nothing changes and the server asks.
import { type Any, ApiError, post } from "./api";

/** Made (running unless its `start_error` says why it couldn't start), or the server's question:
 * the stations a move would leave out, nothing changed yet. */
export type AddResult =
  | { station: Any }
  | { question: string; stops: string[] };

/** Adds the station `body` describes (what POST /radio/tune takes). `confirmed`: the person
 * agreed to the question a first try asked. */
export async function addOnAir(body: Any, confirmed: boolean): Promise<AddResult> {
  try {
    return { station: await post("/radio/tune", { ...body, retune: confirmed }) };
  } catch (e) {
    if (e instanceof ApiError && e.code === "retune" && !confirmed) return { question: e.message, stops: e.data.holders || [] };
    throw e;
  }
}
