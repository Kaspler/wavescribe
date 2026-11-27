# SPDX-License-Identifier: Apache-2.0
"""Find what is on the air: sweep a range with ``rtl_power``, pick the signals out of the noise,
say what each one is from the band plan, and name FM stations from their own RDS.

    rtl_power -f START:STOP:BIN   (a GPL tool: run as a separate process, read as CSV)
      ─► one power spectrum for the whole range, each hop's bins placed as rtl_power lays
         them out (the dongle's DC at each hop's centre bridged)
      ─► peaks a margin above the local noise floor, merged per channel, snapped to the raster
      ─► band plan: what it is, how to demodulate it, its privacy class; digital ones skipped
      ─► FM: a few seconds of IQ per ~2 MHz cluster (rtl_sdr), every station in it demodulated
         at once and its RDS decoded: name, programme identification, type, radiotext

A dongle is open to one process at a time, so a scan needs it idle: no station or monitor on it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import select
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise
from typing import Any

import numpy as np
from scipy.ndimage import percentile_filter, uniform_filter1d

from wavescribe.log import event
from wavescribe.radio.bandplan import (
    TUNER_RANGE_HZ,
    Band,
    channel_of,
    channel_width,
    lookup,
    region_band,
    region_bands,
    region_text,
)
from wavescribe.sources.sdr import claimed, device_arg, ppm_args

log = logging.getLogger(__name__)

SWEEP_LIMITS_HZ = TUNER_RANGE_HZ
SPECTRUM_POINTS = 2_000  # what a scan keeps of its spectrum for the page
REFERENCE_HZ = 28_800_000  # the V4/V3 crystal: its harmonics show up as narrow spurs
CLOCK_HZ = 2_400_000  # the sample rate RTL-SDR receivers run at by default (see _clock_lines)
CROP = 0.2  # of each hop's edges, where the dongle's filter rolls off: rtl_power -c
MAX_RATE_HZ = 2_800_000  # the highest sample rate rtl_power gives a hop
# rtl_power prints every hop at once when a sweep ends, so a stall shows as silence. A hop
# (retune, settle, read) takes about 40 ms: the whole tuner range, 778 hops, in about 30 s.
HOP_S = 0.05  # a hop, with room to spare
START_S = 10.0  # opening the dongle and setting up the tuner, with room to spare
STALL_SWEEPS = 3  # silent for this many sweeps' worth of time: rtl_power has stalled
POLL_S = 0.1  # how often a running sweep looks at its cancel flag
DC_HZ = 3_000  # the dongle's DC offset spreads this far either side of a hop's centre
# At this resolution or finer a narrow carrier fits in the bridged centre, so a second run with
# the hops a quarter of a hop away fills each centre in. rtl_power's shortest interval is 1 s.
FINE_BIN_HZ = 5_000
FILL_INTEGRATE_S = 1.0
# Spurs are learnt at bins of at most 6 kHz: 512-bin FFTs over a whole-range hop (5.47 kHz
# bins), where the 86.8 kHz bins a whole-range sweep gets by default buried a narrow line under
# 12 dB more noise. rtl_power reads the same 16 384 bytes per hop either way, so this costs no
# time, and that is still 16 FFTs averaged per bin in one pass: finer, with fewer, noise makes
# lines (at 2048 bins, 4 FFTs, thousands of them over the whole range).
SPUR_BIN_HZ = 6_000
# librtlsdr's tuner drivers when the tuner can't tune: "[R82XX] PLL not locked!" (no frequency),
# "[R82XX] No valid PLL values for 1770570000 Hz!" (its LO: the hop's centre plus an IF of
# 1-4.57 MHz), "[E4K] PLL not locked for 1150000000 Hz!" (zero IF: the hop's centre itself).
PLL_TROUBLE = re.compile(r"PLL not locked|no valid PLL", re.IGNORECASE)
PLL_HZ = re.compile(r"for (\d+) ?Hz")
IF_MAX_HZ = 4_600_000


class ScanError(RuntimeError):
    """The scan couldn't run: no rtl_power, a busy dongle, a bad range. Messages name no
    dongle: the caller knows which receiver it gave, and a serial has no place in them."""


class Busy(ScanError):
    """Another process has the dongle open."""

    def __init__(self) -> None:
        super().__init__(BUSY)


BUSY = "the receiver is busy: another program has it open"


class Cancelled(ScanError):
    pass


@dataclass
class Sweep:
    start_hz: int
    stop_hz: int
    bin_hz: float
    freqs: np.ndarray  # Hz, ascending
    db: np.ndarray  # power per bin, dB (relative)
    seconds: float
    # Where each of rtl_power's hops after the first begins (bin indices). Every hop is tuned
    # and measured on its own, so its noise level can differ from its neighbours'.
    hop_starts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))
    warnings: list[str] = field(default_factory=list)  # what may be wrong with it, for people
    gaps: list[tuple[float, float]] = field(default_factory=list)  # Hz: no reading, no signals

    def in_gap(self, hz: float) -> bool:
        return any(lo <= hz <= hi for lo, hi in self.gaps)

    def downsampled(self, points: int = SPECTRUM_POINTS) -> dict[str, Any]:
        """The spectrum for the page: the maximum per slot, so narrow signals survive."""
        n = len(self.db)
        if n <= points:
            db = self.db
        else:
            edges = np.linspace(0, n, points + 1).astype(int)
            db = np.maximum.reduceat(self.db, edges[:-1])
        return {"start_mhz": self.start_hz / 1e6, "stop_mhz": self.stop_hz / 1e6,
                "db": [round(float(x), 1) for x in db],
                "warnings": list(self.warnings)}  # fmt: skip


@dataclass
class Signal:
    """One thing found on the air."""

    freq_hz: int
    level_db: float  # peak power, dB (relative to the dongle's full scale, as rtl_power reports)
    snr_db: float  # above the local noise floor
    width_hz: float  # where its power above the floor stays within 6 dB of its peak
    band: str | None = None  # band plan id
    label: str | None = None  # e.g. "PMR446 walkie-talkies, channel 5"
    mode: str | None = None  # how to listen: wfm, nfm, am; None for digital or unknown
    privacy: str = "private"
    spur: bool = False  # probably a receiver's own: a learnt spur, a crystal or clock harmonic
    clock_hz: int | None = None  # the receiver clock it is a harmonic of, when it is one
    rds: dict[str, Any] | None = None
    seen: int = 1  # of `passes` sweeps, how many found it (a watch sweeps again and again)
    passes: int = 1

    @property
    def mhz(self) -> float:
        return self.freq_hz / 1e6

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mhz"] = round(self.mhz, 5)
        d["channel"] = channel_of(self.band, self.freq_hz)  # beside label, for programs
        return d


@dataclass
class Progress:
    phase: str = "starting"  # sweep, rds, done
    done: int = 0
    total: int = 0
    note: str = ""
    signals: list[Signal] = field(default_factory=list)


ProgressFn = Callable[[Progress], None]


def _mhz(hz: float) -> str:
    return f"{hz / 1e6:.6f}".rstrip("0").rstrip(".")


def fit_range(start_hz: float, stop_hz: float) -> tuple[int, int, str]:
    """The part of a range the tuner reaches, and a sentence saying what was left out (empty
    when nothing was). A range it doesn't reach at all is a ScanError worded with the range as
    asked, not a clamped one, which could read "24 to 1.611 MHz"."""
    lo, hi = SWEEP_LIMITS_HZ
    asked = f"{_mhz(start_hz)} to {_mhz(stop_hz)} MHz"
    below = f"below {_mhz(lo)} MHz isn't received"
    above = f"above {_mhz(hi)} MHz is out of the tuner's reach"
    if start_hz >= stop_hz:
        raise ScanError(f"scan {asked}: give the lower frequency first")
    if stop_hz <= lo or start_hz >= hi:
        tunes = f"{_mhz(lo)} to {_mhz(hi)} MHz"
        why = f"; {below}" if stop_hz <= lo else ""
        raise ScanError(f"{asked} is outside what this receiver tunes ({tunes}){why}")
    start, stop = max(start_hz, lo), min(stop_hz, hi)
    left_out = [w for w, cut in ((below, start_hz < lo), (above, stop_hz > hi)) if cut]
    if not left_out:
        return round(start), round(stop), ""
    note = f"scanning {_mhz(start)} to {_mhz(stop)} MHz of the {asked} asked for: "
    return round(start), round(stop), note + ", and ".join(left_out)


def bin_for(start_hz: float, stop_hz: float, band: Band | None = None) -> float:
    """The sweep's resolution: a fifth of the band's channel (a twentieth for broadcast FM),
    or, for a free range, whatever keeps it under about 25 000 bins."""
    if band is not None:
        target = channel_width(band) / (20 if band.mode == "wfm" else 5)
        return max(b for b in (1_000, 2_500, 5_000, 10_000, 25_000) if b <= max(target, 1_000))
    span = stop_hz - start_hz
    for b in (2_500, 5_000, 10_000, 25_000, 50_000, 100_000, 250_000):
        if span / b <= 25_000:
            return b
    return 250_000


def hop_count(start_hz: float, stop_hz: float) -> int:
    """How many hops rtl_power cuts a range into (``frequency_range`` in rtl_power.c): the
    fewest equal ones whose sample rate, the cropped edges included, stays within 2.8 MHz. The
    rate therefore follows the range (2.64 MHz for the airband, 2.08 MHz for 121 to 126 MHz)."""
    span = int(stop_hz) - int(start_hz)
    return next(n for n in range(1, 1500) if int(span // n / (1 - CROP)) <= MAX_RATE_HZ)


def sweep(
    serial: str,
    start_hz: float,
    stop_hz: float,
    *,
    gain_db: float = 30.0,
    ppm: int = 0,
    bin_hz: float | None = None,
    integrate_s: float | None = None,
    command: str = "rtl_power",
    on_progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    stall_s: float | None = None,
    fill_centres: bool | None = None,
) -> Sweep:
    """One pass of ``rtl_power`` over the part of the range the tuner reaches (``fit_range``).
    ``on_progress(hops_done, hops_total)``. ``cancel`` stops it at once. A run silent for
    ``stall_s`` (by default a few sweeps' worth of time) has stalled: it is killed and the
    sweep fails.

    Each hop's centre holds the dongle's DC offset, bridged over ``DC_HZ`` either side, and a
    carrier there would be lost. ``fill_centres`` (by default at ``FINE_BIN_HZ`` or finer,
    where a narrow carrier fits in that bridge) runs ``rtl_power`` a second time, for a second,
    with the hops a quarter of a hop away, and fills each centre in from it."""
    start_hz, stop_hz, _ = fit_range(start_hz, stop_hz)
    if shutil.which(command) is None:
        raise ScanError("rtl_power isn't installed (brew install librtlsdr)")
    bin_hz = bin_hz or bin_for(start_hz, stop_hz)
    total = hop_count(start_hz, stop_hz)
    if integrate_s is None:  # enough averaging for a steady spectrum, quick for wide sweeps
        integrate_s = 1.0 if total > 50 else max(2.0, 0.3 * total)
    fill = bin_hz <= FINE_BIN_HZ if fill_centres is None else fill_centres
    runs = 2 if fill else 1

    def run(lo: float, hi: float, k: int, integrate: float) -> tuple[list[Row], list[str]]:
        # -c crops each hop's edges, where the dongle's filter rolls off; -1 is a single pass.
        cmd = [command, "-d", device_arg(serial), "-f", f"{int(lo)}:{int(hi)}:{int(bin_hz)}",
               "-g", f"{gain_db:g}", *ppm_args(ppm), "-c", f"{CROP:.0%}", "-i", f"{integrate:g}",
               "-1", "-"]  # fmt: skip
        stall = stall_s or START_S + STALL_SWEEPS * max(integrate, total * HOP_S)

        def hops(d: int, t: int) -> None:
            if on_progress is not None:
                on_progress(k * total + min(d, total), runs * total)

        return _run(cmd, serial, total, stall, hops, cancel)

    t0 = time.time()
    event(log, "scan_sweep", serial=serial, start_mhz=start_hz / 1e6, stop_mhz=stop_hz / 1e6,
          bin_hz=bin_hz, hops=total, runs=runs)  # fmt: skip
    rows, err = run(start_hz, stop_hz, 0, integrate_s)
    text = "".join(err)
    if not rows:
        if claimed(text):
            raise Busy()
        raise ScanError(f"rtl_power found nothing to report: {text.strip()[-300:] or 'no output'}")
    sw = _assemble(rows, start_hz, stop_hz, bin_hz, 0.0)
    _unlocked(sw, err, rows)
    if fill:
        shift = (stop_hz - start_hz) // total / 4  # the same hops, a quarter of one higher
        lo, hi = SWEEP_LIMITS_HZ
        lo2, hi2 = start_hz + shift, stop_hz + shift
        if hi2 > hi:
            lo2, hi2 = start_hz - shift, stop_hz - shift
        if lo2 >= lo:
            rows2, err2 = run(lo2, hi2, 1, FILL_INTEGRATE_S)
            if rows2:
                second = _assemble(rows2, lo2, hi2, bin_hz, 0.0)
                _unlocked(second, err2, rows2)
                filled = _fill(sw, rows, second, rows2)
                sw.warnings += [w for w in second.warnings if w not in sw.warnings]
                event(log, "scan_centres", serial=serial, hops=len(rows), filled=filled)
    sw.seconds = time.time() - t0
    for w in sw.warnings:  # for the log: the command line and the page say them themselves
        event(log, "scan_warning", serial=serial, warning=w)
    return sw


def _dc_bins(n: int, step: float) -> tuple[int, int]:
    """The bins ``[a, b)`` of a hop of ``n`` bins that the dongle's DC offset covers, about its
    centre bin n / 2 (``_assemble``); they are bridged when the hop has bins either side."""
    c, w = n // 2, round(DC_HZ / step)
    return c - w, c + w + 1


def _nearest(freqs: np.ndarray, hz: np.ndarray) -> np.ndarray:
    i = np.clip(np.searchsorted(freqs, hz), 1, len(freqs) - 1)
    return np.where(np.abs(freqs[i] - hz) < np.abs(freqs[i - 1] - hz), i, i - 1)


def _fill(sw: Sweep, rows: list[Row], second: Sweep, rows2: list[Row]) -> int:
    """Fill each hop's bridged centre in from a second sweep whose hops sit elsewhere, matched
    in level to the readings either side. How many centres were filled."""
    if len(second.freqs) < 2:
        return 0
    centres2 = np.array([(lo + hi) / 2 for lo, hi, _, _ in rows2])
    filled = 0
    for lo, hi, step, vals in rows:
        n = len(vals) // 2 * 2  # the repeated last bin dropped, as _assemble does
        a, b = _dc_bins(n, step)
        if a <= 0 or b >= n:
            continue  # not bridged
        centre = (lo + hi) / 2
        f_lo, f_hi = centre + (a - n // 2) * step, centre + (b - 1 - n // 2) * step
        if np.min(np.abs(centres2 - centre)) <= (b - a + 4) * step:
            continue  # the second run's own centre is here too
        if sw.in_gap(centre) or second.in_gap(f_lo) or second.in_gap(f_hi):
            continue
        i0 = int(np.searchsorted(sw.freqs, f_lo - step / 2))
        i1 = int(np.searchsorted(sw.freqs, f_hi + step / 2))
        if i1 <= i0:
            continue
        j = _nearest(second.freqs, sw.freqs[i0:i1])
        if np.max(np.abs(second.freqs[j] - sw.freqs[i0:i1])) > step:
            continue  # the second run doesn't reach it
        side = np.r_[max(0, i0 - 4) : i0, i1 : min(len(sw.db), i1 + 4)]
        offset = (float(np.median(sw.db[side] - second.db[_nearest(second.freqs, sw.freqs[side])]))
                  if len(side) else 0.0)  # fmt: skip
        sw.db[i0:i1] = second.db[j] + offset
        filled += 1
    return filled


def _unlocked(sw: Sweep, err: list[str], rows: list[Row]) -> None:
    """Where the tuner's PLL didn't lock, a hop's readings come from wherever the tuner ended
    up. librtlsdr says so on stderr, but rtl_power prints every hop at once when its pass ends
    and reports no progress hop by hop, so neither the order nor the timing of the message says
    which hop it was. A message that names a frequency gives the hops it can be: they become
    gaps, with no reading and no signals. The usual "[R82XX] PLL not locked!" names none, so
    the sweep says its whole range may be wrong. Messages before the gain is set come from the
    tuner's own filter calibration while it starts up (at 56 MHz), not from the sweep."""
    gain = next((i for i, line in enumerate(err) if "tuner gain" in line.lower()), -1)
    trouble = [line for line in err[gain + 1 :] if PLL_TROUBLE.search(line)]
    unnamed = 0
    gaps: list[tuple[float, float]] = []
    for line in trouble:
        m = PLL_HZ.search(line)
        if m is None:
            unnamed += 1
            continue
        hz = int(m.group(1))  # the hop's centre, or its centre plus the IF
        gaps += [(lo, hi) for lo, hi, _, _ in rows if hz - IF_MAX_HZ <= (lo + hi) / 2 <= hz]
    for lo, hi in sorted(set(gaps)):
        if sw.gaps and lo <= sw.gaps[-1][1] + sw.bin_hz:
            sw.gaps[-1] = (sw.gaps[-1][0], max(hi, sw.gaps[-1][1]))
        else:
            sw.gaps.append((lo, hi))
    for lo, hi in sw.gaps:
        a = int(np.searchsorted(sw.freqs, lo))
        b = int(np.searchsorted(sw.freqs, hi, side="right"))
        if a >= b:
            continue
        if a > 0 and b < len(sw.db):  # a straight line between the readings either side
            sw.db[a:b] = np.linspace(sw.db[a - 1], sw.db[b], b - a + 2)[1:-1]
        else:  # at the sweep's edge: the floor of what is left
            rest = np.delete(sw.db, np.s_[a:b])
            sw.db[a:b] = float(np.percentile(rest, 20)) if len(rest) else -100.0
        sw.warnings.append(
            f"no reading from {lo / 1e6:.3f} to {hi / 1e6:.3f} MHz: the tuner couldn't tune there"
        )
    if unnamed:
        times = "once" if unnamed == 1 else f"{unnamed} times"
        sw.warnings.append(
            f"the tuner's PLL didn't lock {times} during this sweep, and rtl_power doesn't say "
            f"where: what was found from {sw.start_hz / 1e6:g} to {sw.stop_hz / 1e6:g} MHz may "
            "be misplaced or missing. Sweep again"
        )


Row = tuple[float, float, float, list[float]]  # one hop: low and high column, bin, power per bin


def _row(line: bytes) -> Row | None:
    parts = line.strip().split(b",")
    if len(parts) < 7:
        return None
    try:
        return float(parts[2]), float(parts[3]), float(parts[4]), [float(v) for v in parts[6:]]
    except ValueError:
        return None


def _run(
    cmd: list[str],
    serial: str,
    total: int,
    stall_s: float,
    on_progress: Callable[[int, int], None] | None,
    cancel: threading.Event | None,
) -> tuple[list[Row], list[str]]:
    """Run one ``rtl_power`` and read its rows, with a deadline: the hops and its stderr."""
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)  # fmt: skip
    assert proc.stdout is not None and proc.stderr is not None
    err: list[str] = []

    def drain(pipe: Any = proc.stderr) -> None:
        for raw in pipe:
            err.append(raw.decode(errors="replace"))

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    rows: list[Row] = []
    fd = proc.stdout.fileno()
    pending = b""
    heard = time.monotonic()
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled("scan cancelled")
            ready, _, _ = select.select([fd], [], [], POLL_S)
            if not ready:
                if time.monotonic() - heard > stall_s:
                    raise ScanError(
                        f"rtl_power stopped reporting (nothing for "
                        f"{stall_s:.0f} s), so it was stopped. Try again; if it keeps "
                        "happening, unplug the dongle and plug it in again"
                    )
                continue
            data = os.read(fd, 1 << 16)
            if not data:
                break
            heard = time.monotonic()
            *lines, pending = (pending + data).split(b"\n")
            for line in lines:
                if (row := _row(line)) is not None:
                    rows.append(row)
                    if on_progress is not None:
                        on_progress(min(len(rows), total), total)
        if (row := _row(pending)) is not None:
            rows.append(row)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)  # it reported; if it doesn't let go of the dongle, it's killed
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        drainer.join(timeout=2)
    if rows and on_progress is not None:
        on_progress(total, total)
    return rows, err


def _assemble(rows: list[Row], start_hz: float, stop_hz: float, bin_hz: float,
              seconds: float) -> Sweep:  # fmt: skip
    """One spectrum from rtl_power's rows, one row per hop, read the way ``csv_dbm`` in
    rtl_power.c writes them. A hop's bins lie about its tuned centre, (low + high) / 2, one
    more below it than above, so of n bins the centre is bin n / 2; the low and high columns
    are rounded and are not the first and last bin's frequencies. The last bin is written
    twice, so a row with an odd count ends in a repeat, which would otherwise land one bin past
    the hop's edge, among the next hop's bins."""
    freqs, db, hop = [], [], []
    top = -np.inf
    for k, (lo, hi, step, vals) in enumerate(sorted(rows, key=lambda r: r[0])):
        v = np.array(vals[: len(vals) // 2 * 2], dtype=np.float64)
        n = len(v)
        if n == 0:
            continue
        f = (lo + hi) / 2 + step * (np.arange(n) - n // 2)
        # rtl_power copies a neighbour into the centre bin, but the dongle's DC offset can
        # spread a few kHz around it at fine resolution: bridge those bins from either side.
        # A carrier right on a hop's centre goes with them, unless `sweep` fills it in.
        a, b = _dc_bins(n, step)
        if a > 0 and b < n:
            v[a:b] = np.linspace(v[a - 1], v[b], b - a + 2)[1:-1]
        new = f > top  # neighbouring hops can overlap by a bin
        freqs.append(f[new])
        db.append(v[new])
        hop.append(np.full(int(new.sum()), k))
        top = max(top, f[-1])
    f_all, d_all, h_all = np.concatenate(freqs), np.concatenate(db), np.concatenate(hop)
    keep = np.isfinite(d_all) & (f_all >= start_hz) & (f_all <= stop_hz)
    starts = np.flatnonzero(np.diff(h_all[keep])) + 1
    return Sweep(int(start_hz), int(stop_hz), bin_hz, f_all[keep], d_all[keep], seconds, starts)


def parse_csv(text: str, start_hz: float, stop_hz: float) -> Sweep:
    """An ``rtl_power`` CSV (for tests and saved sweeps)."""
    rows = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) >= 7:
            rows.append((float(parts[2]), float(parts[3]), float(parts[4]),
                         [float(v) for v in parts[6:]]))  # fmt: skip
    step = rows[0][2] if rows else 1.0
    return _assemble(rows, start_hz, stop_hz, step, 0.0)


def spur_tolerance(bin_hz: float) -> float:
    return max(2.5 * bin_hz, 4_000)


def _floor(sw: Sweep, bin_hz: float) -> np.ndarray:
    """The noise floor under each bin: a low percentile over about 2 MHz, so a crowded FM band
    doesn't lift it, taken within each hop. Every hop is tuned and measured on its own and its
    noise can sit well above its neighbours' (the V4 switches to its HF input below 28.8 MHz,
    and a hop the tuner didn't settle on reads high throughout); a floor that reached into a
    quieter hop would turn the louder hop's noise into signals."""
    floor = np.empty_like(sw.db)
    for a, b in pairwise([0, *sw.hop_starts, len(sw.db)]):
        window = int(min(b - a, max(31, 2e6 / bin_hz))) | 1
        floor[a:b] = percentile_filter(sw.db[a:b], 20, size=window, mode="nearest")
    return floor


def find_signals(
    sw: Sweep, region: str, min_snr_db: float = 8.0, spurs: list[int] | tuple[int, ...] = ()
) -> list[Signal]:
    """Peaks a margin above the local noise floor, one per channel, with what the band plan
    says about each. ``spurs``: this receiver's own signals, learnt with no antenna."""
    if len(sw.db) < 5:
        return []
    bin_hz = float(np.median(np.diff(sw.freqs)))
    floor = _floor(sw, bin_hz)
    # At fine resolution a signal spans several bins: average three, so noise can't make peaks.
    # In power, not decibels: a carrier one bin wide then keeps all but 4.8 dB of its margin,
    # where averaging decibels would cut it to a third. At coarse resolution (whole-range
    # sweeps) every bin stands alone.
    smooth = sw.db
    if bin_hz <= 10_000:
        smooth = 10 * np.log10(uniform_filter1d(10 ** (sw.db / 10), 3, mode="nearest"))
    excess = smooth - floor
    peaks = (
        np.flatnonzero(
            (excess[1:-1] >= min_snr_db)
            & (smooth[1:-1] >= smooth[:-2])
            & (smooth[1:-1] >= smooth[2:])
        )
        + 1
    )
    out: list[Signal] = []
    thin: list[tuple[Signal, float]] = []  # the narrow lines, with their centres
    taken = np.zeros(len(sw.db), dtype=bool)
    for i in sorted(peaks, key=lambda k: -excess[k]):
        if taken[i]:
            continue
        f0 = float(sw.freqs[i])
        if sw.in_gap(f0):
            continue  # no reading there: the tuner couldn't tune
        band = lookup(f0, region)
        half = channel_width(band) / 2
        lo = int(np.searchsorted(sw.freqs, f0 - half))
        hi = int(np.searchsorted(sw.freqs, f0 + half))
        if taken[lo:hi].any():
            continue  # inside a stronger signal's channel
        # Width and centre: the run where the power above the floor stays within 6 dB of its
        # peak, on a spectrum smoothed to a tenth of the band's channel (a raw FM spectrum is
        # spiky), power-weighted. Above the floor, because a carrier thinner than that, smoothed,
        # can sit less than 6 dB over the noise and its run would spread across the channel.
        k = max(1, round(2 * half / 10 / bin_hz))
        above = 10 ** (sw.db[lo:hi] / 10) - 10 ** (floor[lo:hi] / 10)
        seg = uniform_filter1d(above, k, mode="nearest")  # the window holds the peak's own bin
        j = int(np.argmax(seg))
        a = b = j
        while a > 0 and seg[a - 1] >= seg[j] / 4:
            a -= 1
        while b < len(seg) - 1 and seg[b + 1] >= seg[j] / 4:
            b += 1
        a, b = a + lo, b + lo
        p = 10 ** (sw.db[a : b + 1] / 10)
        centre = float(np.sum(sw.freqs[a : b + 1] * p) / np.sum(p))
        width = float(sw.freqs[b] - sw.freqs[a] + bin_hz)
        taken[lo:hi] = True
        freq = band.snap(centre) if band is not None and band.contains(centre) else round(centre)
        narrow = width <= (k + 2) * bin_hz  # the smoothing widens a line to k bins
        harmonic = abs(f0 - round(f0 / REFERENCE_HZ) * REFERENCE_HZ) <= max(3 * bin_hz, 5_000)
        tol = spur_tolerance(bin_hz)
        learnt = any(abs(f0 - x) <= tol or abs(centre - x) <= tol for x in spurs)
        out.append(Signal(
            freq_hz=int(freq), level_db=round(float(np.max(sw.db[a : b + 1])), 1),
            snr_db=round(float(excess[i]), 1), width_hz=round(width),
            band=band.id if band else None,
            label=band.label(freq) if band else None,
            mode=band.mode if band and band.privacy != "digital" else None,
            privacy=band.privacy if band else "private",
            spur=bool(learnt or (harmonic and narrow)),
        ))  # fmt: skip
        if narrow:
            thin.append((out[-1], centre))
    if bin_hz <= 10_000:  # in a whole-range sweep's wide bins every channel looks thin
        for s, clock in _clock_lines(thin, bin_hz):
            s.spur, s.clock_hz = True, round(clock)
    out.sort(key=lambda s: s.freq_hz)
    return out


def _clock_lines(lines: list[tuple[Signal, float]], bin_hz: float) -> list[tuple[Signal, float]]:
    """Which of these narrow lines are harmonics of a receiver's 2.4 MHz sample clock, each
    with the clock it fits.

    A receiver streaming at 2.4 MHz (28.8 MHz / 12, what RTL-SDR software runs at by default)
    puts a comb of steady lines on the receivers near it at multiples of its clock, the odd
    ones strongest: in the airband at 117.6, 122.4, 127.2, 132 and 136.8 MHz, the same for
    any range and any sample rate of the receiver that scans. That clock runs off its own
    crystal, a few ppm to 100 ppm from nominal, so the lines are taken as harmonics of one
    clock within 100 ppm of 2.4 MHz when two or more agree on it to a couple of bins, and a
    line on its own when it lies within 30 ppm. A station that happens to sit on such a line
    is marked with it, not hidden: the mark says "probably"."""
    near = []
    for s, f in lines:
        n = round(f / CLOCK_HZ)
        if n and abs(f / n - CLOCK_HZ) <= 100e-6 * CLOCK_HZ:
            near.append((s, f, n))
    if not near:
        return []
    clock = float(np.median([f / n for _, f, n in near]))
    agree = [(s, n) for s, f, n in near if abs(f - n * clock) <= 2 * bin_hz]
    if len({n for _, n in agree}) >= 2:  # two harmonics, not two lines beside one
        return [(s, clock) for s, _ in agree]
    return [(s, f / n) for s, f, n in near if abs(f - n * CLOCK_HZ) <= max(30e-6 * f, bin_hz)]


def clusters(
    freqs: list[int], rate: int = 2_400_000, margin: int = 140_000
) -> list[tuple[int, list[int]]]:
    """Group frequencies into dongle-sized windows: (centre, members). The centre avoids sitting
    on a member (DC) and keeps everyone ``margin`` inside the band edge."""
    span = rate - 2 * margin
    out: list[tuple[int, list[int]]] = []
    group: list[int] = []
    for f in sorted(freqs):
        if group and f - group[0] > span:
            out.append((_centre(group, rate, margin), group))
            group = []
        group.append(f)
    if group:
        out.append((_centre(group, rate, margin), group))
    return out


def _centre(group: list[int], rate: int, margin: int) -> int:
    c = (group[0] + group[-1]) // 2
    reach = rate // 2 - margin
    for shift in (0, 60_000, -60_000, 120_000, -120_000, 30_000, -30_000):
        cc = c + shift
        if all(abs(f - cc) >= 40_000 and abs(f - cc) <= reach for f in group):
            return cc
    return c


def name_fm(
    serial: str,
    signals: list[Signal],
    *,
    gain_db: float = 30.0,
    ppm: int = 0,
    seconds: float = 12.0,
    rate: int = 2_400_000,
    command: str = "rtl_sdr",
    on_progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    """Decode RDS from every FM signal: a few seconds of IQ per cluster, all its stations
    demodulated at once. Fills ``signal.rds``; a cluster ends early once every station in it has
    a stable name."""
    from wavescribe.dsp.rds import RdsDecoder
    from wavescribe.dsp.wfm import WfmDemodulator, iq_from_u8

    if shutil.which(command) is None:
        raise ScanError("rtl_sdr isn't installed (brew install librtlsdr)")
    fm = {s.freq_hz: s for s in signals if s.mode == "wfm" and not s.spur}
    groups = clusters(list(fm), rate)
    for k, (centre, members) in enumerate(groups):
        if cancel is not None and cancel.is_set():
            raise Cancelled("scan cancelled")
        if on_progress is not None:
            on_progress(k, len(groups))
        demods = {f: WfmDemodulator(rate, f - centre) for f in members}
        decoders = {f: RdsDecoder() for f in members}
        cmd = [command, "-d", device_arg(serial), "-f", str(centre), "-s", str(rate), "-g",
               f"{gain_db:g}", *ppm_args(ppm), "-n", str(int(seconds * rate)), "-"]  # fmt: skip
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)  # fmt: skip
        assert proc.stdout is not None and proc.stderr is not None
        err = bytearray()

        def drain(p: subprocess.Popen[bytes] = proc, into: bytearray = err) -> None:
            into.extend(p.stderr.read())  # type: ignore[union-attr]

        threading.Thread(target=drain, daemon=True).start()
        chunk = 2 * rate // 4  # 0.25 s of 8-bit IQ
        buf = bytearray()
        fd = proc.stdout.fileno()
        got = 0
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("scan cancelled")
                ready, _, _ = select.select([fd], [], [], 5.0)
                if not ready:
                    break
                data = os.read(fd, 1 << 20)
                if not data:
                    break
                buf += data
                while len(buf) >= chunk:
                    iq = iq_from_u8(np.frombuffer(bytes(buf[:chunk]), dtype=np.uint8))
                    del buf[:chunk]
                    got += chunk
                    for f in members:
                        decoders[f](demods[f].mpx(iq))
                if got >= 4 * rate and all(_settled(d.info) for d in decoders.values()):
                    break
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        if got == 0 and claimed(err.decode(errors="replace")):
            raise Busy()
        for f in members:
            info = decoders[f].info
            if info.pi is not None or info.groups:
                fm[f].rds = info.to_dict()
        event(log, "scan_rds", serial=serial, centre_mhz=centre / 1e6, stations=len(members),
              named=sum(1 for f in members if fm[f].rds and fm[f].rds.get("ps")),
              seconds=round(got / 2 / rate, 1))  # fmt: skip
    if on_progress is not None:
        on_progress(len(groups), len(groups))


def _settled(info: Any) -> bool:
    return bool(info.names) and max(info.names.values()) >= 2 and info.pi is not None


def scan(
    serial: str,
    start_hz: float,
    stop_hz: float,
    region: str,
    *,
    gain_db: float = 30.0,
    ppm: int = 0,
    rds: bool = True,
    rds_seconds: float = 12.0,
    min_snr_db: float = 8.0,
    watch_s: float = 0.0,
    spurs: list[int] | tuple[int, ...] = (),
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Sweep, list[Signal]]:
    """Sweep, find, classify and (for FM) name. With ``watch_s``, sweep again and again for
    that long: walkie-talkies, aircraft and amateurs talk now and then, so one sweep rarely
    catches them. The spectrum returned is then the maximum each bin reached. A range that
    reaches past the tuner is scanned where it can be, and the first progress note says so."""
    prog = Progress()

    def report(phase: str, done: int, total: int, note: str = "") -> None:
        prog.phase, prog.done, prog.total, prog.note = phase, done, total, note
        if on_progress is not None:
            on_progress(prog)

    start_hz, stop_hz, cut = fit_range(start_hz, stop_hz)
    if cut:
        event(log, "scan_range_cut", note=cut)
    report("sweep", 0, 1, cut)
    band = scanned_band(start_hz, stop_hz, region)
    bin_hz = bin_for(start_hz, stop_hz, band)
    t0 = time.monotonic()
    hold: Sweep | None = None
    found: list[Signal] = []
    passes = 0
    while True:

        def hops(d: int, t: int, n: int = passes + 1) -> None:
            if watch_s > 0:
                report("watch", int(time.monotonic() - t0), int(watch_s), f"sweep {n}")
            else:
                report("sweep", d, t)

        sw = sweep(serial, start_hz, stop_hz, gain_db=gain_db, ppm=ppm, cancel=cancel,
                   bin_hz=bin_hz, on_progress=hops)  # fmt: skip
        passes += 1
        if hold is None or len(hold.db) != len(sw.db):
            hold = sw
        else:
            hold = replace(sw, db=np.maximum(hold.db, sw.db), seconds=hold.seconds + sw.seconds,
                           warnings=list(dict.fromkeys(hold.warnings + sw.warnings)),
                           gaps=[g for g in hold.gaps if g in sw.gaps])  # fmt: skip
        _merge(found, find_signals(sw, region, min_snr_db, spurs), bin_hz)
        prog.signals = found
        if watch_s <= 0 or time.monotonic() - t0 >= watch_s:
            break
    for s in found:
        s.passes = passes
    found.sort(key=lambda s: s.freq_hz)
    if rds and any(s.mode == "wfm" and not s.spur for s in found):
        report("rds", 0, 1)
        name_fm(serial, found, gain_db=gain_db, ppm=ppm, seconds=rds_seconds, cancel=cancel,
                on_progress=lambda d, t: report("rds", d, t))  # fmt: skip
    assert hold is not None
    report("done", 1, 1, "; ".join(hold.warnings))
    return hold, found


def _merge(into: list[Signal], new: list[Signal], bin_hz: float) -> None:
    """Fold one sweep's signals into those of earlier sweeps: the same channel counts once,
    with how often it was heard and its strongest reading."""
    for s in new:
        tol = max(3 * bin_hz, 3_000)
        m = next((x for x in into if abs(x.freq_hz - s.freq_hz) <= tol), None)
        if m is None:
            into.append(s)
            continue
        m.seen += 1
        m.spur, m.clock_hz = m.spur or s.spur, m.clock_hz or s.clock_hz
        if s.snr_db > m.snr_db:
            m.level_db, m.snr_db, m.width_hz = s.level_db, s.snr_db, s.width_hz


def scanned_band(start_hz: float, stop_hz: float, region: str) -> Band | None:
    """The band a range is, when it is (mostly) one: it sets the sweep's resolution."""
    return next((b for b in region_bands(region) if b.mode and b.start_hz >= start_hz - 200_000
                 and b.stop_hz <= stop_hz + 200_000 and b.width_hz >= 0.5 * (stop_hz - start_hz)),
                None)  # fmt: skip


def learn_spurs(
    serial: str,
    start_hz: float = SWEEP_LIMITS_HZ[0],
    stop_hz: float = SWEEP_LIMITS_HZ[1],
    *,
    gain_db: float = 30.0,
    ppm: int = 0,
    min_snr_db: float = 6.0,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Sweep, list[int]]:
    """The receiver's own signals: sweep with the antenna disconnected, so whatever shows up
    comes from the dongle and the computer (clock harmonics, USB noise). Two passes at
    ``SPUR_BIN_HZ``, and only lines in both count. (The hops' centres aren't filled in: they
    are 0.5% of the range, and a scan's hops fall elsewhere.)"""
    start_hz, stop_hz, cut = fit_range(start_hz, stop_hz)
    prog = Progress(note=cut)
    seen: list[list[int]] = []
    sw: Sweep | None = None
    for k in range(2):

        def hops(d: int, t: int, k: int = k) -> None:
            prog.phase, prog.done, prog.total = "sweep", k * t + d, 2 * t
            if on_progress is not None:
                on_progress(prog)

        sw = sweep(serial, start_hz, stop_hz, gain_db=gain_db, ppm=ppm, cancel=cancel,
                   bin_hz=SPUR_BIN_HZ, fill_centres=False, on_progress=hops)  # fmt: skip
        seen.append([round(f) for f in _lines(sw, min_snr_db)])
    assert sw is not None
    tol = spur_tolerance(sw.bin_hz)
    both = [f for f in seen[0] if any(abs(f - g) <= tol for g in seen[1])]
    return sw, both


def _lines(sw: Sweep, min_snr_db: float) -> list[float]:
    """Every local peak a margin above the floor, at its own bin (no band plan, no channels)."""
    excess = sw.db - _floor(sw, float(np.median(np.diff(sw.freqs))))
    peaks = np.flatnonzero((excess[1:-1] >= min_snr_db) & (sw.db[1:-1] >= sw.db[:-2])
                           & (sw.db[1:-1] >= sw.db[2:])) + 1  # fmt: skip
    return [float(sw.freqs[i]) for i in peaks if not sw.in_gap(float(sw.freqs[i]))]


def band_range(band_id: str, region: str) -> tuple[int, int]:
    """A band's edges for a scan, padded half a channel. The padding stops at the tuner's
    limits; the band itself doesn't, so that ``scan`` can say what it leaves out."""
    b = region_band(band_id, region)
    if b is None:
        raise ScanError(
            f"no band {band_id!r} in {region_text(region)}: `wavescribe radio bands` lists those "
            'there, and [radio] region in the stations file (e.g. region = "2") or the page\'s '
            "Radio tab sets the region"
        )
    fit_range(b.start_hz, b.stop_hz)  # refuses a band the tuner doesn't reach at all
    pad = int(channel_width(b) / 2)
    lo, hi = SWEEP_LIMITS_HZ
    start = max(min(b.start_hz, lo), b.start_hz - pad)
    return start, min(max(b.stop_hz, hi), b.stop_hz + pad)
