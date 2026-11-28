// The kinds of connection the page sets up (outputs.py): a webhook, or an ntfy topic, which is a
// webhook whose headers carry each alert's title and priority. Plain functions, so they can be
// tested without a page.

export type Kind = "webhook" | "ntfy";

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

/** The token as the Authorization header carries it, whether or not "Bearer" was pasted too. */
export function bearer(token: string): string {
  return `Bearer ${token.trim().replace(/^bearer\s+/i, "")}`;
}
