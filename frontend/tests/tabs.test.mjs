// One events stream shared by every tab: which tab holds it, and what the others hear.
import assert from "node:assert/strict";
import { test } from "node:test";
import { share } from "../src/lib/tabs.ts";

/** A browser: one lock, handed to the tabs that asked in turn, and a channel that delivers to
 * every other tab, as BroadcastChannel does. */
function browser({ locks = true, channel = true } = {}) {
  const tabs = [];
  const waiting = [];
  let holder = null;
  const give = () => {
    if (holder || !waiting.length) return;
    holder = waiting.shift();
    holder.lead();
  };
  function tab(name) {
    const t = { name, log: [], streams: [], listeners: [], open: true };
    const env = {
      acquire: locks
        ? (lead, follow) => {
            const w = { tab: t, lead };
            if (holder) {
              follow();
              waiting.push(w);
            } else {
              waiting.push(w);
              give();
            }
          }
        : null,
      channel: channel
        ? {
            post: (m) => tabs.filter((o) => o !== t && o.open).forEach((o) => o.listeners.forEach((f) => f(structuredClone(m)))),
            listen: (f) => t.listeners.push(f),
          }
        : null,
      open: (sink) => t.streams.push(sink),
    };
    tabs.push(t);
    t.shared = share(env, {
      event: (n, d) => t.log.push(`event ${n} ${d}`),
      conn: (c) => t.log.push(`conn ${c}`),
      opened: () => t.log.push("load state"),
      stations: () => t.log.push("reload stations"),
    });
    t.close = () => {
      t.shared.leave();
      t.open = false;
      const i = waiting.findIndex((w) => w.tab === t);
      if (i >= 0) waiting.splice(i, 1);
      if (holder?.tab === t) {
        holder = null;
        give();
      }
    };
    return t;
  }
  return { tab };
}

test("the first tab opens the stream; the next one opens none and hears it", () => {
  const b = browser();
  const one = b.tab("one");
  assert.equal(one.streams.length, 1);
  assert.equal(one.shared.leading(), true);
  const [stream] = one.streams;
  stream.conn("live");
  stream.opened();

  const two = b.tab("two");
  assert.equal(two.streams.length, 0);
  assert.equal(two.shared.leading(), false);
  // it loads its own state at once, and the leader tells it the stream is live
  assert.deepEqual(two.log, ["load state", "conn live"]);

  stream.event("utterance", '{"id":1}');
  assert.deepEqual(one.log.at(-1), 'event utterance {"id":1}');
  assert.deepEqual(two.log.at(-1), 'event utterance {"id":1}');
});

test("when the tab holding the stream goes, the next tab opens it and every tab reloads", () => {
  const b = browser();
  const one = b.tab("one");
  one.streams[0].conn("live");
  const two = b.tab("two");
  const three = b.tab("three");
  two.log.length = three.log.length = 0;

  one.close();
  assert.deepEqual(two.log, ["conn reconnecting"]);
  assert.equal(two.streams.length, 1, "the tab that asked first takes over");
  assert.equal(three.streams.length, 0);

  const [stream] = two.streams;
  stream.conn("live");
  stream.opened();
  assert.deepEqual(three.log, ["conn reconnecting", "conn live", "load state"]);
  stream.event("status", "{}");
  assert.equal(three.log.at(-1), "event status {}");

  two.close();
  assert.equal(three.streams.length, 1);
  assert.equal(three.shared.leading(), true);
});

test("a tab that isn't leading going away changes nothing", () => {
  const b = browser();
  const one = b.tab("one");
  const two = b.tab("two");
  const three = b.tab("three");
  two.close();
  one.close();
  assert.equal(three.streams.length, 1, "the lock skips the tab that went");
});

test("without locks or a channel, every tab keeps its own stream", () => {
  for (const opts of [{ locks: false }, { channel: false }]) {
    const b = browser(opts);
    const one = b.tab("one");
    const two = b.tab("two");
    assert.deepEqual([one.streams.length, two.streams.length], [1, 1]);
    one.streams[0].event("utterance", "{}");
    assert.equal(two.log.length, 0, "nothing is passed on");
  }
});

test("a change of stations in one tab reaches the others", () => {
  const b = browser();
  const one = b.tab("one");
  const two = b.tab("two");
  const three = b.tab("three");
  two.shared.changed();
  assert.equal(one.log.at(-1), "reload stations");
  assert.equal(three.log.at(-1), "reload stations");
  assert.notEqual(two.log.at(-1), "reload stations");
});

test("the stream being held back reaches every tab", () => {
  const b = browser();
  const one = b.tab("one");
  const two = b.tab("two");
  one.streams[0].conn("blocked");
  assert.equal(two.log.at(-1), "conn blocked");
});
