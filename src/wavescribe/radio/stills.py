# SPDX-License-Identifier: Apache-2.0
"""Still pictures of a stretch of the dial, cut from the scans the catalogue keeps
(``data/radio/scans/``). The page shows one where no receiver is tuned live, with its age, so a
band on the dial is never blank; nothing here opens a receiver.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Fewer points than this across the stretch is a smear, not a picture: a sweep of the whole tuner
# has a handful in a band where a sweep of that band has hundreds.
MIN_POINTS = 48


def crop(scan: dict[str, Any], lo_mhz: float, hi_mhz: float) -> dict[str, Any] | None:
    """The scan cut to ``lo_mhz`` to ``hi_mhz``: its spectrum's points inside, and the signals found
    there. None when the scan doesn't reach the stretch."""
    sp = scan.get("spectrum") or {}
    a, b, db = sp.get("start_mhz"), sp.get("stop_mhz"), sp.get("db") or []
    if a is None or b is None or b <= a or not db:
        return None
    step = (b - a) / len(db)  # each point is the strongest of its slot (Sweep.downsampled)
    inside = [i for i in range(len(db)) if lo_mhz <= a + (i + 0.5) * step <= hi_mhz]
    if not inside:
        return None
    i0, i1 = inside[0], inside[-1] + 1
    return {
        "t": scan.get("t"), "serial": scan.get("serial"), "seconds": scan.get("seconds"),
        "spectrum": {"start_mhz": round(a + i0 * step, 6), "stop_mhz": round(a + i1 * step, 6),
                     "db": db[i0:i1]},
        "signals": [s for s in scan.get("signals") or [] if lo_mhz <= s.get("mhz", 0) <= hi_mhz],
    }  # fmt: skip


def still(scans: Path, lo_mhz: float, hi_mhz: float) -> dict[str, Any] | None:
    """The newest saved scan that shows the stretch in enough detail, cut to it; failing that,
    the one that shows it in the most detail; None when no scan reached it."""
    if not hi_mhz > lo_mhz:
        raise ValueError("lo and hi: a stretch of the dial, lowest first, in MHz")
    best: dict[str, Any] | None = None
    files = sorted(scans.glob("*.json"), reverse=True) if scans.is_dir() else []
    for path in files:  # newest first: a scan's file name starts with its time
        try:
            cut = crop(json.loads(path.read_text("utf-8")), lo_mhz, hi_mhz)
        except (OSError, ValueError, TypeError, AttributeError):
            continue  # a scan being written, or one from an older version
        if cut is None:
            continue
        points = len(cut["spectrum"]["db"])
        if points >= MIN_POINTS:
            return cut
        if best is None or points > len(best["spectrum"]["db"]):
            best = cut
    return best
