# SPDX-License-Identifier: Apache-2.0
"""The page's port: taken when some socket listens on it, on any address, by the system's own
table; connections still closing (TIME_WAIT) leave it free. ``--port auto`` takes the first one
nothing listens on, and a fixed ``--port`` something listens on stops serve (exit code 4).

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
from collections import Counter
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
    return next(p for p in range(port, HIGH + 1) if not ports.taken("127.0.0.1", p))


@pytest.fixture(params=["table", "binds"])
def rule(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Each rule test runs twice: by the system's table, and by the sockets that stand in for it
    where the table can't be read (as inside a sandbox)."""
    if request.param == "binds":
        monkeypatch.setattr(ports, "listening", lambda: None)
    elif ports.listening() is None:
        pytest.skip("the system's table of sockets can't be read here")
    return request.param


NETSTAT = """\
Active Internet connections (including servers)
Proto Recv-Q Send-Q  Local Address          Foreign Address        (state)
tcp4       0      0  192.0.2.10.50123       198.51.100.7.443       ESTABLISHED
tcp4       0      0  127.0.0.1.8791         127.0.0.1.50124        TIME_WAIT
tcp4       0      0  127.0.0.1.8790         *.*                    LISTEN
tcp46      0      0  *.8792                 *.*                    LISTEN
tcp6       0      0  ::1.8793               *.*                    LISTEN
tcp6       0      0  fe80::1%lo0.8794       *.*                    LISTEN
tcp4       0      0  *.8793                 *.*                    LISTEN
"""

LSOF = """\
p501
f12
tIPv4
n127.0.0.1:8790
f13
tIPv6
n*:8792
p502
f12
tIPv4
n127.0.0.1:8790
f14
tIPv4
n*:8792
"""

PROC_NET = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:2256 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1
   1: 0100007F:2257 0100007F:C350 06 00000000:00000000 03:00000000 00000000     0        0 0
   2: 00000000:2258 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 2
"""


def test_the_tables_of_listening_sockets_are_read_by_port() -> None:
    assert ports.parse_netstat(NETSTAT) == Counter({8790: 1, 8792: 1, 8793: 2, 8794: 1})
    assert ports.parse_netstat("") is None  # no table: netstat couldn't read it
    # One socket two processes hold counts once; IPv4 and IPv6 on the same port are two.
    assert ports.parse_lsof(LSOF) == Counter({8790: 1, 8792: 2})
    assert ports.parse_lsof("") is None
    assert ports.parse_proc_net(PROC_NET) == Counter({8790: 1, 8792: 1})  # 0x2257 TIME_WAIT


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
    monkeypatch.setattr(ports, "listening", lambda: Counter())
    monkeypatch.setattr(ports, "taken", lambda host, p, table=None: tried.append(p) or p != 61234)
    monkeypatch.setattr(ports, "system_port", lambda: 61234)
    assert ports.choose("127.0.0.1", AutoPort(8790)) == 61234  # taken is stubbed
    assert tried == [*range(8790, 8810), 61234]


def test_auto_reads_the_table_once_for_its_whole_range(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[int] = []
    monkeypatch.setattr(ports, "listening",
                        lambda: reads.append(1) or Counter(range(LOW, LOW + 4)))  # fmt: skip
    assert ports.choose("127.0.0.1", AutoPort(LOW)) == LOW + 4 and reads == [1]


def test_a_port_another_program_listens_on_the_wildcard_is_taken(rule: str) -> None:
    with listening("0.0.0.0", LOW):
        assert ports.taken("127.0.0.1", LOW) and ports.taken("0.0.0.0", LOW)
        chosen = ports.choose("127.0.0.1", AutoPort(LOW))
        assert LOW < chosen <= HIGH and chosen == first_free_from(LOW + 1)


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1"])
def test_a_port_held_on_loopback_is_taken_too(rule: str, addr: str) -> None:
    with listening(addr, LOW + 1):
        assert ports.taken("127.0.0.1", LOW + 1)
        assert ports.taken("0.0.0.0", LOW + 1)


def test_a_port_with_only_closing_connections_is_free(rule: str) -> None:
    from wavescribe.web.server import bind

    port = HIGH
    closing(port)
    with socket.socket() as strict, pytest.raises(OSError):  # it is closing: TIME_WAIT is there
        strict.bind(("127.0.0.1", port))
    assert not ports.taken("127.0.0.1", port)
    ports.check("127.0.0.1", port)  # a fixed port starts again at once

    def make(at: int) -> socket.socket:  # as the page binds: SO_REUSEADDR
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", at))
        s.listen()
        return s

    with bind(make, "127.0.0.1", port, AutoPort(port)) as s:  # and auto keeps it
        assert s.getsockname()[1] == port


def test_a_fixed_port_is_refused_beside_a_program_on_the_wildcard(rule: str) -> None:
    from wavescribe.web.server import check_port

    taken = pytest.raises(PortInUseError, match=f"port {LOW + 2} is already in use")
    with listening("0.0.0.0", LOW + 2), taken:
        check_port("127.0.0.1", LOW + 2)
    with listening("127.0.0.1", LOW + 2), pytest.raises(PortInUseError):
        check_port("0.0.0.0", LOW + 2)  # --lan beside a program on loopback


def test_a_program_that_takes_the_wildcard_while_a_fixed_port_binds_is_caught(rule: str) -> None:
    from wavescribe.web.server import bind

    others: list[socket.socket] = []

    def make(port: int) -> object:
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
        with pytest.raises(PortInUseError, match=f"port {LOW + 3} is already in use"):
            bind(make, "127.0.0.1", LOW + 3)
        assert made[0].closed
    finally:
        for s in others:
            s.close()


def test_auto_moves_on_when_its_port_is_taken_before_it_binds() -> None:
    from wavescribe.web.server import bind

    calls: list[int] = []

    def make(port: int) -> object:
        calls.append(port)
        if len(calls) == 1:
            raise OSError(errno.EADDRINUSE, "taken meanwhile")
        return port

    first = first_free_from(LOW + 4)
    got = bind(make, "127.0.0.1", first, AutoPort(LOW + 4))
    assert got == calls[1] != calls[0] == first


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
    if all(ports.taken("127.0.0.1", p) for p in range(LOW + 1, HIGH + 1)):
        pytest.skip("8791 to 8799 are all taken: serve would look past them")
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
