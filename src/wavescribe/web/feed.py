# SPDX-License-Identifier: Apache-2.0
"""``/feed/v1``: Wavescribe as a component of something else. Text and metadata only.

    GET  /feed/v1/events      live Server-Sent Events: utterance, alert (and status every 10 s
                              with events=status); resumes after Last-Event-ID or ?since_id=,
                              or from ?since=<epoch> after a restart
    GET  /feed/v1/utterances  what was said, as JSON, NDJSON or CSV (?since_id=, ?since=,
                              ?until=, ?stations=, ?langs=, ?limit=, ?format=), in the order
                              stored: a page goes on with ?since_id=<its last id>
    GET  /feed/v1/alerts      recent alerts
    GET  /feed/v1/stations    stations, whether they run, lag
    GET  /feed/v1/node        this machine: name, URL, version, languages
    POST /feed/v1/ingest      another Wavescribe pushes its events here (a "push" token)
    POST /feed/v1/ticket      a one-time stream ticket for a read token in the header
    GET  /healthz             ok / not ready (no token needed)
    GET  /metrics             Prometheus text

From this machine, no token is needed. From elsewhere (``serve --lan``), the feed is open
until the first feed token is made on the page; from then on every request from another
machine needs one, as ``Authorization: Bearer <token>``. A client that can't set headers (a
browser's EventSource) trades it for a ticket and sends ``?ticket=``. ``?token=`` still works,
but is logged as a warning: a URL ends up in logs. docs/FEED.md is the reference.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import queue
import secrets
import threading
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from wavescribe import __version__, jsonsafe
from wavescribe.events import FEED_VERSION, alert_event, to_json, utterance_event

if TYPE_CHECKING:
    from wavescribe.web.server import Handler

log = logging.getLogger(__name__)

REPLAY_MAX = 1000  # utterances replayed on (re)connect
MAX_INGEST = 2 * 1024 * 1024
KEEPALIVE_S = 15.0  # a comment line on a live stream after this long without a write
TICKET_TTL_S = 60.0  # a ticket is for the request it was got for, made straight away
MAX_TICKETS = 256  # unused at once; past it, the oldest goes
URL_TOKEN_WARN_S = 3600.0  # a token sent in a URL is logged at most this often
_url_token_warned = [-URL_TOKEN_WARN_S]


class Tickets:
    """Short-lived, single-use stream tickets. A client that can't set headers, such as a
    browser's EventSource, trades its read token, sent in the Authorization header, for a ticket
    and puts that in the URL (``?ticket=``) instead of the token. URLs end up in logs, here, in
    browsers and on any proxy; a ticket found there is spent or expired."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: dict[str, float] = {}  # a ticket's SHA-256 -> when it expires

    def issue(self) -> str:
        ticket = "wsk_" + secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._open = {k: t for k, t in self._open.items() if t > now}
            while len(self._open) >= MAX_TICKETS:
                del self._open[next(iter(self._open))]
            self._open[_digest(ticket)] = now + TICKET_TTL_S
        return ticket

    def take(self, ticket: str | None) -> bool:
        """Whether ``ticket`` was issued and is neither used nor expired; it is used up."""
        if not ticket:
            return False
        with self._lock:
            expires = self._open.pop(_digest(ticket), None)
        return expires is not None and expires > time.monotonic()


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def _list(value: str | None) -> list[str]:
    return [v for v in (value or "").split(",") if v]


def _epoch(query: dict[str, str], key: str) -> float | None:
    """A time given as a query parameter (seconds since 1970); None when it isn't given.
    ``ValueError`` names the parameter, for the 400 the caller answers with."""
    raw = query.get(key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{key} must be a number of seconds since 1970") from None


def _segment_id(text: str | None) -> int | None:
    """A segment id as a client sends it back (Last-Event-ID, since_id); None for anything
    else. ``str.isdigit`` alone also takes digits ``int`` refuses, such as "²"."""
    return int(text) if text and text.isascii() and text.isdigit() and len(text) < 19 else None


def _cell(value: Any) -> Any:
    """A CSV cell that a spreadsheet won't run: text that starts like a formula gets a leading
    apostrophe. What was said, and station names, can come from other machines."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def token_of(h: Handler, query: dict[str, str]) -> str | None:
    auth = h.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    token = query.get("token")
    if token and time.monotonic() - _url_token_warned[0] >= URL_TOKEN_WARN_S:
        _url_token_warned[0] = time.monotonic()
        log.warning("a feed token came in a URL (?token=), which logs keep, here and on the way: "
                    "send it as Authorization: Bearer, or get a one-time ticket from "
                    "POST /feed/v1/ticket for ?ticket= (docs/FEED.md)")  # fmt: skip
    return token


def authorised(h: Handler, query: dict[str, str], scope: str) -> bool:
    # Pushing always needs a token, even from this machine: any web page open here could
    # otherwise post events (a plain-text form needs no permission) and set off alerts.
    if h._is_local() and scope != "push":
        return True
    app = h.server.hub.app
    proxied = getattr(h.server, "behind_proxy", False)  # whoever reaches the proxy: always a token
    if app is None:
        return scope == "read" and not proxied
    if scope == "push" or proxied or app.tokens_required():
        if app.check_token(token_of(h, query), scope):
            return True
        return scope == "read" and h.server.hub.tickets.take(query.get("ticket"))
    return True  # serve --lan with no tokens made: the LAN reads, as it reads the page


def handle_get(h: Handler, path: str, query: dict[str, str]) -> bool:
    if path == "/healthz":
        hub = h.server.hub
        ready = bool(hub.pipeline and hub.pipeline.ready)
        h._json({"ok": ready, "version": __version__},
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)  # fmt: skip
        return True
    if path != "/metrics" and not path.startswith("/feed/v1/"):
        return False
    if not authorised(h, query, "read"):
        h._json({"error": "a feed token is needed (Authorization: Bearer …)"},
                HTTPStatus.UNAUTHORIZED)  # fmt: skip
        return True
    if path == "/metrics":
        body = metrics(h).encode()
        h._send(HTTPStatus.OK, body, "text/plain; version=0.0.4; charset=utf-8")
    elif path == "/feed/v1/events":
        events(h, query)
    elif path == "/feed/v1/utterances":
        utterances(h, query)
    elif path == "/feed/v1/alerts":
        alerts(h, query)
    elif path == "/feed/v1/stations":
        h._json(stations(h))
    elif path == "/feed/v1/node":
        h._json(node(h))
    else:
        h._json({"error": "no such feed endpoint"}, HTTPStatus.NOT_FOUND)
    return True


def handle_post(h: Handler, path: str, query: dict[str, str]) -> bool:
    if path == "/feed/v1/ticket":
        ticket(h)
        return True
    if path != "/feed/v1/ingest":
        return False
    app = h.server.hub.app
    if app is None:
        h._json({"error": "this run can't take pushed events"}, HTTPStatus.CONFLICT)
        return True
    if not authorised(h, query, "push"):
        h._json({"error": "pushing needs a feed token with the push scope"},
                HTTPStatus.UNAUTHORIZED)  # fmt: skip
        return True
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":  # a page can't send this cross-site without asking first
        h._json({"error": "send JSON (Content-Type: application/json)"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE)  # fmt: skip
        return True
    try:
        length = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        length = -1
    if length < 0:
        h._json({"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
        return True
    if length > MAX_INGEST:
        h._json({"error": "too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        return True
    try:
        body = jsonsafe.loads(h.rfile.read(length) or b"{}")
        counts = app.ingest(body)
    except ValueError as e:  # not JSON, or an event that isn't taken: the message says which
        h._json({"error": str(e)[:300]}, HTTPStatus.BAD_REQUEST)
        return True
    except TypeError:  # a shape the checks didn't foresee: nothing internal goes back
        h._json({"error": "malformed events"}, HTTPStatus.BAD_REQUEST)
        return True
    # 200 even when some events were refused: an error status would make the sender retry
    # the whole batch and repeat the events that were taken.
    h._json(counts)
    return True


# -- endpoints ----------------------------------------------------------------------------------


def ticket(h: Handler) -> None:
    """A one-time ticket for ``?ticket=``, had only for a read token in the Authorization header:
    never one in the URL, which is what tickets keep out of logs, and never without a token, even
    from this machine (which reads without one: it has no need of a ticket)."""
    app = h.server.hub.app
    if app is None or not app.check_token(token_of(h, {}), "read"):
        h._json({"error": "a ticket is had for a read token, sent as Authorization: Bearer …"},
                HTTPStatus.UNAUTHORIZED)  # fmt: skip
        return
    h._json({"ticket": h.server.hub.tickets.issue(), "expires_in": TICKET_TTL_S})


def _context(h: Handler) -> tuple[dict[str, Any], dict[str, Any]]:
    hub = h.server.hub
    stations = {s.id: s for s in hub.config.stations}
    node_ = hub.app.node() if hub.app else {"name": "wavescribe", "url": None}
    return stations, node_


def node(h: Handler) -> dict[str, Any]:
    hub = h.server.hub
    _, n = _context(h)
    leaving = h._exports()  # another machine counts only the stations it is shown
    shown = [s for s in hub.config.stations if leaving is None or s.id in leaving.stations]
    langs = sorted({t for s in shown for t in s.translation_targets})
    return {
        **n,
        "version": __version__,
        "feed_version": FEED_VERSION,
        "stations": len(shown),
        "translation_languages": langs,
        "store": hub.store is not None,
        "ask": hub.ask_unavailable() is None,
    }


def stations(h: Handler) -> list[dict[str, Any]]:
    """The stations, without the ones whose radio stays here when another machine asks."""
    hub = h.server.hub
    leaving = h._exports()
    ok = None if leaving is None else leaving.stations
    status = hub.status()
    running = status.get("stations", {})
    lag = status.get("lag", {})
    heard = hub.store.last_heard() if hub.store is not None else {}
    return [{
        "id": s.id, "name": s.name, "language": s.language,
        "translate_to": list(s.translation_targets),
        "kind": "remote" if s.remote else "sdr" if s.sdr else "stream",
        "running": s.id in running,
        "connected": running.get(s.id, {}).get("connected"),
        "last_heard": heard.get(s.id),
        "lag": lag.get(s.id),
    } for s in hub.config.stations if ok is None or s.id in ok]  # fmt: skip


def _select(h: Handler, query: dict[str, str]) -> list[dict[str, Any]]:
    """The feed is what leaves this machine, so it carries only what may (``WebHub.exports``),
    also when a program on this machine reads it."""
    hub = h.server.hub
    leaving = hub.exports()
    ids = _list(query.get("stations")) or None
    limit = max(1, min(10000, int(query.get("limit") or 500)))
    # What may not leave is left out before the limit, so a page is never empty while more
    # follows: a client paging with since_id would otherwise stop at a run of hidden rows.
    if hub.store is None:
        rows = [r for r in hub.history() if leaving.utterance(r)]
        if ids:
            rows = [r for r in rows if r["station"] in ids]
        rows.sort(key=lambda r: r["t_start"])
        return rows[-limit:]
    if query.get("since_id"):
        until = float(query["until"]) if query.get("until") else None
        return hub.store.after(int(query["since_id"]), station_ids=ids, until=until, limit=limit,
                               exports=leaving)  # fmt: skip
    now = time.time()
    since = float(query.get("since") or now - 3600)
    until = float(query.get("until") or now + 60)
    return hub.store.between(since, until, station_ids=ids, limit=limit, exports=leaving)


def utterances(h: Handler, query: dict[str, str]) -> None:
    try:
        rows = _select(h, query)
    except (ValueError, OverflowError) as e:  # not a number, or one too large for the store
        h._json({"error": f"bad parameter: {e}"}, HTTPStatus.BAD_REQUEST)
        return
    stations_, node_ = _context(h)
    langs = _list(query.get("langs"))
    events_ = [utterance_event(r, stations_, node_, languages=langs) for r in rows]
    fmt = query.get("format", "json")
    if fmt == "ndjson":
        body = "".join(to_json(e) + "\n" for e in events_)
        h._send(HTTPStatus.OK, body.encode(), "application/x-ndjson; charset=utf-8")
    elif fmt == "csv":
        out = io.StringIO()
        cols = langs or sorted({k for e in events_ for k in e["translations"]})
        w = csv.writer(out)
        w.writerow(["id", "time", "station", "station_name", "language", "duration", "text",
                    *[f"text_{c}" for c in cols]])  # fmt: skip
        for e in events_:
            row = [e["id"], e["time"], e["station"]["id"], e["station"]["name"],
                   e["language"], e["duration"], e["text"],
                   *[e["translations"].get(c, "") for c in cols]]  # fmt: skip
            w.writerow([_cell(v) for v in row])
        h.send_response(HTTPStatus.OK)
        body = out.getvalue().encode()
        h.send_header("Content-Type", "text/csv; charset=utf-8")
        h.send_header("Content-Disposition", 'attachment; filename="wavescribe.csv"')
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
    else:
        h._json(events_)


def alerts(h: Handler, query: dict[str, str]) -> None:
    hub = h.server.hub
    stations_, node_ = _context(h)
    try:
        since = _epoch(query, "since") or 0
    except ValueError as e:
        h._json({"error": f"bad parameter: {e}"}, HTTPStatus.BAD_REQUEST)
        return
    rows = [a for a in hub.alerts(hub.exports()) if (a.get("last_at") or 0) >= since]
    h._json([alert_event(a, stations_, node_, languages=_list(query.get("langs"))) for a in rows])


def events(h: Handler, query: dict[str, str]) -> None:
    """Live events. Subscribes first, then replays what the client missed, so nothing falls
    in between; an utterance replayed is skipped when it also arrives live."""
    hub = h.server.hub
    want = set(_list(query.get("events")) or ["utterance", "alert"])
    only = set(_list(query.get("stations")))
    langs = _list(query.get("langs"))
    try:
        since = _epoch(query, "since")
    except ValueError as e:
        h._json({"error": f"bad parameter: {e}"}, HTTPStatus.BAD_REQUEST)
        return
    from wavescribe.web.server import TooManyViewers

    try:  # followers on other machines count towards the same cap as the page's
        q = hub.subscribe(lan=not h._is_local())
    except TooManyViewers:
        h._busy("too many programs on other machines are following along; try again later")
        return
    leaving = hub.exports()
    try:
        last = _segment_id(h.headers.get("Last-Event-ID") or query.get("since_id"))
        replay: list[dict[str, Any]] = []
        if "utterance" in want and hub.store is not None:
            ids = sorted(only) or None
            if last is not None:
                replay = hub.store.after(last, station_ids=ids, limit=REPLAY_MAX,
                                         exports=leaving)  # fmt: skip
            elif since is not None:
                replay = hub.store.between(since, time.time() + 60, station_ids=ids,
                                           limit=REPLAY_MAX, exports=leaving)  # fmt: skip
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")  # behind nginx: don't buffer the stream
        h.end_headers()
        h.wfile.write(b"retry: 3000\n\n")
        sent_id = last or 0
        stations_, node_ = _context(h)
        for row in replay:
            if not leaving.utterance(row):
                continue
            _write(h, "utterance", utterance_event(row, stations_, node_, languages=langs),
                   row["id"])  # fmt: skip
            sent_id = max(sent_id, row["id"] or 0)
        h.wfile.flush()
        next_status = wrote = time.monotonic()
        while True:
            now = time.monotonic()
            if "status" in want and now >= next_status:
                next_status = now + 10
                _write(h, "status", {"type": "status", "stations": stations(h),
                                     "time": time.time()})  # fmt: skip
                h.wfile.flush()
                wrote = now
            # Timed from the last write to this client: events its filter drops don't count,
            # or a stream of one station's alerts would go quiet until the client gives up.
            if now - wrote >= KEEPALIVE_S:
                h.wfile.write(b": keepalive\n\n")
                h.wfile.flush()
                wrote = now
            try:
                item = q.get(timeout=max(0.1, min(5.0, wrote + KEEPALIVE_S - now)))
            except queue.Empty:
                continue
            if item is None:
                break
            name, text = item
            if name == "utterance" and "utterance" in want:
                data = json.loads(text)
                if not data.get("final") or not data.get("text"):
                    continue
                if only and data["station"] not in only:
                    continue
                if not hub.exports().utterance(data):
                    continue
                seg = data.get("id")
                if seg is not None and seg <= sent_id:
                    continue  # already replayed
                stations_, node_ = _context(h)
                _write(h, "utterance", utterance_event(data, stations_, node_, languages=langs),
                       seg)  # fmt: skip
                if seg is not None:
                    sent_id = seg
            elif name == "alert" and "alert" in want:
                data = json.loads(text)
                if only and not only & set(data.get("stations") or []):
                    continue
                data = hub.exports().alert(data)
                stations_, node_ = _context(h)
                _write(h, "alert", alert_event(data, stations_, node_, languages=langs))
            else:
                continue
            h.wfile.flush()
            wrote = time.monotonic()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        hub.unsubscribe(q)


def _write(h: Handler, name: str, data: dict[str, Any], event_id: int | None = None) -> None:
    head = f"id: {event_id}\n" if event_id is not None else ""
    payload = to_json(data)
    h.wfile.write(f"{head}event: {name}\ndata: {payload}\n\n".encode())


def metrics(h: Handler) -> str:
    """Prometheus text: what a server operator watches."""
    hub = h.server.hub
    status = hub.status()
    lines: list[str] = []
    leaving = h._exports()
    ok = None if leaving is None else leaving.stations  # no station whose radio stays here

    def shown(rows: dict[str, Any]) -> dict[str, Any]:
        return rows if ok is None else {k: v for k, v in rows.items() if k in ok}

    def metric(name: str, kind: str, help_: str, samples: list[tuple[dict[str, str], Any]]):
        lines.append(f"# HELP wavescribe_{name} {help_}")
        lines.append(f"# TYPE wavescribe_{name} {kind}")
        for labels, value in samples:
            if value is None:
                continue
            lab = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"wavescribe_{name}{{{lab}}} {float(value):g}" if lab
                         else f"wavescribe_{name} {float(value):g}")  # fmt: skip

    st = shown(status.get("stations", {}))
    metric("ready", "gauge", "1 once the models are loaded", [({}, int(bool(status.get("ready"))))])
    metric("station_running", "gauge", "Stations running", [({"station": s}, 1) for s in st])
    metric("station_connected", "gauge", "Source connected",
           [({"station": s}, int(bool(v.get("connected")))) for s, v in st.items()])  # fmt: skip
    metric("station_reconnects_total", "counter", "Source reconnects",
           [({"station": s}, v.get("reconnects")) for s, v in st.items()])  # fmt: skip
    metric("utterances_total", "counter", "Utterances transcribed since start",
           [({"station": s}, n) for s, n in shown(hub.counts).items()])  # fmt: skip
    metric("queue_depth", "gauge", "Jobs waiting",
           [({"stage": "asr"}, status.get("asr_queue")),
            ({"stage": "mt"}, status.get("mt_queue"))])  # fmt: skip
    metric("dropped_total", "counter", "Work dropped (queue full or filtered)",
           [({"stage": k}, v) for k, v in (status.get("dropped") or {}).items()])  # fmt: skip
    metric("busy_ratio", "gauge", "Share of the last minute each model was busy",
           [({"stage": "asr"}, status.get("asr_busy")),
            ({"stage": "mt"}, status.get("mt_busy"))])  # fmt: skip
    lag_samples = []
    for s, v in shown(status.get("lag") or {}).items():
        for stage in ("text", "translated"):
            for qn in ("50", "95"):
                lag_samples.append(({"station": s, "stage": stage, "quantile": f"0.{qn}"},
                                    v.get(f"{stage}_p{qn}")))  # fmt: skip
    metric("lag_seconds", "gauge", "End of speech to text / translation, last 10 min",
           lag_samples)  # fmt: skip
    app = hub.app
    if app is not None:
        outs = app.outputs.status()
        metric("output_sent_total", "counter", "Output requests delivered",
               [({"output": k}, v["sent"]) for k, v in outs.items()])  # fmt: skip
        metric("output_failed_total", "counter", "Output requests that failed after retries",
               [({"output": k}, v["failed"]) for k, v in outs.items()])  # fmt: skip
        snap = app._last_snapshot or {}
        proc, disk = snap.get("process") or {}, snap.get("disk") or {}
        power = snap.get("power") or {}
        footprint = proc["footprint_mb"] * 2**20 if proc.get("footprint_mb") else None
        metric("process_footprint_bytes", "gauge", "Memory footprint (Activity Monitor)",
               [({}, footprint)])  # fmt: skip
        metric("disk_free_bytes", "gauge", "Free space on the data disk",
               [({}, disk["free_gb"] * 1e9 if disk.get("free_gb") else None)])  # fmt: skip
        metric("system_watts", "gauge", "Whole-machine power draw",
               [({}, power.get("system_watts"))])  # fmt: skip
    return "\n".join(lines) + "\n"
