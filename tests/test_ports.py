# SPDX-License-Identifier: Apache-2.0
"""The page's port: ``--port auto`` finds a free one by binding only, and a fixed ``--port``
never binds beside another program that listens on the wildcard address (exit code 4).

Only ports 8790 to 8799 are used here, so a test run never touches a Wavescribe running on
the usual ones, not even with a bind."""

from __future__ import annotations

import argparse
import contextlib
import errno
import os
import re
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from wavescribe.web import ports
from wavescribe.web.ports import AutoPort, PortInUseError, port_spec

LOW, HIGH = 8790, 8799


@pytest.fixture(autouse=True)
def within_our_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto:8790`` tries 8790 to 8799 here, and the system is never asked for one."""
    monkeypatch.setattr(ports, "AUTO_TRIES", HIGH - LOW + 1)

    def no_system_port() -> int:
        raise AssertionError("asked the system for a port")

    monkeypatch.setattr(ports, "system_port", no_system_port)


@contextlib.contextmanager
def listening(addr: str, port: int, *, reuse: bool = True) -> Iterator[socket.socket]:
    """Another program's socket, listening on ``addr``:``port``; with SO_REUSEADDR, as most
    servers bind, so connections an earlier test left closing there don't stop it."""
    family = socket.AF_INET6 if ":" in addr else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as s:
        if reuse:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((addr, port))
        s.listen()
        yield s


def closing(port: int) -> None:
    """Leaves connections closing (TIME_WAIT) on 127.0.0.1:``port``, as a server that just
    stopped does: the server closes first."""
    with listening("127.0.0.1", port) as srv:
        client = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()
        time.sleep(0.05)
        client.close()


def first_free_from(port: int) -> int:
    return next(p for p in range(port, HIGH + 1) if ports.free("127.0.0.1", p))


def test_the_port_option_takes_a_number_auto_or_auto_n() -> None:
    assert port_spec("8790") == 8790
    assert port_spec("auto") == AutoPort(8765, "auto")
    assert port_spec("AUTO:8790") == AutoPort(8790, "auto:8790")
    assert str(port_spec("auto:8790")) == "auto:8790"  # as serve_argv writes it back
    assert AutoPort(8790).candidates() == range(8790, 8800)  # AUTO_TRIES is 10 here
    for bad in ("auto:0", "auto:x", "x", "0", "70000", "auto:70000"):
        with pytest.raises(argparse.ArgumentTypeError):
            port_spec(bad)


def test_twenty_ports_then_one_the_system_picks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ports, "AUTO_TRIES", 20)
    assert AutoPort(8790).candidates() == range(8790, 8810)
    assert AutoPort(65530).candidates() == range(65530, 65536)
    tried: list[int] = []
    monkeypatch.setattr(ports, "free", lambda host, p: tried.append(p) or p == 61234)
    monkeypatch.setattr(ports, "system_port", lambda: 61234)
    assert ports.choose("127.0.0.1", AutoPort(8790)) == 61234  # nothing bound: free is stubbed
    assert tried == [*range(8790, 8810), 61234]


def test_auto_skips_a_port_another_program_listens_on_the_wildcard() -> None:
    with listening("0.0.0.0", LOW):
        assert not ports.free("127.0.0.1", LOW)
        chosen = ports.choose("127.0.0.1", AutoPort(LOW))
        assert LOW < chosen <= HIGH and chosen == first_free_from(LOW + 1)


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1"])
def test_auto_skips_a_port_held_on_loopback_too(addr: str) -> None:
    with listening(addr, LOW + 1):
        assert not ports.free("127.0.0.1", LOW + 1)
        assert not ports.free("0.0.0.0", LOW + 1)


def test_closing_connections_hold_a_port_for_auto_but_not_for_a_fixed_port() -> None:
    port = HIGH
    closing(port)
    assert not ports.free("127.0.0.1", port)  # auto's rule: nothing may hold it
    assert not ports.listened("127.0.0.1", port)  # nobody listens: a restart binds at once
    ports.check("127.0.0.1", port)


def test_a_fixed_port_is_refused_beside_a_program_on_the_wildcard() -> None:
    from wavescribe.web.server import check_port

    with listening("0.0.0.0", LOW + 2):
        assert ports.listened("127.0.0.1", LOW + 2)
        with pytest.raises(PortInUseError, match=f"port {LOW + 2} is already in use"):
            check_port("127.0.0.1", LOW + 2)
    with listening("127.0.0.1", LOW + 2), pytest.raises(PortInUseError):
        check_port("0.0.0.0", LOW + 2)  # --lan beside a program on loopback


def test_a_program_that_takes_the_wildcard_while_a_fixed_port_binds_is_caught() -> None:
    from wavescribe.web.server import bind

    others: list[socket.socket] = []

    def make(port: int, reuse: bool) -> object:
        assert reuse  # a fixed port binds with SO_REUSEADDR, over closing connections
        ours = socket.socket()
        ours.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ours.bind(("127.0.0.1", port))
        ours.listen()
        other = socket.socket()  # meanwhile, another program
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind(("0.0.0.0", port))
        other.listen()
        others.append(other)

        class Server:
            closed = False

            def server_close(self) -> None:
                self.closed = True
                ours.close()

        made.append(Server())
        return made[-1]

    made: list = []
    try:
        with pytest.raises(PortInUseError, match=r"0\.0\.0\.0"):
            bind(make, "127.0.0.1", LOW + 3)
        assert made[0].closed
    finally:
        for s in others:
            s.close()


def test_auto_moves_on_when_its_port_is_taken_before_it_binds() -> None:
    from wavescribe.web.server import bind

    calls: list[tuple[int, bool]] = []

    def make(port: int, reuse: bool) -> object:
        calls.append((port, reuse))
        if len(calls) == 1:
            raise OSError(errno.EADDRINUSE, "taken meanwhile")
        return port

    first = first_free_from(LOW + 4)
    got = bind(make, "127.0.0.1", first, AutoPort(LOW + 4))
    assert calls[0] == (first, False)  # without SO_REUSEADDR
    assert got == calls[1][0] != first and not calls[1][1]


# -- serve itself --------------------------------------------------------------------------


def serve_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "WAVESCRIBE_HOME"}


def test_serve_on_a_fixed_port_beside_a_wildcard_listener_exits_4(tmp_path: Path) -> None:
    with listening("0.0.0.0", LOW + 5):
        p = subprocess.run(
            [sys.executable, "-m", "wavescribe", "serve", "--home", str(tmp_path / "h"),
             "--port", str(LOW + 5), "--no-store", "--no-alerts"],
            capture_output=True, text=True, env=serve_env(), timeout=60,
        )  # fmt: skip
    assert p.returncode == 4, p.stderr
    assert f"wavescribe: port {LOW + 5} is already in use on 127.0.0.1" in p.stderr
    assert not (tmp_path / "h" / "data" / "control.key").exists()  # nothing was loaded


def test_serve_auto_skips_the_taken_port_and_says_where_it_runs(tmp_path: Path) -> None:
    if not any(ports.free("127.0.0.1", p) for p in range(LOW + 1, HIGH + 1)):
        pytest.skip("8791 to 8799 are all taken or closing: serve would look past them")
    with listening("0.0.0.0", LOW):
        log = (tmp_path / "serve.log").open("w+")
        p = subprocess.Popen(
            [sys.executable, "-m", "wavescribe", "serve", "--home", str(tmp_path / "h"),
             "--port", f"auto:{LOW}", "--no-store", "--no-alerts"],
            stdout=log, stderr=subprocess.STDOUT, env=serve_env(), text=True,
        )  # fmt: skip
        try:
            deadline = time.monotonic() + 60
            while "running at" not in (text := (tmp_path / "serve.log").read_text()):
                assert p.poll() is None and time.monotonic() < deadline, text
                time.sleep(0.2)
            at = re.search(r"Wavescribe is running at http://localhost:(\d+)/", text)
            assert at is not None and LOW < int(at[1]) <= HIGH, text
        finally:
            p.send_signal(signal.SIGINT)
            assert p.wait(30) == 130
