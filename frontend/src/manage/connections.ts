// The kinds of connection the page sets up (outputs.py): a webhook, an ntfy topic, which is a
// webhook whose headers carry each alert's title and priority, or one node of a LoRa mesh
// (mesh.py). Plain functions, so they can be tested without a page.

export type Kind = "webhook" | "ntfy" | "mesh";

/** A topic is the last part of an ntfy URL: letters, digits, - and _, at most 64 (outputs.py). */
const TOPIC = /^[A-Za-z0-9_-]{1,64}$/;

/** An ntfy output's URL as the form shows it: the server's address and the topic; null when the
 * URL has no topic. */
export function ntfyParts(url: unknown): { server: string; topic: string } | null {
  if (typeof url !== "string") return null;
  const m = /^(https?:\/\/[^\s/?#]+(?:\/[^\s?#]*)?)\/([^/\s?#]+)$/.exec(url.trim());
  return m && TOPIC.test(m[2]) ? { server: m[1], topic: m[2] } : null;
}

/** The URL ntfy publishes to: the server's address, without a trailing slash, then the topic. */
export function ntfyUrl(server: string, topic: string): string {
  return `${server.trim().replace(/\/+$/, "")}/${topic.trim()}`;
}

/** Whether a topic is one ntfy takes. */
export const goodTopic = (topic: string) => TOPIC.test(topic.trim());

/** A topic nobody guesses: on a server where anyone may read a topic they know the name of,
 * a long random name is what keeps it to the people it is given to. */
export function newTopic(random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  const letters = "abcdefghijkmnpqrstuvwxyz23456789"; // no 0/o or 1/l to misread
  return "alerts-" + Array.from(random(14), (b) => letters[b % letters.length]).join("");
}

/** A mesh node's id as the server takes it, "!1a2b3c4d"; null for anything else, every node's
 * address (!ffffffff, a broadcast) included. */
export function nodeId(text: string): string | null {
  const id = text.trim().toLowerCase();
  return /^![0-9a-f]{8}$/.test(id) && id !== "!ffffffff" ? id : null;
}

/** The result of a delivery as the log says it: a mesh node's acknowledgement, or a webhook's
 * HTTP status. */
export function resultWord(d: { ok?: unknown; status?: unknown; attempts?: unknown }, mesh: boolean): string {
  if (mesh) return d.ok ? "acknowledged" : d.attempts ? "not acknowledged" : "not sent";
  return d.status != null ? String(d.status) : d.ok ? "ok" : "failed";
}

/** The token as the Authorization header carries it, whether or not "Bearer" was pasted too. */
export function bearer(token: string): string {
  return `Bearer ${token.trim().replace(/^bearer\s+/i, "")}`;
}
