# SPDX-License-Identifier: Apache-2.0
"""Outputs: what was heard, pushed to programs the user points us at.

An output is a URL plus filters: which events (alerts, every utterance), which rules and
stations, from which severity, which translations to include, and in which format (our JSON,
CAP 1.2 XML for alerts, or plain text for chat-style hooks). Alerts go out at once, with
retries and backoff; utterances can be batched (``batch_s``) to keep a live text feed to
another server cheap. Each output has its own thread and a bounded queue, so a dead endpoint
never slows the pipeline or the other outputs; what overflows is dropped and counted.

This is the only runtime network use, and only to URLs the user configured (ARCHITECTURE.md,
"Network use"). Requests are signed with HMAC-SHA256 when the output has a secret.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from wavescribe import __version__, httpclient
from wavescribe.events import alert_event, as_text, to_json, utterance_event
from wavescribe.httpclient import Redirected
from wavescribe.log import event
from wavescribe.rules import SEVERITIES

log = logging.getLogger(__name__)

EVENTS = ("alert", "utterance")
FORMATS = ("json", "cap", "text")
QUEUE_MAX = 2000  # events waiting per output; the oldest utterances go first when full
BATCH_MAX = 200
ALERT_ATTEMPTS = 6  # ~2 minutes of retries: 2, 4, 8, 16, 32, 60 s
OTHER_ATTEMPTS = 3
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")

# transport(url, body, headers, timeout) -> HTTP status; raises OSError when nothing came back,
# and httpclient.Redirected (never followed) for a redirect
Transport = Callable[[str, bytes, dict[str, str], float], int]
_OPENER = httpclient.opener()


class OutputError(ValueError):
    pass


@dataclass(frozen=True)
class OutputConfig:
    id: str
    url: str
    name: str = ""
    events: tuple[str, ...] = ("alert",)
    rules: tuple[str, ...] = ()  # alert rules; empty means every rule
    min_severity: str = "medium"  # alerts below this aren't sent ("low" sends everything)
    stations: tuple[str, ...] = ()  # empty means every station
    languages: tuple[str, ...] = ()  # translations to include; empty means all of them
    format: str = "json"
    batch_s: float = 0.0  # utterances: 0 sends each one, otherwise one request per window
    headers: Mapping[str, str] = field(default_factory=dict)
    secret: str = ""  # signs each request (X-Wavescribe-Signature)
    context: int = 2  # utterances before an alert's to include
    enabled: bool = True
    timeout_s: float = 10.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OutputConfig:
        oid = str(raw.get("id", ""))
        if not _ID.fullmatch(oid):  # match() with $ would let "id\n" through
            raise OutputError("an output id is lowercase letters, digits, - or _")
        url = str(raw.get("url", "")).strip()
        if not re.match(r"^https?://[^\s/]+", url):
            raise OutputError(f"output {oid}: the URL must start with http:// or https://")
        events = tuple(raw.get("events", ("alert",)))
        if not events or any(e not in EVENTS for e in events):
            raise OutputError(f"output {oid}: events are any of {', '.join(EVENTS)}")
        fmt = raw.get("format", "json")
        if fmt not in FORMATS:
            raise OutputError(f"output {oid}: format is one of {', '.join(FORMATS)}")
        if fmt == "cap" and events != ("alert",):
            raise OutputError(f"output {oid}: CAP carries alerts only")
        severity = raw.get("min_severity", "medium")
        if severity not in SEVERITIES:
            raise OutputError(f"output {oid}: min_severity is one of {', '.join(SEVERITIES)}")
        headers = raw.get("headers") or {}
        if not isinstance(headers, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) and "\n" not in k + v
            for k, v in headers.items()
        ):
            raise OutputError(f"output {oid}: headers are name = value text")
        batch_s = float(raw.get("batch_s", 0))
        if not 0 <= batch_s <= 3600:
            raise OutputError(f"output {oid}: batch_s is 0 to 3600 seconds")
        return cls(
            id=oid,
            url=url,
            name=str(raw.get("name", "")),
            events=events,
            rules=tuple(raw.get("rules", ())),
            min_severity=severity,
            stations=tuple(raw.get("stations", ())),
            languages=tuple(raw.get("languages", ())),
            format=fmt,
            batch_s=batch_s,
            headers=dict(headers),
            secret=str(raw.get("secret", "")),
            context=max(0, min(10, int(raw.get("context", 2)))),
            enabled=bool(raw.get("enabled", True)),
            timeout_s=max(1.0, min(60.0, float(raw.get("timeout_s", 10)))),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("events", "rules", "stations", "languages"):
            d[k] = list(d[k])
        d["headers"] = dict(self.headers)
        return d

    def public(self) -> dict[str, Any]:
        """For the page: the secret and header values hidden."""
        d = self.to_dict()
        d["secret"] = bool(self.secret)
        d["headers"] = {k: "•••" for k in self.headers}
        return d

    def wants_alert(self, rule: str, severity: str, stations: list[str]) -> bool:
        if not self.enabled or "alert" not in self.events:
            return False
        if SEVERITIES.index(severity) < SEVERITIES.index(self.min_severity):
            return False
        if self.rules and rule not in self.rules:
            return False
        return not self.stations or any(s in self.stations for s in stations)

    def wants_utterance(self, station: str) -> bool:
        return (
            self.enabled
            and "utterance" in self.events
            and (not self.stations or station in self.stations)
        )


def urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            resp.read(65536)
            return int(resp.status)
    except urllib.error.HTTPError as e:
        moved = httpclient.redirected(e)
        if moved is not None:
            raise moved from None
        return int(e.code)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@dataclass
class _Stats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    last_ok_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    last_status: int | None = None


class _Worker:
    def __init__(self, outputs: Outputs, config: OutputConfig) -> None:
        self.outputs = outputs
        self.config = config
        self.stats = _Stats()
        self._q: collections.deque[dict[str, Any]] = collections.deque()
        self._cv = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"output-{config.id}")
        self._thread.start()

    def put(self, ev: dict[str, Any]) -> None:
        with self._cv:
            if len(self._q) >= QUEUE_MAX:
                # Make room by dropping the oldest utterance; alerts are kept.
                for i, old in enumerate(self._q):
                    if old["type"] != "alert":
                        del self._q[i]
                        break
                else:
                    self._q.popleft()
                self.stats.dropped += 1
            self._q.append(ev)
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify()

    def queued(self) -> int:
        return len(self._q)

    def _take(self) -> list[dict[str, Any]] | None:
        """The next request's events: one alert, or utterances for up to ``batch_s``."""
        with self._cv:
            while not self._q and not self._closed:
                self._cv.wait()
            if self._closed:
                return None
            first = self._q.popleft()
            if first["type"] == "alert" or self.config.batch_s <= 0:
                return [first]
            deadline = time.monotonic() + self.config.batch_s
            batch = [first]
            while len(batch) < BATCH_MAX and not self._closed:
                if self._q and self._q[0]["type"] != "alert":
                    batch.append(self._q.popleft())
                    continue
                if self._q:  # an alert is waiting: send what we have, then the alert
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cv.wait(left)
            return batch

    def _run(self) -> None:
        while (events := self._take()) is not None:
            try:
                self.outputs.send(
                    self.config, events, stats=self.stats, closed=lambda: self._closed
                )
            except Exception:
                log.exception("output %s failed", self.config.id)


class Outputs:
    """Every configured output, fed by the pipeline (utterances) and the alerter (alerts)."""

    def __init__(
        self,
        configs: list[OutputConfig] | None = None,
        *,
        store: Any = None,  # wavescribe.store.Store: delivery log and alert context
        stations: Callable[[], Mapping[str, Any]] = dict,
        node: Callable[[], Mapping[str, Any]] = dict,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self._stations = stations
        self._node = node
        self._transport = transport
        self._sleep = sleep
        self._lock = threading.Lock()
        self._workers: dict[str, _Worker] = {}
        self._log: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        self.configure(configs or [])

    def configure(self, configs: list[OutputConfig]) -> None:
        """Replace the set of outputs; unchanged ones keep their queue and counters."""
        with self._lock:
            keep: dict[str, _Worker] = {}
            for c in configs:
                old = self._workers.pop(c.id, None)
                if old is not None and old.config == c:
                    keep[c.id] = old
                    continue
                if old is not None:
                    old.close()
                keep[c.id] = _Worker(self, c)
            for gone in self._workers.values():
                gone.close()
            self._workers = keep

    def configs(self) -> list[OutputConfig]:
        with self._lock:
            return [w.config for w in self._workers.values()]

    def close(self) -> None:
        self.configure([])

    # -- inputs -----------------------------------------------------------------------------

    def on_utterance(self, payload: Mapping[str, Any]) -> None:
        """A finished utterance in the page's payload shape (``web.server._payload``)."""
        if not payload.get("text"):
            return
        from wavescribe.radio.bandplan import exports

        if not exports(self._stations().values()).utterance(payload):
            # Radio that isn't broadcast stays on this machine (docs/RADIO.md), and so does a
            # station this run doesn't know: nothing says its words may leave.
            return
        with self._lock:
            workers = [w for w in self._workers.values()
                       if w.config.wants_utterance(payload["station"])]  # fmt: skip
        if not workers:
            return
        stations, node = self._stations(), self._node()
        for w in workers:
            w.put(utterance_event(payload, stations, node, languages=w.config.languages))

    def on_alert(
        self, alert: Mapping[str, Any], severity: str, text: str, translations: Mapping[str, str]
    ) -> None:
        """An alert was announced at ``severity`` (Alerter delivery), with the text it announced
        and its checked translations."""
        rule, ids = alert["rule"], list(alert["stations"])
        with self._lock:
            workers = [w for w in self._workers.values()
                       if w.config.wants_alert(rule, severity, ids)]  # fmt: skip
        if not workers:
            return
        stations, node = self._stations(), self._node()
        context: list[dict[str, Any]] = []
        from wavescribe.radio.bandplan import exports

        leaving = exports(stations.values())
        whole = dict(alert)
        alert = leaving.alert(whole)
        if not leaving.quotes(whole):
            # Heard on radio that stays here, or on a station this run doesn't know: the alert
            # goes out, the words don't.
            text, translations = "", {}
        seg = alert.get("segment_id")
        if self.store is not None and seg is not None:
            n = max(w.config.context for w in workers)
            before = self.store.context(seg, before=n, after=0, exports=leaving) if n else []
            context = [c for c in before if c["id"] != seg]
        sent = {**alert, "severity": severity}
        for w in workers:
            ctx = context[-w.config.context :] if w.config.context else []
            w.put(alert_event(sent, stations, node, text=text, translations=translations,
                              context=ctx, languages=w.config.languages))  # fmt: skip

    # -- delivery ---------------------------------------------------------------------------

    def body(self, config: OutputConfig, events: list[dict[str, Any]]) -> tuple[bytes, str]:
        if config.format == "cap":
            from wavescribe.alerts.cap import to_cap

            ev = events[0]
            alert = {
                "id": ev["id"], "rule": ev["rule"], "severity": ev["severity"],
                "first_at": ev["first_at"] or 0, "last_at": ev["last_at"] or 0,
                "stations": [s["id"] for s in ev["stations"]], "language": ev["language"],
                "quote": ev["text"], "terms": ev["terms"], "mentions": ev["mentions"],
                "summaries": ev["translations"], "kind": ev.get("kind"),
                "change": ev.get("change"), "status": ev.get("status"),
            }  # fmt: skip
            names = {s["id"]: s["name"] for s in ev["stations"]}
            return to_cap(alert, station_names=names).encode(), "application/xml; charset=utf-8"
        single = len(events) == 1 and (config.batch_s <= 0 or events[0]["type"] == "alert")
        payload = events[0] if single else {
            "type": "batch", "version": 1, "node": events[0]["node"], "events": events,
        }  # fmt: skip
        if config.format == "text":
            return as_text(payload, config.languages).encode(), "text/plain; charset=utf-8"
        return to_json(payload).encode(), "application/json"  # never NaN: not JSON

    def send(
        self,
        config: OutputConfig,
        events: list[dict[str, Any]],
        *,
        stats: _Stats | None = None,
        closed: Callable[[], bool] = lambda: False,
        attempts: int | None = None,
    ) -> dict[str, Any]:
        """POST one request with retries; returns its log entry."""
        body, ctype = self.body(config, events)
        kind = events[0]["type"] if len(events) == 1 else "batch"
        ref = ",".join(str(e.get("id")) for e in events[:20] if e.get("id") is not None) or None
        headers = {
            "Content-Type": ctype,
            "User-Agent": f"wavescribe/{__version__}",
            "X-Wavescribe-Event": kind,
            **config.headers,
        }
        if config.secret:
            ts = str(int(time.time()))
            headers["X-Wavescribe-Timestamp"] = ts
            headers["X-Wavescribe-Signature"] = sign(config.secret, ts, body)
        tries = attempts or (ALERT_ATTEMPTS if kind == "alert" else OTHER_ATTEMPTS)
        status: int | None = None
        error: str | None = None
        n = 0
        t0 = time.perf_counter()
        for n in range(1, tries + 1):
            try:
                status = self._transport(config.url, body, headers, config.timeout_s)
                error = None if 200 <= status < 300 else f"HTTP {status}"
            except Redirected as e:  # not followed: the headers and signature stay here
                status, error = e.status, str(e)[:300]
            except (OSError, ValueError) as e:  # URLError, timeouts, refused connections
                status, error = None, str(getattr(e, "reason", e))[:200]
            if error is None:
                break
            retryable = status is None or status in (408, 425, 429) or status >= 500
            if not retryable or n == tries or closed():
                break
            self._sleep(min(60.0, 2.0**n))
        ok = error is None
        ms = round((time.perf_counter() - t0) * 1000, 1)
        entry = {"t": time.time(), "output": config.id, "event": kind, "ref": ref, "ok": ok,
                 "status": status, "error": error, "attempts": n, "ms": ms}  # fmt: skip
        self._log.appendleft(entry)
        if stats is not None:
            if ok:
                stats.sent += 1
                stats.last_ok_at = entry["t"]
            else:
                stats.failed += 1
                stats.last_error, stats.last_error_at = error, entry["t"]
            stats.last_status = status
        if self.store is not None:
            try:
                self.store.add_delivery(config.id, kind, ref, ok, status, error, n, ms)
            except Exception:
                log.exception("could not log the delivery")
        level = logging.INFO if ok else logging.WARNING
        event(log, "output", level=level, output=config.id, kind=kind, n=len(events), ok=ok,
              status=status, error=error, attempts=n, ms=ms)  # fmt: skip
        return entry

    def test(self, config: OutputConfig) -> dict[str, Any]:
        """Send one sample event now (no retries) and report what came back."""
        node = self._node()
        now = time.time()
        sample: dict[str, Any]
        if "alert" in config.events:
            sample = alert_event(
                {"id": 0, "rule": "test", "severity": "medium", "status": "test",
                 "first_at": now, "last_at": now, "stations": [], "language": "en",
                 "quote": "This is a test alert from Wavescribe.", "terms": ["test"]},
                {}, node,
            )  # fmt: skip
        else:
            sample = utterance_event(
                {"id": None, "station": "test", "t_start": now, "duration": 1.0,
                 "language": "en", "text": "This is a test from Wavescribe.",
                 "translations": {}},
                {}, node,
            )  # fmt: skip
        return self.send(config, [sample], attempts=1)

    def status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            workers = list(self._workers.values())
        return {w.config.id: {**asdict(w.stats), "queued": w.queued()} for w in workers}

    def recent(self, output_id: str | None = None) -> list[dict[str, Any]]:
        if self.store is not None:
            return self.store.deliveries(output_id)
        return [e for e in list(self._log) if output_id in (None, e["output"])]
