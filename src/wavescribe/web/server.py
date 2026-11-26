# SPDX-License-Identifier: Apache-2.0
"""``wavescribe serve``: the pipeline plus a local web page.

Standard library only: ``ThreadingHTTPServer`` for HTTP and Server-Sent Events for live updates.
The page is the React build in ``web/dist/``, with every asset bundled (no CDNs or web fonts, so
it works with no internet).

Binds to 127.0.0.1 unless told otherwise. With ``--lan`` phones and tablets on the network can
read along and listen, but the page is read-only for them: starting and stopping stations only
works from the machine itself. There is no login, so only expose it on a LAN you trust.
"""

from __future__ import annotations

import contextlib
import functools
import io
import ipaddress
import json
import logging
import math
import queue
import socket
import threading
import time
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from wavescribe import jsonsafe
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.config import StationsConfig
from wavescribe.events import to_json
from wavescribe.pipeline import Pipeline, Plan, Result
from wavescribe.web import feed
from wavescribe.web.audio import AudioTap

if TYPE_CHECKING:
    from wavescribe.app import App
    from wavescribe.ask import Asker
    from wavescribe.radio.bandplan import Exports
    from wavescribe.store import Store

log = logging.getLogger(__name__)

# Utterances kept in memory per station for page loads, in front of the SQLite store. Per
# station, so a chatty station never pushes a quiet one's history out.
RECENT_PER_STATION = 300
AROUND_DEFAULT = 50  # utterances in a /api/history?around_id= window unless limit says
RECENT_ALERTS = 30
CLIENT_BACKLOG = 1000  # events per browser before we drop
STATUS_INTERVAL_S = 1.0
MAX_LISTENERS = 8  # audio listeners per station
CONTROL_HEADER = "X-Wavescribe"  # required on changes: a cross-site form can't set it

# On every response. The page has no inline script or style, so the policy needs no exceptions;
# frame-ancestors and X-Frame-Options stop another site from framing the control centre.
SECURITY_HEADERS = (
    ("Content-Security-Policy", "default-src 'self'; object-src 'none'; base-uri 'none'; "
     "form-action 'none'; frame-ancestors 'none'"),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)  # fmt: skip

# A proxy adds one of these: its visitors are other machines even when it runs on this one.
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP", "Via")
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
ASK_TIMEOUT_S = 180.0  # a question waits at most this long for its answer
MAX_ASKS = 3  # questions queued or being answered at once
MAX_BODY = 16_384
LOAD_FAILED = "could not be loaded; the log on the Wavescribe machine says why"

# What other machines may hold at once. Each limit is far above what a household's phones and
# tablets use, so only a stuck or hostile device on the LAN meets one; this machine's own
# requests are never counted.
REQUEST_TIMEOUT_S = 30.0  # a read or write that makes no progress for this long ends it
REQUEST_DEADLINE_S = 30.0  # request line and headers, however slowly they trickle in
BODY_MIN_RATE = 8_192  # bytes/s a body gets on top: a large push over a slow link still fits
MAX_REMOTE_CONNECTIONS = 64  # served at once: every one is a thread
MAX_REMOTE_REFUSALS = 16  # more, kept only until they are told to come back; the rest are closed
REFUSAL_TIMEOUT_S = 2.0  # how long such a connection has to send its request
MAX_REMOTE_SUBSCRIBERS = 32  # pages following live events: every one is a queue of events
MAX_REMOTE_DECODES = 4  # archived utterances being decoded: every one is an ffmpeg process
WRITE_SLICE = 262_144  # bytes of a response per write, so the timeout measures progress


class TooManyViewers(Exception):
    """As many other machines follow the live events as may at once."""


class WebHub:
    """Bridges the pipeline to browsers: recent utterances, live events, audio relays."""

    def __init__(
        self,
        config: StationsConfig,
        plan: Plan,
        unavailable: dict[str, str],
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.plan = plan
        self.store = store
        self.unavailable = dict(unavailable)
        self.pipeline: Pipeline | None = None
        self.app: App | None = None  # set by App: page changes, outputs, feed tokens
        self.asker: Asker | None = None
        self.final_hooks: list[Any] = []  # called with each finished utterance's payload
        self.counts: dict[str, int] = {}  # finished utterances per station since start
        self.asks = threading.BoundedSemaphore(MAX_ASKS)
        self.decodes = threading.BoundedSemaphore(MAX_REMOTE_DECODES)
        self.tickets = feed.Tickets()  # one-time ?ticket= for clients that can't send headers
        self._lock = threading.Lock()
        self._recent: dict[str, OrderedDict[str, dict[str, Any]]] = {}
        self._clients: set[queue.Queue[tuple[str, str] | None]] = set()
        self._lan_clients: set[queue.Queue[tuple[str, str] | None]] = set()  # other machines
        self._taps: dict[str, AudioTap] = {}
        self._alerts: OrderedDict[int, dict[str, Any]] = OrderedDict()  # without a store
        self._closed = threading.Event()

    def attach(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        threading.Thread(target=self._status_loop, daemon=True, name="web-status").start()

    # -- pipeline callbacks -------------------------------------------------------------------

    def on_transcript(self, result: Result) -> None:
        if result.transcript.text:  # silence the ASR heard nothing in: nothing to show
            self._remember(self._payload(result, final=False))

    def on_result(self, result: Result) -> None:
        if not result.transcript.text:
            return
        payload = self._payload(result, final=True)
        self._remember(payload)
        sid = result.station.id
        self.counts[sid] = self.counts.get(sid, 0) + 1
        for hook in list(self.final_hooks):
            try:
                hook(payload)
            except Exception:
                log.exception("utterance hook %r failed", hook)

    def _payload(self, result: Result, *, final: bool) -> dict[str, Any]:
        archived = self.store is not None and self.store.archive is not None
        return _payload(result, final=final, mt=self.plan.mt is not None, audio=archived)

    def on_alert(self, alert: dict[str, Any]) -> None:
        """Alerter observer: a new alert, a new mention, a translation or a delivery outcome."""
        with self._lock:
            self._alerts[alert["id"] or 0] = alert
            self._alerts.move_to_end(alert["id"] or 0)
            while len(self._alerts) > RECENT_ALERTS:
                self._alerts.popitem(last=False)
        self.publish("alert", alert)

    def alerts(self, exports: Exports | None = None) -> list[dict[str, Any]]:
        """Recent alerts, newest first; as they may leave this machine when ``exports`` is
        given."""
        if self.store is not None:
            alerts = self.store.alerts(limit=RECENT_ALERTS)
        else:
            with self._lock:
                alerts = list(reversed(self._alerts.values()))
        return alerts if exports is None else [exports.alert(a) for a in alerts]

    def on_audio(self, station_id: str, chunk: AudioChunk) -> None:
        tap = self._taps.get(station_id)
        if tap is not None:
            tap.feed(chunk.samples)

    def _remember(self, payload: dict[str, Any]) -> None:
        with self._lock:
            recent = self._recent.setdefault(payload["station"], OrderedDict())
            recent[payload["key"]] = payload
            recent.move_to_end(payload["key"])
            while len(recent) > RECENT_PER_STATION:
                recent.popitem(last=False)
        self.publish("utterance", payload)

    # -- browsers -----------------------------------------------------------------------------

    def subscribe(self, *, lan: bool = False) -> queue.Queue[tuple[str, str] | None]:
        """A queue of live events. For another machine (``lan``) it raises TooManyViewers when
        as many of them follow along as may."""
        q: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=CLIENT_BACKLOG)
        with self._lock:
            if lan and len(self._lan_clients) >= MAX_REMOTE_SUBSCRIBERS:
                raise TooManyViewers
            (self._lan_clients if lan else self._clients).add(q)
        return q

    def unsubscribe(self, q: queue.Queue[tuple[str, str] | None]) -> None:
        with self._lock:
            self._clients.discard(q)
            self._lan_clients.discard(q)

    def publish(self, event: str, data: dict[str, Any]) -> None:
        text = to_json(data)
        with self._lock:
            clients, lan = list(self._clients), list(self._lan_clients)
        for q in clients:
            with contextlib.suppress(queue.Full):
                q.put_nowait((event, text))
        if not lan:
            return
        shown = self.for_lan(event, data)
        if shown is None:
            return
        lan_text = text if shown is data else to_json(shown)
        for q in lan:
            with contextlib.suppress(queue.Full):
                q.put_nowait((event, lan_text))

    # -- what other machines see --------------------------------------------------------------

    def exports(self) -> Exports:
        """What may leave this machine as things stand (radio/bandplan.py). Everything another
        machine is given goes through it: a station it doesn't list isn't shown at all (radio
        that stays here, the Listen tab, a spectrum monitor, a station that was deleted), and
        stored words are judged by the class they were heard under."""
        from wavescribe.radio.bandplan import exports

        return exports(self.config.stations)

    def for_lan(self, event: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """An event as other machines may see it: None to leave it out, as for any event this
        doesn't know."""
        leaving = self.exports()
        if event == "utterance":
            return data if leaving.utterance(data) else None
        if event == "alert":
            return leaving.alert(data)  # an action's failure text stays here: only ok/failed
        if event == "status":
            return self._lan_status(data, leaving)
        return None

    def _lan_status(self, status: dict[str, Any], leaving: Exports) -> dict[str, Any]:
        out = dict(status)
        for key in ("stations", "listeners", "lag"):
            if isinstance(out.get(key), dict):
                out[key] = {k: v for k, v in out[key].items() if k in leaving.stations}
        models = out.get("models")
        if isinstance(models, dict) and models.get("errors"):
            # Why a model didn't load is the exception's own text, which may name folders of
            # this machine's user: another machine learns which model, not why.
            out["models"] = {**models, "errors": dict.fromkeys(models["errors"], LOAD_FAILED)}
        if isinstance(out.get("stations"), dict):
            # Where a followed station's text comes from, and why that connection failed, stay
            # here: the address can carry a name or credentials of a private network.
            out["stations"] = {k: {f: v for f, v in s.items() if f not in ("remote", "error")}
                               if isinstance(s, dict) else s
                               for k, s in out["stations"].items()}  # fmt: skip
        return out

    def tap(self, station_id: str) -> AudioTap:
        with self._lock:
            if station_id not in self._taps:
                self._taps[station_id] = AudioTap(station_id)
            return self._taps[station_id]

    def state(self, *, can_control: bool = True, exports: Exports | None = None) -> dict[str, Any]:
        """What the page loads; for another machine, only what ``exports`` lets leave."""
        running = set(self.status()["stations"])
        stations = []
        for s in self.config.stations:
            if exports is not None and s.id not in exports.stations:
                continue
            entry = self.plan.asr.get(s.id)
            stations.append(
                {
                    "id": s.id,
                    "name": s.name,
                    "language": s.language,
                    "translate_to": (
                        list(s.translation_targets) if self.plan.translator_for(s) else []
                    ),
                    "transcribe": s.transcribe or bool(s.remote),
                    # Started listen-only for this run; ``transcribe`` is its saved mode.
                    "listen_only_run": s.id in getattr(self.app, "listen_only_run", ()),
                    "priority": s.priority,
                    "available": s.id not in self.unavailable,
                    "reason": self.unavailable.get(s.id),
                    "running": s.id in running,
                    "asr_model": entry[0].id if entry else ("remote" if s.remote else None),
                    "kind": "remote" if s.remote else "sdr" if s.sdr else "stream",
                    "disabled": s.disabled,
                }
            )
        recent = self.history(exports=exports)
        recent.sort(key=lambda u: u["t_start"])
        status = self.status()
        if exports is not None:
            status = self._lan_status(status, exports)
        return {
            "stations": stations,
            "can_control": can_control,
            "store": self.store is not None,
            "mt_model": self.plan.mt[0].id if self.plan.mt else None,
            "ask": self.ask_unavailable(),
            "noncommercial": self.plan.noncommercial(),
            "audio_rate": SAMPLE_RATE,
            "recent": recent,
            "alerts": self.alerts(exports),
            "status": status,
            "node": self.app.node() if self.app else None,
            "manage": can_control and self.app is not None,
        }

    def ask_unavailable(self) -> str | None:
        """Why questions can't be asked in this run, or None when they can."""
        if self.store is None:
            return "asking needs the store (this run was started with --no-store)"
        if self.plan.mt is None or self.plan.mt[0].backend != "mlx-lm":
            return "asking needs an LLM translator such as qwen3.5-9b (none is loaded)"
        if self.asker is None:
            return "asking isn't set up in this run"
        return None

    def history(
        self,
        station_id: str | None = None,
        *,
        before_id: int | None = None,
        around_id: int | None = None,
        limit: int = RECENT_PER_STATION,
        exports: Exports | None = None,
    ) -> list[dict[str, Any]]:
        """Recent utterances (oldest first), at most ``limit`` per station: from the store when
        there is one, else memory. ``around_id``: one station's window around that segment
        instead (``Store.around``). For another machine, only what ``exports`` lets leave."""
        ids = [station_id] if station_id else [s.id for s in self.config.stations]
        if self.store is not None:
            if around_id is not None and station_id:
                return self.store.around(station_id, around_id, limit=limit, exports=exports)
            return [
                u
                for sid in ids
                for u in self.store.recent(sid, limit=limit, before_id=before_id, exports=exports)
            ]
        with self._lock:
            return [
                u
                for sid in ids
                for u in list(self._recent.get(sid, {}).values())[-limit:]
                # without a store there is nothing older to page to, nor ids to look around
                if before_id is None
                and around_id is None
                and (exports is None or exports.utterance(u))
            ]

    def status(self) -> dict[str, Any]:
        status = self.pipeline.status() if self.pipeline else {"ready": False, "stations": {}}
        if self.app is not None:
            status["stations"].update(self.app.remote_status())
        status["listeners"] = {sid: t.listeners for sid, t in list(self._taps.items())}
        return status

    def start_station(self, station_id: str) -> None:
        if self.app is not None:  # remembers it for the next start, follows remote stations
            self.app.start_station(station_id)
            return
        if station_id in self.unavailable:
            raise ValueError(self.unavailable[station_id])
        assert self.pipeline is not None
        self.pipeline.add_station(self.config.get(station_id))

    def stop_station(self, station_id: str) -> None:
        assert self.pipeline is not None
        self.pipeline.remove_station(station_id)
        tap = self._taps.pop(station_id, None)
        if tap is not None:
            tap.close()

    def close(self) -> None:
        self._closed.set()
        for tap in list(self._taps.values()):
            tap.close()
        with self._lock:
            for q in (*self._clients, *self._lan_clients):  # this machine's and the others'
                with contextlib.suppress(queue.Full):
                    q.put_nowait(None)

    def _status_loop(self) -> None:
        while not self._closed.wait(STATUS_INTERVAL_S):
            self.publish("status", self.status())


def _payload(result: Result, *, final: bool, mt: bool, audio: bool = False) -> dict[str, Any]:
    from wavescribe.radio.bandplan import class_of

    u, st = result.utterance, result.station
    targets = list(st.translation_targets)
    stored = result.segment_id is not None
    return {
        "id": result.segment_id,
        # The same key as the store's rows, so live updates and history merge on the page.
        "key": f"{st.id}:{result.segment_id}" if stored else f"{st.id}:s{u.seq}",
        "audio": audio and stored,
        "flags": list(result.flags),
        "hits": [h.payload() for h in result.hits],
        "station": st.id,
        "privacy": class_of(st),
        "t_start": round(u.t_start, 2),
        "duration": round(u.duration, 1),
        "language": result.transcript.language,
        "text": result.transcript.text,
        "translations": dict(result.translations),
        "pending": [] if final or not mt else targets,
        "final": final,
        "latency_ms": round(result.timings["total_ms"]) if "total_ms" in result.timings else None,
    }


PAGE_TYPES = {
    "html": "text/html; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "svg": "image/svg+xml",
    "txt": "text/plain; charset=utf-8",
    "png": "image/png",
    "ico": "image/x-icon",
    "webmanifest": "application/manifest+json",
}
# The built page's files at its root (frontend/public/): icons, the web manifest, licences.
PAGE_ROOT_FILES = frozenset({
    "/favicon.svg", "/favicon.ico", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png",
    "/icon-maskable-512.png", "/manifest.webmanifest", "/licenses.txt",
})  # fmt: skip


class Handler(BaseHTTPRequestHandler):
    server: WebServer
    server_version = "wavescribe"

    def setup(self) -> None:
        # The base class applies it to every read and write on the connection. The streams (live
        # events, audio, answers) only write, a piece at a time, so it ends them only when the
        # other side stops reading.
        over_limit = self.server.over_limit(self.request)
        self.timeout = REFUSAL_TIMEOUT_S if over_limit else REQUEST_TIMEOUT_S
        super().setup()
        # That timeout is per read, so a client sending a byte now and then would keep its
        # thread for as long as it liked: reading a request also ends at a deadline.
        allowed = REFUSAL_TIMEOUT_S if over_limit else REQUEST_DEADLINE_S
        self._reads = _Deadline(self.rfile.detach(), self.connection, self.timeout, allowed)
        self.rfile = io.BufferedReader(self._reads)

    def handle_one_request(self) -> None:
        self._reads.start()
        super().handle_one_request()

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        size = self.headers.get("Content-Length") or ""  # read later, by the handler
        if size.isascii() and size.isdigit() and len(size) < 19:
            self._reads.deadline += min(int(size), feed.MAX_INGEST) / BODY_MIN_RATE
        return True

    def _admit(self) -> bool:
        """Whether to serve this request at all; the reason was sent if not."""
        if not self._is_local():  # e.g. forwarded by a proxy on this machine: counted from now
            self.server.count_remote(self.request)
        if self.server.over_limit(self.request):
            self._busy("too many connections from other machines; try again in a moment")
            return False
        return self._host_ok()

    def _busy(self, why: str) -> None:
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Retry-After", "5")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(why.encode())))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(why.encode())

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def version_string(self) -> str:
        return self.server_version  # no Python version in the Server header

    def end_headers(self) -> None:
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

    def _host_ok(self) -> bool:
        """Against DNS rebinding: a website whose name is made to resolve to this machine's
        address (127.0.0.1 in a browser here, its LAN address in a viewer's) would reach this
        server from that browser, with its own name in the Host header. So a request must name
        this machine; it gets 421 otherwise. Another machine may also use any ``.local`` name
        (only the LAN itself answers for those) and the name of the public URL."""
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True  # no browser leaves it out
        name = host[1 : host.find("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
        if name in LOOPBACK_NAMES or name.endswith(".localhost") or name in _own_names():
            return True
        with contextlib.suppress(ValueError):
            ipaddress.ip_address(name)
            return True  # an address typed by hand; rebinding needs a name
        if self._is_local():
            log.warning("refused a local request for host %r (DNS rebinding?)", host)
            self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unknown host name", "text/plain")
            return False
        app = self.server.hub.app
        public = urlsplit(app.node()["url"]).hostname if app is not None else None
        if name.endswith(".local") or name == public:
            return True
        log.warning("refused a request for host %r (DNS rebinding?)", host)
        why = (
            "unknown host name: open the page by this machine's address or its .local name, "
            "or set this name as the public URL (Settings → Connections, on the Wavescribe machine)"
        )
        self._send(HTTPStatus.MISDIRECTED_REQUEST, why.encode(), "text/plain; charset=utf-8")
        return False

    def do_GET(self) -> None:
        if not self._admit():
            return
        url = urlsplit(self.path)
        path, query = url.path, {k: v[-1] for k, v in parse_qs(url.query).items()}
        if path in ("/", "/index.html"):
            self._page("index.html")
        elif path.startswith("/assets/") or path in PAGE_ROOT_FILES:
            self._page(path[1:])
        elif path.startswith("/api/manage/"):
            self._manage("GET", path, query)
        elif feed.handle_get(self, path, query):
            pass
        elif path.startswith("/api/segments/") and path.endswith("/audio.wav"):
            self._wav(path[len("/api/segments/") : -len("/audio.wav")])
        elif path == "/api/state":
            hub = self.server.hub
            self._json(hub.state(can_control=self._is_local(), exports=self._exports()))
        elif path == "/api/events":
            self._events()
        elif path.startswith("/audio/") and path.endswith(".pcm"):
            self._audio(path[len("/audio/") : -len(".pcm")])
        elif path == "/api/history" and query.get("station"):
            before_id = _row_id(query.get("before_id", ""))
            around_id = _row_id(query.get("around_id", ""))
            default = RECENT_PER_STATION if around_id is None else AROUND_DEFAULT
            limit = min(_row_id(query.get("limit", "")) or default, RECENT_PER_STATION)
            hub = self.server.hub
            self._json(hub.history(query["station"], before_id=before_id, around_id=around_id,
                                   limit=limit, exports=self._exports()))  # fmt: skip
        elif path == "/api/search":
            self._search(query.get("q", ""), query.get("station"))
        elif path.startswith("/api/segments/") and path.endswith("/audio.pcm"):
            self._replay(path[len("/api/segments/") : -len("/audio.pcm")])
        elif path == "/api/alerts":
            self._json(self.server.hub.alerts(self._exports()))
        elif path.startswith("/api/alerts/") and path.endswith(".cap"):
            self._cap(path[len("/api/alerts/") : -len(".cap")])
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def do_DELETE(self) -> None:
        if not self._admit():
            return
        url = urlsplit(self.path)
        if url.path.startswith("/api/manage/"):
            self._manage("DELETE", url.path, {})
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def _manage(self, method: str, path: str, query: dict[str, str]) -> None:
        """The control centre: only from this machine, and every request needs the header (some
        GETs reach the network or open a receiver, so an <img> on another site mustn't)."""
        from wavescribe.web import api

        hub = self.server.hub
        if not self._is_local():
            self._json({"error": "the page's controls only work on the Wavescribe machine"},
                       HTTPStatus.FORBIDDEN)  # fmt: skip
            return
        if self.headers.get(CONTROL_HEADER) != "1":
            self._json({"error": f"missing {CONTROL_HEADER} header"}, HTTPStatus.FORBIDDEN)
            return
        if hub.app is None:
            self._json({"error": "this run has no page controls"}, HTTPStatus.CONFLICT)
            return
        try:
            body = self._read_json() if method in ("POST", "PUT") else None
        except ValueError as e:
            self._json({"error": f"bad JSON: {e}"}, HTTPStatus.BAD_REQUEST)
            return
        status, data = api.dispatch(hub.app, method, path, query, body)
        self._json(data, HTTPStatus(status))

    def do_POST(self) -> None:
        if not self._admit():
            return
        url = urlsplit(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if feed.handle_post(self, url.path, query):  # token-checked for other machines
            return
        if url.path.startswith("/api/manage/"):
            self._manage("POST", url.path, query)
            return
        if self.path == "/api/ask":  # reads only, so LAN viewers may ask too
            if self.headers.get(CONTROL_HEADER) != "1":
                self._json({"error": f"missing {CONTROL_HEADER} header"}, HTTPStatus.FORBIDDEN)
                return
            self._ask()
            return
        if not self._is_local():
            msg = "read-only from the LAN: start and stop stations on the Wavescribe machine"
            self._json({"error": msg}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get(CONTROL_HEADER) != "1":
            self._json({"error": f"missing {CONTROL_HEADER} header"}, HTTPStatus.FORBIDDEN)
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "stations"] and parts[3] in ("start", "stop"):
            try:
                hub = self.server.hub
                if parts[3] == "start":
                    hub.start_station(parts[2])
                elif hub.app is not None:  # remembers it for the next start
                    hub.app.stop_station(parts[2])
                else:
                    hub.stop_station(parts[2])
            except ValueError as e:
                self._json({"error": str(e)}, HTTPStatus.CONFLICT)
                return
            self._json({"ok": True})
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

    def _exports(self) -> Exports | None:
        """What this request may be given: everything (None) on this machine, and for another
        machine what may leave this one. Every read path passes it on to the hub or the store,
        so none decides for itself what another machine sees."""
        return None if self._is_local() else self.server.hub.exports()

    def _hidden_here(self, station_id: str) -> bool:
        """A station another machine may not see (its radio stays here); never for this one."""
        leaving = self._exports()
        return leaving is not None and station_id not in leaving.stations

    def _audio_file(self, segment: str) -> Any:
        """An archived utterance's file, or None (also when this viewer may not hear it)."""
        store, segment_id = self.server.hub.store, _row_id(segment)
        if store is None or segment_id is None:
            return None
        return store.audio_file(segment_id, exports=self._exports())

    def _is_local(self) -> bool:
        """The request comes from this machine (only it may start and stop stations). Behind a
        proxy, even one on this machine, every request is another machine's."""
        if getattr(self.server, "behind_proxy", False):
            return False
        if any(h in self.headers for h in PROXY_HEADERS):
            return False
        return _loopback(self.client_address[0])

    def _page(self, name: str) -> None:
        """The built page: frontend/ builds into web/dist/, which ships with the package, so
        running Wavescribe never needs Node. Assets carry a content hash, so they may be cached."""
        parts = [p for p in name.split("/") if p not in ("", ".", "..")]
        kind = PAGE_TYPES.get(name.rsplit(".", 1)[-1])
        file = resources.files("wavescribe.web").joinpath("dist", *parts)
        if not parts or kind is None or not file.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        cache = "public, max-age=31536000, immutable" if parts[0] == "assets" else "no-store"
        self._send(HTTPStatus.OK, file.read_bytes(), kind, cache)

    def _send(
        self, status: HTTPStatus, body: bytes, content_type: str, cache: str = "no-store"
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        for start in range(0, len(body), WRITE_SLICE):
            self.wfile.write(body[start : start + WRITE_SLICE])

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = to_json(data).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _events(self) -> None:
        hub = self.server.hub
        try:
            q = hub.subscribe(lan=not self._is_local())
        except TooManyViewers:
            self._busy("too many pages on other machines are following along; try again later")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if item is None:
                    break
                event, data = item
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub.unsubscribe(q)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY:  # rfile.read(-1) would wait for the connection to close
            raise ValueError("request too large" if length > 0 else "bad Content-Length")
        return jsonsafe.loads(self.rfile.read(length) or b"{}")

    def _ask(self) -> None:
        """A question in, the answer out as Server-Sent Events while the LLM writes it."""
        hub = self.server.hub
        reason = hub.ask_unavailable()
        if reason:
            self._json({"error": reason}, HTTPStatus.CONFLICT)
            return
        try:
            body = self._read_json()
            question = str(body.get("question", "")).strip()
            history = [(str(q), str(a)) for q, a in body.get("history", [])][-4:]
        except (ValueError, TypeError, AttributeError):
            self._json({"error": "expected {question, history}"}, HTTPStatus.BAD_REQUEST)
            return
        if not question:
            self._json({"error": "ask a question"}, HTTPStatus.BAD_REQUEST)
            return
        if not hub.asks.acquire(blocking=False):
            msg = "several questions are already waiting; try again in a moment"
            self._json({"error": msg}, HTTPStatus.TOO_MANY_REQUESTS)
            return
        events: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        released = threading.Event()

        def emit(name: str, data: dict[str, Any]) -> None:
            events.put((name, data))
            if name in ("done", "error") and not released.is_set():
                released.set()
                hub.asks.release()

        assert hub.asker is not None
        if not hub.asker.ask(question, history, emit, exports=self._exports()):
            released.set()
            hub.asks.release()
            self._json({"error": "the model's queue is full; try again"}, HTTPStatus.CONFLICT)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        deadline = time.monotonic() + ASK_TIMEOUT_S
        try:
            self.wfile.write(b": queued\n\n")
            self.wfile.flush()
            while time.monotonic() < deadline:
                try:
                    name, data = events.get(timeout=10)
                except queue.Empty:
                    self.wfile.write(b": waiting\n\n")
                    self.wfile.flush()
                    continue
                payload = to_json(data)
                self.wfile.write(f"event: {name}\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                if name in ("done", "error"):
                    return
            msg = json.dumps({"error": "no answer in time; the model may be busy"})
            self.wfile.write(f"event: error\ndata: {msg}\n\n".encode())
        except (BrokenPipeError, ConnectionResetError):
            pass  # the job finishes on its own and releases its slot

    def _search(self, q: str, station_id: str | None) -> None:
        store = self.server.hub.store
        if store is None:
            self._json({"error": "search needs the store (started with --no-store)"}, 409)
            return
        self._json(store.search(q[:200], station_id=station_id, limit=60, exports=self._exports()))

    def _cap(self, alert_id: str) -> None:
        from wavescribe.alerts.cap import to_cap

        hub, row_id = self.server.hub, _row_id(alert_id)
        alert = hub.store.alert(row_id) if hub.store and row_id is not None else None
        if alert is None:
            self._send(HTTPStatus.NOT_FOUND, b"no such alert", "text/plain")
            return
        leaving = self._exports()
        if leaving is not None:
            alert = leaving.alert(alert)
        names = {s.id: s.name for s in hub.config.stations}
        body = to_cap(alert, station_names=names).encode()
        self._send(HTTPStatus.OK, body, "application/xml; charset=utf-8")

    def _decoded(self, segment: str) -> bytes | None:
        """One archived utterance as PCM, or None once the reason was sent. Each decode is an
        ffmpeg process, so other machines get only so many at a time."""
        hub = self.server.hub
        path = self._audio_file(segment)
        if path is None or hub.store is None or hub.store.archive is None:
            self._send(HTTPStatus.NOT_FOUND, b"no audio kept for this moment", "text/plain")
            return None
        limited = not self._is_local()
        if limited and not hub.decodes.acquire(blocking=False):
            self._busy("busy with other viewers' recordings; try again in a moment")
            return None
        try:
            return hub.store.archive.decode(path)
        except Exception as e:
            log.warning("decoding segment %s failed: %s", segment, e)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, b"could not decode", "text/plain")
            return None
        finally:
            if limited:
                hub.decodes.release()

    def _replay(self, segment: str) -> None:
        """One archived utterance, decoded to the same raw PCM the live player takes."""
        pcm = self._decoded(segment)
        if pcm is not None:
            self._send(HTTPStatus.OK, pcm, f"audio/L16;rate={SAMPLE_RATE};channels=1")

    def _wav(self, segment: str) -> None:
        """One archived utterance as a WAV file: the ``audio_url`` in feed events and webhooks."""
        import struct

        pcm = self._decoded(segment)
        if pcm is None:
            return
        header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
            "<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16
        ) + b"data" + struct.pack("<I", len(pcm))  # fmt: skip
        self._send(HTTPStatus.OK, header + pcm, "audio/wav")

    def _audio(self, station_id: str) -> None:
        """Raw s16le mono PCM at SAMPLE_RATE until the listener leaves or the station stops."""
        hub = self.server.hub
        agent = self.headers.get("User-Agent", "?")
        app = hub.app
        live = station_id.startswith("live.") and app is not None
        live = live and getattr(app, "_live", None) is not None
        live = live and app.live.is_open(station_id[5:])
        if self._hidden_here(station_id):
            self._send(HTTPStatus.NOT_FOUND, b"no such station", "text/plain")
            return
        if any(s.id == station_id and s.remote for s in hub.config.stations):
            self._send(HTTPStatus.CONFLICT, b"a remote station's audio stays on its machine",
                       "text/plain")  # fmt: skip
            return
        if not live and station_id not in hub.status().get("stations", {}):
            log.info("audio %s refused: station not running (%s)", station_id, agent)
            self._send(HTTPStatus.CONFLICT, b"station is not running", "text/plain")
            return
        tap = hub.tap(station_id)
        if tap.listeners >= MAX_LISTENERS:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"too many listeners", "text/plain")
            return
        q = tap.add_listener()
        log.info("audio %s: listener joined (%s)", station_id, agent)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"audio/L16;rate={SAMPLE_RATE};channels=1")
        self.send_header("X-Sample-Rate", str(SAMPLE_RATE))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        try:
            while True:
                try:
                    block = q.get(timeout=30)
                except queue.Empty:
                    break  # no audio for 30 s: the station stalled; the page can retry
                if block is None or self._hidden_here(station_id):
                    break  # stopped, or retuned meanwhile to radio that stays on this machine
                self.wfile.write(block)
                self.wfile.flush()
                sent += len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            tap.remove_listener(q)
            log.info("audio %s: listener left after %.0f s", station_id, sent / 2 / SAMPLE_RATE)


class _Deadline(io.RawIOBase):
    """A connection's reads, each bounded by the time left until ``deadline`` as well as by the
    per-read timeout. ``start`` sets the deadline for the next request; writes keep the per-read
    timeout alone, so the streams go on as long as the other side reads them."""

    def __init__(self, raw: Any, sock: socket.socket, timeout: float, allowed_s: float) -> None:
        super().__init__()
        self.raw, self.sock, self.timeout, self.allowed_s = raw, sock, timeout, allowed_s
        self.deadline = math.inf

    def start(self) -> None:
        self.deadline = time.monotonic() + self.allowed_s

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int | None:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("the request took too long to arrive")
        self.sock.settimeout(min(self.timeout, left))
        try:
            return self.raw.readinto(buffer)
        finally:
            self.sock.settimeout(self.timeout)

    def close(self) -> None:
        if not self.closed:
            self.raw.close()
        super().close()


def _row_id(text: str) -> int | None:
    """A row id from a URL, or None when it isn't one. ASCII digits only: str.isdigit() alone
    also passes characters such as "²", which int() then refuses."""
    return int(text) if text.isascii() and text.isdigit() else None


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")


@functools.cache
def _own_names() -> frozenset[str]:
    """This machine's own names, as a browser on it may use them."""
    name = socket.gethostname().lower()
    short = name.split(".")[0]
    return frozenset({name, short, f"{short}.local"})


class PortInUseError(OSError):
    """Another program already listens on the port."""


def check_port(host: str, port: int) -> None:
    """Fail early if another program owns the port on this machine.

    On macOS a wildcard bind (``--lan``) succeeds even when another program holds
    127.0.0.1:<port>, and then "localhost" reaches that program instead of us. A probe bind to
    127.0.0.1 catches it; SO_REUSEADDR keeps our own recent TIME_WAIT sockets from tripping it.
    """
    probes = [host] if host not in ("0.0.0.0", "::") else [host, "127.0.0.1"]
    for addr in probes:
        family = socket.AF_INET6 if ":" in addr else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((addr, port))
            except OSError as e:
                raise PortInUseError(
                    f"port {port} is already in use on {addr} by another program; "
                    "choose another with --port"
                ) from e


class WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], hub: WebHub, behind_proxy: bool = False) -> None:
        super().__init__(address, Handler)
        self.hub = hub
        self.behind_proxy = behind_proxy
        self._remote_lock = threading.Lock()
        # Open connections from other machines; True for those past the limit.
        self._remote: dict[socket.socket, bool] = {}

    def process_request(self, request: socket.socket, client_address: tuple[str, int]) -> None:
        """A thread per connection, as the base class does it, but only so many for other
        machines: past MAX_REMOTE_CONNECTIONS a few more get a thread just to be told to come
        back (``Handler._admit``), and beyond those a connection is closed unanswered. Nothing
        was read from it yet, so it is another machine's by its address alone."""
        if self.behind_proxy or not _loopback(client_address[0]):
            with self._remote_lock:
                open_now = len(self._remote)
                room = open_now < MAX_REMOTE_CONNECTIONS + MAX_REMOTE_REFUSALS
                if room:
                    self._remote[request] = open_now >= MAX_REMOTE_CONNECTIONS
            if not room:
                self.shutdown_request(request)
                return
        super().process_request(request, client_address)

    def shutdown_request(self, request: socket.socket) -> None:
        with self._remote_lock:
            self._remote.pop(request, None)
        super().shutdown_request(request)

    def count_remote(self, request: socket.socket) -> None:
        """Count a connection from this machine's address as another machine's, once a request
        on it shows it is (a proxy here forwarded it): past the limit, it is told to come back."""
        with self._remote_lock:
            if request not in self._remote:
                self._remote[request] = len(self._remote) >= MAX_REMOTE_CONNECTIONS

    def over_limit(self, request: socket.socket) -> bool:
        """Whether this connection is one of those past the limit for other machines."""
        with self._remote_lock:
            return self._remote.get(request, False)


def serve(
    app: App, *, host: str, port: int, on_ready: Any = None, behind_proxy: bool = False
) -> None:
    """Run ``app`` (``wavescribe.app.App``) with its page until Ctrl-C."""
    check_port(host, port)
    server = WebServer((host, port), app.hub, behind_proxy)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    app.start_sampling()
    if on_ready is not None:
        local = host in ("127.0.0.1", "::1", "0.0.0.0", "::")
        on_ready(f"http://{'localhost' if local else host}:{port}/")
    try:
        app.run()
    finally:
        server.shutdown()
        time.sleep(0.1)


def lan_urls(port: int) -> list[str]:
    """Addresses other devices on the LAN can use: the mDNS name and its private IPv4 addresses.

    Resolving our own ``.local`` name returns the addresses mDNS advertises, which are the real
    LAN interfaces; asking for the default route instead would pick a VPN tunnel when one is up.
    """
    name = socket.gethostname()
    if not name or name == "localhost":
        return []
    name = name if name.endswith(".local") else name + ".local"
    urls = [f"http://{name}:{port}/"]
    try:
        infos = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return urls  # mDNS unavailable: the name may still work from the phones
    ips = sorted({str(info[4][0]) for info in infos})
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        if addr.is_private and not addr.is_loopback and not addr.is_link_local:
            urls.append(f"http://{ip}:{port}/")
    return urls
