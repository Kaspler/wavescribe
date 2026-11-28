# SPDX-License-Identifier: Apache-2.0
"""SDR sources: an RTL-SDR read through an ``rtl_sdr`` subprocess, demodulated in numpy/scipy.

librtlsdr and its tools are GPL, so they only ever run as a separate process connected by a pipe
(the same boundary as ffmpeg). ``rtl_sdr`` writes raw 8-bit IQ to stdout; ``RtlSdrDevice`` owns
that process, restarts it with backoff when it dies or stalls (a dongle unplugged, a USB hiccup),
and hands each block of IQ to every station tuned on it. Each ``SdrChannelSource`` mixes its
station down, demodulates it (``wavescribe.dsp.wfm``) and yields 16 kHz PCM like any other
``AudioSource``.

One device serves several stations as long as they fit in its band (2.4 MS/s covers about
±1 MHz around the centre). The device runs while at least one of its stations is iterated.
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import select
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import IO

import numpy as np

from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.dsp.channel import NarrowbandDemodulator
from wavescribe.dsp.rds import RdsDecoder
from wavescribe.dsp.wfm import WfmDemodulator, iq_from_u8
from wavescribe.log import event
from wavescribe.radio.words import receiver_name
from wavescribe.sources import SourceHealth

log = logging.getLogger(__name__)

LEVEL_LOG_S = 30.0  # how often each device logs its signal levels
SPECTRUM_BINS = 512  # what the page draws
SPECTRUM_FFT = 2048
SPECTRUM_EVERY_S = 0.4
LOAD_SMOOTHING = 0.2  # of each block's share in ``RtlSdrDevice.load``


class RtlSdrDevice:
    """One dongle and its ``rtl_sdr`` process, shared by the stations tuned on it."""

    def __init__(
        self,
        config: SdrDevice,
        *,
        command: str = "rtl_sdr",
        chunk_seconds: float = 0.256,
        stall_timeout: float = 5.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 30.0,
        clock: Callable[[], float] = time.time,
        command_line: bool = False,
    ) -> None:
        """A dongle that gives nothing when first started (another program has it, it isn't
        plugged in) is tried again with backoff, as a server must. ``command_line`` is for a
        command that says itself what goes wrong (``radio watch``): such a dongle stops the
        device instead, its stations end, and ``unopened`` says what rtl_sdr said; rtl_sdr's
        words and the reconnects go to the log, not to the terminal."""
        self.config = config
        self._command = command
        self._command_line = command_line
        self._trouble_level = logging.INFO if command_line else logging.WARNING
        # Whole 16 kHz output samples per block, so every station's audio comes out in step.
        per_out = config.sample_rate // SAMPLE_RATE
        self._chunk_bytes = 2 * per_out * max(1, round(chunk_seconds * SAMPLE_RATE))
        self._stall_timeout = stall_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._clock = clock
        self._lock = threading.Lock()
        self._channels: list[SdrChannelSource] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self.connected = False
        self.reconnects = 0
        self.heard_at: float | None = None  # when the dongle first sent anything
        self.said: deque[str] = deque(maxlen=20)  # rtl_sdr's last words on stderr, this run's
        self.unopened: str | None = None  # what rtl_sdr said when it gave up (command_line)
        self.clipped: float | None = None  # share of IQ samples at the ADC's limits (overload)
        # The share of a block's length spent handing it to everything on the dongle,
        # smoothed: near 1, the thread is falling behind the dongle.
        self.load = 0.0
        # The spectrum is computed only while someone looks at it (the receivers panel asks
        # again every half second), so a dongle nobody watches costs nothing extra.
        self.spectrum_wanted_until = 0.0
        self.spectrum: dict[str, object] | None = None
        self._next_spectrum = 0.0

    @property
    def available(self) -> bool:
        return shutil.which(self._command) is not None

    def command(self) -> list[str]:
        c = self.config
        return [
            self._command, "-d", device_arg(c.serial), "-f", str(c.center_hz),
            "-s", str(c.sample_rate), "-g", f"{c.gain_db:g}", *ppm_args(c.ppm), "-",
        ]  # fmt: skip

    def attach(self, channel: SdrChannelSource) -> None:
        with self._lock:
            if channel not in self._channels:
                self._channels.append(channel)
            if self._thread is None or self._stop.is_set() or not self._thread.is_alive():
                # A thread still winding down keeps its own (set) stop event; at worst the new
                # rtl_sdr finds the dongle busy for a moment and retries.
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._loop,
                    args=(self._stop,),
                    daemon=True,
                    name=f"rtl_sdr-{self.config.serial}",
                )
                self._thread.start()

    def detach(self, channel: SdrChannelSource) -> None:
        with self._lock:
            if channel in self._channels:
                self._channels.remove(channel)
            if self._channels:
                return
            self._stop.set()
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def _loop(self, stop: threading.Event) -> None:
        failures = 0  # consecutive runs that ended without data
        while not stop.is_set():
            if self._run_once(stop):
                failures = 0
            self.connected = False
            if not stop.is_set():  # counted before the channels hear of it: a first drop is one
                self.reconnects += 1
            for ch in self._snapshot():
                ch._disconnected(self.reconnects)
            if stop.is_set():
                break
            if self.heard_at is None and self._command_line:
                self.unopened = "\n".join(self.said) or "it said nothing"
                event(log, "source_unopened", device=self.config.id,
                      serial=self.config.serial, said=self.unopened)  # fmt: skip
                for ch in self._snapshot():  # their iterators end: their stations stop
                    ch.close()
                break
            delay = min(self._backoff_max, self._backoff_initial * 2 ** min(failures, 16))
            failures += 1
            event(
                log,
                "source_reconnect",
                level=self._trouble_level,
                device=self.config.id,
                serial=self.config.serial,
                delay_s=delay,
                reconnects=self.reconnects,
            )
            if stop.wait(delay):
                break

    def _snapshot(self) -> list[SdrChannelSource]:
        with self._lock:
            return list(self._channels)

    def _run_once(self, stop: threading.Event) -> bool:
        """One rtl_sdr lifetime: spawn, read until EOF/stall/stop, then reap. True if data came."""
        try:
            proc = subprocess.Popen(
                self.command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            event(log, "source_error", level=logging.ERROR, serial=self.config.serial, error=str(e))
            return False
        with self._lock:
            self._proc = proc
        assert proc.stdout is not None and proc.stderr is not None
        self.said.clear()
        drain = threading.Thread(
            target=_drain_stderr,
            args=(proc.stderr, self.config, self.said, self._trouble_level),
            daemon=True,
            name=f"rtl_sdr-stderr-{self.config.id}",
        )
        drain.start()
        event(
            log,
            "source_connect",
            device=self.config.id,
            serial=self.config.serial,
            center_mhz=self.config.center_hz / 1e6,
            gain_db=self.config.gain_db,
            ppm=self.config.ppm,
            pid=proc.pid,
        )
        fd = proc.stdout.fileno()
        block_s = self._chunk_bytes / 2 / self.config.sample_rate
        buf = bytearray()
        first, got = True, False
        reason = "eof"
        clipped_max, next_level = 0.0, self._clock() + LEVEL_LOG_S
        try:
            while not stop.is_set():
                ready, _, _ = select.select([fd], [], [], self._stall_timeout)
                if not ready:
                    reason = "stall"
                    break
                data = os.read(fd, 1 << 20)
                if not data:
                    break
                buf += data
                while len(buf) >= self._chunk_bytes:
                    raw = np.frombuffer(bytes(buf[: self._chunk_bytes]), dtype=np.uint8)
                    del buf[: self._chunk_bytes]
                    clipped = float(np.count_nonzero((raw == 0) | (raw == 255))) / len(raw)
                    self.clipped = clipped
                    clipped_max = max(clipped_max, clipped)
                    self.connected, got = True, True
                    iq = iq_from_u8(raw)
                    now = self._clock()
                    if self.heard_at is None:
                        self.heard_at = now
                    began = time.perf_counter()
                    for ch in self._snapshot():
                        try:
                            ch._feed(iq, now, first)
                        except Exception:  # one broken channel must not silence the others
                            log.exception("receiver %s: a channel failed; it is detached",
                                          receiver_name(self.config.id))  # fmt: skip
                            self.detach(ch)  # type: ignore[arg-type]
                            with contextlib.suppress(Exception):
                                ch.close()
                    first = False
                    share = (time.perf_counter() - began) / block_s
                    self.load += LOAD_SMOOTHING * (share - self.load)
                    if now < self.spectrum_wanted_until and now >= self._next_spectrum:
                        self._next_spectrum = now + SPECTRUM_EVERY_S
                        self.spectrum = spectrum(iq, self.config, now, clipped)
                    if now >= next_level:
                        self._log_levels(clipped_max)
                        clipped_max, next_level = 0.0, now + LEVEL_LOG_S
            else:
                reason = "closed"
        finally:
            if stop.is_set():
                reason = "closed"
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            drain.join(2)  # all it said, for ``said``
            with self._lock:
                if self._proc is proc:
                    self._proc = None
            event(
                log,
                "source_disconnect",
                device=self.config.id,
                serial=self.config.serial,
                reason=reason,
                returncode=proc.returncode,
            )
        return got

    def _log_levels(self, clipped_max: float) -> None:
        event(
            log,
            "sdr_level",
            device=self.config.id,
            serial=self.config.serial,
            gain_db=self.config.gain_db,
            clipped_max=round(clipped_max, 5),
            stations={
                ch.station_id: round(ch.health.signal_db, 1)
                for ch in self._snapshot()
                if ch.health.signal_db is not None
            },
        )


def spectrum(iq: np.ndarray, config: SdrDevice, now: float, clipped: float) -> dict[str, object]:
    """Power across the band the dongle sees, averaged over the block, in dB (relative to
    full scale), centre frequency in the middle: what the receivers panel's waterfall draws."""
    n = SPECTRUM_FFT
    segments = max(1, min(16, len(iq) // n))
    window = np.hanning(n).astype(np.float32)
    power = np.zeros(n, dtype=np.float64)
    for k in range(segments):
        block = iq[k * n : (k + 1) * n]
        if len(block) < n:
            break
        power += np.abs(np.fft.fft(block * window)) ** 2
    power = np.fft.fftshift(power / segments) / (n * window.sum() / n) ** 2
    db = 10 * np.log10(power.reshape(SPECTRUM_BINS, -1).mean(axis=1) + 1e-12)
    return {
        "t": round(now, 2),
        "center_hz": config.center_hz,
        "sample_rate": config.sample_rate,
        "gain_db": config.gain_db,
        "clipped": round(clipped, 5),
        "db": [round(float(x), 1) for x in db],
    }


class SpectrumMonitor:
    """A listener with no station: keeps a dongle running so its spectrum can be watched while
    aiming the antenna or choosing a gain, before any station is started on it."""

    def __init__(self, device: RtlSdrDevice) -> None:
        self.device = device
        self.station_id = f"monitor:{device.config.id}"
        self.health = SourceHealth()

    def _feed(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        pass

    def _disconnected(self, reconnects: int) -> None:
        pass

    def close(self) -> None:
        self.device.detach(self)  # type: ignore[arg-type]


def ppm_args(ppm: int) -> list[str]:
    """The rtl tools' frequency correction (``rtl_sdr``, ``rtl_power``): none for 0, their
    default, so a dongle that needs no correction is started as it always was."""
    return ["-p", str(ppm)] if ppm else []


_devices: dict[str, RtlSdrDevice] = {}
_devices_lock = threading.Lock()


def find_device(serial: str) -> RtlSdrDevice | None:
    with _devices_lock:
        return _devices.get(serial)


def forget_device(serial: str) -> bool:
    """Drop an idle dongle's shared device, so the next station opens it with new settings.
    False if stations still use it."""
    with _devices_lock:
        device = _devices.get(serial)
        if device is None:
            return True
        if device._snapshot():
            return False
        del _devices[serial]
        return True


def list_dongles(command: str = "rtl_test", timeout: float = 3.0) -> list[dict[str, str]]:
    """RTL-SDR dongles on USB, from ``rtl_test``'s device list. It is asked for a dongle no
    serial can match, so it prints the list and opens none: a dongle in use is listed and left
    alone. [] if rtl_test isn't there."""
    if shutil.which(command) is None:
        return []
    try:
        done = subprocess.run([command, "-d", "no-such-dongle"], capture_output=True, text=True,
                              timeout=timeout)  # fmt: skip
        text = done.stderr + done.stdout
    except subprocess.TimeoutExpired as e:
        text = (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (
            e.stderr or "")  # fmt: skip
    return parse_dongles(text)


DONGLES_FOR_S = 5.0  # the page asks on every load of several screens: one listing serves them
_listed: tuple[object, float, list[dict[str, str]]] | None = None


def recent_dongles() -> list[dict[str, str]]:
    """``list_dongles`` for the page: the listing of the last few seconds, rather than a
    subprocess for each screen that shows receivers. A dongle plugged in shows after
    ``DONGLES_FOR_S`` at the latest."""
    global _listed
    listing = list_dongles  # as it is now: a test's stand-in is asked afresh
    now = time.monotonic()
    if _listed is None or _listed[0] is not listing or now - _listed[1] > DONGLES_FOR_S:
        _listed = (listing, now, listing())
    return [dict(d) for d in _listed[2]]


def reads_as_index(serial: str) -> bool:
    """Whether the rtl tools may read ``serial`` as a device index: they try -d as a number
    first (C strtol, base 0, so a leading 0 means octal) and use it when it is below the number
    of dongles plugged in. The factory serial 00000001 reads as index 1."""
    import re

    for pattern, base in ((r"0[0-7]*", 8), (r"0[xX][0-9a-fA-F]+", 16), (r"[1-9][0-9]*", 10)):
        if re.fullmatch(pattern, serial) and int(serial, base) < 32:
            return True
    return False


def device_arg(
    serial: str, listing: Callable[[], list[dict[str, str]]] = lambda: list_dongles()
) -> str:
    """What to pass as -d for a dongle: its serial, or its index on USB when the serial could
    be read as another dongle's index. Resolved at every start, since indices change when
    dongles are plugged in or out."""
    if not reads_as_index(serial):
        return serial
    found = [d["index"] for d in listing() if d["serial"] == serial]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        event(log, "sdr_serial_shared", level=logging.WARNING, serial=serial, dongles=len(found),
              fix="give each dongle its own serial: rtl_eeprom -s 00001234")  # fmt: skip
    return serial  # alone on USB, the serial matches it; not listed, rtl_sdr reports it


def parse_dongles(text: str) -> list[dict[str, str]]:
    import re

    out = []
    for m in re.finditer(r"^\s*(\d+):\s+(.+?),\s+(.+?),\s+SN:\s*(\S+)\s*$", text, re.M):
        out.append({"index": m.group(1), "vendor": m.group(2), "product": m.group(3),
                    "serial": m.group(4)})  # fmt: skip
    return out


def shared_device(config: SdrDevice, **kwargs: object) -> RtlSdrDevice:
    """The process-wide device for this dongle's serial: stations on one dongle share it."""
    with _devices_lock:
        device = _devices.get(config.serial)
        if device is None:
            device = _devices[config.serial] = RtlSdrDevice(config, **kwargs)  # type: ignore[arg-type]
        elif device.config != config:
            now, asked = _setup(device.config), _setup(config)
            differ = [k for k in now if now[k] != asked[k]] or list(now)
            raise ValueError(
                f"Receiver {receiver_name(config.id)} is already in use at "
                f"{', '.join(now[k] for k in differ)}: a station can't retune it to "
                f"{', '.join(asked[k] for k in differ)} while it is."
            )
        return device


def _setup(c: SdrDevice) -> dict[str, str]:
    """A dongle's settings in words, for a message (its serial is in none)."""
    return {"centre": f"{c.center_hz / 1e6:g} MHz", "gain": f"{c.gain_db:g} dB gain",
            "rate": f"{c.sample_rate / 1e6:g} MS/s", "ppm": f"{c.ppm} ppm"}  # fmt: skip


class SdrChannelSource:
    """One station on an SDR: an ``AudioSource`` fed by its device's IQ."""

    def __init__(
        self,
        station_id: str,
        tuning: SdrTuning,
        *,
        device: RtlSdrDevice | None = None,
        queue_chunks: int = 64,
    ) -> None:
        self.station_id = station_id
        self.tuning = tuning
        self._device = device or shared_device(tuning.device)
        self._rds: RdsDecoder | None = None
        self._new_demod()
        self._q: queue.Queue[AudioChunk | None] = queue.Queue(maxsize=queue_chunks)
        self._closed = threading.Event()
        self._discontinuity = True
        self._error: Exception | None = None  # what demodulating raised, on the device thread
        self._health = SourceHealth()
        self.dropped = 0  # chunks dropped because nobody read them in time

    def _new_demod(self) -> None:
        t = self.tuning
        offset = t.freq_hz - t.device.center_hz
        self._demod: WfmDemodulator | NarrowbandDemodulator
        if t.mode == "wfm":
            self._demod = WfmDemodulator(t.device.sample_rate, offset, t.deemphasis_us)
            # RDS keeps decoding across gaps: the station's name doesn't change with a dropout.
            if getattr(self, "_rds", None) is None:
                self._rds = RdsDecoder()
            self._demod.on_mpx = self._rds
        else:
            self._rds = None
            self._demod = NarrowbandDemodulator(
                t.device.sample_rate, offset, t.mode, t.channel_hz, t.squelch_db
            )

    @property
    def health(self) -> SourceHealth:
        return self._health

    def __iter__(self) -> Iterator[AudioChunk]:
        if not self._device.available:
            raise RuntimeError("'rtl_sdr' not found; install it with: brew install librtlsdr")
        if self._error is not None:  # iterated again after a failure: start afresh
            self._error = None
            self._rds = None  # kept across gaps otherwise, but it may be what raised
            self._new_demod()
            self._discontinuity = True
            with contextlib.suppress(queue.Empty):
                while True:  # the end marker and the audio from before the failure
                    self._q.get_nowait()
        self._device.attach(self)
        try:
            while not self._closed.is_set() and self._error is None:
                try:
                    chunk = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if chunk is None:
                    break
                yield chunk
            if self._error is not None and not self._closed.is_set():
                raise RuntimeError(f"demodulating failed: {self._error}") from self._error
        finally:
            self._device.detach(self)
            self._health.connected = False

    def close(self) -> None:
        self._closed.set()
        # If the queue is full, the iterator still sees _closed within half a second.
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(None)

    # Called on the device thread.

    def _feed(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        if self._closed.is_set() or self._error is not None:
            return
        if first:
            self._new_demod()  # filter state from before a gap would smear into new audio
            self._discontinuity = True
        try:
            audio = self._demod(iq)  # the RDS decoder runs in here too
        except Exception as e:
            # The iterator raises it in the station's own thread, so the station is restarted
            # like any failed source while the device and its other stations go on.
            self._error = e
            with contextlib.suppress(queue.Full):  # when full, the iterator sees _error itself
                self._q.put_nowait(None)
            return
        if len(audio) == 0:
            return
        h = self._health
        h.connected = True
        h.last_data_at = t_end
        h.samples_total += len(audio)
        h.signal_db = self._demod.power_db
        h.clipped = self._device.clipped
        if self._rds is not None and self._rds.info.groups:
            h.rds = self._rds.info.to_dict()
        if isinstance(self._demod, NarrowbandDemodulator):
            h.squelch_open = self._demod.open
        chunk = AudioChunk(samples=audio, t_end=t_end, discontinuity=self._discontinuity)
        self._discontinuity = False
        try:
            self._q.put_nowait(chunk)
        except queue.Full:
            # The pipeline fell behind: drop the oldest audio, keep the newest.
            with contextlib.suppress(queue.Empty):
                self._q.get_nowait()
            self.dropped += 1
            with contextlib.suppress(queue.Full):  # close() may have just taken the slot
                self._q.put_nowait(chunk)

    def _disconnected(self, reconnects: int) -> None:
        self._health.connected = False
        self._health.reconnects = reconnects
        self._discontinuity = True


def claimed(text: str) -> bool:
    """Whether an rtl tool's stderr says another program has the dongle open."""
    t = text.lower()
    return "usb_claim_interface" in t or "claimed by second instance" in t


def _drain_stderr(
    stream: IO[bytes], config: SdrDevice, said: deque[str], trouble: int = logging.WARNING
) -> None:
    # Keeps rtl_sdr from blocking on a full stderr pipe, and puts its messages in our log (what
    # sounds like trouble at ``trouble``) and in ``said``, the account of a run that gave nothing.
    noisy = ("error", "failed", "no matching", "no supported", "lost", "not locked", "usb_")
    with stream:
        for line in stream:
            text = line.decode("utf-8", "replace").strip()
            if text:
                said.append(text)
                level = trouble if any(w in text.lower() for w in noisy) else logging.INFO
                event(log, "rtl_sdr", level=level, device=config.id, serial=config.serial,
                      stderr=text)  # fmt: skip
