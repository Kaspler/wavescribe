# SPDX-License-Identifier: Apache-2.0
"""Telling a wide signal (a broadcast station, a digital service) from a narrowband
transmission, for ``dsp/openwatch.py``: a wide signal is noted, and never caught.

How wide a signal measures when it is first seen does not tell them apart. A voice puts most
of a carrier's power near its centre, so a weak broadcast station measures 7 to 24 kHz within
10 dB of its strongest in its first frames, and a loud talker on a 25 kHz channel up to 13
(14 in a single frame). What does is how far the carrier goes. In one FFT (1.7 ms) an FM
carrier is in one place, with nearly all of its power. A narrowband one never leaves its
channel: at 5 kHz deviation, peak-limited and loud, no FFT of it had its strongest bin
further than 10 kHz from the centre, from 8 dB to 45 dB. A broadcast carrier (75 kHz
deviation) is out there and back several times in most frames, at 3 dB as at 30.

So a signal is wide when, in one frame, its carrier is seen both within half of ``narrow_hz``
of its centre and further out, in different FFTs and never in both at once (two transmissions
side by side are in both at once), in two frames of ten; or when what it has put over the
floor so far measures wider than ``narrow_hz``; or when a run of bins over the threshold is
``WIDE_HZ`` wide at sight.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import percentile_filter, uniform_filter1d

from wavescribe.dsp.activity import runs

# What is narrower than this is a narrowband transmission, unless a band says otherwise: over
# the 16 kHz a 25 kHz FM channel occupies at the most, and under any broadcast signal.
NARROW_HZ = 20_000.0
WIDE_HZ = 40_000.0  # a run of bins over the threshold this wide is no narrowband channel
SEEN_DB = 15.0  # a bin this far over its floor in one FFT: the carrier is there
EMPTY_DB = 9.0  # no bin this far over its floor in one FFT: it is not
RANGING_FFTS = 3  # of a frame's FFTs out, and as many back, for the frame to count
RANGED_OF = (2, 10)  # in two of the last ten frames: wide
CLICK = 2.0  # an FFT with this many times the frame's usual power in all is a click: left out
SAID_S = 0.5  # a wide signal is noted once it has been there this long: its extent has settled
OVER = 1.5  # it is there while its bins stand this far over what was there before it
GONE_S = 5.0  # and gone when it has not been there for this long
HUMP = 4.0  # there from the start: the floor stands this far over the window's own level
LEVEL_HZ = 600_000.0  # the window's own level: the lower fifth of the floor across this much


@dataclass(eq=False)
class Wide:
    """A wide signal from bin ``lo`` to bin ``hi``."""

    id: int
    lo: int
    hi: int
    first: int
    last: int
    base: float  # the level per bin it stands over
    frames: int = 0
    said: bool = False
    freq_hz: int = 0
    width_hz: float = 0.0
    snr_db: float = 0.0
    power_db: float = -200.0


def ranging(
    rows: np.ndarray, floors: np.ndarray, bound: np.ndarray, usual: np.ndarray
) -> np.ndarray:
    """For each place, whether a carrier ranged further than ``bound`` (bins) from it and
    back in this frame. ``rows`` are the frame's FFTs about each place (FFTs by places by
    bins, the place in the middle), ``floors`` the floors of the same bins, and ``usual``
    says which of the FFTs had no click in them."""
    away = np.abs(np.arange(rows.shape[2]) - rows.shape[2] // 2)
    near = away[None, :] <= bound[:, None]
    over = rows / floors
    inside = np.where(near, over, 0.0).max(axis=2)
    outside = np.where(near, 0.0, over).max(axis=2)
    seen, empty = 10 ** (SEEN_DB / 10), 10 ** (EMPTY_DB / 10)
    out = (outside >= seen) & (inside < empty) & usual[:, None]
    back = (inside >= seen) & (outside < empty) & usual[:, None]
    least = RANGING_FFTS
    return (np.count_nonzero(out, axis=0) >= least) & (np.count_nonzero(back, axis=0) >= least)


def humps(
    floor: np.ndarray, where: np.ndarray, half_bins: int, least_bins: int, bin_hz: float
) -> list[tuple[int, int, float]]:
    """Wide signals that were there when the floors were learnt: the floor itself shows them,
    as a hump ``HUMP`` over the window's own level and ``least_bins`` wide. Each as its first
    and last bin, and the window's own level there."""
    smooth = uniform_filter1d(floor, 2 * half_bins + 1, mode="nearest")
    size = min(len(floor), round(LEVEL_HZ / bin_hz)) | 1
    level = percentile_filter(smooth, 20, size=size, mode="nearest")
    found = runs((smooth >= HUMP * level) & where)
    return [(a, b, float(level[a : b + 1].mean())) for a, b in found if b - a + 1 >= least_bins]
