# SPDX-License-Identifier: Apache-2.0
"""Digests read aloud, in the alerts' voices, without ever holding an alert back.

Alerts and digests share the machine's one voice (``say``, which would talk over itself).
``Speech`` stands in for the alerts' actions and takes the turns: an alert waits only for the
sentence being spoken to end; a digest waits while an alert speaks, and ends at the next
sentence once another alert wants to speak, as the alert is the news then. A digest is read
a point at a time, so an alert never waits long.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING

from wavescribe.languages import same_language
from wavescribe.rules.text import NO_SPACES

if TYPE_CHECKING:
    from wavescribe.alerts import Voice
    from wavescribe.alerts.actions import Actions
    from wavescribe.digest import Digest

log = logging.getLogger(__name__)

SECONDS = 45.0  # a spoken digest at most: a few points, not a programme
CHARS_PER_S = 14.0  # as `say` reads at its usual rate
DENSE_CHARS_PER_S = 6.0  # scripts written without spaces carry more in a character


class Speech:
    """The alerts' actions, with digests taking their turn behind alerts."""

    def __init__(self, actions: Actions) -> None:
        self.actions = actions
        self._voice = threading.Lock()  # held while anything is being said
        self._turns = threading.Condition()
        self._alerts = 0  # alerts waiting to speak, or speaking
        self._alerts_said = 0  # so that a digest can tell that one came while it was read
        self._digest = threading.Lock()  # one digest read aloud at a time

    def notify(self, title: str, body: str) -> None:
        self.actions.notify(title, body)

    def show(self, title: str, body: str) -> None:
        """A notification without the alerts' sound: a digest is no alert."""
        self.actions.notify(title, body, sound=False)

    def voices(self) -> set[str] | None:
        return self.actions.voices()

    def speak(self, voice: str, text: str) -> None:
        """An alert, as soon as the sentence being said ends."""
        with self._turns:
            self._alerts += 1
        with self._voice:
            try:
                self.actions.speak(voice, text)
            finally:
                with self._turns:
                    self._alerts -= 1
                    self._alerts_said += 1

    def claim(self) -> bool:
        """The turn of one digest; False while another is being read aloud. ``read`` gives it
        back."""
        return self._digest.acquire(blocking=False)

    def read(self, voice: str, sentences: Iterable[str], stop: threading.Event) -> int:
        """A digest whose turn was claimed, a sentence at a time, never over an alert: it
        starts once an alert being said has ended, and ends early when another alert comes or
        ``stop`` is set. Returns how many sentences were said."""
        said, seen = 0, None
        try:
            for sentence in sentences:
                with self._voice:
                    with self._turns:
                        if seen is None:
                            seen = self._alerts_said
                        if self._alerts or self._alerts_said != seen or stop.is_set():
                            break
                    self.actions.speak(voice, sentence)
                said += 1
        finally:
            self._digest.release()
        return said


def spoken(digest: Digest, *, seconds: float = SECONDS) -> list[str]:
    """What is read aloud of a digest: the alerts of the window in the listener's language,
    then each point with the stations that said it, without the numbers of its sources; as
    many as fit in ``seconds``, and at least one. With no points, the model's sentence that
    nothing of substance was said."""
    language = digest.request.language
    out: list[str] = []
    for a in digest.alerts:
        summaries = a.get("summaries") or {}
        said = next((t for lang, t in summaries.items() if same_language(lang, language)), None)
        if said is None and same_language(a.get("language") or "", language):
            said = a.get("quote")
        if said:
            out.append(said)
    out += [f"{digest.heard_on(p)}{p.text}" for p in digest.points]
    if not digest.points and digest.nothing:
        out.append(digest.text())
    kept, left = [], seconds
    for line in out:
        left -= len(line) / (DENSE_CHARS_PER_S if NO_SPACES.search(line) else CHARS_PER_S)
        if kept and left < 0:
            break
        kept.append(line)
    return kept


class Speaker:
    """Reads digests aloud in the voice the alerts use for their language (``[speak.<lang>]``
    in alerts.toml)."""

    def __init__(
        self, speech: Speech, voices: Callable[[], Mapping[str, Voice]], closed: threading.Event
    ) -> None:
        self.speech = speech
        self._voices = voices  # read each time: the page changes the alerts' settings
        self._closed = closed  # the app is closing: stop at the next sentence

    def voice(self, language: str) -> str | None:
        return next(
            (v.voice for lang, v in self._voices().items() if same_language(lang, language)), None
        )

    def start(self, digest: Digest) -> dict[str, object]:
        """Start reading ``digest`` aloud in the background: ``{"started": true}``, or
        ``{"started": false, "error": why}``."""
        voice, why = self._ready(digest)
        if voice is None:
            return {"started": False, "error": why}
        threading.Thread(
            target=self._read, args=(voice, digest), daemon=True, name="digest-speech"
        ).start()
        return {"started": True}

    def say(self, digest: Digest) -> str | None:
        """Read ``digest`` aloud and return when done; None, or why it wasn't read."""
        voice, why = self._ready(digest)
        if voice is None:
            return why
        self._read(voice, digest)
        return None

    def _ready(self, digest: Digest) -> tuple[str | None, str]:
        language = digest.request.language
        voice = self.voice(language)
        if voice is None:
            return None, (
                f"no voice speaks {language}: give it one in alerts.toml, [speak.{language}], "
                "as for spoken alerts"
            )
        installed = self.speech.voices()
        if installed is not None and voice not in installed:
            return None, (
                f"the voice {voice!r} is not installed (System Settings > Accessibility > "
                "Spoken Content)"
            )
        if not spoken(digest):
            return None, "there is nothing to read aloud: " + digest.text()
        if not self.speech.claim():
            return None, "another digest is being read aloud; try again when it ends"
        return voice, ""

    def _read(self, voice: str, digest: Digest) -> None:
        sentences = spoken(digest)
        try:
            said = self.speech.read(voice, sentences, self._closed)
        except Exception:
            log.exception("reading a digest aloud failed")
            return
        if said < len(sentences):
            log.info(
                "digest read aloud in part (%d of %d): an alert came first", said, len(sentences)
            )
