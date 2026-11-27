# SPDX-License-Identifier: Apache-2.0
from dataclasses import replace

import pytest

from tests.test_pipeline import station
from wavescribe.radio.bandplan import (
    POLICIES,
    REGIONS,
    bands,
    exportable,
    lookup,
    mhz_text,
    privacy_of,
    quotable,
    region_bands,
    region_for_country,
)


def test_every_band_is_well_formed() -> None:
    ids = [b.id for b in bands()]
    assert len(ids) == len(set(ids))
    for b in bands():
        assert b.start_hz < b.stop_hz and b.regions, b.id
        assert (b.mode is None) == (b.privacy == "digital"), b.id
        assert all(b.contains(c) for c in b.channels), b.id
    for r in REGIONS:
        assert region_bands(r)


def test_the_narrowest_band_names_the_channel() -> None:
    pmr = lookup(446_006_250, "cept")
    assert pmr is not None and pmr.id == "pmr446" and pmr.privacy == "private"
    assert pmr.label(446_006_250) == "PMR446 walkie-talkies, channel 1"
    assert pmr.snap(446_032_000) == 446_031_250  # channel 3
    assert lookup(98_600_000, "cept").id == "fm"
    assert lookup(98_600_000, "2").id == "fm-americas"
    assert lookup(145_500_000, "cept").privacy == "amateur"


def test_airband_snaps_to_the_833_khz_raster() -> None:
    air = lookup(121_503_000, "cept")
    assert air.id == "airband" and air.mode == "am"
    assert air.snap(121_503_000) == 121_500_000
    assert air.snap(118_008_000) == 118_008_333
    us = lookup(121_510_000, "2")  # the Americas keep 25 kHz channels
    assert us.id == "airband-25" and us.snap(121_510_000) == 121_500_000


def test_a_listed_channel_is_snapped_to_only_when_it_is_near() -> None:
    frs = lookup(464_500_000, "2")  # business radio, between the two FRS groups
    assert frs.id == "frs" and frs.snap(464_500_000) == 464_500_000
    assert frs.snap(462_566_000) == 462_562_500  # FRS channel 1
    pmr = lookup(446_007_000, "cept")
    assert pmr.snap(446_007_000) == 446_006_250


def test_amateur_fm_in_the_americas_keeps_its_own_raster() -> None:
    two_m = lookup(146_520_000, "2")
    assert two_m.snap(146_520_000) == 146_520_000  # the calling frequency, not 146.525
    assert two_m.snap(146_940_000) == 146_940_000 and two_m.channel_hz == 25_000
    assert lookup(145_510_000, "cept").snap(145_510_000) == 145_512_500  # 12.5 kHz in Europe


def test_cb_is_fm_in_the_cept_countries_and_am_elsewhere(monkeypatch) -> None:
    from wavescribe.config import default_mode
    from wavescribe.radio import bandplan

    channel_19 = 27_185_000
    assert lookup(channel_19, "cept").mode == "nfm"
    assert [lookup(channel_19, r).mode for r in ("1", "2", "3")] == ["am", "am", "am"]
    assert next(b for b in region_bands("cept") if b.id == "cb").to_dict()["mode"] == "nfm"
    assert default_mode(channel_19, "cept") == "nfm" and default_mode(channel_19, "2") == "am"
    monkeypatch.setattr(bandplan, "default_region", lambda: "cept")
    assert lookup(channel_19, bandplan.AUTO).mode == "nfm"  # the locale's way
    assert bandplan.find("cb").mode == "am"  # the plan's own entry is unchanged


def test_the_gap_in_marine_vhf_is_land_mobile_radio(monkeypatch) -> None:
    from wavescribe.radio import bandplan
    from wavescribe.radio.scan import band_range, scanned_band

    for region in ("cept", "1", "2", "3"):
        assert lookup(158_000_000, region).id == "land-mobile-vhf"
        assert privacy_of(158_000_000, region) == "private"
        assert lookup(156_800_000, region).privacy == "utility"  # channel 16
        assert lookup(157_425_000, region).id == "marine"  # channel 88, the ships' last
        assert lookup(160_625_000, region).id == "marine"  # channel 60, the coast's first
    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert privacy_of(159_000_000, bandplan.AUTO) == "private"
    assert scanned_band(*band_range("marine", "cept"), "cept").id == "marine"


def test_frequencies_are_written_to_the_hertz() -> None:
    assert mhz_text(446.00625) == "446.00625" and mhz_text(98.6) == "98.6"
    assert mhz_text(462_562_500 / 1e6) == "462.5625" and mhz_text(100.0) == "100"


def test_a_band_any_region_marks_digital_is_digital_everywhere() -> None:
    assert lookup(390_000_000, "cept").privacy == "digital"
    assert lookup(390_000_000, "2").privacy == "digital"  # listed for Europe; refused anywhere
    assert lookup(1_090_000_000, "jp").privacy == "digital"


def test_a_channel_takes_the_strictest_class_its_edges_reach(monkeypatch) -> None:
    from wavescribe.config import radio_policy
    from wavescribe.radio import bandplan

    # A 25 kHz channel at 379.99 MHz reaches the digital services from 380 MHz.
    for region in ("cept", "2", bandplan.AUTO):
        assert privacy_of(379_990_000, region) == "digital", region
    assert privacy_of(379_950_000, "cept") == "private"  # 12.5 kHz short of it
    assert privacy_of(379_998_000, "cept", width_hz=2_000) == "private"  # a narrow one isn't
    # A marine channel's centre with its edge in the land mobile gap: marine's raster and
    # name, the gap's class.
    edge = lookup(160_610_000, "cept")
    assert (edge.id, edge.privacy, edge.mode) == ("marine", "private", "nfm")
    assert lookup(160_625_000, "cept").privacy == "utility"  # channel 60 only touches it
    # Spectrum no plan lists doesn't count: 88.1 MHz is the Americas' first FM channel.
    assert lookup(88_100_000, "2").privacy == "broadcast"
    assert lookup(118_000_000, "cept").privacy == "utility"
    # Every caller gets it, the stations file included.
    assert radio_policy(379_990_000, "nfm", "cept", True, "here") == (
        "digital",
        "digital-emergency",
    )
    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert lookup(379_990_000, bandplan.AUTO).id == "digital-emergency"


def test_unlisted_frequencies_count_as_private() -> None:
    assert privacy_of(300_000_000, "cept") == "private"
    with pytest.raises(ValueError):
        region_bands("mars")


@pytest.mark.parametrize("country,region", [("PT", "cept"), ("de", "cept"), ("US", "2"),
                                            ("BR", "2"), ("JP", "jp"), ("AU", "3"), ("ZA", "1"),
                                            ("MN", "1"), (None, "1")])  # fmt: skip
def test_region_from_the_country(country: str | None, region: str) -> None:
    assert region_for_country(country) == region


def test_what_each_class_may_do() -> None:
    broadcast, ham = station("a"), replace(station("b"), privacy="amateur")
    private = replace(station("c"), privacy="private")
    assert exportable(broadcast) and quotable(broadcast)
    assert not exportable(ham) and quotable(ham)
    assert not exportable(private) and not quotable(private)
    assert POLICIES["private"].text_days == 0 and not POLICIES["private"].audio
    assert POLICIES["utility"].needs_ack and not POLICIES["amateur"].needs_ack
    assert not exportable(replace(station("d"), privacy="made-up"))  # unknown: as private


def test_out_of_range_frequencies_say_which_way() -> None:
    from wavescribe.dsp import check_tunable

    with pytest.raises(ValueError, match="medium wave and shortwave aren't supported yet"):
        check_tunable(1_000_000)
    with pytest.raises(ValueError, match="1766 MHz, no higher") as above:
        check_tunable(2_400_000_000)
    assert "medium wave" not in str(above.value)


@pytest.mark.parametrize(
    ("given", "said"),
    [
        ("1e303", "the receiver tunes 24 to 1766 MHz, no higher"),  # was an OverflowError
        ("9999", "the receiver tunes 24 to 1766 MHz, no higher"),
        ("5", "the receiver tunes 24 to 1766 MHz; medium wave and shortwave aren't supported yet"),
        ("99999", "99999 looks like kHz; frequencies are in MHz: 99.999"),
        ("x", "'x' isn't one: give a frequency in MHz, e.g. 99.5 or 446.00625"),
    ],
)
def test_a_frequency_no_receiver_tunes_is_refused_in_words(given: str, said: str) -> None:
    from wavescribe.dsp import tunable_hz

    with pytest.raises(ValueError) as e:
        tunable_hz(given)
    assert str(e.value) == said
    assert tunable_hz("446.00625") == 446_006_250


def test_radio_bands_marks_what_the_receiver_cannot_reach(capsys) -> None:
    from wavescribe.cli import main

    assert main(["radio", "bands", "--region", "1"]) == 0
    rows = {r.split()[0]: r for r in capsys.readouterr().out.splitlines() if r[:1] != "#"}
    assert rows["mw"].endswith("(outside the receiver's range)")
    assert "outside" not in rows["fm"]
