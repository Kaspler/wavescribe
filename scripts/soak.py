# SPDX-License-Identifier: Apache-2.0
"""Sample a running `wavescribe serve` for a soak test: memory, disk, queues, drops, reconnects.

    uv run python scripts/soak.py --pid <serve pid> --hours 24 --out data/soak/<name>.csv

Every ``--every`` seconds it appends one CSV row. After ``--hours`` it stops the server (SIGTERM,
so it shuts down cleanly) unless ``--keep-running``. Measure, don't guess: this is how the
24/7 claims (bounded memory and disk, recovery from dropouts) are checked.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

FIELDS = [
    "t", "elapsed_h", "footprint_mb", "rss_mb", "threads", "fds", "db_mb", "wal_mb", "audio_mb",
    "ready", "asr_busy", "mt_busy", "asr_queue", "mt_queue", "dropped_asr", "dropped_mt",
    "dropped_filter", "music_skipped_s", "stations_connected", "reconnects", "max_data_age_s",
    "alerts",
]  # fmt: skip


def _mb(path: Path) -> float:
    if path.is_file():
        return path.stat().st_size / 2**20
    total = 0
    for root, _dirs, files in os.walk(path):
        total += sum((Path(root) / f).stat().st_size for f in files)
    return total / 2**20


def _footprint_mb(pid: int) -> float | None:
    """phys_footprint, what Activity Monitor calls Memory (macOS); None elsewhere."""
    try:
        out = subprocess.run(["footprint", "--noCategories", str(pid)], capture_output=True,
                             text=True, timeout=60).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"phys_footprint:\s*([\d.]+)\s*([KMG]B)", out)
    if not m:
        return None
    return float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]


def _ps(pid: int) -> tuple[float, int]:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    threads = subprocess.run(["ps", "-M", "-p", str(pid)], capture_output=True, text=True)
    return int(out.stdout.strip() or 0) / 1024, max(0, len(threads.stdout.splitlines()) - 1)


def _fds(pid: int) -> int:
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True)
    return max(0, len(out.stdout.splitlines()) - 1)


def sample(pid: int, url: str, data: Path, t0: float) -> dict:
    rss, threads = _ps(pid)
    wal, audio = data / "wavescribe.db-wal", data / "audio"
    row: dict = {
        "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_h": round((time.time() - t0) / 3600, 2),
        "footprint_mb": _footprint_mb(pid),
        "rss_mb": round(rss),
        "threads": threads,
        "fds": _fds(pid),
        "db_mb": round(_mb(data / "wavescribe.db"), 1),
        "wal_mb": round(_mb(wal), 1) if wal.exists() else 0,
        "audio_mb": round(_mb(audio), 1) if audio.exists() else 0,
    }
    try:
        state = json.load(urllib.request.urlopen(url + "api/state", timeout=30))
        s = state["status"]
        stations = s.get("stations", {}).values()
        ages = [st["last_data_age"] for st in stations if st.get("last_data_age") is not None]
        dropped = s.get("dropped", {})
        row.update(
            ready=s.get("ready"), asr_busy=s.get("asr_busy"), mt_busy=s.get("mt_busy"),
            asr_queue=s.get("asr_queue"), mt_queue=s.get("mt_queue"),
            dropped_asr=dropped.get("asr"), dropped_mt=dropped.get("mt"),
            dropped_filter=dropped.get("filter"), music_skipped_s=s.get("music_skipped_s"),
            stations_connected=sum(1 for st in stations if st.get("connected")),
            reconnects=sum(st.get("reconnects", 0) for st in stations),
            max_data_age_s=max(ages) if ages else None,
            alerts=len(state.get("alerts", [])),
        )  # fmt: skip
    except Exception as e:  # the server being unreachable is itself a result
        row["ready"] = f"unreachable: {e}"[:80]
    return row


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--pid", type=int, required=True, help="pid of the wavescribe serve process")
    p.add_argument("--url", default="http://127.0.0.1:8765/")
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--hours", type=float, default=24)
    p.add_argument("--every", type=float, default=600, help="seconds between samples")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--keep-running", action="store_true", help="don't stop the server at the end")
    args = p.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    new = not args.out.exists()
    t0 = time.time()
    with args.out.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        while True:
            try:
                os.kill(args.pid, 0)
            except ProcessLookupError:
                print(f"server {args.pid} exited after {(time.time() - t0) / 3600:.2f} h",
                      file=sys.stderr)  # fmt: skip
                return 1
            writer.writerow(sample(args.pid, args.url, args.data, t0))
            f.flush()
            if time.time() - t0 >= args.hours * 3600:
                break
            time.sleep(args.every)
    if args.keep_running:
        return 0
    # SIGTERM, not SIGINT: a server started in the background may have SIGINT ignored.
    os.kill(args.pid, signal.SIGTERM)
    for _ in range(60):
        time.sleep(1)
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            print(f"stopped server {args.pid} after {(time.time() - t0) / 3600:.2f} h",
                  file=sys.stderr)  # fmt: skip
            return 0
    print(f"server {args.pid} still running 60 s after SIGTERM", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
