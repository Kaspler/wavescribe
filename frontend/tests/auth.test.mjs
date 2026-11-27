// A read token for a Wavescribe reached through a proxy: sent with reads, traded for a one-time
// ticket where a header can't go, forgotten when refused, and asked for again.
import assert from "node:assert/strict";
import { test } from "node:test";

/** A server as the page sees it: `needs` says whether reading takes a token, `tokens` which are
 * accepted. Every request is logged as "METHOD path auth". */
function server({ needs = true, tokens = ["good"] } = {}) {
  const log = [];
  let n = 0;
  const refuse = () => new Response(JSON.stringify({ error: "a feed token is needed", token_required: true }), { status: 401 });
  globalThis.fetch = async (url, init = {}) => {
    const auth = new Headers(init.headers).get("Authorization");
    const token = auth?.startsWith("Bearer ") ? auth.slice(7) : null;
    log.push(`${init.method || "GET"} ${url} ${token || "-"}`);
    const ok = tokens.includes(token);
    if (url === "/feed/v1/ticket") return ok ? Response.json({ ticket: `wsk_${++n}`, expires_in: 60 }) : refuse();
    if (needs && !ok) return refuse();
    return Response.json([]);
  };
  return log;
}

/** A browser's storage, and a fresh copy of the module that reads it on load. */
let copy = 0;
async function page(stored = {}) {
  const box = new Map(Object.entries(stored));
  globalThis.localStorage = {
    getItem: (k) => box.get(k) ?? null,
    setItem: (k, v) => box.set(k, String(v)),
    removeItem: (k) => box.delete(k),
  };
  const auth = await import(`../src/lib/auth.ts?copy=${++copy}`);
  let told = 0;
  auth.onWanted(() => told++);
  return { auth, box, told: () => told };
}

test("without a token, reads go as they are, and a refusal asks for one", async () => {
  const log = server();
  const { auth, told } = await page();
  assert.equal(await auth.ticketed("/api/events"), "/api/events");
  const r = await auth.fetchRead("/api/state");
  assert.equal(r.status, 401);
  assert.deepEqual(log, ["GET /api/state -"]);
  assert.equal(auth.wanted(), "token");
  assert.equal(told(), 1);
});

test("a token that is accepted is kept, sent with reads, and traded for a ticket per stream", async () => {
  const log = server();
  const { auth, box } = await page();
  assert.equal(await auth.tryToken("bad"), false);
  assert.equal(box.size, 0, "a refused token isn't kept");
  assert.equal(await auth.tryToken("good"), true);
  assert.equal(box.get("wavescribe.token"), "good");
  assert.equal(auth.wanted(), null);

  await auth.fetchRead("/api/history?station=x", { cache: "no-store" });
  assert.equal(log.at(-1), "GET /api/history?station=x good");
  assert.equal(await auth.ticketed("/api/events"), "/api/events?ticket=wsk_1");
  assert.equal(await auth.ticketed("/api/events?since_id=7"), "/api/events?since_id=7&ticket=wsk_2", "a new ticket each time");
  assert.ok(log.every((l) => !l.includes("?token=")), "the token never goes into an address");
});

test("a kept token that was revoked is forgotten, and the screen says it isn't accepted any more", async () => {
  const log = server({ tokens: [] });
  const { auth, box, told } = await page({ "wavescribe.token": "old" });
  assert.equal(await auth.ticketed("/api/events"), "/api/events", "the stream is tried without it");
  assert.equal(box.size, 0);
  assert.equal(auth.wanted(), null, "nothing is asked until a read is refused");
  assert.equal(await auth.streamWantsToken(), true);
  assert.equal(auth.wanted(), "refused");
  assert.equal(told(), 1);
  assert.equal(log.at(-1), "GET /api/alerts -");
});

test("a read refused with a kept token goes to the screen too", async () => {
  server({ tokens: [] });
  const { auth } = await page({ "wavescribe.token": "old" });
  await auth.fetchRead("/api/state");
  assert.equal(auth.wanted(), "refused");
  assert.equal(auth.hasToken(), false);
});

test("where reading needs no token, a stale one changes nothing", async () => {
  server({ needs: false, tokens: [] });
  const { auth, told } = await page({ "wavescribe.token": "old" });
  assert.equal(await auth.ticketed("/api/events"), "/api/events");
  assert.equal(await auth.streamWantsToken(), false, "the stream failed for another reason: it tries again");
  assert.equal(auth.wanted(), null);
  assert.equal(told(), 0);
});

test("other refusals are not mistaken for a token being needed", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ error: "too many pages" }), { status: 503 });
  const { auth } = await page();
  const r = await auth.fetchRead("/api/state");
  assert.equal(r.status, 503);
  assert.equal(auth.wanted(), null);
  globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
  assert.equal(await auth.streamWantsToken(), false);
});
