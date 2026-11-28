# SPDX-License-Identifier: Apache-2.0
"""The page's port: a fixed ``--port N``, or the first free one of a range (``--port auto``,
``--port auto:N``). Standard library only: ``cli`` reads the option with it.

A port is only ever tried by binding a socket to it, never by connecting to it, so no program
that listens there sees a request. Binding alone isn't enough on macOS: a socket with
SO_REUSEADDR binds 127.0.0.1:P beside another program that listens on 0.0.0.0:P, and then
takes that program's connections to 127.0.0.1. So:

- ``free`` (``--port auto``): a socket without SO_REUSEADDR or SO_REUSEPORT binds on the
  wildcard address and on the bind host, and an IPv6-only one on IPv6's wildcard where the
  machine has IPv6. A port where connections are still closing (TIME_WAIT, up to 30 s on macOS
  after a server let go of it) doesn't bind that way, so it counts as taken and the next is
  tried.
- ``listened`` (a fixed ``--port``): another program listens where this one would share its
  connections: on the wildcard address, on the bind host (and 127.0.0.1 when the bind host is
  the wildcard), or on IPv6. These probes use SO_REUSEADDR, which connections still closing
  don't stop, so a fixed port binds again at once after a restart; the page's socket then binds
  with it too, and the wildcard is checked once more after that bind.
"""

from __future__ import annotations

import errno
import socket
from dataclasses import dataclass

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


def _family(addr: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in addr else socket.AF_INET


def _binds(addr: str, port: int, *, reuse: bool, v6only: bool = False) -> bool:
    """Whether a socket binds ``addr``:``port`` (and lets go of it at once). Without ``reuse``
    it has neither SO_REUSEADDR nor SO_REUSEPORT."""
    with socket.socket(_family(addr), socket.SOCK_STREAM) as s:
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if v6only:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        try:
            s.bind((addr, port))
        except OSError:
            return False
    return True


def _ipv6_taken(port: int) -> bool:
    """Whether an IPv6 socket holds ``port`` (on ::1, ::, or any other IPv6 address); False
    where the machine has no IPv6."""
    if not socket.has_ipv6:
        return False
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind(("::", port))
        except OSError as e:
            return e.errno == errno.EADDRINUSE
    return False


def free(host: str, port: int) -> bool:
    """``--port auto``'s rule: sockets without SO_REUSEADDR or SO_REUSEPORT bind ``port`` on the
    wildcard address and on ``host``, and nothing holds it on IPv6."""
    places = ["0.0.0.0"] + ([host] if host not in WILDCARD else [])
    return all(_binds(addr, port, reuse=False) for addr in places) and not _ipv6_taken(port)


def listened(host: str, port: int) -> bool:
    """A fixed ``--port``'s rule: whether another program listens where a page on ``host`` would
    share its connections. Connections still closing don't count."""
    places = ["0.0.0.0"]
    places += ["127.0.0.1"] if host in WILDCARD else [host]
    return not all(_binds(addr, port, reuse=True) for addr in places) or _ipv6_taken(port)


def check(host: str, port: int) -> None:
    """Raise PortInUseError when a fixed ``port`` is taken on ``host`` (``listened``)."""
    if listened(host, port):
        raise PortInUseError(f"port {port} is already in use on {host}; choose another with --port")


def wildcard_taken(host: str, port: int) -> bool:
    """After the page's own socket bound a specific ``host`` with SO_REUSEADDR: whether another
    program listens on the wildcard address beside it (it may have started meanwhile)."""
    return host not in WILDCARD and not _binds("0.0.0.0", port, reuse=True)


def system_port() -> int:
    """A port the system picks as free on every address (a bind of the wildcard to port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("0.0.0.0", 0))
        return int(s.getsockname()[1])


def choose(host: str, auto: AutoPort, skip: frozenset[int] = frozenset()) -> int:
    """``--port auto``: the first port of ``auto``'s range that is ``free`` (and not in
    ``skip``), else one the system picks that is free too."""
    for port in auto.candidates():
        if port not in skip and free(host, port):
            return port
    for _ in range(SYSTEM_TRIES):
        port = system_port()
        if port not in skip and free(host, port):
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
