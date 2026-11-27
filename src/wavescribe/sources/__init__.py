# SPDX-License-Identifier: Apache-2.0
"""Audio sources: anything that yields 16 kHz mono PCM chunks for one station."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from wavescribe.audio import AudioChunk

if TYPE_CHECKING:
    from wavescribe.config import Station


@dataclass
class SourceHealth:
    connected: bool = False
    reconnects: int = 0
    last_data_at: float | None = None
    samples_total: int = 0
    signal_db: float | None = None  # SDR only: the station's power in dBFS (0 = full scale)
    clipped: float | None = None  # SDR only: share of samples at the ADC's limits (overload)
    rds: dict[str, object] | None = None  # SDR FM only: what the station says about itself
    squelch_open: bool | None = None  # SDR NFM/AM only: someone is transmitting
    warning: str | None = None  # what keeps it from hearing right now (a watch made deaf)


class AudioSource(Protocol):
    station_id: str

    def __iter__(self) -> Iterator[AudioChunk]:
        """Yield chunks until closed. Reconnection is the source's own job; if this raises
        anyway, the pipeline iterates the same source again after a delay."""
        ...

    def close(self) -> None:
        """Stop iteration from any thread."""
        ...

    @property
    def health(self) -> SourceHealth: ...


def open_source(station: Station) -> AudioSource:
    """The station's default source: its SDR when one is configured, else its internet stream."""
    if station.sdr is not None and station.sdr.watch is not None:
        from wavescribe.radio.catalog import Catalog
        from wavescribe.sources.watch import WatchSource

        # Learnt with no antenna. A list an earlier version learnt (no bin_hz: about 87 kHz
        # bins) can't place a narrow line, so it isn't used: the noise floors learn them.
        catalog, serial = Catalog(), station.sdr.device.serial
        bin_hz = (catalog.spur_info().get(serial) or {}).get("bin_hz") or 0.0
        spurs = catalog.spurs(serial) if bin_hz else []
        return WatchSource(
            station.id, station.sdr, spurs=spurs, spur_bin_hz=bin_hz, privacy=station.privacy
        )
    if station.sdr is not None:
        from wavescribe.sources.sdr import SdrChannelSource

        return SdrChannelSource(station.id, station.sdr)
    url = station.stream or station.stream_hls
    if not url:
        raise ValueError(f"station {station.id!r} has neither an SDR device nor a stream URL")
    from wavescribe.sources.stream import StreamSource

    return StreamSource(station.id, url)
