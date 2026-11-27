# SPDX-License-Identifier: Apache-2.0
"""The privacy class of everything a watch's window holds, between its edges and not only on
its channels: what a detector that hears the whole window is kept under."""

from __future__ import annotations

import pytest

from wavescribe.dsp.activity import THRESHOLD_DB
from wavescribe.radio import bandplan
from wavescribe.radio import watch as watches
from wavescribe.radio.bandplan import (
    AUTO,
    EDGE_HZ,
    REGIONS,
    privacy_of,
    region_bands,
    strictest_over,
)
from wavescribe.radio.watch import plan_window

WATCHED = [
    (region, band.id) for region in REGIONS for band in region_bands(region) if band.mode != "wfm"
]


@pytest.mark.parametrize(("region", "band_id"), WATCHED)
def test_a_band_watched_whole_keeps_the_class_of_its_strictest_channel(
    region: str, band_id: str
) -> None:
    watch, _, width = watches._plan(band_id, region, None, None, THRESHOLD_DB)
    today = privacy_of(watch.strictest_in(region, width), region, width)
    assert strictest_over(watch.start_hz, watch.stop_hz, region)[0] == today
    hz, reach = watch.strictest_anywhere(region)
    assert watch.start_hz <= hz <= watch.stop_hz and privacy_of(hz, region, reach) == today


@pytest.mark.parametrize("region", ["cept", "2"])
def test_a_range_a_signal_inside_can_reach_a_digital_band_from_is_digital(region: str) -> None:
    """Digital services start at 380 MHz, in any region: a signal at 379.99 MHz reaches them,
    though a 12.5 kHz channel there stays clear."""
    assert privacy_of(379_987_500, region, 12_500) == "private"
    cls, hz = strictest_over(379_900_000, 379_990_000, region)
    assert (cls, hz) == ("digital", 379_990_000)
    assert privacy_of(hz, region, 2 * EDGE_HZ) == "digital"
    assert strictest_over(379_900_000, 379_960_000, region)[0] == "private"


def test_a_range_over_spectrum_no_plan_lists_is_private_and_its_edges_are_not() -> None:
    # 2 m ends at 146 MHz in Region 1; nothing is listed above it until marine VHF.
    assert strictest_over(145_500_000, 146_000_000, "1")[0] == "amateur"
    cls, hz = strictest_over(145_500_000, 146_500_000, "1")
    assert cls == "private" and 146_000_000 < hz <= 146_500_000
    assert strictest_over(150_000_000, 151_000_000, "1")[0] == "private"
    # A band past an edge counts where a signal inside reaches it; empty spectrum there doesn't.
    assert strictest_over(145_990_000, 145_995_000, "1")[0] == "amateur"
    # Marine VHF, with business radio from 10 kHz past the range's edge.
    assert strictest_over(156_100_000, 157_440_000, "1")[0] == "private"


def test_with_no_region_chosen_a_range_takes_the_strictest_class_any_region_gives(
    monkeypatch,
) -> None:
    """A Mac set to US English, where 446 MHz is amateur radio, may be in Europe."""
    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert strictest_over(446_000_000, 446_200_000, "2")[0] == "amateur"
    assert strictest_over(446_000_000, 446_200_000, AUTO)[0] == "private"
    assert strictest_over(145_000_000, 145_500_000, AUTO)[0] == "amateur"


def test_a_watch_keeps_the_edges_it_was_asked_for() -> None:
    band, _, _ = plan_window("pmr446", "cept", rate=2_400_000)
    assert (band.start_hz, band.stop_hz) == (446_000_000, 446_200_000)
    assert band.channels[0] == 446_006_250  # its channels sit inside its edges
    span, _, _ = plan_window((446.0, 446.05), "cept", rate=2_400_000)
    assert (span.start_hz, span.stop_hz) == (446_000_000, 446_050_000)
    assert span.strictest_anywhere("cept") == (446_025_000, 2 * EDGE_HZ)
