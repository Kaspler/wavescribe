# SPDX-License-Identifier: Apache-2.0
"""Outputs: what was heard, pushed to programs the user points us at.

An output is a URL plus filters: which events (alerts, every utterance), which rules and
stations, from which severity, which translations to include, and in which format (our JSON,
CAP 1.2 XML for alerts, plain text for chat-style hooks, or an alert as one short line for
channels that take small messages). Alerts go out at once, with
retries and backoff; utterances can be batched (``batch_s``) to keep a live text feed to
another server cheap. Each output has its own thread and a bounded queue, so a dead endpoint
never slows the pipeline or the other outputs; what overflows is dropped and counted.

An alert that hasn't got there yet is also a row in the store's ``outbox`` table, written before
the first try and deleted once the other end has it, so a restart or a crash in between doesn't
lose it: the next run sends what is left. That makes delivery at least once. Each alert delivery
has an id (``X-Wavescribe-Delivery``, and ``delivery_id`` in the JSON body) that stays the same
on every try, for the other end to tell a repeat by. A waiting alert is given up once it is
older than the output's ``alert_max_age_hours``, so an old alert never arrives as news.
Utterances wait in memory only, and so does everything when there is no store.

This is the only runtime network use, and only to URLs the user configured (ARCHITECTURE.md,
"Network use"). Requests are signed with HMAC-SHA256 when the output has a secret.
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from wavescribe import __version__, httpclient
from wavescribe.events import (
    LINE_BYTES,
    LINE_BYTES_MIN,
    alert_event,
    as_line,
    as_text,
    to_json,
    utterance_event,
)
from wavescribe.httpclient import Redirected
from wavescribe.log import event
from wavescribe.rules import SEVERITIES

log = logging.getLogger(__name__)

EVENTS = ("alert", "utterance")
FORMATS = ("json", "cap", "text", "line")
LINE_BYTES_MAX = 4096
QUEUE_MAX = 2000  # events waiting per output; the oldest utterances go first when full
BATCH_MAX = 200
ALERT_ATTEMPTS = 6  # five waits between them, 2, 4, 8, 16 and 32 s: about a minute
OTHER_ATTEMPTS = 3
# Past those tries, while the other end can't be reached (or answers 408, 425, 429, 5xx), the
# events wait at the head of the queue, still bounded by QUEUE_MAX, and go again after these
# waits, doubling: a central server restarting loses nothing.
HOLD_MIN_S = 5.0
HOLD_MAX_S = 60.0
# An alert that hasn't got there this long after it was raised is given up: late, it would read
# as news. Each output can set its own, up to a week. There is no "never", so what waits is
# bounded by age as well as by QUEUE_MAX.
ALERT_MAX_AGE_HOURS = 24.0
ALERT_MAX_AGE_LIMIT_HOURS = 168.0
DELIVERY_HEADER = "X-Wavescribe-Delivery"
CLOSE_WAIT_S = 2.0  # closing waits this long for the requests on their way to finish
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
    alert_max_age_hours: float = ALERT_MAX_AGE_HOURS  # a waiting alert older than this is given up
    max_bytes: int = LINE_BYTES  # the one-line form's longest message, in bytes of UTF-8

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OutputConfig:
        """An output as saved or sent by the page. Whatever is wrong with it is an
        ``OutputError`` that names it: a hand-edited settings file is read at start, where any
        other error would stop Wavescribe instead of skipping one output."""
        try:
            return cls._read(raw)
        except OutputError:
            raise
        except (TypeError, ValueError, AttributeError) as e:
            who = raw.get("id") if isinstance(raw, Mapping) else None
            raise OutputError(
                f"{who or 'an output'}: a setting has the wrong kind of value ({e})"
            ) from None

    @classmethod
    def _read(cls, raw: Mapping[str, Any]) -> OutputConfig:
        oid = str(raw.get("id", ""))
        if not _ID.fullmatch(oid):  # match() with $ would let "id\n" through
            raise OutputError("an output id is lowercase letters, digits, - or _")
        # Named as the page names it: by its name, else by its id (an [[output]] in a file).
        who = repr(str(raw["name"]).strip()) if str(raw.get("name") or "").strip() else oid
        url = str(raw.get("url", "")).strip()
        if not re.match(r"^https?://[^\s/]+", url):
            raise OutputError(f"{who}: the URL must start with http:// or https://")
        events = tuple(raw.get("events", ("alert",)))
        if not events or any(e not in EVENTS for e in events):
            raise OutputError(f"{who}: events are any of {', '.join(EVENTS)}")
        fmt = raw.get("format", "json")
        if fmt not in FORMATS:
            raise OutputError(f"{who}: format is one of {', '.join(FORMATS)}")
        if fmt == "cap" and events != ("alert",):
            raise OutputError(f"{who}: CAP carries alerts only")
        if fmt == "line" and events != ("alert",):
            raise OutputError(f"{who}: the one-line form carries alerts only")
        severity = raw.get("min_severity", "medium")
        if severity not in SEVERITIES:
            raise OutputError(f"{who}: min_severity is one of {', '.join(SEVERITIES)}")
        headers = raw.get("headers") or {}
        if not isinstance(headers, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) and "\n" not in k + v
            for k, v in headers.items()
        ):
            raise OutputError(f"{who}: headers are name = value text")

        def number(key: str, default: float) -> float:
            value = raw.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise OutputError(f"{who}: {key} is a number, not {value!r}")
            return float(value)

        batch_s = number("batch_s", 0)
        max_bytes = number("max_bytes", LINE_BYTES)
        if not LINE_BYTES_MIN <= max_bytes <= LINE_BYTES_MAX or max_bytes != int(max_bytes):
            raise OutputError(
                f"{who}: max_bytes is a whole number from {LINE_BYTES_MIN} to {LINE_BYTES_MAX}"
            )
        if not 0 <= batch_s <= 3600:
            raise OutputError(f"{who}: batch_s is 0 to 3600 seconds")
        try:
            max_age = float(raw.get("alert_max_age_hours", ALERT_MAX_AGE_HOURS))
        except (TypeError, ValueError):
            max_age = 0.0
        if not 0 < max_age <= ALERT_MAX_AGE_LIMIT_HOURS:  # no "never": what waits stays bounded
            raise OutputError(
                f"{who}: alert_max_age_hours is more than 0 and at most "
                f"{ALERT_MAX_AGE_LIMIT_HOURS:g} (a week)"
            )
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
            context=max(0, min(10, int(number("context", 2)))),
            enabled=bool(raw.get("enabled", True)),
            timeout_s=max(1.0, min(60.0, number("timeout_s", 10))),
            alert_max_age_hours=max_age,
            max_bytes=int(max_bytes),
        )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("events", "rules", "stations", "languages"):
            d[k] = list(d[k])
        d["headers"] = dict(self.headers)
        if self.format != "line":
            del d["max_bytes"]  # only the one-line form has a length
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


def _retryable(status: int | None) -> bool:
    """Whether a failure may pass: nothing came back, or the other end asked to wait."""
    return status is None or status in (408, 425, 429) or status >= 500


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@dataclass
class _Stats:
    sent: int = 0
    failed: int = 0
    dropped: int = 0  # events that made room in a full queue, alerts among them
    alerts_expired: int = 0  # alerts given up because they were too old to send
    alerts_dropped: int = 0  # alerts that made room in a full queue
    last_ok_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    last_status: int | None = None
    waiting: bool = False  # the other end is away: events wait for it to be back


@dataclass
class _Waiting:
    """An alert on its way to one output, until the other end has it or it is given up."""

    raised_at: float
    attempts: int = 0  # tries so far, the ones before a restart included
    stored: bool = False  # it has a row in the store's outbox
    after_restart: bool = False  # an earlier run raised it
    done: bool = False  # over: a copy still queued anywhere isn't sent


def _fit(config: OutputConfig, ev: dict[str, Any]) -> dict[str, Any] | None:
    """An alert that was waiting for an output, as that output sends it now: None when the
    output no longer asks for it (its events, rules, stations or severity changed while it
    waited), and without the translations and context it no longer asks for. The URL, the
    format and the headers are the output's own at every try."""
    ids = [s.get("id") for s in ev.get("stations") or [] if isinstance(s, Mapping)]
    try:
        asked = replace(config, enabled=True).wants_alert(ev.get("rule"), ev.get("severity"), ids)
    except ValueError:  # a severity this version doesn't know
        return None
    if not asked:
        return None
    context = [c for c in ev.get("context") or [] if isinstance(c, Mapping)]
    context = context[-config.context :] if config.context else []
    translations = dict(ev.get("translations") or {})
    if config.languages:
        translations = {k: v for k, v in translations.items() if k in config.languages}
        context = [
            {**c, "translations": {k: v for k, v in (c.get("translations") or {}).items()
                                   if k in config.languages}}
            for c in context
        ]  # fmt: skip
    return {**ev, "translations": translations, "context": context}


def _unquoted(ev: dict[str, Any]) -> dict[str, Any]:
    """An alert event without the words that were heard, as ``bandplan.redact_alert`` leaves
    an alert: which rule, when, on which stations and how severe."""
    out = {**ev, "text": "", "translations": {}, "terms": [], "reason": None, "context": [],
           "segment_id": None}  # fmt: skip
    out.pop("audio_url", None)
    return out


def _kept(classes: Iterable[str], first_at: float, now: float) -> bool:
    """Whether the store still keeps words first heard at ``first_at`` on radio of these
    classes: a class that keeps text for days, or not at all, puts an end to it."""
    from wavescribe.radio.bandplan import POLICIES

    for cls in classes:
        days = POLICIES.get(cls, POLICIES["private"]).text_days
        if days is not None and first_at + days * 86400 <= now:
            return False
    return True


class _Worker:
    def __init__(
        self,
        outputs: Outputs,
        config: OutputConfig,
        alerts: Iterable[tuple[dict[str, Any], _Waiting]] = (),
        not_before: float = 0.0,
    ) -> None:
        """``alerts``: the ones already waiting for this output, oldest first (left by an
        earlier run, or by the output as it was before a change). ``not_before``: when their
        next try was planned."""
        self.outputs = outputs
        self.config = config
        self.stats = _Stats()
        self._q: collections.deque[dict[str, Any]] = collections.deque()
        self._alerts: dict[str, _Waiting] = {}  # by delivery id: every alert that isn't over
        self._flying: list[dict[str, Any]] = []  # the request on its way
        self._not_before = not_before
        self._cv = threading.Condition()
        self._closed = False
        for ev, waiting in alerts:
            self._q.append(ev)
            self._alerts[ev["delivery_id"]] = waiting
        extra = [self._drop_one() for _ in range(max(0, len(self._q) - QUEUE_MAX))]
        self._over(extra, lost="dropped")
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"output-{config.id}")
        self._thread.start()

    def put(self, ev: dict[str, Any], waiting: _Waiting | None = None) -> None:
        """Queue an event. An alert has a ``delivery_id`` and comes with its ``waiting``."""
        dropped = []
        with self._cv:
            if len(self._q) >= QUEUE_MAX:
                dropped.append(self._drop_one())
            self._q.append(ev)
            if waiting is not None:
                self._alerts[ev["delivery_id"]] = waiting
            self._cv.notify()
        self._over(dropped, lost="dropped")

    def _drop_one(self) -> dict[str, Any]:
        """Make room by dropping the oldest utterance, or the oldest alert when only alerts
        wait. Returns what went."""
        for i, old in enumerate(self._q):
            if old["type"] != "alert":
                del self._q[i]
                break
        else:
            old = self._q.popleft()
        self.stats.dropped += 1
        return old

    def _hold(self, events: list[dict[str, Any]]) -> None:
        """Events the other end couldn't take go back to the head of the queue, in order,
        to go again once it is back; the oldest utterances make room past QUEUE_MAX."""
        dropped = []
        with self._cv:
            self._flying = []
            self._q.extendleft(reversed(events))
            while len(self._q) > QUEUE_MAX:
                dropped.append(self._drop_one())
        self._over(dropped, lost="dropped")

    def _over(self, events: list[dict[str, Any]], lost: str | None = None) -> None:
        """These events are over: delivered, refused for good, or ``lost`` ("expired" or
        "dropped", which is counted and logged). Their alerts leave the outbox, after the
        request and never before it: a crash in between sends one again and loses none."""
        rows = []
        for ev in events:
            if ev["type"] != "alert":
                continue
            with self._cv:
                waiting = self._alerts.pop(ev.get("delivery_id", ""), None)
            if waiting is None:
                continue
            waiting.done = True
            if waiting.stored:
                rows.append(ev["delivery_id"])
            if lost == "expired":
                self.stats.alerts_expired += 1
                hours = self.config.alert_max_age_hours
                why = f"not delivered within {hours:g} h of the alert: given up"
                self.outputs.lost(self.config, ev, waiting, why)
            elif lost == "dropped":
                self.stats.alerts_dropped += 1
                why = f"more than {QUEUE_MAX} events were waiting: dropped"
                self.outputs.lost(self.config, ev, waiting, why)
        self.outputs.forget(rows)

    def _tried(self, events: list[dict[str, Any]], attempts: int, wait: float) -> None:
        """An alert didn't get there: its tries so far and when it goes again are kept, so a
        restart neither starts over nor tries sooner."""
        for ev in events:
            waiting = self._alerts.get(ev.get("delivery_id", "")) if ev["type"] == "alert" else None
            if waiting is not None:
                waiting.attempts += attempts
                if waiting.stored:
                    self.outputs.tried(ev["delivery_id"], waiting.attempts, wait)

    def close(self) -> list[tuple[dict[str, Any], _Waiting]]:
        """Stop. Returns the alerts that hadn't got there, oldest first, the one on its way
        included: whoever takes this output over goes on with them."""
        with self._cv:
            self._closed = True
            self._cv.notify()
            left = [ev for ev in (*self._flying, *self._q) if ev["type"] == "alert"]
            return [(ev, self._alerts[ev["delivery_id"]]) for ev in left
                    if ev.get("delivery_id") in self._alerts]  # fmt: skip

    def join(self, timeout: float) -> None:
        self._thread.join(timeout)

    def queued(self) -> int:
        return len(self._q)

    def alerts(self, now: float) -> dict[str, Any]:
        """The alerts that haven't got there yet, the one being sent included: how many, and
        how long ago the oldest was raised."""
        with self._cv:
            raised = [w.raised_at for w in self._alerts.values()]
        oldest = round(max(0.0, now - min(raised)), 1) if raised else None
        return {"alerts_waiting": len(raised), "alerts_oldest_age_s": oldest}

    def _old(self) -> list[dict[str, Any]]:
        """Take the alerts that are too old to send out of the queue and return them. One that
        this output delivered meanwhile, as it was before a change, just goes."""
        if not self._alerts:
            return []
        limit = self.outputs.clock() - self.config.alert_max_age_hours * 3600
        old: list[dict[str, Any]] = []
        keep: collections.deque[dict[str, Any]] = collections.deque()
        for ev in self._q:
            waiting = self._alerts.get(ev.get("delivery_id", "")) if ev["type"] == "alert" else None
            if waiting is not None and waiting.done:
                del self._alerts[ev["delivery_id"]]
            elif waiting is not None and waiting.raised_at < limit:
                old.append(ev)
            else:
                keep.append(ev)
        if len(keep) != len(self._q):
            self._q = keep
        return old

    def _next_check(self) -> float | None:
        """How long an output with nothing to send may sleep: until its oldest waiting alert
        is too old. One that is switched off sends nothing, and still gives them up in time."""
        if not self._alerts:
            return None
        oldest = min(w.raised_at for w in self._alerts.values())
        left = oldest + self.config.alert_max_age_hours * 3600 - self.outputs.clock()
        return min(600.0, max(0.05, left))  # the clock may be set while this sleeps

    def _take(self) -> list[dict[str, Any]] | None:
        """The next request's events: one alert, or utterances for up to ``batch_s``."""
        while True:
            with self._cv:
                if self._closed:
                    return None
                old = self._old()
                if not old:
                    if self._q and self.config.enabled:
                        self._flying = self._pick()
                        return self._flying
                    self._cv.wait(self._next_check())
                    continue
            self._over(old, lost="expired")

    def _pick(self) -> list[dict[str, Any]]:
        # An alert goes before any utterance waiting, even ones held while the other end
        # was away.
        alert = next((i for i, ev in enumerate(self._q) if ev["type"] == "alert"), None)
        if alert:
            first = self._q[alert]
            del self._q[alert]
            return [first]
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
        wait = HOLD_MIN_S
        # Alerts an earlier run left go no sooner than their next try was planned (at most
        # HOLD_MAX_S away): a service that restarts in a loop doesn't try a struggling end at
        # every start.
        pause = min(HOLD_MAX_S, self._not_before - self.outputs.clock())
        if self._alerts and self.config.enabled and pause > 0:
            self.stats.waiting = True
            with self._cv:
                self._cv.wait_for(lambda: self._closed, timeout=pause)
        while (events := self._take()) is not None:
            waiting = self._alerts.get(events[0].get("delivery_id", ""))
            try:
                entry = self.outputs.send(
                    self.config, events, stats=self.stats, closed=lambda: self._closed,
                    after_restart=bool(waiting and waiting.after_restart),
                )  # fmt: skip
            except Exception:
                log.exception("output %s failed", self.config.id)
                if not self._closed:
                    self._over(events)  # not again: it would fail the same way at every start
                with self._cv:
                    self._flying = []
                continue
            if entry["ok"]:
                self._over(events)
            elif self._closed:
                # Stopped while trying: an alert stays in the outbox, for this output as it is
                # after the change, or for the next run.
                self._tried(events, entry["attempts"], wait)
                return
            elif not _retryable(entry["status"]):
                self._over(events)  # refused for good: not tried again
            else:
                self.stats.waiting = True
                self._tried(events, entry["attempts"], wait)
                self._hold(events)
                with self._cv:  # closing ends the wait
                    self._cv.wait_for(lambda: self._closed, timeout=wait)
                wait = min(HOLD_MAX_S, wait * 2)
                continue
            self.stats.waiting, wait = False, HOLD_MIN_S
            with self._cv:
                self._flying = []


class Outputs:
    """Every configured output, fed by the pipeline (utterances) and the alerter (alerts)."""

    def __init__(
        self,
        configs: list[OutputConfig] | None = None,
        *,
        store: Any = None,  # wavescribe.store.Store: the outbox, delivery log and alert context
        stations: Callable[[], Mapping[str, Any]] = dict,
        node: Callable[[], Mapping[str, Any]] = dict,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        via: Callable[[str], list[str]] = lambda _station: [],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.clock = clock  # the time alerts are raised at and aged by
        self._stations = stations
        self._node = node
        self._via = via  # where a station's lines from another machine have been
        self._transport = transport
        self._sleep = sleep
        self._lock = threading.Lock()
        self._workers: dict[str, _Worker] = {}
        self._log: collections.deque[dict[str, Any]] = collections.deque(maxlen=200)
        left, not_before = self._left_waiting()
        self._configure(configs or [], left, not_before)

    def configure(self, configs: list[OutputConfig]) -> None:
        """Replace the set of outputs. Unchanged ones keep their queue and counters. A changed
        one keeps the alerts that were waiting for it, as far as it still asks for them, and
        loses its utterances. What waited for a removed one is dropped."""
        self._configure(configs, {}, {})

    def _configure(
        self,
        configs: list[OutputConfig],
        left: dict[str, list[tuple[dict[str, Any], _Waiting]]],
        not_before: dict[str, float],
    ) -> None:
        dropped: list[tuple[dict[str, Any], _Waiting]] = []
        with self._lock:
            keep: dict[str, _Worker] = {}
            for c in configs:
                old = self._workers.pop(c.id, None)
                if old is not None and old.config == c:
                    keep[c.id] = old
                    continue
                waiting = old.close() if old is not None else left.pop(c.id, [])
                fits = []
                for ev, w in waiting:
                    fitted = _fit(c, ev)
                    if fitted is None:
                        dropped.append((ev, w))
                    else:
                        fits.append((fitted, w))
                keep[c.id] = _Worker(self, c, fits, not_before.get(c.id, 0.0))
            for gone in self._workers.values():
                dropped += gone.close()
            for rows in left.values():  # an earlier run's, for outputs that are no more
                dropped += rows
            self._workers = keep
        for _ev, w in dropped:
            w.done = True
        self.forget(ev["delivery_id"] for ev, w in dropped if w.stored)
        if dropped:
            event(log, "output_alerts_dropped", alerts=len(dropped),
                  why="their output was removed or no longer asks for them")  # fmt: skip

    def configs(self) -> list[OutputConfig]:
        with self._lock:
            return [w.config for w in self._workers.values()]

    def close(self) -> None:
        """Stop sending. The alerts that haven't got there stay in the outbox for the next
        run; requests on their way get a moment to finish, so that fewer are sent twice."""
        with self._lock:
            workers, self._workers = list(self._workers.values()), {}
        for w in workers:
            w.close()
        deadline = time.monotonic() + CLOSE_WAIT_S
        for w in workers:
            w.join(max(0.0, deadline - time.monotonic()))

    # -- the outbox ---------------------------------------------------------------------------

    def _left_waiting(
        self,
    ) -> tuple[dict[str, list[tuple[dict[str, Any], _Waiting]]], dict[str, float]]:
        """The alerts an earlier run left in the outbox, by output and oldest first, and when
        each output's next try was planned. Words that may no longer leave this machine (the
        station is gone, or its class of radio stopped being exported) are taken out, in the
        row too: the alert still goes, the words don't."""
        left: dict[str, list[tuple[dict[str, Any], _Waiting]]] = {}
        not_before: dict[str, float] = {}
        if self.store is None:
            return left, not_before
        from wavescribe.radio.bandplan import exports

        try:
            rows = self.store.outbox()
        except Exception:
            log.exception("could not read the alerts that were waiting for outputs")
            return left, not_before
        leaving = exports(self._stations().values())
        unreadable = []
        for row in rows:
            try:
                ev = json.loads(row["event"])
            except ValueError:
                ev = None
            if not isinstance(ev, dict) or ev.get("type") != "alert":
                unreadable.append(row["delivery_id"])
                continue
            ev["delivery_id"] = row["delivery_id"]
            waiting = _Waiting(row["raised_at"], attempts=row["attempts"], stored=True,
                               after_restart=True)  # fmt: skip
            if row["privacy"] is not None:
                ids = [s.get("id") for s in ev.get("stations") or [] if isinstance(s, dict)]
                if not leaving.quotes({"stations": ids, "privacy": row["privacy"]}):
                    ev = _unquoted(ev)
                    self._keep(row["output_id"], ev, waiting, None, row["first_at"])
                    if not waiting.stored:  # the row couldn't be rewritten: it goes
                        self.forget([row["delivery_id"]])
            left.setdefault(row["output_id"], []).append((ev, waiting))
            not_before[row["output_id"]] = max(not_before.get(row["output_id"], 0.0),
                                               row["next_at"])  # fmt: skip
        self.forget(unreadable)
        return left, not_before

    def _keep(
        self,
        output_id: str,
        ev: dict[str, Any],
        waiting: _Waiting,
        classes: Iterable[str] | None,
        first_at: float | None,
    ) -> None:
        """Write an alert delivery to the store's outbox, before its first try. ``classes``:
        the classes of radio its words were heard on, None when it carries none. Words the
        store itself wouldn't keep (radio whose class keeps no text, or kept it for fewer days
        than have passed) don't go to disk: such an alert waits in memory only. Without a
        store, every alert does."""
        if self.store is None:
            return
        waiting.stored = False
        if classes is not None:
            classes = sorted(classes)
            if not _kept(classes, first_at or waiting.raised_at, self.clock()):
                return
        try:
            self.store.outbox_add(output_id, ev["delivery_id"], to_json(ev), waiting.raised_at,
                                  privacy=classes, first_at=first_at)  # fmt: skip
            waiting.stored = True
        except Exception as e:  # a full disk: it is sent all the same, from memory
            log.warning("an alert for output %s is not in the outbox: %s", output_id, e)

    def forget(self, delivery_ids: Iterable[str]) -> None:
        """These alert deliveries are over: their outbox rows go. When that fails the rows
        are still there at the next start and go again: a repeat, never a loss."""
        ids = list(delivery_ids)
        if self.store is None or not ids:
            return
        try:
            self.store.outbox_remove(ids)
        except Exception as e:
            log.warning("could not take %d alert(s) out of the outbox: %s", len(ids), e)

    def tried(self, delivery_id: str, attempts: int, wait: float) -> None:
        """An alert delivery failed for now: the outbox notes its tries and the next one."""
        if self.store is None:
            return
        try:
            self.store.outbox_tried(delivery_id, attempts, self.clock() + wait)
        except Exception as e:
            log.warning("could not note a failed alert delivery in the outbox: %s", e)

    def lost(self, config: OutputConfig, ev: dict[str, Any], waiting: _Waiting, error: str) -> None:
        """An alert that is given up goes into the delivery log as a failure, with the
        reason, so that "did it go out?" has an answer."""
        ref = None if ev.get("id") is None else str(ev["id"])
        entry = {
            "t": time.time(), "output": config.id, "event": "alert", "ref": ref, "ok": False,
            "status": None, "error": error, "attempts": waiting.attempts, "ms": None,
            "delivery": ev.get("delivery_id"), "after_restart": waiting.after_restart,
        }  # fmt: skip
        self._record(entry)
        event(log, "output_alert_lost", level=logging.WARNING, output=config.id, ref=ref,
              error=error, attempts=waiting.attempts,
              age_s=round(self.clock() - waiting.raised_at))  # fmt: skip

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
        via = self._via(payload["station"])
        for w in workers:
            w.put(utterance_event(payload, stations, node, languages=w.config.languages, via=via))

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
        from wavescribe.radio.bandplan import class_of, exports

        leaving = exports(stations.values())
        whole = dict(alert)
        alert = leaving.alert(whole)
        classes: set[str] | None = None  # of the radio its words were heard on
        if leaving.quotes(whole):
            classes = {*(whole.get("privacy") or ()),
                       *(class_of(stations[s]) for s in ids if s in stations)}  # fmt: skip
        else:
            # Heard on radio that stays here, or on a station this run doesn't know: the alert
            # goes out, the words don't.
            text, translations = "", {}
        seg = alert.get("segment_id")
        if self.store is not None and seg is not None:
            n = max(w.config.context for w in workers)
            before = self.store.context(seg, before=n, after=0, exports=leaving) if n else []
            context = [c for c in before if c["id"] != seg]
        sent = {**alert, "severity": severity}
        now = self.clock()
        ready = []
        for w in workers:
            ctx = context[-w.config.context :] if w.config.context else []
            ev = alert_event(sent, stations, node, text=text, translations=translations,
                             context=ctx, languages=w.config.languages)  # fmt: skip
            # What the other end tells a repeat by. Random: it says nothing about this machine
            # or the alert, and each output's is its own.
            ev["delivery_id"] = uuid.uuid4().hex
            ready.append((w, ev))
        with self._lock:  # no output changes between the outbox row and the queue
            for w, ev in ready:
                now_is = self._workers.get(w.config.id)
                if now_is is None:
                    continue  # removed meanwhile
                if now_is is not w:  # changed meanwhile
                    fitted = _fit(now_is.config, ev) if now_is.config.enabled else None
                    if fitted is None:
                        continue
                    ev = fitted
                waiting = _Waiting(raised_at=now)
                self._keep(now_is.config.id, ev, waiting, classes, whole.get("first_at"))
                now_is.put(ev, waiting)

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
                "checked": ev.get("checked"),
            }  # fmt: skip
            names = {s["id"]: s["name"] for s in ev["stations"]}
            return to_cap(alert, station_names=names).encode(), "application/xml; charset=utf-8"
        if config.format == "line":  # alerts only: one per request
            return as_line(events[0], config.max_bytes).encode(), "text/plain; charset=utf-8"
        single = len(events) == 1 and (config.batch_s <= 0 or events[0]["type"] == "alert")
        payload = events[0] if single else {
            "type": "batch", "version": 1, "node": events[0]["node"], "events": events,
        }  # fmt: skip
        if config.format == "text":
            return as_text(payload, config.languages).encode(), "text/plain; charset=utf-8"
        # An alert's ``delivery_id`` is in the event, so in the JSON, and so under the signature.
        return to_json(payload).encode(), "application/json"  # never NaN: not JSON

    def send(
        self,
        config: OutputConfig,
        events: list[dict[str, Any]],
        *,
        stats: _Stats | None = None,
        closed: Callable[[], bool] = lambda: False,
        attempts: int | None = None,
        after_restart: bool = False,
    ) -> dict[str, Any]:
        """POST one request with retries; returns its log entry. ``after_restart``: the alert
        was raised by an earlier run and waited in the outbox."""
        body, ctype = self.body(config, events)
        kind = events[0]["type"] if len(events) == 1 else "batch"
        ref = ",".join(str(e.get("id")) for e in events[:20] if e.get("id") is not None) or None
        delivery = events[0].get("delivery_id") if kind == "alert" else None
        headers = {
            "Content-Type": ctype,
            "User-Agent": f"wavescribe/{__version__}",
            "X-Wavescribe-Event": kind,
            **config.headers,
        }
        if delivery:  # the same on every try of this alert to this output, also after a restart
            headers[DELIVERY_HEADER] = delivery
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
            except http.client.HTTPException as e:  # an answer that isn't HTTP, or cut short
                status, error = None, (str(e) or type(e).__name__)[:200]
            if error is None:
                break
            if not _retryable(status) or n == tries or closed():
                break
            self._sleep(min(60.0, 2.0**n))
            if closed():  # stopped while it waited: nothing more goes to this URL
                break
        ok = error is None
        ms = round((time.perf_counter() - t0) * 1000, 1)
        entry = {"t": time.time(), "output": config.id, "event": kind, "ref": ref, "ok": ok,
                 "status": status, "error": error, "attempts": n, "ms": ms,
                 "delivery": delivery, "after_restart": after_restart}  # fmt: skip
        if stats is not None:
            if ok:
                stats.sent += 1
                stats.last_ok_at = entry["t"]
            else:
                stats.failed += 1
                stats.last_error, stats.last_error_at = error, entry["t"]
            stats.last_status = status
        self._record(entry)
        level = logging.INFO if ok else logging.WARNING
        event(log, "output", level=level, output=config.id, kind=kind, n=len(events), ok=ok,
              status=status, error=error, attempts=n, ms=ms)  # fmt: skip
        return entry

    def _record(self, entry: dict[str, Any]) -> None:
        """Put a delivery into the log the page shows (and the store's ``deliveries``)."""
        self._log.appendleft(entry)
        if self.store is not None:
            try:
                self.store.add_delivery(
                    entry["output"], entry["event"], entry["ref"], entry["ok"], entry["status"],
                    entry["error"], entry["attempts"], entry["ms"],
                    delivery=entry["delivery"], after_restart=entry["after_restart"],
                )  # fmt: skip
            except Exception:
                log.exception("could not log the delivery")

    def test(self, config: OutputConfig) -> dict[str, Any]:
        """Send one sample event now (no retries) and report what came back. It is marked
        ``"test": true``: a Wavescribe that takes pushes answers it and keeps nothing."""
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
            sample["test"] = True
            sample["delivery_id"] = uuid.uuid4().hex  # as a real alert has one
        else:
            sample = utterance_event(
                {"id": None, "station": "test", "t_start": now, "duration": 1.0,
                 "language": "en", "text": "This is a test from Wavescribe.",
                 "translations": {}},
                {}, node,
            )  # fmt: skip
            sample["test"] = True
        return self.send(config, [sample], attempts=1)

    def status(self) -> dict[str, dict[str, Any]]:
        """Per output: its counters since it was started or last changed, what is queued, and
        the alerts that haven't got there yet."""
        with self._lock:
            workers = list(self._workers.values())
        now = self.clock()
        return {w.config.id: {**asdict(w.stats), "queued": w.queued(), **w.alerts(now)}
                for w in workers}  # fmt: skip

    def recent(self, output_id: str | None = None) -> list[dict[str, Any]]:
        if self.store is not None:
            return self.store.deliveries(output_id)
        return [e for e in list(self._log) if output_id in (None, e["output"])]
