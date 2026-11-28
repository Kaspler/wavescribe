# SPDX-License-Identifier: Apache-2.0
"""``/feed/v1``: Wavescribe as a component of something else. Text and metadata only.

    GET  /feed/v1/events      live Server-Sent Events: utterance, alert (and status every 10 s
                              and on pause or resume, with events=status); resumes after
                              Last-Event-ID or ?since_id=,
                              or from ?since=<epoch> after a restart
    GET  /feed/v1/utterances  what was said, as JSON, NDJSON or CSV (?since_id=, ?since=,
                              ?until=, ?stations=, ?langs=, ?limit=, ?format=), in the order
                              stored: a page goes on with ?since_id=<its last id>
    GET  /feed/v1/alerts      recent alerts
    GET  /feed/v1/catches     what band watches caught, newest first (?hours=, ?mhz=,
                              ?width_khz=, ?stations=, ?limit=)
    GET  /feed/v1/stations    stations, whether they run, lag
    GET  /feed/v1/node        this machine: name, URL, version, languages
    POST /feed/v1/ingest      another Wavescribe pushes its events here (a "push" token)
    POST /feed/v1/ticket      a one-time stream ticket for a read token in the header
    GET  /healthz             ok / not ready (no token needed)
    GET  /metrics             Prometheus text

From this machine, no token is needed. From elsewhere (``serve --lan``), the feed is open
until the first feed token is made on the page; from then on every request from another
machine needs one, as ``Authorization: Bearer <token>``. Through a proxy (``serve
--behind-proxy``, or a request that carries a proxy's header) a token is always needed, for
the page's own reads too (``page_grant``) unless ``serve --public-read``. A client that can't
set headers (a browser's EventSource) trades it for a ticket and sends ``?ticket=``.
``?token=`` still works, but is logged as a warning: a URL ends up in logs. docs/FEED.md, "Who
can read what", is the reference.
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
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from wavescribe import __version__, jsonsafe
from wavescribe.asr.language import AUTO
from wavescribe.events import (
    FEED_VERSION,
    MAX_AHEAD,
    alert_event,
    resume_id,
    resume_point,
    to_json,
    utterance_event,
)

if TYPE_CHECKING:
    from wavescribe.web.server import Handler

log = logging.getLogger(__name__)

REPLAY_MAX = 1000  # utterances replayed on (re)connect
ALERTS_SINCE_MAX = 500  # alerts given to a client catching up with ?since=
CONTEXT = 2  # utterances before an alert's, in its feed event (as an output's default)
MAX_INGEST = 2 * 1024 * 1024
KEEPALIVE_S = 15.0  # a comment line on a live stream after this long without a write
STATUS_S = 10.0  # between status events
DOWNLOADING_STATUS_S = 3.0  # between status events while a model downloads
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
        # a ticket's SHA-256 -> (when it expires, the id of the token it was made from)
        self._open: dict[str, tuple[float, str]] = {}

    def issue(self, token_id: str) -> str:
        ticket = "wsk_" + secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._open = {k: t for k, t in self._open.items() if t[0] > now}
            while len(self._open) >= MAX_TICKETS:
                del self._open[next(iter(self._open))]
            self._open[_digest(ticket)] = (now + TICKET_TTL_S, token_id)
        return ticket

    def take(self, ticket: str | None) -> str | None:
        """The id of the token ``ticket`` was made from, if it was issued and is neither used
        nor expired; it is used up."""
        if not ticket:
            return None
        with self._lock:
            got = self._open.pop(_digest(ticket), None)
        return got[1] if got is not None and got[0] > time.monotonic() else None

    def void(self, token_id: str) -> None:
        """Drop the tickets made from a token, unused: it was revoked."""
        with self._lock:
            self._open = {k: t for k, t in self._open.items() if t[1] != token_id}


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


@dataclass(frozen=True)
class Mark:
    """Where the utterances stood when a final one was queued (``WebHub.mark``): the highest
    segment id given so far, and the ids transcribed but not final yet, with their stations.
    Those finish later, out of order: a translation that waited, a line Whisper re-checked."""

    highest: int
    inflight: tuple[tuple[int, str], ...] = ()


class Cursor:
    """What one live stream has sent, as its event ids say it (``events.resume_point``): every
    utterance up to ``low`` that this stream may send has gone or never will, and ``ahead`` holds
    the ids above ``low`` that went too, at most ``MAX_AHEAD``. ``low`` sits just below the
    oldest utterance still in flight on the stations the client follows, so a client resuming
    from the last id it had gets each line it hadn't, once."""

    def __init__(self, start: tuple[int, frozenset[int]] | None, follows: Callable[[str], bool]):
        self.low = start[0] if start else 0
        self.ahead: set[int] = set(start[1]) if start else set()
        self.cap: int | None = None  # a replay cut at REPLAY_MAX: what lies past it wasn't sent
        self._follows = follows

    def sent(self, seg: int, mark: Mark | None, before: int | None = None) -> str:
        """``seg`` went out, queued at ``mark``: the event id to send with it. ``before``: in a
        replay, the id of the next utterance still to come (they go out in the order of their
        ids), which this id mustn't pass: a client that stops in the middle of a replay
        resumes from the line it had, not from the replay's end."""
        if mark is not None:
            waiting = [i for i, station in mark.inflight if self._follows(station)]
            self.low = min(waiting) - 1 if waiting else mark.highest
            if self.cap is not None:
                self.low = min(self.low, self.cap)
            if before is not None:
                self.low = min(self.low, before - 1)
        self.ahead = {i for i in self.ahead if i > self.low}
        if seg > self.low:
            self.ahead.add(seg)
        while len(self.ahead) > MAX_AHEAD:  # a resume sends these again: clients dedupe by id
            self.ahead.remove(min(self.ahead))
        return resume_id(self.low, self.ahead)


def _start(h: Handler, query: dict[str, str]) -> tuple[int, frozenset[int]] | None:
    """Where a stream resumes: ``Last-Event-ID`` (an EventSource sends back the last id it had),
    else ``?since_id=``. A ``since_id`` that isn't an event id is a 400 (``ValueError``); a
    header that isn't one is ignored, and the stream starts from now."""
    raw = query.get("since_id")
    asked = resume_point(raw) if raw else None
    if raw and asked is None:
        raise ValueError("since_id must be an event id or a segment id")
    return resume_point(h.headers.get("Last-Event-ID")) or asked


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


@dataclass(frozen=True)
class Grant:
    """Why a request may go on: ``token`` is the id of the feed token it was let in by (sent
    itself, or a ticket made from it), or None when it needed none."""

    token: str | None = None


OPEN = Grant()


def _read_token(h: Handler, query: dict[str, str]) -> Grant | None:
    """A read token in the request, or a ticket made from one."""
    app = h.server.hub.app
    if app is None:
        return None
    token_id = app.token_id(token_of(h, query), "read")
    if token_id is None:
        token_id = h.server.hub.tickets.take(query.get("ticket"))
        if token_id is not None and h.server.hub.revoked(token_id):
            token_id = None  # made just before its token went
    return Grant(token_id) if token_id is not None else None


def grant(h: Handler, query: dict[str, str], scope: str) -> Grant | None:
    """Whether a feed request may go on (None: it may not)."""
    # Pushing always needs a token, even from this machine: any web page open here could
    # otherwise post events (a plain-text form needs no permission) and set off alerts.
    if h._is_local() and scope != "push":
        return OPEN
    app = h.server.hub.app
    proxied = h._proxied()  # whoever reaches the proxy: always a token
    if app is None:
        return OPEN if scope == "read" and not proxied else None
    if scope == "push":
        token_id = app.token_id(token_of(h, query), "push")
        return Grant(token_id) if token_id is not None else None
    if proxied or app.tokens_required():
        return _read_token(h, query)
    return OPEN  # serve --lan with no tokens made: the LAN reads, as it reads the page


def page_grant(h: Handler, query: dict[str, str]) -> Grant | None:
    """Whether a request may read the page's own data: what was said, alerts, audio, the live
    events (None: it may not). This machine and the LAN may; a request through a proxy needs a
    read token, as the feed does, unless the server was started with ``--public-read``."""
    if not h._proxied() or getattr(h.server, "public_read", False):
        return OPEN
    return _read_token(h, query)


def handle_get(h: Handler, path: str, query: dict[str, str]) -> bool:
    if path == "/healthz":
        hub = h.server.hub
        ready = bool(hub.pipeline and hub.pipeline.ready)
        h._json({"ok": ready, "version": __version__},
                HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE)  # fmt: skip
        return True
    if path != "/metrics" and not path.startswith("/feed/v1/"):
        return False
    if not h._let_in(grant(h, query, "read")):
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
    elif path == "/feed/v1/catches":
        catches(h, query)
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
    if grant(h, query, "push") is None:
        h._unauthorised("pushing needs a feed token with the push scope")
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
    token_id = app.token_id(token_of(h, {}), "read") if app is not None else None
    if token_id is None:
        h._unauthorised("a ticket is had for a read token, sent as Authorization: Bearer …")
        return
    h._json({"ticket": h.server.hub.tickets.issue(token_id), "expires_in": TICKET_TTL_S})


def _context(h: Handler) -> tuple[dict[str, Any], dict[str, Any]]:
    hub = h.server.hub
    stations = {s.id: s for s in hub.config.stations}
    node_ = hub.app.node() if hub.app else {"name": "wavescribe", "url": None}
    return stations, node_


def _via(h: Handler) -> Callable[[str], list[str]]:
    """Where a station's lines from another machine passed through (``App.via``)."""
    app = h.server.hub.app
    return app.via if app is not None else lambda _station: []


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
        "ask": h._ask_unavailable() is None,
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
        "id": s.id, "name": s.name, "language": None if s.language == AUTO else s.language,
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
    try:
        limit = max(1, min(10000, int(query.get("limit") or 500)))
    except ValueError:
        raise ValueError("limit must be a whole number") from None
    # What may not leave is left out before the limit, so a page is never empty while more
    # follows: a client paging with since_id would otherwise stop at a run of hidden rows.
    if hub.store is None:
        rows = [r for r in hub.history() if leaving.utterance(r)]
        if ids:
            rows = [r for r in rows if r["station"] in ids]
        rows.sort(key=lambda r: r["t_start"])
        return rows[-limit:]
    if query.get("since_id"):
        start = resume_point(query["since_id"])  # a segment id, or a live stream's event id
        if start is None:
            raise ValueError("since_id must be a segment id or an event id")
        until = _epoch(query, "until")
        rows = hub.store.after(start[0], station_ids=ids, until=until, limit=limit,
                               exports=leaving)  # fmt: skip
        return [r for r in rows if r["id"] not in start[1]]
    now = time.time()
    since = _epoch(query, "since") or now - 3600
    until = _epoch(query, "until") or now + 60
    return hub.store.between(since, until, station_ids=ids, limit=limit, exports=leaving)


def utterances(h: Handler, query: dict[str, str]) -> None:
    try:
        rows = _select(h, query)
    except (ValueError, OverflowError) as e:  # not a number, or one too large for the store
        h._json({"error": f"bad parameter: {e}"}, HTTPStatus.BAD_REQUEST)
        return
    stations_, node_ = _context(h)
    langs = _list(query.get("langs"))
    via = _via(h)
    events_ = [utterance_event(r, stations_, node_, languages=langs, via=via(r["station"]))
               for r in rows]  # fmt: skip
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
    leaving = hub.exports()
    # Catching up (``since``) reads further back than the recent ones a page shows: a client
    # that was away for a day would otherwise miss all but the newest few.
    found = hub.alerts(leaving, ALERTS_SINCE_MAX) if since else hub.alerts(leaving)
    rows = [a for a in found if (a.get("last_at") or 0) >= since]
    langs = _list(query.get("langs"))
    h._json([alert_event(a, stations_, node_, languages=langs,
                         context=_before(hub, a, leaving)) for a in rows])  # fmt: skip


def catch_filter(query: dict[str, str]) -> dict[str, Any]:
    """The arguments of ``Store.catches`` from a request's query, for the page's /api/catches
    and for /feed/v1/catches alike. ValueError says what a valid query is."""
    from wavescribe.dsp import tunable_hz
    from wavescribe.radio.watch import CATCHES, catch_hours, catch_limit, catch_width

    try:  # in the words `radio catches --mhz` uses
        freq = tunable_hz(query["mhz"]) if query.get("mhz") else None
    except ValueError as e:
        raise ValueError(f"mhz: {e}") from None
    got: dict[str, Any] = {"hours": 24.0, "width_khz": 0.0, "limit": CATCHES}
    for key, check in (("hours", catch_hours), ("width_khz", catch_width), ("limit", catch_limit)):
        if query.get(key):  # the same rule as `radio catches` (radio/watch.py)
            try:
                got[key] = check(query[key])
            except ValueError as e:
                raise ValueError(f"{key} {e}") from None
    return {
        "freq_hz": freq,
        "width_hz": got["width_khz"] * 1e3,
        "station_ids": _list(query.get("stations")) or None,
        "since": time.time() - got["hours"] * 3600 if got["hours"] else None,
        "limit": got["limit"],
    }


def catches(h: Handler, query: dict[str, str]) -> None:
    """What band watches caught, as the page lists them, but only what may leave this machine
    (``WebHub.exports``), also when a program on this machine reads it, and without the user's
    notes about each frequency: those are the user's own, not something heard."""
    hub = h.server.hub
    try:
        args = catch_filter(query)
    except ValueError as e:
        h._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        return
    h._json(hub.store.catches(**args, exports=hub.exports()) if hub.store else [])


def _before(hub: Any, alert: dict[str, Any], leaving: Any) -> list[dict[str, Any]]:
    """The utterances just before an alert's on its station, as far as they may leave."""
    seg = alert.get("segment_id")
    if hub.store is None or seg is None:
        return []
    rows = hub.store.context(seg, before=CONTEXT, after=0, exports=leaving)
    return [r for r in rows if r["id"] != seg]


def events(h: Handler, query: dict[str, str]) -> None:
    """Live events. Subscribes first, then reads where things stand (``WebHub.mark``) and
    replays what the client missed: the utterances final by then, up to that mark. Those still
    in flight, and everything after the mark, come live, each once (docs/FEED.md, Resuming)."""
    hub = h.server.hub
    want = set(_list(query.get("events")) or ["utterance", "alert"])
    only = set(_list(query.get("stations")))
    langs = _list(query.get("langs"))
    try:
        since = _epoch(query, "since")
        start = _start(h, query)
    except ValueError as e:
        h._json({"error": f"bad parameter: {e}"}, HTTPStatus.BAD_REQUEST)
        return
    from wavescribe.web.server import TooManyViewers, lan_downloads

    try:  # followers on other machines count towards the same cap as the page's
        q = hub.subscribe(lan=not h._is_local(), marked=True)
    except TooManyViewers:
        h._busy("too many programs on other machines are following along; try again later")
        return
    token = h.token_id
    ended = hub.hold(token, q)  # revoking the token it came with ends it
    if ended is None:
        hub.unsubscribe(q)
        h._unauthorised()
        return
    leaving = hub.exports()
    try:
        mark = hub.mark(sync=True)
        waiting = {i for i, _ in mark.inflight}
        cursor = Cursor(start, lambda station: not only or station in only)
        replay: list[dict[str, Any]] = []
        if "utterance" in want and hub.store is not None and (start or since is not None):
            ids = sorted(only) or None
            if start is not None:
                rows = hub.store.after(start[0], station_ids=ids, limit=REPLAY_MAX,
                                       exports=leaving)  # fmt: skip
            else:
                assert since is not None
                rows = hub.store.between(since, time.time() + 60, station_ids=ids,
                                         limit=REPLAY_MAX, exports=leaving)  # fmt: skip
            if len(rows) >= REPLAY_MAX:
                cursor.cap = rows[-1]["id"]  # the rest is replayed on the next resume
            have = start[1] if start else frozenset()
            # Final ones only: one in flight would go out unfinished, and comes live when done.
            replay = [r for r in rows if r["id"] <= mark.highest and r["id"] not in waiting
                      and r["id"] not in have and r.get("text")]  # fmt: skip
        h.send_response(HTTPStatus.OK)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.send_header("X-Accel-Buffering", "no")  # behind nginx: don't buffer the stream
        h.end_headers()
        h.wfile.write(b"retry: 3000\n\n")
        stations_, node_ = _context(h)
        replay = [row for row in replay if leaving.utterance(row)]
        for row, then in zip(replay, [*replay[1:], None], strict=False):  # then: the next
            _write(h, "utterance", utterance_event(row, stations_, node_, languages=langs,
                                                   via=_via(h)(row["station"])),
                   cursor.sent(row["id"], mark, then["id"] if then else None))  # fmt: skip
        h.wfile.flush()
        next_status = wrote = time.monotonic()
        said: tuple[Any, ...] | None = None  # as the last status event said it (_turns)
        local = h._is_local()
        while True:
            now = time.monotonic()
            if "status" in want and now >= next_status:
                now_status = hub.status()
                state, problem = hub.condition(leaving, now_status)
                downloads = now_status["downloads"]
                if not local:
                    downloads = lan_downloads(downloads)
                _write(h, "status", {"type": "status", "stations": stations(h),
                                     "time": time.time(), "paused": now_status["paused"],
                                     "state": state, "problem": problem,
                                     "downloads": downloads})  # fmt: skip
                h.wfile.flush()
                wrote = now
                said = _turns(now_status)
                next_status = now + (DOWNLOADING_STATUS_S if downloads["running"] else STATUS_S)
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
            if item is None or ended.is_set():
                break
            name, text, queued_at = item
            if name == "utterance" and "utterance" in want:
                data = json.loads(text)
                if not data.get("final") or not data.get("text"):
                    continue
                if only and data["station"] not in only:
                    continue
                if not hub.exports().utterance(data):
                    continue
                seg = data.get("id")
                if type(seg) is int and seg <= mark.highest and seg not in waiting:
                    continue  # final before this stream began: replayed, or not asked for
                stations_, node_ = _context(h)
                _write(h, "utterance", utterance_event(data, stations_, node_, languages=langs,
                                                       via=_via(h)(data["station"])),
                       cursor.sent(seg, queued_at) if type(seg) is int else None)  # fmt: skip
            elif name == "alert" and "alert" in want:
                data = json.loads(text)
                if only and not only & set(data.get("stations") or []):
                    continue
                leaving_now = hub.exports()
                data = leaving_now.alert(data)
                stations_, node_ = _context(h)
                before = _before(hub, data, leaving_now)
                _write(
                    h, "alert", alert_event(data, stations_, node_, languages=langs, context=before)
                )
            elif name == "status" and "status" in want:
                # Paused or resumed, a download started, ended or failed: said at once rather
                # than at the next 10 s.
                if _turns(json.loads(text)) != said:
                    next_status = 0.0
                continue
            else:
                continue
            h.wfile.flush()
            wrote = time.monotonic()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        hub.unsubscribe(q)
        hub.let_go(token, q)


def _turns(status: dict[str, Any]) -> tuple[Any, ...]:
    """What in a status, once changed, is said at once: paused, how many downloads run, and
    which failed."""
    downloads = status.get("downloads") or {}
    failed = tuple(f.get("model") for f in downloads.get("failed") or [])
    return status.get("paused"), downloads.get("running"), failed


def _write(h: Handler, name: str, data: dict[str, Any], event_id: str | None = None) -> None:
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
