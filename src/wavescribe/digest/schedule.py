# SPDX-License-Identifier: Apache-2.0
"""Digests on a schedule: ``[digest]`` in alerts.toml.

    [digest]
    every_minutes = 60            # on the hour; 30 is on the hour and half past
    stations = ["tsf", "antena1"] # leave out for every station
    language = "en"               # default: [delivery] notify_language
    speak = true                  # in the [speak.<language>] voice
    notify = true                 # a notification with the points
    quiet_hours = ["22:00", "07:00"]

At whole periods on the local clock (every 15 minutes: at :00, :15, :30 and :45), a digest
of what was said since the last one. When nothing was said, the model isn't asked; when
nothing of substance was, nothing is said or shown. It covers only radio that may leave this
machine, as if another machine had asked: nobody chose to hear it at that moment, and anyone
in the room hears it or sees it on the screen. Wavescribe reads the table when it starts.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from wavescribe.config import ConfigError
from wavescribe.digest import (
    LANGUAGE_TAG,
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

KEYS = {"every_minutes", "stations", "language", "speak", "notify", "quiet_hours"}
MIN_EVERY = 5  # a digest every few minutes would be most of what the model does
DAY_MINUTES = 24 * 60
CLOCK = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


@dataclass(frozen=True)
class Schedule:
    every_minutes: int
    language: str
    stations: tuple[str, ...] = ()  # empty: every station
    speak: bool = False
    notify: bool = True
    quiet: tuple[int, int] | None = None  # minutes after midnight: from, until

    def next_run(self, now: float) -> float:
        """The next time on the local clock that is a whole number of periods after
        midnight, after ``now``."""
        day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        period = self.every_minutes * 60
        done = int((now - day.timestamp()) // period) + 1
        return (day + timedelta(seconds=done * period)).timestamp()

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
    speak, notify = raw.get("speak", False), raw.get("notify", not raw.get("speak", False))
    if not isinstance(speak, bool) or not isinstance(notify, bool):
        raise ConfigError(f"{where}: speak and notify are true or false")
    if not speak and not notify:
        raise ConfigError(f"{where}: say how a digest reaches you: speak, notify or both")
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
    return Schedule(every, lang, tuple(dict.fromkeys(stations)), speak, notify, quiet)


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
    ) -> None:
        self.schedule = schedule
        self.digester = digester
        self.speaker = speaker
        self._exports = exports
        self._closed = closed
        self._clock = clock

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self._loop, daemon=True, name="digests")
        thread.start()
        return thread

    def _loop(self) -> None:
        while True:
            at = self.schedule.next_run(self._clock())
            if self._closed.wait(max(0.0, at - self._clock())):
                return
            try:
                self.tick()
            except Exception:
                log.exception("scheduled digest failed")

    def tick(self) -> Digest | None:
        """One scheduled digest, delivered; None when there was nothing to make one of."""
        s = self.schedule
        now = self._clock()
        if s.is_quiet(now):
            return None
        leaving = self._exports()
        body: dict[str, object] = {"minutes": s.every_minutes, "language": s.language}
        if s.stations:
            stations = [sid for sid in s.stations if sid in leaving.stations]
            if not stations:
                log.warning("scheduled digest: none of %s may be read out", ", ".join(s.stations))
                return None
            body["stations"] = stations
        try:
            request = self.digester.request(body, leaving)
        except RequestError as e:
            log.warning("scheduled digest: %s", e)
            return None
        try:
            digest = self.digester.run(request, leaving, stop=self._closed)
        except (Busy, Unavailable, Failed) as e:
            event(log, "digest_skipped", level=logging.WARNING, reason=str(e)[:200])
            return None
        if digest.points:
            self.deliver(digest)
        return digest

    def deliver(self, digest: Digest) -> None:
        r = digest.request
        if self.schedule.notify:
            names = ", ".join(digest.names.get(s, s) for s in r.stations)
            span = f"{local_time(r.since, '%H:%M')}-{local_time(r.until, '%H:%M')}"
            body = "\n".join(f"{digest.heard_on(p)}{p.text}" for p in digest.points)
            try:
                self.speaker.speech.notify(f"Digest · {names} · {span}", body)
            except Exception as e:  # ActionError, OSError, a timeout
                event(log, "digest_notify_failed", level=logging.ERROR, error=str(e)[:200])
        if self.schedule.speak:
            why = self.speaker.say(digest)
            if why:
                event(log, "digest_not_spoken", level=logging.WARNING, reason=why)


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
) -> Scheduler | None:
    """The scheduled digests ``[digest]`` asks for, or None. ``voices`` and ``language`` are
    the alerts' (``[speak.<lang>]`` and ``notify_language``)."""
    schedule = parse_schedule(raw, where, voices=voices, language=language or digester.language())
    if schedule is None:
        return None
    return Scheduler(schedule, digester, speaker, exports=exports, closed=closed)
