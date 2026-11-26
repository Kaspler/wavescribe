# SPDX-License-Identifier: Apache-2.0
"""Alerts about stations rather than words: a station that went silent.

There is no second monitor. Every few seconds the watch reads the pipeline's status snapshot,
the one the page shows (``Pipeline.status``: connected, age of the last audio, audio level), and
checks the station rules against it. What they raise goes through the ``Alerter`` like any alert
(cooldown, actions, CAP, webhooks, store), worded by Wavescribe, since no words were heard.

**Silence** (``rules.SilenceRule``): a running, transcribed station whose source has delivered
nothing for ``STALE_S`` or is reconnecting, or whose audio level stayed below ``level_db`` at
every reading, for ``after_minutes``. A listen-only, switched-off or stopped station never
alerts. A narrowband channel is silent between transmissions by design, so only its outages
count. Once the station has had audio again for ``BACK_AFTER_S``, the alert says so, once, and
closes; the next silence is a new alert.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from wavescribe.rules import SilenceRule

if TYPE_CHECKING:
    from wavescribe.alerts import Alert, Alerter
    from wavescribe.config import Station

log = logging.getLogger(__name__)

INTERVAL_S = 5.0  # how often the status is read
STALE_S = 15.0  # no audio for this long is no audio (sources restart after 5 s of nothing)
BACK_AFTER_S = 10.0  # audio for this long before a silent station counts as back


@dataclass
class _Silence:
    """One station quiet under one rule."""

    since: float  # first found quiet
    dead_air: bool  # audio arrives, but only below the level
    alert: Alert | None = None  # raised once quiet for the rule's time
    audio_since: float | None = None  # audio again since (not yet long enough to be back)


def _span(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 120:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    return f"{round(minutes / 60)} hours"


def quiet(status: dict[str, Any], level_db: float) -> str | None:
    """Why a station counts as silent in one status reading: "no audio" (down, reconnecting,
    nothing for STALE_S) or "dead air" (audio, all below ``level_db``); None while it sounds."""
    age = status.get("last_data_age")
    if not status.get("connected") or age is None or age > STALE_S:
        return "no audio"
    if status.get("squelch_open") is not None:
        return None  # a narrowband channel: quiet between transmissions is what it does
    if float(status.get("level") or 0.0) < 10 ** (level_db / 20):
        return "dead air"
    return None


def watched(station: Station | None, rule: Any, status: dict[str, Any]) -> bool:
    """Whether a station is one a silence rule looks at now."""
    return (
        station is not None
        and not station.disabled
        and station.transcribe
        and not station.remote
        and not status.get("stopping")
        and (not rule.stations or station.id in rule.stations)
    )


class StationWatch:
    """Checks the station rules on the pipeline's status every ``interval_s`` (``check``)."""

    def __init__(
        self,
        alerter: Alerter,
        status: Callable[[], dict[str, Any]],
        stations: Callable[[], Iterable[Station]],
        *,
        interval_s: float = INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.alerter = alerter
        self._status = status
        self._stations = stations
        self._interval = interval_s
        self._clock = clock
        self._silent: dict[tuple[str, str], _Silence] = {}  # (rule id, station id) -> state
        self._began: dict[int, float] = {}  # id of an open silence alert -> when it began
        self._names: dict[str, str] = {}  # station id -> name, from the latest check
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="station-watch")
        self._thread.start()

    def close(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.check()
            except Exception:
                log.exception("station watch failed")

    def check(self) -> None:
        """Read the status once and act on what changed."""
        rules = list(self.alerter.config.rules.station_rules)
        if not rules and not self._silent:
            return  # nothing to watch: the status isn't even read
        snapshot = self._status()
        now = self._clock()
        live: dict[str, dict[str, Any]] = snapshot.get("stations") or {}
        stations = {s.id: s for s in self._stations()}
        self._names.update({sid: s.name or sid for sid, s in stations.items()})
        silence = {r.id: r for r in rules if isinstance(r, SilenceRule)}
        for key, state in list(self._silent.items()):
            rule = silence.get(key[0])
            if rule is None or key[1] not in live:  # rule gone or off, station stopped
                self._end(key, state, None)
            elif not watched(stations.get(key[1]), rule, live[key[1]]):
                self._end(key, state, None)  # switched to listen only, off, stopping
        if snapshot.get("ready", True):  # models still loading: sources aren't read yet
            for rule in silence.values():
                self._check_silence(rule, live, stations, now)

    def _check_silence(
        self,
        rule: SilenceRule,
        live: dict[str, dict[str, Any]],
        stations: dict[str, Station],
        now: float,
    ) -> None:
        for sid, status in live.items():
            station = stations.get(sid)
            if station is None or not watched(station, rule, status):
                continue
            key = (rule.id, sid)
            state = self._silent.get(key)
            why = quiet(status, rule.level_db)
            if why is not None:
                if state is None:
                    state = self._silent[key] = _Silence(now, why == "dead air")
                state.audio_since = None
                if state.alert is None and now - state.since >= rule.after_s:
                    state.dead_air = why == "dead air"
                    name, span = self._names[sid], _span(now - state.since)
                    text = (
                        f"{name} has been silent for {span}."
                        if state.dead_air
                        else f"No audio from {name} for {span}."
                    )
                    state.alert = self.alerter.on_station(rule, station, text)
                    # When it began, for "back after …": the earliest of the stations in it.
                    began = self._began.get(id(state.alert), state.since)
                    self._began[id(state.alert)] = min(began, state.since)
            elif state is not None:
                if state.alert is None:
                    del self._silent[key]  # quiet, but not for long enough
                    continue
                if state.audio_since is None:
                    state.audio_since = now
                if now - state.audio_since >= BACK_AFTER_S:
                    self._end(key, state, now)

    def _end(self, key: tuple[str, str], state: _Silence, now: float | None) -> None:
        """A station is no longer silent under a rule (``now``), or no longer watched (None:
        nothing is said). Its alert is over once none of its stations is still silent."""
        del self._silent[key]
        alert = state.alert
        if alert is None or any(s.alert is alert for s in self._silent.values()):
            return
        if now is None:
            self._began.pop(id(alert), None)
            self.alerter.station_back(alert, None)
            return
        names = ", ".join(self._names.get(s, s) for s in alert.station_ids)
        span = _span(now - self._began.pop(id(alert), state.since))
        self.alerter.station_back(alert, f"Audio is back on {names} after {span}.")
