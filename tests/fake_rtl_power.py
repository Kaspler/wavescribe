# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``rtl_power``: prints a synthetic sweep as its CSV, with the hops laid out
and the rows written the way rtl_power.c (librtlsdr 2.0) does it.

Environment:
    FAKE_POWER_SIGNALS  "mhz:width_khz:db,..." bumps above a -40 dB floor
    FAKE_POWER_BUSY     if set, fail like a dongle another program holds
    FAKE_POWER_STARTS   a file that gets one line per start: the arguments
    FAKE_POWER_PID      a file that gets the process id
    FAKE_POWER_HANG     if set, stall after setting up, as a wedged dongle does: print nothing
"""

import argparse
import os
import sys
import time

import numpy as np

MAX_RATE, MIN_RATE = 2_800_000, 1_000_000


def layout(lo: int, hi: int, step: int, crop: float) -> tuple[int, int, int, int, int]:
    """rtl_power's ``frequency_range``: (hops, Hz each hop covers, sample rate, decimation,
    FFT length)."""
    for hops in range(1, 1500):
        seen = (hi - lo) // hops
        rate = int(seen / (1 - crop))
        if rate <= MAX_RATE:
            break
    down = 1
    if rate < MIN_RATE:
        hops, down = 1, MAX_RATE // rate
        rate *= down
    for e in range(1, 22):
        if rate / ((1 << e) * down) <= step:
            break
    return hops, seen, rate, down, 1 << e


def rows(
    lo: int,
    hi: int,
    step: int,
    signals: list[tuple[float, float, float]],
    *,
    crop: float = 0.2,
    dc_db: float = 15.0,
    floors: dict[int, float] | None = None,
    seed: int = 1,
) -> list[str]:
    """One CSV line per hop, as ``csv_dbm`` writes them: the low and high columns rounded
    inwards, the bins of the uncropped middle (one more below the centre than above it), then
    the last bin again. ``dc_db`` lifts the bin at each hop's tuned centre; ``floors`` sets a
    hop's own noise level (hop index: dB)."""
    hops, seen, rate, down, n = layout(lo, hi, step, crop)
    rng = np.random.default_rng(seed)
    out = []
    for k in range(hops):
        centre = lo + k * seen + seen // 2
        bw2 = int(rate * int(n * (1 - crop)) / (n * 2 * down))
        i1 = int(n * crop * 0.5)
        f = centre + rate / (n * down) * (np.arange(i1, n - i1) - n // 2)
        db = (floors or {}).get(k, -40.0) + rng.normal(0, 0.5, len(f))
        for c, w, level in signals:
            sigma = max(w / 2.355, rate / (n * down) / 2)
            db = np.maximum(db, -40 + level * np.exp(-0.5 * ((f - c) / sigma) ** 2))
        db[n // 2 - i1] += dc_db
        vals = ", ".join(f"{v:.2f}" for v in [*db, db[-1]])
        out.append(
            f"2026-11-26, 12:00:00, {centre - bw2}, {centre + bw2}, "
            f"{rate / (n * down):.2f}, 100, {vals}"
        )
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("-d")
    p.add_argument("-f", required=True)
    p.add_argument("-g")
    p.add_argument("-p", type=int)
    p.add_argument("-c")
    p.add_argument("-i")
    p.add_argument("-1", dest="once", action="store_true")
    p.add_argument("out", nargs="?")
    a = p.parse_args()
    if started := os.environ.get("FAKE_POWER_STARTS"):
        with open(started, "a") as f:
            f.write(" ".join(sys.argv[1:]) + "\n")
    if pid := os.environ.get("FAKE_POWER_PID"):
        with open(pid, "w") as f:
            f.write(str(os.getpid()))
    if os.environ.get("FAKE_POWER_BUSY"):
        print("usb_claim_interface error -6\nFailed to open rtlsdr device #0.", file=sys.stderr)
        return 1
    print("Found Rafael Micro R820T tuner", file=sys.stderr)
    print(f"Tuner gain set to {float(a.g or 0):.2f} dB.", file=sys.stderr, flush=True)
    if os.environ.get("FAKE_POWER_HANG"):
        time.sleep(3600)
    lo, hi, step = (int(x) for x in a.f.split(":"))
    sigs = []
    for item in filter(None, os.environ.get("FAKE_POWER_SIGNALS", "").split(",")):
        mhz, khz, db = (float(x) for x in item.split(":"))
        sigs.append((mhz * 1e6, khz * 1e3, db))
    crop = float(a.c.rstrip("%")) / 100 if a.c else 0.0
    print("\n".join(rows(lo, hi, step, sigs, crop=crop)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
