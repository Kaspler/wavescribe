// What the page offers beside a refused Start (app.py, start_station): its sentence is shown as
// the server wrote it, and its code says whether there is something to press. A plain function,
// so it can be tested without a page.

const START = /^\/api\/(?:manage\/)?stations\/([^/?#]+)\/start$/;

/** The station "Switch it on" is offered for: a start refused with code switched_off (switched off
 * on the page). Null for anything else, "unconfirmed" included: a region's confirmation is given
 * in Settings, so that sentence is only said. */
export function switchOnFor(url: string, data: unknown): string | null {
  const code = data && typeof data === "object" ? (data as { code?: unknown }).code : null;
  if (code !== "switched_off") return null;
  const m = START.exec(url);
  return m ? decodeURIComponent(m[1]) : null;
}
