# SPDX-License-Identifier: Apache-2.0
"""Listening by hand, like a scanner radio or an SDR program: one receiver per dongle, tuned
from the page (Listen → Tune by hand), its audio played there and never transcribed, stored or
exported.

The receiver rides on the dongle's shared ``rtl_sdr`` process (``sources/sdr.py``), so it sits
beside running stations and tunes anywhere in the band they leave it, at once. A frequency
outside that band, or another gain, moves the dongle, which needs it to itself: its stations
are paused when the user says so, and start again when listening ends.

The waterfall is computed only while the page watches. A scan checks every channel of its list
in one FFT per block (about four times a second) and stops on the strongest one that rises above
the noise, so a whole band of channels is covered at once instead of stepped through. A receiver
that nobody listens to and no page watches closes after a minute (battery).
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from wavescribe.config import ConfigError, SdrDevice, radio_policy
from wavescribe.dsp import (
    DEFAULT_CHANNEL_HZ,
    MAX_CHANNEL_HZ,
    MIN_CHANNEL_HZ,
    MODES,
    OFF_CENTRE_HZ,
    check_tunable,
    frequency_hz,
    in_band,
    off_centre,
)
from wavescribe.dsp.activity import CLIPPING
from wavescribe.dsp.channel import NarrowbandDemodulator
from wavescribe.dsp.rds import RdsDecoder
from wavescribe.dsp.wfm import WfmDemodulator
from wavescribe.log import event
from wavescribe.radio.bandplan import Band, lookup, mhz_text
from wavescribe.radio.service import RadioError, span_mhz
from wavescribe.radio.watch import OVERLOAD_SHOWN_S, overload_report
from wavescribe.radio.words import joined, receiver_name, sentence, them
from wavescribe.sources import SourceHealth

if TYPE_CHECKING:
    from wavescribe.app import App
    from wavescribe.sources.sdr import RtlSdrDevice

log = logging.getLogger(__name__)

LIVE_FFT = 8192  # 293 Hz per bin at 2.4 MS/s: narrow channels show up on the waterfall
LIVE_SEGMENTS = 8  # FFTs averaged per spectrum
SPECTRUM_EVERY_S = 0.25
SPECTRUM_STALE_S = 2.0  # an older spectrum isn't sent: the receiver has stopped hearing
MAX_BINS = 1024  # what one spectrum sends to the page
IDLE_CLOSE_S = 60.0  # nobody listening, no page watching: close
REAP_EVERY_S = 5.0
SCAN_THRESHOLD_DB = 10.0  # a channel this far above the noise floor counts as active
SCAN_HOLD_S = 2.0  # stay on a channel this long after it goes quiet
WFM_WIDTH_HZ = 200_000


def live_id(device_id: str) -> str:
    """The audio id of a dongle's live receiver: ``/audio/live.<device>.pcm``. Station ids
    can't contain a dot, so it never collides with one."""
    return f"live.{device_id}"


@dataclass(frozen=True)
class Tuning:
    freq_hz: int
    mode: str = "wfm"
    squelch_db: float | None = None  # narrowband: a fixed level (dBFS); None: automatic
    channel_hz: float | None = None  # narrowband channel width; None: the mode's default
    deemphasis_us: float = 50.0

    @property
    def width_hz(self) -> float:
        if self.mode == "wfm":
            return WFM_WIDTH_HZ
        return float(self.channel_hz or DEFAULT_CHANNEL_HZ[self.mode])


@dataclass
class Scan:
    freqs: list[int]
    mode: str
    channel_hz: float | None
    threshold_db: float = SCAN_THRESHOLD_DB
    hold_s: float = SCAN_HOLD_S
    locked_out: set[int] = field(default_factory=set)
    holding: int | None = None
    quiet_since: float | None = None
    hits: dict[int, int] = field(default_factory=dict)
    levels: dict[int, float] = field(default_factory=dict)  # dB above the noise, last block
    started: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "freqs_mhz": [f / 1e6 for f in self.freqs], "mode": self.mode,
            "threshold_db": self.threshold_db, "hold_s": self.hold_s,
            "holding_mhz": self.holding / 1e6 if self.holding else None,
            "locked_out_mhz": sorted(f / 1e6 for f in self.locked_out),
            "hits": {f"{f / 1e6:.5f}": n for f, n in self.hits.items()},
            "levels": {f"{f / 1e6:.5f}": round(v, 1) for f, v in self.levels.items()},
            "seconds": round(time.time() - self.started),
        }  # fmt: skip


class LiveReceiver:
    """A consumer of one dongle's IQ, like a station's channel, but retuned in place and with
    its audio going only to the page's player."""

    def __init__(self, device: RtlSdrDevice, tuning: Tuning, on_audio: Any) -> None:
        self.device = device
        self.station_id = live_id(device.config.id)  # the device logs levels per consumer
        self.health = SourceHealth()
        self.tuning = tuning
        self.scan: Scan | None = None
        self._applied: Tuning | None = None
        self._demod: WfmDemodulator | NarrowbandDemodulator | None = None
        self._rds: RdsDecoder | None = None
        self._on_audio = on_audio
        self._power: np.ndarray | None = None  # the band's power, LIVE_FFT bins, linear
        self._power_t = 0.0
        self.overloaded_at: float | None = None  # when the converter last clipped hard
        self._next_spectrum = 0.0
        self.watched_until = 0.0
        self.last_seen = time.time()
        self.closed = False
        self.error: str | None = None  # why it stopped, when the device dropped it
        self.started = time.time()

    # Called on the device thread.

    def _feed(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        if self.closed:
            return
        try:
            self._receive(iq, t_end, first)
        except Exception as e:
            # The device logs it and drops this receiver. The page is told why, and tuning
            # again opens a new one (LiveService.tune).
            self.error = str(e) or type(e).__name__
            raise

    def _receive(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        h = self.health
        h.connected, h.last_data_at = True, t_end  # before the spectrum, which says it's live
        now = time.time()
        if self.scan is not None or (now < self.watched_until and now >= self._next_spectrum):
            self._next_spectrum = now + SPECTRUM_EVERY_S
            self._power = band_power(iq)
            self._power_t = now
            if self.scan is not None:
                self._scan_step(self._power, now)
        want = self.tuning
        if first or self._demod is None or want != self._applied:
            self._build(want)
        assert self._demod is not None
        audio = self._demod(iq)
        h.signal_db = self._demod.power_db
        h.clipped = self.device.clipped
        if (h.clipped or 0.0) >= CLIPPING:  # a transmitter close by: Listen says what to do
            self.overloaded_at = now
        if isinstance(self._demod, NarrowbandDemodulator):
            h.squelch_open = self._demod.open
        if self._rds is not None and self._rds.info.groups:
            h.rds = self._rds.info.to_dict()
        if len(audio):
            self._on_audio(audio)

    def _disconnected(self, reconnects: int) -> None:
        self.health.connected = False
        self.health.reconnects = reconnects

    def _build(self, t: Tuning) -> None:
        demod = demodulator(self.device.config, t)
        if isinstance(demod, WfmDemodulator):
            # A new station has a new name: RDS starts over after a retune.
            same = self._applied is not None and self._applied.freq_hz == t.freq_hz
            if self._rds is None or not same:
                self._rds = RdsDecoder()
                self.health.rds = None
            demod.on_mpx = self._rds
        else:
            self._rds = None
            self.health.rds = None
        self._demod, self._applied = demod, t

    def _scan_step(self, power: np.ndarray, now: float) -> None:
        scan = self.scan
        if scan is None:
            return
        cfg = self.device.config
        noise = float(np.median(power)) + 1e-20
        width = scan.channel_hz or DEFAULT_CHANNEL_HZ.get(scan.mode, 12_500)
        levels: dict[int, float] = {}
        for f in scan.freqs:
            lo, hi = bin_range(f - width / 2, f + width / 2, cfg.center_hz, cfg.sample_rate)
            if hi > lo:
                levels[f] = 10 * np.log10(float(power[lo:hi].mean()) / noise)
        scan.levels = levels
        if scan.holding is not None:
            open_now = isinstance(self._demod, NarrowbandDemodulator) and self._demod.open
            active = open_now or levels.get(scan.holding, -99) >= scan.threshold_db - 3
            if active:
                scan.quiet_since = None
                return
            if scan.quiet_since is None:
                scan.quiet_since = now
                return
            if now - scan.quiet_since < scan.hold_s:
                return
            scan.holding, scan.quiet_since = None, None
        busy = {f: v for f, v in levels.items()
                if v >= scan.threshold_db and f not in scan.locked_out}  # fmt: skip
        if not busy:
            return
        f = max(busy, key=lambda k: busy[k])
        scan.holding, scan.quiet_since = f, None
        scan.hits[f] = scan.hits.get(f, 0) + 1
        self.tuning = dataclasses.replace(self.tuning, freq_hz=f, mode=scan.mode,
                                          channel_hz=scan.channel_hz)  # fmt: skip

    # Called from the page's threads.

    def spectrum(self, lo_hz: float | None = None, hi_hz: float | None = None) -> dict | None:
        """The band's power between ``lo_hz`` and ``hi_hz`` (default: all of it), in at most
        MAX_BINS bins; each bin keeps the strongest of the FFT bins it covers, so a narrow
        signal stays visible when zoomed out. None when there is no recent one: a picture that
        no longer changes would pass for a quiet band."""
        power = self._power
        if power is None or time.time() - self._power_t > SPECTRUM_STALE_S:
            return None
        cfg = self.device.config
        start, stop = cfg.center_hz - cfg.sample_rate / 2, cfg.center_hz + cfg.sample_rate / 2
        lo_hz = max(start, lo_hz if lo_hz is not None else start)
        hi_hz = min(stop, hi_hz if hi_hz is not None else stop)
        lo, hi = bin_range(lo_hz, hi_hz, cfg.center_hz, cfg.sample_rate)
        part = power[lo:hi]
        if len(part) == 0:
            return None
        group = max(1, -(-len(part) // MAX_BINS))
        usable = len(part) - len(part) % group
        if usable == 0:
            usable, group = len(part), 1
        merged = part[:usable].reshape(-1, group).max(axis=1)
        db = 10 * np.log10(merged + 1e-20)
        bin_hz = cfg.sample_rate / LIVE_FFT
        return {
            "t": round(self._power_t, 2),
            "start_hz": round(start + lo * bin_hz),
            "stop_hz": round(start + (lo + usable) * bin_hz),
            "noise_db": round(float(10 * np.log10(np.median(power) + 1e-20)), 1),
            "db": [round(float(x), 1) for x in db],
        }  # fmt: skip

    def close(self) -> None:
        self.closed = True
        self.health.connected = False
        self.device.detach(self)  # type: ignore[arg-type]


def demodulator(cfg: SdrDevice, t: Tuning) -> WfmDemodulator | NarrowbandDemodulator:
    """The demodulator for ``t`` on a dongle set up as ``cfg``; ValueError if they don't fit."""
    offset = t.freq_hz - cfg.center_hz
    if t.mode == "wfm":
        return WfmDemodulator(cfg.sample_rate, offset, t.deemphasis_us)
    return NarrowbandDemodulator(cfg.sample_rate, offset, t.mode, t.channel_hz, t.squelch_db)


def _band_channel_hz(band: Band | None, mode: str) -> float | None:
    """The band plan's channel width for a narrowband mode, if it is one such a channel can
    have. The raster of a broadcast band (100 or 200 kHz) says nothing about an NFM or AM signal
    someone listens for inside it: that gets the mode's default width."""
    if band is None or mode == "wfm":
        return None
    width = band.channel_hz or band.step_hz
    return width if width and MIN_CHANNEL_HZ <= width <= MAX_CHANNEL_HZ else None


def _channel_hz(raw: dict[str, Any]) -> float | None:
    """The channel width asked for, checked here: a bad one would fail on the dongle's thread."""
    channel = raw.get("channel_khz")
    if channel in (None, ""):
        return None
    try:
        hz = float(channel) * 1e3
    except (TypeError, ValueError):
        hz = -1.0
    if not MIN_CHANNEL_HZ <= hz <= MAX_CHANNEL_HZ:
        raise LiveError(f"A channel is {MIN_CHANNEL_HZ / 1e3:g} to {MAX_CHANNEL_HZ / 1e3:g} kHz "
                        "wide, e.g. 12.5 or 25.")  # fmt: skip
    return hz


def band_power(iq: np.ndarray) -> np.ndarray:
    """Average power per frequency across the block, LIVE_FFT bins, centre in the middle."""
    n = LIVE_FFT
    count = len(iq) // n
    if count == 0:
        iq = np.pad(iq, (0, n - len(iq)))
        count = 1
    picks = np.linspace(0, count - 1, min(LIVE_SEGMENTS, count)).astype(int)
    window = np.hanning(n).astype(np.float32)
    acc = np.zeros(n, dtype=np.float64)
    for k in picks:
        acc += np.abs(np.fft.fft(iq[k * n : (k + 1) * n] * window)) ** 2
    return np.fft.fftshift(acc / len(picks)) / float(window.sum()) ** 2


def bin_range(lo_hz: float, hi_hz: float, center_hz: float, rate: int) -> tuple[int, int]:
    def k(hz: float) -> int:
        return int(np.clip(round((hz - center_hz + rate / 2) * LIVE_FFT / rate), 0, LIVE_FFT))

    return k(lo_hz), max(k(lo_hz) + 1, k(hi_hz))


class LiveError(RadioError):
    """What the page asked for can't be done; ``code`` tells the page what to offer."""


class LiveService:
    """The live receivers of a ``wavescribe serve`` session, one per dongle at most."""

    def __init__(self, app: App) -> None:
        self.app = app
        self._rx: dict[str, LiveReceiver] = {}
        self._paused: dict[str, dict[str, Any]] = {}  # device id -> what was paused for it
        self._acks: dict[str, set[str]] = {}  # device id -> privacy classes acknowledged
        # device id -> the page view it was last tuned from ("find" or "listen"), when it said
        self._views: dict[str, str] = {}
        self._lock = threading.RLock()
        self._reaper: threading.Thread | None = None

    def is_open(self, device_id: str) -> bool:
        return device_id in self._rx

    def open_ids(self) -> list[str]:
        return list(self._rx)

    def tuned(self, device_id: str) -> dict[str, Any]:
        """How the receiver is tuned, as ``tune`` takes it: to tune it so again on resume."""
        rx = self._rx[device_id]
        t = rx.tuning
        return {"mhz": t.freq_hz / 1e6, "mode": t.mode, "squelch_db": t.squelch_db,
                "channel_khz": t.channel_hz / 1e3 if t.channel_hz else None,
                "gain_db": rx.device.config.gain_db, "from": self._views.get(device_id),
                "acknowledge": True}  # fmt: skip

    def carry(self, device_id: str, paused: dict[str, Any]) -> None:
        """What the receiver had stopped when it was tuned by hand, to start again when
        listening there ends (after resume tunes it again)."""
        with self._lock:
            self._paused[device_id] = paused

    def centre(self, device_id: str) -> int | None:
        """Where the dongle is tuned for its hand-tuned receiver, if one is open."""
        rx = self._rx.get(device_id)
        return rx.device.config.center_hz if rx is not None else None

    def paused_ids(self, device_id: str) -> list[str]:
        """The stations paused while the receiver is tuned by hand, which start again after."""
        return list((self._paused.get(device_id) or {}).get("stations", []))

    def holds(self, device_id: str) -> str | None:
        """Why a station can't start on this dongle now, or None."""
        rx = self._rx.get(device_id)
        d = self._config(device_id, missing_ok=True)
        if rx is None or d is None or rx.device.config == d:
            return None
        mhz, name = mhz_text(rx.tuning.freq_hz / 1e6), receiver_name(device_id)
        view = self._views.get(device_id)
        if view == "find":
            return (f"Receiver {name} is tuned by hand to {mhz} MHz under Find: let it go there "
                    "first.")  # fmt: skip
        if view == "listen":
            return (f"Receiver {name} is tuned by hand to {mhz} MHz under Listen: stop listening "
                    "there first.")  # fmt: skip
        return (f"Receiver {name} is tuned by hand to {mhz} MHz (under Find or Listen): let it go "
                "there first.")  # fmt: skip

    def tune(self, device_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Open the dongle's live receiver, or retune it. ``from`` says which page view tunes
        it ("find" or "listen"), for messages that tell the user where to let it go."""
        if self.app.paused_since is not None:
            raise LiveError("Listening is paused: resume it to listen here.", "paused")
        d = self._config(device_id)
        view = raw.get("from")
        if view not in (None, "find", "listen"):
            raise LiveError("'from' is the view that tunes the receiver: find or listen.")
        region, _ = self.app.radio.region()
        try:
            freq = frequency_hz(raw.get("mhz"))
            check_tunable(freq)
        except ValueError as e:
            raise LiveError(sentence(str(e))) from e
        band = lookup(freq, region)
        mode = str(raw.get("mode") or (band.mode if band and band.mode else "nfm"))
        if mode not in MODES:
            raise LiveError(f"The mode is one of {', '.join(MODES)}.")
        squelch = raw.get("squelch_db")
        try:
            squelch_db = float(squelch) if squelch not in (None, "") else None
        except (TypeError, ValueError) as e:
            raise LiveError("The squelch is a level in dB, e.g. -40.") from e
        channel_hz = None if mode == "wfm" else _channel_hz(raw) or _band_channel_hz(band, mode)
        tuning = Tuning(freq, mode, squelch_db=squelch_db, channel_hz=channel_hz)
        self._check_ack(d.id, freq, mode, region, bool(raw.get("acknowledge")), tuning.width_hz)
        gain = raw.get("gain_db")
        with self._lock:
            self._refuse_while_scanning(d.id)
            rx = self._rx.get(d.id)
            dev_cfg = self._current_config(d)
            try:
                want_gain = float(gain) if gain not in (None, "") else dev_cfg.gain_db
            except (TypeError, ValueError) as e:
                raise LiveError("The gain is a number in dB, e.g. 30.") from e
            fits = in_band(freq, dev_cfg.center_hz, dev_cfg.sample_rate, mode) and off_centre(
                freq, dev_cfg.center_hz, mode, channel_hz
            )
            centre = dev_cfg.center_hz if fits else freq - OFF_CENTRE_HZ[mode]
            cfg = dataclasses.replace(dev_cfg, center_hz=round(centre), gain_db=want_gain)
            try:
                # Built here once, where the page gets the reason. The receiver builds its own
                # on the dongle's thread, and a failure there could only detach it.
                demodulator(cfg, tuning)
            except ValueError as e:
                raise LiveError(sentence(f"can't listen to that: {e}")) from e
            if cfg == dev_cfg:
                if rx is not None and not rx.closed:  # closed: the device dropped it
                    rx.scan = None  # a frequency chosen by hand ends a scan
                    rx.tuning = tuning
                else:
                    self._open(d, dev_cfg, tuning)
            else:
                self._move(d, cfg, tuning, bool(raw.get("pause")))
            if view is not None:
                self._views[d.id] = view
        self._start_reaper()
        return self.state(d.id)

    def close(self, device_id: str, *, resume: bool = True) -> dict[str, Any] | None:
        """Stop listening: the dongle goes back to its stations, which start again if they
        were paused for it (with ``resume=False``, what was paused is returned instead)."""
        from wavescribe.sources import sdr

        with self._lock:
            rx = self._rx.pop(device_id, None)
            paused = self._paused.pop(device_id, None)
            self._acks.pop(device_id, None)
            self._views.pop(device_id, None)
            if rx is None:
                return paused
            rx.close()
            self.app.hub.tap(rx.station_id).close()
            d = self._config(device_id, missing_ok=True)
            if d is not None and rx.device.config != d:
                deadline = time.time() + 5
                while not sdr.forget_device(d.serial) and time.time() < deadline:
                    time.sleep(0.05)
            event(log, "live_close", device=device_id, s=round(time.time() - rx.started))
        if paused and resume:
            self.app.radio._resume(paused)
            return None
        return paused

    def close_all(self) -> None:
        for device_id in list(self._rx):
            self.close(device_id)

    def state(self, device_id: str, *, lo_mhz: Any = None, hi_mhz: Any = None,
              watch: bool = False) -> dict[str, Any]:  # fmt: skip
        d = self._config(device_id)
        rx = self._rx.get(d.id)
        cfg = rx.device.config if rx else self._current_config(d)
        out: dict[str, Any] = {
            "id": d.id, "device": d.id, "open": rx is not None, "center_mhz": cfg.center_hz / 1e6,
            "span_mhz": span_mhz(cfg), "gain_db": cfg.gain_db,
            "holders": self._holders(d.id), "paused": self._names(
                (self._paused.get(d.id) or {}).get("stations", [])),
        }  # fmt: skip
        if rx is None:
            return out
        if watch:
            rx.watched_until = time.time() + 3.0
            rx.last_seen = time.time()
        region, _ = self.app.radio.region()
        t, h = rx.tuning, rx.health
        band = lookup(t.freq_hz, region, t.width_hz)  # its class, as wide as it's heard
        lo = float(lo_mhz) * 1e6 if lo_mhz not in (None, "") else None
        hi = float(hi_mhz) * 1e6 if hi_mhz not in (None, "") else None
        out.update({
            "mhz": t.freq_hz / 1e6, "mode": t.mode, "squelch_db": t.squelch_db,
            "channel_khz": t.width_hz / 1e3,
            "signal_db": _r(h.signal_db) if h.connected else None,
            "squelch_open": h.squelch_open if t.mode != "wfm" else None,
            "connected": h.connected, "reconnects": h.reconnects, "clipped": h.clipped,
            "overload": self._overload(rx),
            "error": rx.error,
            "rds": h.rds if t.mode == "wfm" else None,
            "band": band.to_dict() if band else None,
            "privacy": band.privacy if band else "private",
            "scan": rx.scan.to_dict() if rx.scan else None,
            "audio": rx.station_id,
            "listeners": self.app.hub.tap(rx.station_id).listeners,
            "spectrum": rx.spectrum(lo, hi) if watch else None,
        })  # fmt: skip
        return out

    @staticmethod
    def _overload(rx: LiveReceiver) -> dict[str, object] | None:
        """The receiver's last overload, as a watch's status gives it (``overload_report``),
        while its converter clips hard and for ``OVERLOAD_SHOWN_S`` after; the channel on top
        isn't known here, so ``freq_hz`` is None."""
        at = rx.overloaded_at
        if at is None or time.time() - at > OVERLOAD_SHOWN_S:
            return None
        return overload_report(at, None, f"Receiver {receiver_name(rx.device.config.id)}")

    def start_scan(self, device_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Scan a list of channels in the band the dongle hears now, stopping on activity."""
        d = self._config(device_id)
        rx = self._rx.get(d.id)
        if rx is None or rx.closed:
            raise LiveError("Tune the receiver first: the scan uses the band it hears.", "closed")
        cfg = rx.device.config
        region, _ = self.app.radio.region()
        try:
            freqs = sorted({frequency_hz(f) for f in raw.get("freqs_mhz") or []})
        except (TypeError, ValueError) as e:
            raise LiveError(sentence(f"the channels to scan, in MHz: {e}")) from e
        mode = str(raw.get("mode") or "nfm")
        if mode not in ("nfm", "am"):
            raise LiveError("A scan stops on voice channels: choose NFM or AM.")
        channel = _channel_hz(raw)
        inside = [
            f for f in freqs
            if in_band(f, cfg.center_hz, cfg.sample_rate, mode)
            and off_centre(f, cfg.center_hz, mode, channel)  # or the scan would stop there for good
        ]  # fmt: skip
        if not inside:
            lo = (cfg.center_hz - cfg.sample_rate / 2) / 1e6
            hi = (cfg.center_hz + cfg.sample_rate / 2) / 1e6
            raise LiveError(f"None of those channels is in what receiver {receiver_name(d.id)} "
                            f"hears now ({lo:.3f} to {hi:.3f} MHz): tune near them first.",
                            "band")  # fmt: skip
        for f in inside:  # once per privacy class: the answer is remembered
            self._check_ack(d.id, f, mode, region, bool(raw.get("acknowledge")))
        spurs = set(self.app.radio.catalog.spurs(d.serial))
        width = channel or DEFAULT_CHANNEL_HZ[mode]
        locked = {f for f in inside if any(abs(f - s) <= width / 2 for s in spurs)}
        rx.scan = Scan(
            inside, mode, channel,
            threshold_db=float(raw.get("threshold_db") or SCAN_THRESHOLD_DB),
            hold_s=float(raw.get("hold_s") or SCAN_HOLD_S), locked_out=locked,
        )  # fmt: skip
        event(log, "live_scan", device=d.id, channels=len(inside), skipped=len(freqs) - len(inside))
        return self.state(d.id)

    def stop_scan(self, device_id: str) -> dict[str, Any]:
        rx = self._rx.get(device_id)
        if rx is not None:
            rx.scan = None
        return self.state(device_id)

    def lock_out(self, device_id: str, mhz: Any, on: bool = True) -> dict[str, Any]:
        """Skip (or stop skipping) a channel in the running scan: a spur, a busy data link."""
        rx = self._rx.get(device_id)
        if rx is None or rx.scan is None:
            raise LiveError(f"No scan is running on receiver {receiver_name(device_id)}.",
                            "closed")  # fmt: skip
        f = min(rx.scan.freqs, key=lambda x: abs(x - float(mhz) * 1e6))
        (rx.scan.locked_out.add if on else rx.scan.locked_out.discard)(f)
        if on and rx.scan.holding == f:
            rx.scan.holding, rx.scan.quiet_since = None, None
        return self.state(device_id)

    def make_station(self, device_id: str, raw: dict[str, Any]) -> dict[str, Any]:
        """Keep what is tuned as a station (listened to, transcribed, or also translated), as
        Find's Add as a station does (``RadioService.tune``): the receiver is let go once the
        station is made, and stays tuned by hand while the page asks about a retune."""
        rx = self._rx.get(device_id)
        if rx is None:
            raise LiveError(f"Nothing is tuned on receiver {receiver_name(device_id)}.", "closed")
        t = rx.tuning
        tune: dict[str, Any] = {
            "device": device_id, "mhz": t.freq_hz / 1e6, "mode": t.mode,
            "acknowledge": True,  # given when this frequency was tuned here
            "retune": bool(raw.get("retune")),
            # languages: those language "auto" may be, as /radio/tune takes them
            **{k: raw[k] for k in ("name", "language", "languages", "translate_to") if raw.get(k)},
        }  # fmt: skip
        if t.squelch_db is not None:
            tune["squelch_db"] = t.squelch_db
        does = raw.get("does", "translate")
        if does == "listen":
            tune["transcribe"] = False
        elif does == "transcribe":
            tune["translate"] = False
        return self.app.radio.tune(tune)

    # -- internals ------------------------------------------------------------------------

    def _config(self, device_id: str, missing_ok: bool = False) -> SdrDevice:
        d = next((x for x in self.app.config.devices if x.id == device_id), None)
        if d is None and not missing_ok:
            raise LiveError(f"There is no receiver {device_id!r}.")
        return d  # type: ignore[return-value]

    def _names(self, station_ids: list[str]) -> list[str]:
        """Stations as the user knows them: by name."""
        names = {s.id: s.name for s in self.app.config.stations}
        return [names.get(sid, sid) for sid in station_ids]

    def _current_config(self, d: SdrDevice) -> SdrDevice:
        from wavescribe.sources import sdr

        rx = self._rx.get(d.id)
        if rx is not None:
            return rx.device.config
        dev = sdr.find_device(d.serial)
        return dev.config if dev is not None and dev._snapshot() else d

    def _holders(self, device_id: str) -> list[str]:
        rx = next((r for r in self.app.radio.receivers() if r["id"] == device_id), None)
        return [h for h in (rx["busy"] if rx else []) if h != "the live receiver"]

    def _check_ack(self, device_id: str, freq: int, mode: str, region: str, ack: bool,
                   width_hz: float | None = None) -> None:  # fmt: skip
        band = lookup(freq, region, width_hz)
        privacy = band.privacy if band else "private"
        known = self._acks.setdefault(device_id, set())
        try:
            radio_policy(freq, mode, region, ack or privacy in known, "here", width_hz)
        except ConfigError as e:
            raise LiveError(sentence(str(e).split(": ", 1)[-1].replace(
                " Set acknowledge = true to confirm", " Confirm")), "acknowledge",
                privacy=privacy) from e  # fmt: skip
        known.add(privacy)

    def _refuse_while_scanning(self, device_id: str) -> None:
        job = self.app.radio.scan_status()
        if job and job["device"] == device_id and job["phase"] not in ("done", "error"):
            raise LiveError(f"Receiver {receiver_name(device_id)} is scanning the band: wait for "
                            "it, or stop the scan.", "scanning")  # fmt: skip

    def _on_audio(self, sid: str):
        tap = self.app.hub.tap(sid)
        return tap.feed

    def _open(self, d: SdrDevice, cfg: SdrDevice, tuning: Tuning) -> None:
        from wavescribe.sources import sdr

        try:
            device = sdr.shared_device(cfg)
        except ValueError as e:
            raise LiveError(sentence(str(e))) from e
        if not device.available:
            raise LiveError("rtl_sdr isn't installed: brew install librtlsdr.", "tools")
        rx = LiveReceiver(device, tuning, self._on_audio(live_id(d.id)))
        device.attach(rx)  # type: ignore[arg-type]
        self._rx[d.id] = rx
        event(log, "live_open", device=d.id, mhz=tuning.freq_hz / 1e6, mode=tuning.mode,
              center_mhz=cfg.center_hz / 1e6, gain_db=cfg.gain_db)  # fmt: skip

    def _move(self, d: SdrDevice, cfg: SdrDevice, tuning: Tuning, pause: bool) -> None:
        """Reopen the dongle with another centre or gain; it needs it to itself."""
        from wavescribe.sources import sdr

        holders = self._holders(d.id)
        if holders and not pause:
            raise LiveError(
                f"Receiver {receiver_name(d.id)} is running {joined(holders)}. That frequency or "
                f"gain needs the receiver to itself: pause {them(holders)} while you listen, or "
                "use another receiver.", "busy", holders=holders,
            )  # fmt: skip
        old = self._rx.pop(d.id, None)
        if old is not None:  # first, or pausing the stations waits for it to let go
            old.close()
        if holders:
            rx_view = next(r for r in self.app.radio.receivers() if r["id"] == d.id)
            paused = self.app.radio._pause(d, rx_view)
            prev = self._paused.get(d.id)
            if prev:  # already paused once: keep the first list
                paused["stations"] = sorted({*prev["stations"], *paused["stations"]})
                paused["monitor"] = paused["monitor"] or prev["monitor"]
            self._paused[d.id] = paused
        deadline = time.time() + 5
        while not sdr.forget_device(d.serial) and time.time() < deadline:
            time.sleep(0.05)
        self._open(d, cfg, tuning)

    def _start_reaper(self) -> None:
        if self._reaper is not None and self._reaper.is_alive():
            return
        self._reaper = threading.Thread(target=self._reap, daemon=True, name="live-reaper")
        self._reaper.start()

    def _reap(self) -> None:
        while self._rx:
            time.sleep(REAP_EVERY_S)
            for device_id, rx in list(self._rx.items()):
                idle = time.time() - rx.last_seen > IDLE_CLOSE_S
                if idle and self.app.hub.tap(rx.station_id).listeners == 0:
                    event(log, "live_idle", device=device_id)
                    with contextlib.suppress(Exception):
                        self.close(device_id)


def _r(x: float | None) -> float | None:
    return round(x, 1) if x is not None else None
