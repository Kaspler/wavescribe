# SPDX-License-Identifier: Apache-2.0
"""The page's port: a fixed ``--port N``, or the first free one of a range (``--port auto``,
``--port auto:N``). Standard library only: ``cli`` reads the option with it.

A port is taken when some socket listens on it, on any address, as the system's own table of
sockets says: ``netstat`` on macOS (every user's sockets), else ``lsof`` (this user's), and
``/proc/net/tcp`` on Linux. One reading answers for every port tried, and nothing connects to a
port to find out, so no program that listens there sees a request. Connections still closing
(TIME_WAIT, after a run stopped) don't listen, so a port they hold is free: the page's socket
binds with SO_REUSEADDR as servers usually do. Where no table can be read (a sandbox may hide
it), sockets with SO_REUSEADDR stand in for it: one that binds where another listens fails, on
the IPv4 and IPv6 wildcard and loopback addresses and the bind host.

The table is what catches a program on the wildcard address: on macOS a socket with
SO_REUSEADDR binds 127.0.0.1:P beside another that listens on 0.0.0.0:P, and then takes that
program's connections to 127.0.0.1. Once the page listens, the table is read once more, for a
program that started listening on the port meanwhile.
"""

from __future__ import annotations

import errno
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

AUTO_FROM = 8765  # where --port auto starts
AUTO_TRIES = 20  # ports tried in turn before the system picks one
SYSTEM_TRIES = 10  # ports the system picks, each checked like the others
WILDCARD = ("0.0.0.0", "::", "")


class PortInUseError(OSError):
    """Something already listens on the port: another program, or a Wavescribe already running
    (``doctor.port_taken`` tells which)."""


@dataclass(frozen=True)
class AutoPort:
    """``--port auto`` (from 8765) or ``--port auto:N``: the first free port of N to N+19, else
    one the system picks. ``str`` gives the option back as it was typed."""

    start: int = AUTO_FROM
    typed: str = "auto"

    def __str__(self) -> str:
        return self.typed

    def candidates(self) -> range:
        return range(self.start, min(self.start + AUTO_TRIES, 65536))


def port_number(value: str) -> int:
    """A TCP port: 1 to 65535."""
    import argparse

    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a port number") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"{port} isn't a port: ports are 1 to 65535")
    return port


def port_spec(value: str) -> int | AutoPort:
    """``serve --port``: a port number, ``auto`` or ``auto:N``."""
    import argparse

    text = value.strip().lower()
    if text == "auto":
        return AutoPort()
    if text.startswith("auto:"):
        try:
            start = port_number(text[len("auto:") :])
        except argparse.ArgumentTypeError as e:
            raise argparse.ArgumentTypeError(f"{value!r}: auto:N starts at a port N; {e}") from None
        return AutoPort(start, f"auto:{start}")
    try:
        return port_number(text)
    except argparse.ArgumentTypeError:
        raise argparse.ArgumentTypeError(
            f"{value!r} isn't a port: give a number from 1 to 65535, auto or auto:N"
        ) from None


def parse_netstat(text: str) -> Counter[int] | None:
    """The LISTEN rows of ``netstat -an -p tcp`` (macOS, the BSDs), IPv4 and IPv6, by port; a
    row's local address ends in ``.<port>`` (``*.8765``, ``127.0.0.1.8765``, ``::1.8765``). None
    without the table's heading: netstat couldn't read the table."""
    if "Active Internet connections" not in text:
        return None
    found: Counter[int] = Counter()
    for line in text.splitlines():
        cols = line.split()
        if len(cols) >= 6 and cols[0].startswith("tcp") and cols[5] == "LISTEN":
            port = cols[3].rpartition(".")[2]
            if port.isdigit():
                found[int(port)] += 1
    return found


def parse_lsof(text: str) -> Counter[int] | None:
    """The listening sockets of ``lsof -nP -iTCP -sTCP:LISTEN -Ftn``, by port: its ``t`` (IPv4,
    IPv6) and ``n`` (``*:8765``, ``127.0.0.1:8765``, ``[::1]:8765``) fields. A socket that more
    than one process holds is listed under each and counted once. None when nothing is listed:
    lsof may not have been able to read anything."""
    seen: set[tuple[str, str]] = set()
    kind = ""
    for line in text.splitlines():
        if line.startswith("t"):
            kind = line[1:]
        elif line.startswith("n"):
            seen.add((kind, line[1:]))
    found: Counter[int] = Counter()
    for _, name in seen:
        port = name.rpartition(":")[2]
        if port.isdigit():
            found[int(port)] += 1
    return found or None


def parse_proc_net(text: str) -> Counter[int]:
    """The sockets in LISTEN (state 0A) of Linux's ``/proc/net/tcp`` or ``tcp6``, by port."""
    found: Counter[int] = Counter()
    for line in text.splitlines()[1:]:
        cols = line.split()
        if len(cols) > 3 and cols[3] == "0A":
            found[int(cols[1].rpartition(":")[2], 16)] += 1
    return found


def _netstat() -> Counter[int] | None:
    try:
        out = subprocess.run(["netstat", "-an", "-p", "tcp"], capture_output=True, text=True,
                             timeout=10, check=True).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_netstat(out)


def _lsof() -> Counter[int] | None:
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Ftn"],
                             capture_output=True, text=True, timeout=10).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_lsof(out)


def _proc_net() -> Counter[int] | None:
    found: Counter[int] = Counter()
    read = False
    for name in ("tcp", "tcp6"):
        try:
            found += parse_proc_net(Path("/proc/net", name).read_text())
        except OSError:
            continue
        read = True
    return found if read else None


def listening() -> Counter[int] | None:
    """How many sockets listen on each TCP port of this machine, on any address, by the
    system's table; None where it can't be read."""
    if sys.platform.startswith("linux"):
        return _proc_net()
    table = _netstat()
    return _lsof() if table is None else table


def _binds(addr: str, port: int) -> bool:
    """Where the table can't be read: whether a socket with SO_REUSEADDR binds ``addr``:``port``
    (and lets go of it at once). Connections still closing don't stop it; a listener on the
    same address does. False only for EADDRINUSE: an address this machine doesn't have
    (IPv6 switched off) holds nothing."""
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
    except OSError as e:
        return e.errno != errno.EADDRINUSE
    return True


def taken(host: str, port: int, table: Counter[int] | None = None) -> bool:
    """Whether some socket listens on ``port``, on any address (``table``: one reading of
    ``listening()``, else it is read now)."""
    table = listening() if table is None else table
    if table is not None:
        return table[port] > 0
    places = {"0.0.0.0", "127.0.0.1", *(["::", "::1"] if socket.has_ipv6 else [])}
    places |= set() if host in WILDCARD or host == "localhost" else {host}
    return not all(_binds(addr, port) for addr in sorted(places))


def check(host: str, port: int) -> None:
    """Raise PortInUseError when something listens on a fixed ``port`` (``taken``)."""
    if taken(host, port):
        raise PortInUseError(f"port {port} is already in use on {host}; choose another with --port")


def shared(host: str, port: int) -> bool:
    """Once the page listens on ``host``:``port``: whether another socket listens there too (a
    program that started meanwhile). Where the table can't be read, only a program on the
    wildcard address beside a page on one address is seen."""
    table = listening()
    if table is not None:
        return table[port] > 1
    return host not in WILDCARD and not _binds("0.0.0.0", port)


def system_port() -> int:
    """A port the system picks as free on every address (a bind of the wildcard to port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


def choose(host: str, auto: AutoPort, skip: frozenset[int] = frozenset()) -> int:
    """``--port auto``: the first port of ``auto``'s range that nothing listens on (and not in
    ``skip``), else one the system picks that nothing listens on either. One reading of the
    table answers for the whole range."""
    table = listening()
    for port in auto.candidates():
        if port not in skip and not taken(host, port, table):
            return port
    for _ in range(SYSTEM_TRIES):
        port = system_port()
        if port not in skip and not taken(host, port):
            return port
    raise PortInUseError(f"no free port for the page on {host}")


def loopback_url(host: str, port: int) -> str:
    """The page on this machine's loopback address, as the ready file and hello give it: where
    a program on this machine reaches it, also under ``--lan``. A page bound to one other
    address only is reached there."""
    if host in ("0.0.0.0", "", "localhost", "127.0.0.1"):
        return f"http://127.0.0.1:{port}/"
    if ":" in host:
        return f"http://[{'::1' if host in ('::', '::1') else host}]:{port}/"
    return f"http://{host}:{port}/"
