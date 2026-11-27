// Reading a Wavescribe through a reverse proxy can take a read token, which its owner makes under
// Settings, Connections on the Wavescribe machine. The page keeps it for this browser and sends it
// with every read of the page's data. The events stream can't carry a header, so it gets a
// one-time ticket for the token instead: the token itself never goes into an address, which logs
// and proxies keep. Where no token is needed (this machine, the network at home) none is asked
// for or sent. Plain functions over fetch and localStorage, so they can be tested without a page.

const KEY = "wavescribe.token";

/** Why the page shows the token screen instead of the text: no token was given yet, or the one
 * given isn't accepted (mistyped, or revoked since). Null while reading needs nothing more. */
export type Want = "token" | "refused" | null;

let token: string | null = null;
try {
  token = localStorage.getItem(KEY);
} catch {
  /* storage switched off: the token lasts until the page closes */
}
let want: Want = null;
/** A token kept here was refused since the page opened: the screen says it isn't accepted any more. */
let lost = false;
let tell: () => void = () => {};

export const wanted = (): Want => want;
export const hasToken = () => !!token;

/** `f` runs when the page goes to the token screen. */
export function onWanted(f: () => void) {
  tell = f;
}

export function keepToken(t: string | null) {
  token = t;
  try {
    if (t) localStorage.setItem(KEY, t);
    else localStorage.removeItem(KEY);
  } catch {
    /* kept for this page only */
  }
}

/** The kept token, if any, was refused: it is forgotten. */
function drop() {
  if (!token) return;
  lost = true;
  keepToken(null);
}

/** A read came back asking for a token. */
function asked() {
  drop();
  const w: Want = lost ? "refused" : "token";
  if (w !== want) {
    want = w;
    tell();
  }
}

/** `init` with the kept token in the Authorization header; as it is without one. */
export function withToken(init: RequestInit = {}): RequestInit {
  if (!token) return init;
  const headers = new Headers(init.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return { ...init, headers };
}

/** Whether an answer says a token is needed: 401 with `token_required`. */
export async function asksForToken(r: Response): Promise<boolean> {
  if (r.status !== 401) return false;
  const body = await r.clone().json().catch(() => null);
  return !!body?.token_required;
}

/** Reads the page's data with the kept token. An answer asking for a token sends the page to the
 * token screen; the caller still gets it, and fails as it would. */
export async function fetchRead(url: string, init?: RequestInit): Promise<Response> {
  const r = await fetch(url, withToken(init));
  if (await asksForToken(r)) asked();
  return r;
}

/** `url` for what can't send a header (the events stream): with a one-time ticket made from the
 * kept token, which works once and soon expires, so every (re)connection asks for a new one.
 * Without a token, `url` as it is. A token the server won't trade is forgotten and the stream
 * tried without one: where reading needs no token that works, and where it does, the refusal
 * sends the page to the token screen (see `streamWantsToken`). */
export async function ticketed(url: string): Promise<string> {
  if (!token) return url;
  const r = await fetch("/feed/v1/ticket", withToken({ method: "POST" }));
  if (r.status === 401) {
    drop();
    return url;
  }
  if (!r.ok) throw new Error(`the server answered ${r.status}`);
  const { ticket } = await r.json();
  return `${url}${url.includes("?") ? "&" : "?"}ticket=${encodeURIComponent(ticket)}`;
}

/** After the events stream was refused: whether that was for want of a token, in which case the
 * page is on the token screen. A stream can't say why it was refused; a small read can. */
export async function streamWantsToken(): Promise<boolean> {
  try {
    await fetchRead("/api/alerts");
  } catch {
    return false; // not reached at all: the stream tries again
  }
  return want !== null;
}

/** Tries a token typed on the token screen; keeps it when the server accepts it. */
export async function tryToken(t: string): Promise<boolean> {
  const r = await fetch("/api/alerts", { headers: { Authorization: `Bearer ${t}` } });
  if (await asksForToken(r)) return false;
  if (!r.ok) throw new Error(`the server answered ${r.status}`);
  keepToken(t);
  want = null;
  lost = false;
  return true;
}
