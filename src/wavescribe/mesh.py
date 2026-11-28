# SPDX-License-Identifier: Apache-2.0
"""Alerts to one node of a LoRa mesh (Meshtastic), through the ``meshtastic`` command-line program
the user installs. That program is GPL-3.0: Wavescribe runs it as a separate program and never
imports it (docs/LICENSING.md, "External programs").

A message is a direct message to one node, which that node acknowledges when it gets it:
``meshtastic [--port <serial> | --host <address>] --dest !<node> --sendtext <text> --ack``.
Never a channel, so never the default public one. The program's documentation doesn't say what
it prints when an acknowledgement comes or doesn't, nor how it exits; ``answer`` reads its output
for the words ACK and NAK, and this is not yet verified on real nodes (docs/FEED.md, "Mesh").

The band is shared and slow, and in Europe's 868 MHz band a node may transmit 10% of any hour
(the node itself stops at that). ``Pacer`` keeps Wavescribe's share small: a floor between
messages and a cap per 24 hours, for each node attached to this machine.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

COMMAND = "meshtastic"
MESSAGE_BYTES = 200  # the longest message: a packet carries at most 239 bytes, all included
MIN_GAP_S = 120.0  # the floor between two messages through one node
MIN_GAP_LIMITS = (30.0, 3600.0)
DAILY_CAP = 20  # messages through one node in any 24 hours
DAILY_CAP_LIMITS = (1, 200)
ACK_TIMEOUT_S = 120.0  # how long the program may wait for the node's acknowledgement
ACK_TIMEOUT_LIMITS = (10.0, 600.0)
HOLD_MAX_S = 1800.0  # a message not acknowledged goes again after the floor, doubling to this
DAY_S = 86400.0
OUTPUT_KEEP = 65536  # what is read of the program's output, from its end

NODE = re.compile(r"^![0-9a-f]{8}$")  # a node's id, as the apps show it
EVERYONE = "!ffffffff"  # the address of every node: a broadcast, never sent to
_SERIAL = re.compile(r"^(?:/dev/[\w.+-]+|COM[0-9]{1,3})$")
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_ACK = re.compile(r"\bACK\b")
_NAK = re.compile(r"\bNAK\b")
_IMPLICIT = re.compile(r"\bimplicit\b", re.IGNORECASE)


def node_id(text: str) -> str | None:
    """A node's id as ``--dest`` takes it (``!1a2b3c4d``, lower case), or None for anything else,
    every node's address (a broadcast) included."""
    node = text.strip().lower()
    return node if NODE.fullmatch(node) and node != EVERYONE else None


def device_error(device: str) -> str | None:
    """What is wrong with how the node attached to this machine is named: empty (the program
    finds one on USB), a serial port (``/dev/cu.usbserial-…``), or the name or address of one on
    the network. None when it is fine."""
    if not device or _SERIAL.fullmatch(device) or _HOST.fullmatch(device):
        return None
    return "the node's connection is a serial port (/dev/…), a host name or address, or empty"


def connection(device: str) -> list[str]:
    """The program's options that reach the node: ``--port`` for a serial port, ``--host`` for
    one on the network, none to let it find one on USB."""
    if not device:
        return []
    return ["--port", device] if _SERIAL.fullmatch(device) else ["--host", device]


def find(command: str = COMMAND) -> str | None:
    """The program's path: on the PATH, or where pipx puts the programs it installs
    (``~/.local/bin``), which a background service's PATH may lack."""
    found = shutil.which(command)
    if found or "/" in command:
        return found
    local = Path.home() / ".local" / "bin" / command
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


@dataclass(frozen=True)
class Sent:
    """What became of one message."""

    acked: bool  # the node said it has it
    on_air: bool  # it went out, as far as the program said: it counts towards the cap
    retry: bool  # worth sending again later
    error: str | None  # what to say when it wasn't acknowledged


def answer(
    code: int, said: str, *, node: str, timed_out: bool, timeout_s: float, command: str = COMMAND
) -> Sent:
    """Read what the program printed and how it ended. A NAK, or no answer within
    ``timeout_s``, means the node didn't acknowledge it: it may still have arrived, and it goes
    again later. A relay heard (an implicit ACK) is not the node's acknowledgement either, but
    the message is out on the mesh: it isn't sent again. An ACK with a clean exit is the node's.
    A failure before any of that (no node attached, the port busy) sent nothing."""
    lines = [line.strip() for line in said.splitlines() if line.strip()]
    last = lines[-1][:160] if lines else ""
    unsure = "it may or may not have arrived"
    if timed_out:
        return Sent(
            False, True, True, f"sent to {node}, not acknowledged within {timeout_s:g} s: {unsure}"
        )
    nak = next((line for line in lines if _NAK.search(line)), None)
    if nak is not None:
        return Sent(False, True, True, f"sent to {node}, not acknowledged ({nak[:160]}): {unsure}")
    if any(_IMPLICIT.search(line) and _ACK.search(line) for line in lines):
        return Sent(
            False, True, False, f"relayed by another node, not acknowledged by {node}: {unsure}"
        )
    acked = any(_ACK.search(line) for line in lines)
    if acked and code == 0:
        return Sent(True, True, False, None)
    said_last = f": {last}" if last else ""
    if not acked and code != 0:
        return Sent(False, False, True, f"not sent: {command} ended with status {code}{said_last}")
    # Neither a clear acknowledgement nor a clear failure: counted as sent, and not sent again,
    # so output this version doesn't read can never repeat a message until the cap.
    return Sent(
        False,
        True,
        False,
        f"{command} reported no acknowledgement from {node} (status {code}{said_last})",
    )


def send(
    text: str,
    node: str,
    *,
    device: str = "",
    command: str = COMMAND,
    timeout_s: float = ACK_TIMEOUT_S,
    closed: Callable[[], bool] = lambda: False,
) -> Sent:
    """Send ``text`` to ``node`` as a direct message and wait for its acknowledgement, at most
    ``timeout_s``. ``closed``: Wavescribe is stopping, so the program is stopped too."""
    exe = find(command)
    if exe is None:
        return Sent(
            False,
            False,
            True,
            f"not sent: the {command} program isn't installed, or isn't on the PATH",
        )
    if text.startswith("-"):  # it would read as an option
        text = " " + text
    args = [exe, *connection(device), "--dest", node, "--sendtext", text, "--ack"]
    with tempfile.TemporaryFile() as out:  # a pipe could fill up and stall the program
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT
            )
        except OSError as e:
            return Sent(False, False, True, f"not sent: {exe} could not start ({e})")
        deadline = time.monotonic() + timeout_s
        timed_out = stopped = False
        while True:
            try:
                code = proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                stopped = closed()
                timed_out = not stopped and time.monotonic() >= deadline
                if stopped or timed_out:
                    proc.kill()
                    code = proc.wait()
                    break
        size = out.seek(0, os.SEEK_END)
        out.seek(max(0, size - OUTPUT_KEEP))
        said = out.read().decode("utf-8", "replace")
    if stopped:
        return Sent(False, True, True, "stopped while waiting for an acknowledgement")
    name = Path(command).name  # the program, not where it is
    return answer(code, said, node=node, timed_out=timed_out, timeout_s=timeout_s, command=name)


@dataclass
class Pacer:
    """The messages sent through one node in the last 24 hours, for the floor between them and
    the cap. ``busy`` is held while the program runs: a node takes one message at a time."""

    sent: list[float] = field(default_factory=list)  # when, oldest first
    busy: threading.Lock = field(default_factory=threading.Lock)
    seeded: set[str] = field(default_factory=set)  # outputs whose earlier messages are counted
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, times: Iterable[float]) -> None:
        with self._lock:
            self.sent = sorted([*self.sent, *times])

    def note(self, t: float) -> None:
        self.add([t])

    def undo(self, t: float) -> None:
        """A message noted when the program started didn't go out after all."""
        with self._lock:
            if t in self.sent:
                self.sent.remove(t)

    def count(self, now: float) -> int:
        with self._lock:
            return sum(1 for t in self.sent if t > now - DAY_S)

    def wait(self, now: float, gap_s: float, cap: int) -> tuple[float, str | None]:
        """How long until the next message may go, and why it can't yet: "cap" (``cap``
        messages in the last 24 hours) or "gap" (less than ``gap_s`` since the last one)."""
        with self._lock:
            self.sent = [t for t in self.sent if t > now - DAY_S]
            until_cap = self.sent[-cap] + DAY_S - now if len(self.sent) >= cap else 0.0
            until_gap = self.sent[-1] + gap_s - now if self.sent else 0.0
        if until_cap > 0:
            return max(until_cap, until_gap), "cap"
        if until_gap > 0:
            return until_gap, "gap"
        return 0.0, None
