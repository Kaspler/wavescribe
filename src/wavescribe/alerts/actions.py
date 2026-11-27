# SPDX-License-Identifier: Apache-2.0
"""How alerts reach people: a desktop notification and a spoken message.

Mac-specific code stays here, behind ``Actions``. On macOS, notifications go through
``osascript`` and speech through ``say`` (offline once a voice is downloaded; Apple's licence
allows local playback). Elsewhere, ``LogOnlyActions`` logs instead and says so.
"""

from __future__ import annotations

import functools
import logging
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger(__name__)

SAY_TIMEOUT_S = 180.0
STOP_POLL_S = 0.05


class ActionError(RuntimeError):
    """An action could not confirm delivery."""


class Actions(Protocol):
    def notify(self, title: str, body: str, *, sound: bool = True) -> None: ...

    def speak(self, voice: str, text: str) -> None: ...

    def speak_until(self, voice: str, text: str, stopped: Callable[[], bool]) -> bool:
        """As ``speak``, cut off as soon as ``stopped()``; False when it was."""
        ...

    def voices(self) -> set[str] | None:
        """Installed voice names, or None if unknown."""
        ...


class MacActions:
    def notify(self, title: str, body: str, *, sound: bool = True) -> None:
        # Text goes in as arguments, never spliced into the script, so quotes can't break it.
        shown = "display notification (item 2 of argv) with title (item 1 of argv)"
        script = ["on run argv", shown + (' sound name "Glass"' if sound else ""), "end run"]
        cmd = ["osascript"] + [a for line in script for a in ("-e", line)] + [title, body[:1000]]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if done.returncode != 0:
            raise ActionError(done.stderr.strip() or f"osascript exited {done.returncode}")

    def speak(self, voice: str, text: str) -> None:
        # `say` reads stdin when no text is given, so text starting with "-" is safe.
        done = subprocess.run(
            ["say", "-v", voice], input=_plain(text), capture_output=True, text=True,
            timeout=SAY_TIMEOUT_S,
        )  # fmt: skip
        if done.returncode != 0:
            raise ActionError(done.stderr.strip() or f"say exited {done.returncode}")

    def speak_until(self, voice: str, text: str, stopped: Callable[[], bool]) -> bool:
        proc = subprocess.Popen(
            ["say", "-v", voice],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _, err = proc.communicate(_plain(text), timeout=STOP_POLL_S)
        except subprocess.TimeoutExpired:
            deadline = time.monotonic() + SAY_TIMEOUT_S
            while proc.poll() is None:
                if stopped() or time.monotonic() > deadline:
                    proc.terminate()
                    proc.wait(timeout=5)
                    return False
                time.sleep(STOP_POLL_S)
            _, err = proc.communicate()
        finally:
            if proc.poll() is None:  # an exception on the way: `say` mustn't go on alone
                proc.kill()
                proc.wait()
        if proc.returncode != 0:
            raise ActionError(err.strip() or f"say exited {proc.returncode}")
        return True

    def voices(self) -> set[str] | None:
        return installed_voices()


class LogOnlyActions:
    """Portable fallback: notifications are logged; there is no speech."""

    def notify(self, title: str, body: str, *, sound: bool = True) -> None:
        log.warning("%s %s: %s", "ALERT" if sound else "NOTE", title, body)

    def speak(self, voice: str, text: str) -> None:
        raise ActionError("no text-to-speech on this platform")

    def speak_until(self, voice: str, text: str, stopped: Callable[[], bool]) -> bool:
        raise ActionError("no text-to-speech on this platform")

    def voices(self) -> set[str] | None:
        return None


def platform_actions() -> Actions:
    if sys.platform == "darwin" and shutil.which("say") and shutil.which("osascript"):
        return MacActions()
    return LogOnlyActions()


@functools.cache
def installed_voices() -> set[str] | None:
    """Names `say` accepts. `say -v Unknown` silently uses the default voice, so check first."""
    try:
        out = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=30, check=True
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_voices(out)


def parse_voices(listing: str) -> set[str]:
    """Voice names from `say -v ?`, whose lines look like
    "Daniel (English (UK)) en_GB    # Hello! ..." or "Lesya               uk_UA    # ...".
    """
    names: set[str] = set()
    for line in listing.splitlines():
        left = line.split("#", 1)[0].rstrip()
        parts = left.rsplit(None, 1)
        if len(parts) != 2 or not re.fullmatch(r"[a-z]{2,3}_\w+", parts[1]):
            continue
        name = parts[0].strip()
        names.add(name)
        names.add(name.split(" (")[0])  # "Daniel (English (UK))" answers to "Daniel"
    return names


def _plain(text: str) -> str:
    """``text`` as `say` reads it without obeying it. `say` obeys commands embedded in its
    text: "[[volm 0]]" mutes the rest of the alert and "[[slnc 60000]]" holds it back a
    minute. The text comes from the radio or from a translation of it, so the opening "[[" is
    split and read as plain brackets."""
    return re.sub(r"\[(?=\[)", "[ ", text)
