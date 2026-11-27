# SPDX-License-Identifier: Apache-2.0
"""Digests read aloud, in the alerts' voices, without ever holding an alert back.

Alerts and digests share the machine's one voice (``say``, which would talk over itself).
``Speech`` stands in for the alerts' actions and takes the turns: an alert stops a digest
being read at once, mid-sentence, and the rest of the digest isn't read, as the alert is the
news then; a digest waits while an alert speaks. Closing stops it too.

A digest never sounds like an alert: it opens with a fixed sentence in the listener's
language that says what it is, who wrote it and from how much (``summary`` in
``[speak.<lang>]``), names the window's earlier alerts only by their time and rule
(``earlier``), and then reads its points, each cut to a sentence or two.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from wavescribe.digest import within
from wavescribe.events import local_time
from wavescribe.languages import same_language
from wavescribe.rules.text import NO_SPACES

if TYPE_CHECKING:
    from wavescribe.alerts import Voice
    from wavescribe.alerts.actions import Actions
    from wavescribe.digest import Digest

log = logging.getLogger(__name__)

SECONDS = 45.0  # a spoken digest's points at most: a few points, not a programme
CHARS_PER_S = 14.0  # as `say` reads at its usual rate
DENSE_CHARS_PER_S = 6.0  # scripts written without spaces carry more in a character
SPOKEN_CHARS = 300  # one utterance at most, cut at a sentence's end: about 20 s of `say`
STATIONS_CHARS = 80  # of the opening, so that the sentence after it is still said
EARLIER_LISTED = 5  # earlier alerts named; the count says how many there were
CLOSE_WAIT_S = 2.0
# The words a digest is introduced with, for English; other languages give their own in
# [speak.<lang>], as they do the alerts' fallback.
SUMMARY = (
    "Wavescribe summary of {stations}, {start} to {end}. Written by the local model from {read} "
    "of {said} lines; it can miss things or get them wrong."
)
EARLIER = "Earlier alerts, {count} in all: {alerts}."
FIELDS = {
    "summary": "{stations}, {start}, {end}, {read} and {said}",
    "earlier": "{count} and {alerts}",
}


class Speech:
    """The alerts' actions, with digests taking their turn behind alerts."""

    def __init__(self, actions: Actions) -> None:
        self.actions = actions
        self._voice = threading.Lock()  # held while anything is being said
        self._turns = threading.Condition()
        self._alerts = 0  # alerts waiting to speak, or speaking
        self._cut = threading.Event()  # an alert came, or the app is closing: stop the digest
        self._closing = False
        self._digest = threading.Lock()  # one digest read aloud at a time

    def notify(self, title: str, body: str) -> None:
        self.actions.notify(title, body)

    def show(self, title: str, body: str) -> None:
        """A notification without the alerts' sound: a digest is no alert."""
        self.actions.notify(title, body, sound=False)

    def voices(self) -> set[str] | None:
        return self.actions.voices()

    def speak(self, voice: str, text: str) -> None:
        """An alert, at once: a digest being read stops mid-sentence."""
        with self._turns:
            self._alerts += 1
            self._cut.set()
        try:
            with self._voice:
                self.actions.speak(voice, text)
        finally:
            with self._turns:
                self._alerts -= 1

    def claim(self) -> bool:
        """The turn of one digest; False while another is being read aloud. ``read`` gives it
        back."""
        return self._digest.acquire(blocking=False)

    def read(self, voice: str, sentences: Iterable[str], stop: threading.Event) -> int:
        """A digest whose turn was claimed, a sentence at a time, never over an alert: it
        starts once an alert being said has ended, and stops at once when another alert comes
        or ``stop`` is set. Returns how many sentences were said to the end."""
        said = 0
        try:
            with self._turns:
                if self._alerts or self._closing:
                    return 0
                self._cut.clear()  # an alert said before this digest began isn't one after it
            for sentence in sentences:
                with self._voice:
                    if self._cut.is_set() or stop.is_set():
                        break
                    done = self.actions.speak_until(
                        voice, sentence, lambda: self._cut.is_set() or stop.is_set()
                    )
                if not done:
                    break
                said += 1
        finally:
            self._digest.release()
        return said

    def close(self) -> None:
        """Stops a digest being read and waits a moment for its ``say`` to end, so that it
        doesn't outlive the app."""
        with self._turns:
            self._closing = True
            self._cut.set()
        if self._voice.acquire(timeout=CLOSE_WAIT_S):
            self._voice.release()


def spoken(digest: Digest, voice: Voice, *, seconds: float = SECONDS) -> list[str]:
    """What is read aloud of a digest: its opening, its earlier alerts by time and rule, then
    each point with the stations that said it, without the numbers of its sources; as many
    points as fit in ``seconds``, at least one. Nothing without points: a voice saying that
    nothing stood out would sound like an all-clear, so that is only shown."""
    if not digest.points:
        return []
    r = digest.request
    phrases = phrases_of(voice, r.language)
    if phrases is None:
        return []
    summary, earlier = phrases
    names = ", ".join(digest.names.get(s, s) for s in r.stations)
    out = [
        within(
            summary.format(
                stations=within(names, STATIONS_CHARS),
                start=local_time(r.since, "%H:%M"),
                end=local_time(r.until, "%H:%M"),
                read=f"{digest.read:,}",
                said=f"{sum(digest.said.values()):,}",
            ),
            SPOKEN_CHARS,
        )
    ]
    alerts = digest.alerts  # each incident once, when it was announced
    if alerts:
        listed = "; ".join(
            f"{local_time(a['announced_at'], '%H:%M')}, {a['rule']}"
            for a in alerts[-EARLIER_LISTED:]
        )
        out.append(within(earlier.format(count=len(alerts), alerts=listed), SPOKEN_CHARS))
    left, points = seconds, []
    for p in digest.points:
        line = within(f"{digest.heard_on(p)}{p.text}", SPOKEN_CHARS)
        left -= len(line) / (DENSE_CHARS_PER_S if NO_SPACES.search(line) else CHARS_PER_S)
        if points and left < 0:
            break
        points.append(line)
    return out + points


def phrases_of(voice: Voice, language: str) -> tuple[str, str] | None:
    """The opening and the earlier-alerts sentence in ``language``; None when the voice has
    none and the language isn't English."""
    english = same_language(language, "en")
    summary = voice.summary or (SUMMARY if english else "")
    earlier = voice.earlier or (EARLIER if english else "")
    return (summary, earlier) if summary and earlier else None


def check_phrases(summary: str, earlier: str) -> str | None:
    """Why a voice's own phrases can't be used, or None."""
    for key, text, sample in (
        ("summary", summary, {"stations": "", "start": "", "end": "", "read": "", "said": ""}),
        ("earlier", earlier, {"count": "", "alerts": ""}),
    ):
        try:
            text.format(**sample)
        except (KeyError, IndexError, ValueError):
            return f"{key} may use only {FIELDS[key]}, each in braces"
    return None


class Speaker:
    """Reads digests aloud in the voice the alerts use for their language (``[speak.<lang>]``
    in alerts.toml)."""

    def __init__(
        self, speech: Speech, voices: Callable[[], Mapping[str, Voice]], closed: threading.Event
    ) -> None:
        self.speech = speech
        self._voices = voices  # read each time: the page changes the alerts' settings
        self._closed = closed  # the app is closing: stop at once

    def voice(self, language: str) -> Voice | None:
        return next(
            (v for lang, v in self._voices().items() if same_language(lang, language)), None
        )

    def start(self, digest: Digest) -> dict[str, object]:
        """Start reading ``digest`` aloud in the background: ``{"started": true}``, or
        ``{"started": false, "error": why}``."""
        ready, why = self._ready(digest)
        if ready is None:
            return {"started": False, "error": why}
        threading.Thread(target=self._read, args=ready, daemon=True, name="digest-speech").start()
        return {"started": True}

    def say(self, digest: Digest) -> str | None:
        """Read ``digest`` aloud and return when done; None, or why it wasn't read."""
        ready, why = self._ready(digest)
        if ready is None:
            return why
        self._read(*ready)
        return None

    def _ready(self, digest: Digest) -> tuple[tuple[str, list[str]] | None, str]:
        language = digest.request.language
        voice = self.voice(language)
        if voice is None:
            return None, (
                f"no voice speaks {language}: give it one in alerts.toml, [speak.{language}], "
                "as for spoken alerts"
            )
        installed = self.speech.voices()
        if installed is None:
            return None, "this machine has no speech to read it with (macOS `say`)"
        if voice.voice not in installed:
            return None, (
                f"the voice {voice.voice!r} is not installed (System Settings > Accessibility > "
                "Spoken Content)"
            )
        if not digest.points:
            return None, "there is nothing to read aloud: " + digest.text()
        phrases = phrases_of(voice, language)
        if phrases is None:
            return None, (
                f"a digest read aloud opens by saying what it is: give [speak.{language}] in "
                "alerts.toml a summary and an earlier sentence, as alerts.example.toml shows"
            )
        wrong = check_phrases(*phrases)
        if wrong:
            return None, f"[speak.{language}] in alerts.toml: {wrong}"
        if not self.speech.claim():
            return None, "another digest is being read aloud; try again when it ends"
        return (voice.voice, spoken(digest, voice)), ""

    def _read(self, voice: str, sentences: list[str]) -> None:
        try:
            said = self.speech.read(voice, sentences, self._closed)
        except Exception:
            log.exception("reading a digest aloud failed")
            return
        if said < len(sentences):
            log.info(
                "digest read aloud in part (%d of %d): stopped for an alert", said, len(sentences)
            )
