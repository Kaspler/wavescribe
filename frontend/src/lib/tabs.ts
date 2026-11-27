// One events stream for every Wavescribe tab in this browser. A browser keeps at most six
// connections open to one address, and each tab's stream holds one for good, so a seventh tab
// would wait forever. One tab, the one holding a lock, keeps the stream and passes every event on
// over a channel the other tabs listen to; when it goes, the lock goes to the next tab, which opens
// the stream in its place. Where the browser has no locks or channels, every tab keeps its own.
// The browser parts are passed in, so this can be tested without a page.

/** "blocked": the stream waited too long to open, the browser holding it back. */
export type Conn = "connecting" | "live" | "reconnecting" | "blocked";

export type Msg =
  | { k: "event"; name: string; data: string }
  | { k: "conn"; conn: Conn }
  /** The stream (re)opened: events may have been missed, so every tab reloads its state. */
  | { k: "open" }
  /** A tab arrived and asks how the stream is. */
  | { k: "hello" }
  /** A tab changed the stations. */
  | { k: "stations" };

export interface Channel {
  post(m: Msg): void;
  listen(f: (m: Msg) => void): void;
}

/** Where a stream sends what it hears. */
export interface Sink {
  event(name: string, data: string): void;
  conn(c: Conn): void;
  opened(): void;
}

export interface Env {
  /** Asks for the lock: `lead` runs once this tab holds it, which it does until the tab goes;
   * `follow` runs first when another tab holds it now. Null without locks. */
  acquire: ((lead: () => void, follow: () => void) => void) | null;
  channel: Channel | null;
  /** Opens the events stream; it keeps itself open, reconnecting as needed. */
  open(sink: Sink): void;
}

export interface Handlers extends Sink {
  /** The stations changed in another tab. */
  stations(): void;
}

export interface Shared {
  /** Whether this tab holds the stream. */
  leading(): boolean;
  /** Tells the other tabs the stations changed. */
  changed(): void;
  /** The tab is going: the others hear the stream is gone until the next tab opens it. */
  leave(): void;
}

export function share(env: Env, h: Handlers): Shared {
  const ch = env.acquire ? env.channel : null;
  let leading = false;
  let conn: Conn = "connecting";
  const lead = () => {
    leading = true;
    env.open({
      event: (name, data) => {
        h.event(name, data);
        ch?.post({ k: "event", name, data });
      },
      conn: (c) => {
        conn = c;
        h.conn(c);
        ch?.post({ k: "conn", conn: c });
      },
      opened: () => {
        h.opened();
        ch?.post({ k: "open" });
      },
    });
  };
  const shared: Shared = {
    leading: () => leading,
    changed: () => ch?.post({ k: "stations" }),
    leave: () => { if (leading) ch?.post({ k: "conn", conn: "reconnecting" }); },
  };
  if (!ch || !env.acquire) {
    lead();
    return shared;
  }
  ch.listen((m) => {
    if (m.k === "stations") return h.stations();
    if (m.k === "hello") {
      if (leading) ch.post({ k: "conn", conn });
      return;
    }
    if (leading) return; // it hears its own stream
    if (m.k === "event") h.event(m.name, m.data);
    else if (m.k === "conn") h.conn(m.conn);
    else if (m.k === "open") h.opened();
  });
  env.acquire(lead, () => {
    // The stream is already open in another tab: this one loads its state now and asks how the
    // stream is, rather than waiting for the next time it opens.
    h.opened();
    ch.post({ k: "hello" });
  });
  return shared;
}
