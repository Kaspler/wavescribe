# SPDX-License-Identifier: Apache-2.0
"""Replay stored transcripts through the alert rules: what would have been announced?

Rules are tuned on real radio, not on guesses. The replay runs the real ``Alerter`` (the same
cooldown, folding and one-announcement-per-sentence logic as live) with a clock that follows the
recordings and actions that only count, so nothing is notified or spoken, and nothing is stored.
"""

from __future__ import annotations

import collections
import types
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from wavescribe.alerts import AlertConfig, Alerter
from wavescribe.rules import SEVERITIES


class _Silent:
    """Actions that do nothing: a replay must never reach a person."""

    def notify(self, title: str, body: str, *, sound: str | None = None) -> None:
        pass

    def speak(self, voice: str, text: str) -> None:
        pass

    def voices(self) -> set[str] | None:
        return None


@dataclass
class Report:
    utterances: int = 0
    first_at: float | None = None
    last_at: float | None = None
    hits: collections.Counter = field(default_factory=collections.Counter)  # rule -> hits
    terms: dict[str, collections.Counter] = field(default_factory=dict)  # rule -> term counts
    # (rule, first_at) -> (t, the highest severity it was delivered at, rule, terms, quote)
    alerts: dict[tuple[str, float], tuple[float, str, str, list[str], str]] = field(
        default_factory=dict
    )

    @property
    def announced(self) -> collections.Counter:
        """(severity, rule) -> alerts. Each alert counts once, at the highest severity it
        reached: one held at low and then confirmed is one alert."""
        return collections.Counter((a[1], a[2]) for a in self.alerts.values())

    @property
    def examples(self) -> list[tuple[float, str, str, list[str], str]]:
        return sorted(self.alerts.values())

    @property
    def hours(self) -> float:
        if self.first_at is None or self.last_at is None:
            return 0.0
        return (self.last_at - self.first_at) / 3600

    def by_severity(self) -> dict[str, int]:
        out = dict.fromkeys(reversed(SEVERITIES), 0)
        for (sev, _), n in self.announced.items():
            out[sev] += n
        return out


def replay(
    config: AlertConfig, rows: Iterable[tuple[float, str, str, str, tuple[str, ...]]]
) -> Report:
    """``rows``: (t_start, station, language, text, flags) in time order (``Store.transcripts``)."""
    report = Report()
    clock = [0.0]

    def on_alert(a: dict[str, Any]) -> None:
        if a["status"] in ("pending", "covered"):  # covered: another alert spoke for it
            return
        sev = a["delivered_severity"] or a["severity"]
        key = (a["rule"], a["first_at"])
        old = report.alerts.get(key)
        if old is None or SEVERITIES.index(sev) > SEVERITIES.index(old[1]):
            report.alerts[key] = (clock[0], sev, a["rule"], list(a["terms"]), a["quote"])

    alerter = Alerter(config, actions=_Silent(), on_alert=on_alert, clock=lambda: clock[0])
    try:
        for t, station, lang, text, flags in rows:
            clock[0] = t
            report.utterances += 1
            report.first_at = t if report.first_at is None else report.first_at
            report.last_at = t
            result = types.SimpleNamespace(
                station=types.SimpleNamespace(id=station, language=lang),
                transcript=types.SimpleNamespace(text=text),
                flags=flags,
                segment_id=None,
                hits=[],
            )
            alerter.on_transcript(result)
            for h in result.hits:
                report.hits[h.rule_id] += 1
                report.terms.setdefault(h.rule_id, collections.Counter()).update(h.terms)
            if result.hits:
                alerter.drain()  # deliveries finish before the next sentence, as in real time
    finally:
        alerter.close()
    return report
