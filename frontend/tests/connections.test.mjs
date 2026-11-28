// An ntfy connection's form: the server and topic its URL is made of, a topic nobody guesses,
// and the token as the Authorization header carries it.
import assert from "node:assert/strict";
import { test } from "node:test";
import { bearer, goodTopic, newTopic, nodeId, ntfyParts, ntfyUrl, resultWord } from "../src/manage/connections.ts";

test("an ntfy URL is the server and the topic, and back", () => {
  assert.deepEqual(ntfyParts("https://ntfy.example.org/alerts-k3q9x"), { server: "https://ntfy.example.org", topic: "alerts-k3q9x" });
  assert.deepEqual(ntfyParts("http://192.168.1.20:8080/ntfy/kitchen_1"), { server: "http://192.168.1.20:8080/ntfy", topic: "kitchen_1" });
  assert.equal(ntfyUrl("https://ntfy.example.org/ ", " alerts-k3q9x"), "https://ntfy.example.org/alerts-k3q9x");
  assert.equal(ntfyUrl("https://ntfy.example.org//", "t"), "https://ntfy.example.org/t");
  for (const bad of ["https://ntfy.example.org", "https://ntfy.example.org/", "ntfy.example.org/t", "https://h/a b", null, 3])
    assert.equal(ntfyParts(bad), null, String(bad));
});

test("a topic is letters, digits, - and _, at most 64", () => {
  assert.ok(goodTopic("alerts-k3q9x_2"));
  for (const bad of ["", "a b", "ä", "a/b", "t".repeat(65)]) assert.equal(goodTopic(bad), false, bad);
});

test("a new topic is long, random and easy to read out", () => {
  const a = newTopic(), b = newTopic();
  assert.notEqual(a, b);
  assert.match(a, /^alerts-[a-km-np-z2-9]{14}$/);
  assert.ok(goodTopic(a));
  assert.equal(newTopic((n) => new Uint8Array(n)), "alerts-aaaaaaaaaaaaaa", "every byte maps to a letter");
});

test("a token goes as a bearer token, once", () => {
  assert.equal(bearer(" tk_abc "), "Bearer tk_abc");
  assert.equal(bearer("Bearer tk_abc"), "Bearer tk_abc");
  assert.equal(bearer("bearer  tk_abc"), "Bearer tk_abc");
});

test("a mesh node is one node, never every node", () => {
  assert.equal(nodeId(" !1A2B3C4D "), "!1a2b3c4d");
  for (const bad of ["!ffffffff", "1a2b3c4d", "!1a2b3c4", "!1a2b3c4dd", "^all", ""]) assert.equal(nodeId(bad), null, bad);
});

test("the log says acknowledged, not acknowledged or not sent for a mesh node, the status for a webhook", () => {
  assert.equal(resultWord({ ok: true, status: null, attempts: 1 }, true), "acknowledged");
  assert.equal(resultWord({ ok: false, status: null, attempts: 1 }, true), "not acknowledged");
  assert.equal(resultWord({ ok: false, status: null, attempts: 0 }, true), "not sent");
  assert.equal(resultWord({ ok: true, status: 200, attempts: 1 }, false), "200");
  assert.equal(resultWord({ ok: false, status: null, attempts: 6 }, false), "failed");
  assert.equal(resultWord({ ok: true, status: null, attempts: 1 }, false), "ok");
});
