// What the server's answer for a new station says fell short of what was asked: it couldn't
// start, or it plays without text until a speech model that can write it down is there (a
// language to detect with no model that tells languages apart, for one). A plain function, so it
// can be tested without a page.

/** A station as the add routes answer it. */
export interface Made {
  name: string;
  start_error?: string | null;
  /** To be written down, but played without text for now; `reason` says why. */
  listen_only?: boolean;
  reason?: string | null;
}

/** The toast for a station just added: the server's sentence where it fell short (`bad`), else
 * `ok`. */
export function addedToast(made: Made, ok: string): { text: string; bad: boolean } {
  if (made.start_error) return { text: `${made.name} was added and didn't start: ${made.start_error}`, bad: true };
  if (made.listen_only && made.reason) return { text: `${made.name} was added. ${made.reason}`, bad: true };
  return { text: ok, bad: false };
}
