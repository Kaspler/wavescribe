# SPDX-License-Identifier: Apache-2.0
"""`wavescribe serve` with three made-up receivers and a made-up sky, for driving the page
without hardware:

    uv run python tests/page_fixture.py --port 8771

Everything lives in a fresh directory (``--dir``, or a temporary one): the stations file, the
page's settings, the store, the world. Only stand-ins for rtl_sdr, rtl_test and rtl_power are
on the PATH, so no real dongle can be opened. The receivers have serials no dongle has; their
stations are listen only, so no model is loaded. Receiver A is on the made-up FM band with two
of its stations on; B and C are off until something sends them out.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SERIALS = ("SIM00001", "SIM00002", "SIM00003")
STARTED = ("valley", "harbour")

STATIONS = f"""\
[defaults]
language = "es"
translate_to = ["en"]

[[device]]
id = "a"
serial = "{SERIALS[0]}"
center_mhz = 92.0
gain_db = 20

[[device]]
id = "b"
serial = "{SERIALS[1]}"
center_mhz = 446.1
gain_db = 20

[[device]]
id = "c"
serial = "{SERIALS[2]}"
center_mhz = 120.3
gain_db = 20

[[station]]
id = "valley"
name = "Valley FM"
device = "a"
fm_mhz = [92.4]
transcribe = false

[[station]]
id = "harbour"
name = "Harbour Talk"
device = "a"
fm_mhz = [92.9]
transcribe = false
"""

# Made-up transmitters across what the receivers can reach. A dongle's edges give way and it
# has a spike at its centre, as real ones do.
WORLD: dict[str, object] = {
    "clock": "wall",
    "pace": True,
    "rolloff_db": 6,
    "dc": 0.01,
    "transmitters": [
        {"kind": "wfm", "mhz": 91.3, "tone_hz": 700, "level": 0.1},
        {"kind": "wfm", "mhz": 92.4, "ps": "VALLEY", "rt": "Valley FM: music all day"},
        {"kind": "wfm", "mhz": 92.9, "tone_hz": 1500, "ps": "HARBOUR", "level": 0.15},
        {"kind": "wfm", "mhz": 97.5, "ps": "RIVER", "level": 0.1},
        {"kind": "wfm", "mhz": 98.3, "tone_hz": 400, "level": 0.05},
        {"kind": "nfm", "mhz": 446.03125, "period_s": 20, "on_s": 4, "level": 0.05},
        {"kind": "nfm", "mhz": 446.08125, "period_s": 33, "on_s": 6, "start_s": 7,
         "level": 0.02},
        {"kind": "am", "mhz": 120.5, "tone_hz": 800, "period_s": 15, "on_s": 3, "level": 0.05},
        {"kind": "line", "mhz": 433.92, "level": 0.02},
        {"kind": "block", "mhz": 211.648, "width_khz": 1536, "level": 0.03},
    ],
}  # fmt: skip


@dataclass
class Fixture:
    url: str
    directory: Path
    process: subprocess.Popen[bytes]


def prepare(directory: Path, *, pace: bool = True) -> dict[str, str]:
    """Writes the stations file, the world and the stand-ins into ``directory``; returns the
    environment to run serve in."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stations.toml").write_text(STATIONS)
    (directory / "world.json").write_text(json.dumps(WORLD | {"pace": pace}, indent=1))
    bin_dir = directory / "bin"
    bin_dir.mkdir(exist_ok=True)
    for tool, script in (("rtl_sdr", "fake_rtl_sdr.py"), ("rtl_test", "fake_rtl_test.py"),
                         ("rtl_power", "fake_rtl_power.py")):  # fmt: skip
        exe = bin_dir / tool
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{TESTS / script}" "$@"\n')
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_")}
    env |= {
        "PATH": os.pathsep.join([str(bin_dir), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]),
        "FAKE_RTL_WORLD": str(directory / "world.json"),
        "FAKE_RTL_SERIALS": ",".join(SERIALS),
    }
    return env


@contextmanager
def running(
    port: int,
    directory: Path | None = None,
    *,
    pace: bool = True,
    start: tuple[str, ...] = STARTED,
    timeout: float = 60.0,
) -> Iterator[Fixture]:
    """``wavescribe serve`` on ``port`` with the made-up receivers, until the block ends."""
    with tempfile.TemporaryDirectory(prefix="wavescribe-page-") as tmp:
        where = directory or Path(tmp)
        env = prepare(where, pace=pace)
        log = (where / "serve.log").open("wb")
        process = subprocess.Popen(
            [sys.executable, "-m", "wavescribe", "serve", "--config", "stations.toml",
             "--settings", "ui.toml", "--port", str(port), "--no-alerts", *start],
            cwd=where, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        url = f"http://127.0.0.1:{port}/"
        try:
            _wait_for(url + "api/state", process, timeout, where / "serve.log")
            yield Fixture(url, where, process)
        finally:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            log.close()


def _wait_for(url: str, process: subprocess.Popen[bytes], timeout: float, log: Path) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"serve ended ({process.returncode}):\n{log.read_text()[-3000:]}")
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"serve didn't answer within {timeout:g} s:\n{log.read_text()[-3000:]}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=8771)
    p.add_argument("--dir", type=Path, help="keep everything here (default: a temporary one)")
    p.add_argument("--no-pace", action="store_true",
                   help="let the stand-in rtl_sdr run as fast as it is read")  # fmt: skip
    args = p.parse_args()
    with running(args.port, args.dir, pace=not args.no_pace) as f:
        print(f"{f.url}  (files in {f.directory}; Ctrl-C stops it)", flush=True)
        with suppress(KeyboardInterrupt):
            f.process.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
