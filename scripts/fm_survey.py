# SPDX-License-Identifier: Apache-2.0
"""List the strongest carriers in an ``rtl_power`` sweep: a quick band survey without a GUI.

    rtl_power -d 00001234 -f 87.5M:108M:10k -g 20 -c 20% -i 10 -1 data/sdr/survey-A.csv
    uv run python scripts/fm_survey.py data/sdr/survey-A.csv [min dB above the floor, default 10]

``rtl_power`` (part of librtlsdr) hops across the range and writes one CSV row per hop: date,
time, low Hz, high Hz, bin step Hz, sample count, then one dB value per bin. The values are
relative (uncalibrated), so what matters is how far a carrier stands above the noise floor.
Run it at two gains 10 dB apart: real stations stay; peaks that vanish at the lower gain are
the dongle overloading (mixing strong stations into false ones).
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path


def load(path: Path) -> list[tuple[int, float]]:
    """(Hz, dB) for every bin, sorted by frequency; later hops overwrite overlapping bins."""
    bins: dict[int, float] = {}
    with path.open(newline="") as f:
        for row in csv.reader(f):
            if len(row) < 7:
                continue
            low, step = float(row[2]), float(row[4])
            for i, value in enumerate(row[6:]):
                value = value.strip()
                if value and value.lower() != "nan":
                    bins[round(low + i * step)] = float(value)
    return sorted(bins.items())


def find_peaks(
    spectrum: list[tuple[int, float]], min_above: float, half_width: int = 100_000
) -> tuple[float, list[tuple[int, float]]]:
    """Noise floor (median bin) and local maxima at least ``min_above`` dB over it.

    ``half_width`` is half an FM channel (200 kHz), so one station gives one peak.
    """
    floor = statistics.median(db for _, db in spectrum)
    found: list[tuple[int, float]] = []
    for hz, db in spectrum:
        if db < floor + min_above:
            continue
        window = max(d for h, d in spectrum if abs(h - hz) <= half_width)
        if db == window and all(abs(hz - p) > half_width for p, _ in found):
            found.append((hz, db))
    return floor, found


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    min_above = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    spectrum = load(path)
    if not spectrum:
        sys.exit(f"{path}: no data")
    floor, found = find_peaks(spectrum, min_above)
    lo, hi = spectrum[0][0] / 1e6, spectrum[-1][0] / 1e6
    print(f"{lo:.2f}-{hi:.2f} MHz, {len(spectrum)} bins, noise floor {floor:.1f} dB")
    print(f"{len(found)} carriers at least {min_above:.0f} dB above the floor:")
    for hz, db in found:
        above = db - floor
        print(f"  {hz / 1e6:7.2f} MHz  +{above:5.1f} dB  {'#' * round(above / 2)}")


if __name__ == "__main__":
    main()
