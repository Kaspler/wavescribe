# SPDX-License-Identifier: Apache-2.0
"""Remote stations: another Wavescribe's station, followed through its text feed.

A station with ``remote = "http://other-mac.local:8765"`` and ``remote_station = "tsf"``
subscribes to that machine's ``/feed/v1/events`` (Server-Sent Events) and hands every
utterance to the local pipeline (``Pipeline.ingest``): it is stored, searched, asked about and
run through the local rules like a local station, and translated here into languages the other
machine didn't provide. No audio crosses, and no model runs here for it.

Machines behind NAT can push instead: an output on the other machine pointed at our
``/feed/v1/ingest`` creates ``remote = "push"`` stations here on first sight (``App.ingest``).

Either way the other machine isn't trusted to be right: every event is checked field by field
before it is taken (``read_utterance``), and what is read from a feed is bounded. Events carry
their station's privacy class (radio/bandplan.py), and the text is kept, quoted and passed on
here by that class.

Reconnects back off from 2 s to 60 s and resume after the last utterance seen (the feed's
``Last-Event-ID``), or, after a restart, from the last utterance stored for the station.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from wavescribe import __version__, jsonsafe
from wavescribe.asr import Transcript
from wavescribe.config import _BCP47, Station
from wavescribe.log import event
from wavescribe.pipeline import Result
from wavescribe.radio.bandplan import PRIVACY
from wavescribe.segment import Utterance

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 45.0  # the feed sends a keepalive every 15 s
# An utterance event is a few kilobytes. A feed that sends a megabyte without ending a line or
# an event isn't one, and reading on would hold whatever it sends in memory.
MAX_EVENT_BYTES = 1024 * 1024


# What an utterance from another machine may hold. That machine can be faulty or hostile, and
# what is taken is stored, shown to every viewer and sent on, so nothing is taken on trust.
MAX_EVENT_AGE_S = 7 * 86400.0  # catching up after a week away still works; older is a bad clock
# Machines that run for weeks with no internet have nothing to set their clocks by, and drift
# apart. A time up to this far ahead of ours is a sender whose clock runs fast: its text is
# taken and stored as heard when it arrived, so no stored time is ever in the future, which
# ordering and pruning rely on. Further ahead, the clock is wrong rather than drifting.
SENDER_CLOCK_DRIFT_S = 600.0
MAX_DURATION_S = 600.0  # the segmenter cuts at 20 s
MAX_TEXT_CHARS = 4000  # 20 s of speech is a few hundred characters
MAX_NAME_CHARS = 120
MAX_TAG_CHARS = 35  # the longest tag BCP-47 asks software to handle
MAX_TRANSLATIONS = 32
MAX_FLAGS = 16
MAX_FLAG_CHARS = 40


class BadEvent(ValueError):
    """An event from another machine that isn't taken. The message names the field and never
    quotes its value, so it can be counted and logged as it is."""


@dataclass(frozen=True)
class Heard:
    """An ``utterance`` event from another machine, checked: every field has its type and
    lies within the limits above."""

    station_id: str  # over there
    station_name: str | None
    station_language: str | None
    privacy: str | None  # the station's class over there; None: the sender didn't say
    node: str | None
    seq: int
    t_start: float
    duration: float
    language: str | None
    text: str
    translations: Mapping[str, str]
    flags: tuple[str, ...]


def read_utterance(ev: object, *, now: float | None = None) -> Heard:
    """Check an ``utterance`` event, pushed to us or read from a feed we follow. It is taken
    whole or not at all (``BadEvent``): a sender whose numbers can't be trusted has no claim on
    the rest. A missing time means "as it arrived", and so does one ahead of our clock by
    no more than ``SENDER_CLOCK_DRIFT_S``; any other time, when given, has to be one."""
    if not isinstance(ev, dict):
        raise BadEvent("the event is not an object")
    now = time.time() if now is None else now
    station, node = _object(ev, "station"), _object(ev, "node")
    station_id = _string(station.get("id"), "station.id", MAX_NAME_CHARS)
    text = _string(ev.get("text"), "text", MAX_TEXT_CHARS)
    if station_id is None or text is None:
        raise BadEvent(f"{'text' if station_id else 'station.id'} is missing")
    # The class is the sender's statement. A sender that says nothing is taken to send what
    # may be passed on (broadcast); one that sends radio that isn't broadcast must say so.
    privacy = _string(station.get("privacy"), "station.privacy", MAX_NAME_CHARS)
    if privacy is not None and privacy not in PRIVACY:
        privacy = "private"  # a class this version doesn't know: the strictest reading
    seq, duration = ev.get("id"), ev.get("duration")
    t_start = ev.get("t_start")
    if t_start is not None:
        t_start = _number(t_start, "t_start", now - MAX_EVENT_AGE_S, now + SENDER_CLOCK_DRIFT_S)
    raw = ev.get("translations") or {}
    if not isinstance(raw, dict) or len(raw) > MAX_TRANSLATIONS:
        raise BadEvent(f"translations is not an object of at most {MAX_TRANSLATIONS} languages")
    translations = {}
    for key, value in raw.items():
        lang = _tag(key, "translations")
        said = _string(value, "translations", MAX_TEXT_CHARS)
        if lang and said:
            translations[lang] = said
    flags = ev.get("flags") or []
    if not isinstance(flags, list) or len(flags) > MAX_FLAGS:
        raise BadEvent(f"flags is not a list of at most {MAX_FLAGS}")
    return Heard(
        station_id=station_id,
        station_name=_string(station.get("name"), "station.name", MAX_NAME_CHARS),
        station_language=_tag(station.get("language"), "station.language"),
        privacy=privacy,
        node=_string(node.get("name"), "node.name", MAX_NAME_CHARS),
        seq=0 if seq is None else int(_number(seq, "id", 0, 2**63 - 1, whole=True)),
        t_start=now if t_start is None else min(t_start, now),
        duration=0.0 if duration is None else _number(duration, "duration", 0, MAX_DURATION_S),
        language=_tag(ev.get("language"), "language"),
        text=text,
        translations=translations,
        flags=tuple(f for f in (_string(v, "flags", MAX_FLAG_CHARS) for v in flags) if f),
    )


def _object(ev: dict[str, Any], key: str) -> dict[str, Any]:
    value = ev.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BadEvent(f"{key} is not an object")
    return value


def _string(value: object, what: str, limit: int) -> str | None:
    """A string within its limit; None when it is absent or empty."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise BadEvent(f"{what} is not a string of at most {limit} characters")
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # JSON can spell half of a surrogate pair ("\ud800"). It is not text: neither the
            # database nor ui.toml can hold it.
            raise BadEvent(f"{what} is not Unicode text") from None
    return value


def _tag(value: object, what: str) -> str | None:
    tag = _string(value, what, MAX_TAG_CHARS)
    if tag is not None and not _BCP47.fullmatch(tag):
        raise BadEvent(f"{what} is not a BCP-47 language tag")
    return tag


def _number(value: object, what: str, low: float, high: float, *, whole: bool = False) -> float:
    # JSON as Python reads it has NaN and Infinity, and integers of any size. NaN fails every
    # comparison and the infinities one of them; the comparison is made before any conversion,
    # since float() of a huge integer raises. A bool is an int to isinstance, and isn't one here.
    kinds = int if whole else int | float
    if isinstance(value, bool) or not isinstance(value, kinds) or not low <= value <= high:
        raise BadEvent(f"{what} is not a number within its range")
    return float(value)


def to_result(heard: Heard, station: Station) -> Result:
    """A checked utterance from another machine as a pipeline result for ``station`` (ours).
    The result's station has the class the sender gave: what is kept of it, whether alerts
    quote it and whether it is sent on then follow that class as for a station heard here."""
    if heard.privacy is not None and heard.privacy != station.privacy:
        station = replace(station, privacy=heard.privacy)
    utt = Utterance(station.id, heard.seq, np.zeros(0, dtype=np.float32), heard.t_start,
                    heard.t_start + heard.duration, time.time())  # fmt: skip
    node = heard.node or "remote"
    transcript = Transcript(heard.text, heard.language or station.language, f"remote:{node}")
    result = Result(station, utt, transcript, translations=dict(heard.translations),
                    flags=heard.flags)  # fmt: skip
    result.translations_model = f"remote:{node}" if heard.translations else None
    result.asr_done_at = time.time()
    return result


def feed_lines(resp: Any) -> Iterator[bytes]:
    """The lines of a feed response, none longer than ``MAX_EVENT_BYTES``: a longer one is an
    error (``ValueError``) and what was read of it is dropped."""
    while line := resp.readline(MAX_EVENT_BYTES + 1):
        if len(line) > MAX_EVENT_BYTES:
            raise ValueError("the feed sent a line too long to be an event")
        yield line


def sse_events(lines: Iterator[bytes]) -> Iterator[tuple[str, str, str | None]]:
    """(event, data, id) from a Server-Sent Events byte stream. An event whose data lines add
    up to more than ``MAX_EVENT_BYTES`` is an error (``ValueError``)."""
    name, data, size, last_id = "message", [], 0, None
    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data:
                yield name, "\n".join(data), last_id
            name, data, size = "message", [], 0
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value
        elif field == "data":
            size += len(raw)
            if size > MAX_EVENT_BYTES:
                raise ValueError("the feed sent an event too large to be one")
            data.append(value)
        elif field == "id":
            last_id = value


class RemoteFollower:
    """Follows one remote station on its own thread until closed."""

    def __init__(
        self,
        station: Station,
        ingest: Callable[[Result], None],
        *,
        since: float | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], bool] | None = None,
    ) -> None:
        self.station = station
        self._ingest = ingest
        self._since = since
        self._open = opener
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait
        self.last_id: str | None = None
        self.connected = False
        self.reconnects = 0
        self.received = 0
        self.skipped = 0  # events that weren't taken: not JSON, or a value out of bounds
        self.last_data_at: float | None = None
        self.started_at = time.time()
        self.error: str | None = None
        self._resp: Any = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"remote-{station.id}")  # fmt: skip
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._stop.is_set()

    def close(self) -> None:
        self._stop.set()
        resp = self._resp
        if resp is not None:
            with contextlib.suppress(Exception):  # the socket may already be gone
                resp.close()

    def url(self) -> str:
        q = {"stations": self.station.remote_station or "", "events": "utterance"}
        if self.last_id is None and self._since:
            q["since"] = f"{self._since:.3f}"
        # The token goes in the Authorization header only: a URL is written to logs, here and
        # on any proxy in between.
        return f"{self.station.remote}/feed/v1/events?{urllib.parse.urlencode(q)}"

    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            taken = self.received
            try:
                self._follow()
            except (OSError, ValueError, urllib.error.URLError) as e:
                self.error = str(getattr(e, "reason", e))[:200]
            except Exception:
                # This thread is the station: whatever a feed makes the code here or behind
                # ``ingest`` raise, it is logged and the feed is tried again. Closing still
                # ends the loop, through the stop flag.
                log.exception("following %s failed; reconnecting", self.station.id)
                self.error = "an unexpected error (see the log)"
            finally:
                self.connected = False
                resp, self._resp = self._resp, None
                if resp is not None:
                    with contextlib.suppress(Exception):  # the socket may already be gone
                        resp.close()
            if self._stop.is_set():
                break
            if self.received > taken:
                backoff = 2.0  # the feed worked: the next wait starts short again
            self.reconnects += 1
            event(log, "remote_reconnect", level=logging.WARNING, station=self.station.id,
                  error=self.error, wait_s=backoff)  # fmt: skip
            if self._sleep(backoff):
                break
            backoff = min(60.0, backoff * 2)

    def _follow(self) -> None:
        headers = {"Accept": "text/event-stream", "User-Agent": f"wavescribe/{__version__}"}
        if self.last_id:
            headers["Last-Event-ID"] = self.last_id
        if self.station.remote_token:
            headers["Authorization"] = f"Bearer {self.station.remote_token}"
        req = urllib.request.Request(self.url(), headers=headers)
        resp = self._open(req, timeout=READ_TIMEOUT_S)
        self._resp = resp
        status = getattr(resp, "status", 200)
        if status != 200:
            raise OSError(f"HTTP {status}")
        self.connected, self.error = True, None
        event(log, "remote_connect", station=self.station.id, url=self.station.remote)
        noted = False
        for name, data, last_id in sse_events(feed_lines(resp)):
            if self._stop.is_set():
                return
            # Ids are segment numbers. Anything else isn't sent back as Last-Event-ID: a
            # header the HTTP client refuses would fail every reconnect from then on.
            if last_id and last_id.isascii() and last_id.isdigit() and len(last_id) < 20:
                self.last_id = last_id
            if name != "utterance":
                continue
            try:
                ev = jsonsafe.loads(data)
                if isinstance(ev, dict) and not ev.get("text"):
                    continue  # nothing was heard
                heard = read_utterance(ev)
            except ValueError as e:  # not JSON, or an event that isn't taken (BadEvent)
                self.skipped += 1
                if not noted:
                    noted = True  # once a connection: every event may have the same fault
                    event(log, "remote_event_skipped", level=logging.WARNING,
                          station=self.station.id, error=str(e))  # fmt: skip
                continue
            self._ingest(to_result(heard, self.station))
            self.received += 1
            self.last_data_at = time.time()
        raise OSError("the feed ended")

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "connected": self.connected,
            "reconnects": self.reconnects,
            "last_data_age": round(now - self.last_data_at, 1) if self.last_data_at else None,
            "in_speech": False,
            "music": False,
            "quiet_s": round(now - (self.last_data_at or self.started_at)),
            "level": 0,
            "stopping": self.closed,
            "remote": self.station.remote,
            "received": self.received,
            "skipped": self.skipped,
            "error": self.error,
        }
