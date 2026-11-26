# SPDX-License-Identifier: Apache-2.0
"""Live audio for browsers: a station's decoded PCM, fanned out to whoever is listening.

The page fetches raw 16-bit 16 kHz mono PCM and plays it with the Web Audio API. That avoids the
browsers' media players, which handle endless streams differently (WebKit is the fussy one), and
needs no encoder: 32 KB/s per listener is nothing on localhost or a LAN. You hear exactly the
audio that is being transcribed, and it works the same for SDR sources.
"""

from __future__ import annotations

import contextlib
import queue
import threading

import numpy as np

_BACKLOG = 40  # chunks (~20 s) per listener before we drop


class AudioTap:
    def __init__(self, station_id: str) -> None:
        self.station_id = station_id
        self._lock = threading.Lock()
        self._listeners: set[queue.Queue[bytes | None]] = set()

    @property
    def listeners(self) -> int:
        return len(self._listeners)

    def add_listener(self) -> queue.Queue[bytes | None]:
        q: queue.Queue[bytes | None] = queue.Queue(maxsize=_BACKLOG)
        with self._lock:
            self._listeners.add(q)
        return q

    def remove_listener(self, q: queue.Queue[bytes | None]) -> None:
        with self._lock:
            self._listeners.discard(q)

    def feed(self, samples: np.ndarray) -> None:
        """Called from the source thread with every chunk; free when nobody listens."""
        if not self._listeners:
            return
        data = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        with self._lock:
            listeners = list(self._listeners)
        for q in listeners:
            with contextlib.suppress(queue.Full):
                q.put_nowait(data)

    def close(self) -> None:
        with self._lock:
            for q in self._listeners:
                with contextlib.suppress(queue.Full):
                    q.put_nowait(None)
            self._listeners.clear()
