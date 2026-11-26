# SPDX-License-Identifier: Apache-2.0
"""StreamSource: an internet radio stream decoded to PCM by an ffmpeg subprocess.

ffmpeg (GPL in the Homebrew build) only ever runs as a separate process connected by pipes.
We own reconnection, rather than ffmpeg's ``-reconnect`` options, so that every dropout is
visible in the logs and the health counters.
"""

from __future__ import annotations

import logging
import math
import os
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
from wavescribe.log import event
from wavescribe.sources import SourceHealth

# What ffmpeg may open for a web stream. A playlist may name other protocols (file:, concat:, a
# listening socket); every ffmpeg and ffprobe run on a URL from the web gets this list.
WEB_PROTOCOLS = "http,https,tcp,tls,crypto"

log = logging.getLogger(__name__)

_BYTES_PER_SAMPLE = 2  # s16le
# A connection counts as recovered once it has delivered this much audio. A server that sends
# a moment of audio and hangs up is still failing, and must keep backing off.
STEADY_AUDIO_S = 30.0

# A live stream leaves the source at the pace it was spoken. A segmented stream (HLS) arrives a
# whole segment of several seconds at a time, and both HLS and Icecast servers send 10 to 20 s at
# once on connect (measured on the world example's stations). Passed on as it came, a listener
# drops most of each burst, and seconds of speech share one time.
PACE_SLACK_S = 2.0  # audio kept in hand at its lowest, against a segment that comes late
PACE_TRIM_ABOVE_S = 6.0  # never less than this in hand for a whole window: let the excess go
PACE_WINDOW_S = 30.0  # spans a few gaps of a stream with 10 s segments
PACE_MAX_S = 60.0  # never more in hand than this (a file served over http arrives at once)
PACE_SETTLE_S = 3.0  # the burst on connect comes over a second or so: measure after it


class _Pacer:
    """Whole chunks in, released at real time. Audio that comes late moves the schedule on
    rather than coming out in a rush; audio held longer than needed (the burst on connect, a
    catch-up after a stall) is let go at once, so the delay doesn't stay. Nothing is dropped."""

    def __init__(self, chunk_s: float) -> None:
        self.chunk_s = chunk_s
        self.queue: deque[bytes] = deque()
        self.due: float | None = None  # when the next chunk leaves
        self.window_at = math.inf  # measuring the least audio in hand from then on
        self.low = math.inf  # the least audio in hand after a release, since window_at
        self.let_go_s = 0.0  # released early, all told
        self.late_s = 0.0  # schedule moved on for audio that came late, all told

    @property
    def held_s(self) -> float:
        return len(self.queue) * self.chunk_s

    def push(self, raw: bytes, now: float) -> None:
        if not self.queue and (self.due is None or now > self.due + self.chunk_s):
            if self.due is None:
                self.window_at = now + PACE_SETTLE_S
            else:
                self.late_s += now - self.due
            self.due = now  # the first audio, or audio that came late: carry on from now
        self.queue.append(raw)

    def wait(self, now: float) -> float | None:
        """Seconds until the next chunk is due; None with nothing in hand."""
        if not self.queue or self.due is None:
            return None
        return max(0.0, self.due - now)

    def pop(self, now: float) -> list[bytes]:
        """The chunks due by ``now``, plus any excess let go."""
        out = []
        while self.queue and self.due is not None and now >= self.due:
            out.append(self.queue.popleft())
            self.due += self.chunk_s
            if now >= self.window_at:
                self.low = min(self.low, self.held_s)
        n = max(0, math.ceil((self.held_s - PACE_MAX_S) / self.chunk_s - 1e-9))
        if now - self.window_at >= PACE_WINDOW_S:
            if math.isfinite(self.low) and self.low > PACE_TRIM_ABOVE_S:
                n = max(n, int((self.low - PACE_SLACK_S) / self.chunk_s))
            self.window_at, self.low = now, math.inf
        n = min(n, len(self.queue))
        for _ in range(n):
            out.append(self.queue.popleft())
        self.let_go_s += n * self.chunk_s
        return out


class StreamSource:
    def __init__(
        self,
        station_id: str,
        url: str,
        *,
        chunk_seconds: float = 0.512,
        stall_timeout: float = 20.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        max_reconnects: int | None = None,
        realtime: bool = False,
        ffmpeg: str = "ffmpeg",
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Args:
            chunk_seconds: size of each yielded chunk.
            stall_timeout: kill and reconnect if no audio arrives for this long.
            max_reconnects: give up after this many reconnects. By default a web stream is
                reconnected for ever; anything else (a file given to ``listen --source``) is
                read once, because its end is the end and not a dropped connection.
            realtime: read input at its native rate (``-re``), to replay a file as if live.
        """
        self.station_id = station_id
        self.url = url
        self._chunk_bytes = int(chunk_seconds * SAMPLE_RATE) * _BYTES_PER_SAMPLE
        self._stall_timeout = stall_timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._web = url.startswith(("http://", "https://"))
        self._max_reconnects = max_reconnects if max_reconnects is not None or self._web else 0
        self._realtime = realtime
        self._ffmpeg = ffmpeg
        self._clock = clock
        self._closed = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._health = SourceHealth()

    @property
    def health(self) -> SourceHealth:
        return self._health

    def close(self) -> None:
        self._closed.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.kill()

    def __iter__(self) -> Iterator[AudioChunk]:
        if shutil.which(self._ffmpeg) is None:
            raise RuntimeError(f"{self._ffmpeg!r} not found; install ffmpeg (brew install ffmpeg)")
        failures = 0  # consecutive connections that ended without a steady run of audio
        while not self._closed.is_set():
            heard = 0  # samples from this connection
            for chunk in self._run_once():
                heard += len(chunk.samples)
                if heard >= STEADY_AUDIO_S * SAMPLE_RATE:
                    failures = 0
                yield chunk
            self._health.connected = False
            if self._closed.is_set():
                break
            if self._max_reconnects is not None and self._health.reconnects >= self._max_reconnects:
                break
            delay = self._delay(failures)
            failures += 1
            self._health.reconnects += 1
            event(
                log,
                "source_reconnect",
                level=logging.WARNING,
                station=self.station_id,
                delay_s=delay,
                reconnects=self._health.reconnects,
            )
            if delay and self._closed.wait(delay):
                break

    def _delay(self, failures: int) -> float:
        """Seconds before the next try. The exponent is capped: a station down for a day makes
        thousands of tries, and 2.0 ** 1024 overflows."""
        return min(self._backoff_max, self._backoff_initial * 2 ** min(failures, 16))

    def _command(self) -> list[str]:
        cmd = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if self._web:
            cmd += ["-rw_timeout", str(int(self._stall_timeout * 1_000_000))]
            # A playlist may name other protocols (file:, concat:, a listening socket): only the
            # web ones. A local file given to `listen --source` is the user's own choice.
            cmd += ["-protocol_whitelist", WEB_PROTOCOLS]
        if self._realtime:
            cmd += ["-re"]
        cmd += ["-i", self.url, "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE)]
        cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
        return cmd

    def _run_once(self) -> Iterator[AudioChunk]:
        """One ffmpeg process lifetime: spawn, read until EOF/stall/close, then reap."""
        proc = subprocess.Popen(
            self._command(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._proc = proc
        assert proc.stdout is not None and proc.stderr is not None
        threading.Thread(
            target=_drain_stderr,
            args=(proc.stderr, self.station_id),
            daemon=True,
            name=f"ffmpeg-stderr-{self.station_id}",
        ).start()
        event(log, "source_connect", station=self.station_id, url=self.url, pid=proc.pid)
        fd = proc.stdout.fileno()
        buf = bytearray()
        first = True
        reason = "eof"
        # A web stream is live, so it is paced; a file goes as fast as ffmpeg decodes it (or at
        # its own rate with `realtime`, which ffmpeg paces).
        pacer = _Pacer(self._chunk_bytes / _BYTES_PER_SAMPLE / SAMPLE_RATE) if self._web else None
        heard_at = time.monotonic()  # the last data from ffmpeg
        let_go = 0.0
        try:
            while not self._closed.is_set():
                if pacer is not None:
                    for raw in pacer.pop(time.monotonic()):
                        yield self._make_chunk(raw, first)
                        first = False
                    if pacer.let_go_s > let_go:
                        event(log, "source_pace", station=self.station_id,
                              let_go_s=round(pacer.let_go_s - let_go, 1),
                              held_s=round(pacer.held_s, 1),
                              late_s=round(pacer.late_s, 1))  # fmt: skip
                        let_go = pacer.let_go_s
                now = time.monotonic()
                timeout = self._stall_timeout - (now - heard_at)
                if timeout <= 0:
                    reason = "stall"
                    break
                due = pacer.wait(now) if pacer is not None else None
                if due is not None:
                    timeout = min(timeout, due)
                ready, _, _ = select.select([fd], [], [], timeout)
                if not ready:
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                heard_at = time.monotonic()
                buf += data
                while len(buf) >= self._chunk_bytes:
                    raw = bytes(buf[: self._chunk_bytes])
                    del buf[: self._chunk_bytes]
                    if pacer is not None:
                        pacer.push(raw, heard_at)
                    else:
                        yield self._make_chunk(raw, first)
                        first = False
            else:
                reason = "closed"
            if not self._closed.is_set():
                # The connection is over: what is in hand goes now, and the next connection
                # keeps its own time.
                for raw in pacer.queue if pacer is not None else ():
                    yield self._make_chunk(raw, first)
                    first = False
                tail = len(buf) - len(buf) % _BYTES_PER_SAMPLE
                if tail:
                    yield self._make_chunk(bytes(buf[:tail]), first)
        finally:
            if self._closed.is_set():
                reason = "closed"
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
            self._proc = None
            event(
                log,
                "source_disconnect",
                station=self.station_id,
                reason=reason,
                returncode=proc.returncode,
            )

    def _make_chunk(self, raw: bytes, first: bool) -> AudioChunk:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        now = self._clock()
        h = self._health
        h.connected = True
        h.last_data_at = now
        h.samples_total += len(samples)
        return AudioChunk(samples=samples, t_end=now, discontinuity=first)


def _drain_stderr(stream: IO[bytes], station_id: str) -> None:
    # Keeps ffmpeg from blocking on a full stderr pipe, and surfaces its errors in our log.
    with stream:
        for line in stream:
            text = line.decode("utf-8", "replace").strip()
            if text:
                event(log, "ffmpeg", level=logging.WARNING, station=station_id, stderr=text)
