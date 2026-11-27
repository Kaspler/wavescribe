# SPDX-License-Identifier: Apache-2.0
"""Digests on a schedule: ``[digest]`` in alerts.toml.

    [digest]
    every_minutes = 60            # on the hour; 30 is on the hour and half past
    stations = ["tsf", "antena1"] # leave out for every station
    language = "en"               # default: [delivery] notify_language
    speak = true                  # also read the points aloud, in the [speak.<language>] voice
    quiet_hours = ["22:00", "07:00"]

At whole periods on the local clock (every 15 minutes: at :00, :15, :30 and :45), a digest of
what was said since the one before, so that the digests cover the time between them, whatever
was skipped. Each is shown as a notification, without the alerts' sound and titled as a
summary, whatever became of it: its points, that nothing stood out in what was read, that
nothing was said, or why none could be made. A quiet notification centre would otherwise read
as a quiet radio. Only the points are read aloud. It covers only radio that may leave this
machine, as if another machine had asked: nobody chose to hear it at that moment, and anyone
in the room hears it or sees it on the screen. Wavescribe reads the table when it starts.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from wavescribe.config import ConfigError
from wavescribe.digest import (
    LANGUAGE_TAG,
    MAX_MINUTES,
    Busy,
    Digest,
    Digester,
    Failed,
    RequestError,
    Unavailable,
)
from wavescribe.events import local_time
from wavescribe.languages import same_language
from wavescribe.log import event

if TYPE_CHECKING:
    from wavescribe.alerts import Voice
    from wavescribe.digest.speech import Speaker
    from wavescribe.radio.bandplan import Exports

log = logging.getLogger(__name__)

KEYS = {"every_minutes", "stations", "language", "speak", "quiet_hours"}
MIN_EVERY = 5  # a digest every few minutes would be most of what the model does
DAY_MINUTES = 24 * 60
MIN_GAP_S = 60.0  # between two digests, whatever the clock says
CLOCK = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class Schedule:
    every_minutes: int
    language: str
    stations: tuple[str, ...] = ()  # empty: every station
    speak: bool = False
    quiet: tuple[int, int] | None = None  # minutes after midnight: from, until

    def next_run(self, now: float) -> float:
        """The first minute after ``now`` whose time on the local clock is a whole number of
        periods after midnight. Stepping through real minutes, a change of the clocks can't
        put it in the past: the hour that doesn't happen when they go forward has no digest,
        and the one that happens twice when they go back has two."""
        t = (int(now) // 60 + 1) * 60
        for _ in range(2 * DAY_MINUTES):
            local = time.localtime(t)
            if (local.tm_hour * 60 + local.tm_min) % self.every_minutes == 0:
                break
            t += 60
        return float(t)

    def is_quiet(self, t: float) -> bool:
        if self.quiet is None:
            return False
        moment = datetime.fromtimestamp(t)
        minute, (start, end) = moment.hour * 60 + moment.minute, self.quiet
        return start <= minute < end if start <= end else minute >= start or minute < end


def parse_schedule(
    raw: object, where: str, *, voices: Mapping[str, Voice], language: str
) -> Schedule | None:
    """``[digest]``, checked; None when there is none. ``language`` is the default: the
    alerts' notification language, else that of the stations' translations."""
    if raw is None:
        return None
    where = f"{where} [digest]"
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: must be a table")
    unknown = set(raw) - KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; it takes {sorted(KEYS)}")
    every = raw.get("every_minutes", 60)
    if (
        not isinstance(every, int)
        or isinstance(every, bool)
        or not MIN_EVERY <= every <= DAY_MINUTES
        or DAY_MINUTES % every
    ):
        raise ConfigError(
            f"{where}: every_minutes must divide a day into equal parts, from {MIN_EVERY} to "
            f"{DAY_MINUTES}: 15, 30, 60, 120 or 180, for example"
        )
    stations = raw.get("stations", [])
    if not isinstance(stations, list) or not all(isinstance(s, str) for s in stations):
        raise ConfigError(f'{where}: stations must be a list of station ids, e.g. ["tsf"]')
    lang = raw.get("language", language)
    if not isinstance(lang, str) or not LANGUAGE_TAG.fullmatch(lang):
        raise ConfigError(f"{where}: language must be a BCP-47 tag, e.g. en or pt-PT")
    speak = raw.get("speak", False)
    if not isinstance(speak, bool):
        raise ConfigError(f"{where}: speak is true or false")
    if speak and not any(same_language(v, lang) for v in voices):
        raise ConfigError(f"{where}: speak = true needs a [speak.{lang}] voice")
    quiet = raw.get("quiet_hours")
    if quiet is not None:
        parts = quiet if isinstance(quiet, list) else []
        times = [CLOCK.fullmatch(t) for t in parts if isinstance(t, str)]
        if len(parts) != 2 or len(times) != 2 or not all(times):
            raise ConfigError(
                f'{where}: quiet_hours is a start and an end, e.g. ["22:00", "07:00"]'
            )
        quiet = tuple(int(m[1]) * 60 + int(m[2]) for m in times if m)
        if quiet[0] == quiet[1]:
            raise ConfigError(f"{where}: quiet_hours starts and ends at the same time")
    return Schedule(every, lang, tuple(dict.fromkeys(stations)), speak, quiet)


class Scheduler:
    """Makes and delivers the scheduled digests, on a thread of its own until ``closed``."""

    def __init__(
        self,
        schedule: Schedule,
        digester: Digester,
        speaker: Speaker,
        *,
        exports: Callable[[], Exports],
        closed: threading.Event,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], bool] | None = None,
        unavailable: Callable[[], str | None] | None = None,
    ) -> None:
        self.schedule = schedule
        self.digester = digester
        self.speaker = speaker
        self._exports = exports
        self._closed = closed
        self._clock = clock
        self._wait = wait or closed.wait  # True once closed
        self._unavailable = unavailable  # why no digest can be made in this run, if so
        self._covered: float | None = None  # the end of the last digest delivered
        self._told: str | None = None  # the reason for none last shown, said once

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, daemon=True, name="digests")
        thread.start()
        return thread

    def _loop(self) -> None:
        last: float | None = None
        while True:
            now = self._clock()
            at = self.schedule.next_run(now)
            if last is not None:
                at = max(at, last + MIN_GAP_S)  # whatever the clock or the schedule says
            if self._wait(max(0.0, at - now)):
                return
            last = self._clock()
            try:
                self.tick()
            except Exception:
                log.exception("scheduled digest failed")

    def tick(self) -> Digest | None:
        """One scheduled digest, delivered; None when there was none to deliver."""
        s = self.schedule
        now = self._clock()
        if s.is_quiet(now):
            return None
        reason = self._unavailable() if self._unavailable is not None else None
        if reason:
            if reason != self._told:
                self._show(f"No summaries for now: {reason}.")
            self._told = reason
            return None
        self._told = None
        since = now - s.every_minutes * 60 if self._covered is None else self._covered
        since = max(since, now - MAX_MINUTES * 60 + 1)
        leaving = self._exports()
        body: dict[str, object] = {"since": since, "language": s.language}
        if s.stations:
            stations = [sid for sid in s.stations if sid in leaving.stations]
            if not stations:
                log.warning("scheduled digest: none of %s may be read out", ", ".join(s.stations))
                return None
            body["stations"] = stations
        try:
            request = self.digester.request(body, leaving)
            digest = self.digester.run(request, leaving, stop=self._closed)
        except (RequestError, Busy, Unavailable, Failed) as e:
            event(log, "digest_skipped", level=logging.WARNING, reason=str(e)[:200])
            if not self._closed.is_set():
                self._show(f"No summary this time: {e}. The next one covers this time too.")
            return None
        self._covered = request.until
        self.deliver(digest)
        return digest

    def deliver(self, digest: Digest) -> None:
        """Shown whatever became of it, with who wrote it from how much; the points, if any,
        also read aloud when the schedule says so."""
        lines = [f"{digest.heard_on(p)}{p.text}" for p in digest.points] or [digest.text()]
        notice = digest.notice()
        self._show("\n".join([*lines, *([notice] if notice else [])]), digest)
        if self.schedule.speak and digest.points:
            why = self.speaker.say(digest)
            if why:
                event(log, "digest_not_spoken", level=logging.WARNING, reason=why)

    def _show(self, body: str, digest: Digest | None = None) -> None:
        if digest is not None:
            r = digest.request
            span = f", {local_time(r.since, '%H:%M')}-{local_time(r.until, '%H:%M')}"
        else:
            span = ""
        try:
            self.speaker.speech.show(f"Wavescribe summary{span}", body)
        except Exception as e:  # ActionError, OSError, a timeout
            event(log, "digest_notify_failed", level=logging.ERROR, error=str(e)[:200])


def scheduler(
    raw: object,
    where: str,
    *,
    digester: Digester,
    speaker: Speaker,
    voices: Mapping[str, Voice],
    language: str | None,
    exports: Callable[[], Exports],
    closed: threading.Event,
    unavailable: Callable[[], str | None] | None = None,
) -> Scheduler | None:
    """The scheduled digests ``[digest]`` asks for, or None. ``voices`` and ``language`` are
    the alerts' (``[speak.<lang>]`` and ``notify_language``)."""
    schedule = parse_schedule(raw, where, voices=voices, language=language or digester.language())
    if schedule is None:
        return None
    _known(schedule.stations, digester.stations(), f"{where} [digest]")
    return Scheduler(
        schedule, digester, speaker, exports=exports, closed=closed, unavailable=unavailable
    )


def _known(asked: Iterable[str], stations: Mapping[str, object], where: str) -> None:
    unknown = [sid for sid in asked if sid not in stations]
    if unknown:
        raise ConfigError(f"{where}: no station {', '.join(map(repr, unknown))} is set up")
