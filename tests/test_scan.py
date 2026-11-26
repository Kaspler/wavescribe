# SPDX-License-Identifier: Apache-2.0
import os
import stat
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from tests.fake_rtl_power import layout as power_layout
from tests.fake_rtl_power import rows as power_rows
from wavescribe.radio import scan as S

FAKE = Path(__file__).with_name("fake_rtl_power.py")
AIRBAND = (117_995_834, 137_004_166)  # nine hops of 2.112 MHz at 2.64 MHz, 644.5 Hz bins
COARSE = (250_000_000, 290_000_000)  # eighteen hops at 2.78 MHz, 86.8 kHz bins, no band


def power_sweep(lo: int, hi: int, step: int, sigs: tuple = (), **kw: object) -> S.Sweep:
    """What scan.py makes of rtl_power's CSV for a range, with these signals on the air."""
    return S.parse_csv("\n".join(power_rows(lo, hi, step, list(sigs), **kw)), lo, hi)


@pytest.fixture
def fake_power(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    exe = tmp_path / "rtl_power"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def spectrum(start: float, stop: float, step: float, sigs: list[tuple[float, float, float]],
             seed: int = 0) -> S.Sweep:  # fmt: skip
    f = np.arange(start, stop, step)
    db = -40 + np.random.default_rng(seed).normal(0, 0.5, len(f))
    for c, w, level in sigs:
        db = np.maximum(db, -40 + level * np.exp(-0.5 * ((f - c) / max(w / 2.355, step / 2)) ** 2))
    return S.Sweep(int(start), int(stop), step, f, db, 1.0)


def test_fm_stations_are_found_snapped_and_classified() -> None:
    sw = spectrum(87.4e6, 108.1e6, 10_000, [(89.8e6, 150e3, 25), (94.31e6, 150e3, 12),
                                            (94.8e6, 150e3, 20)])  # fmt: skip
    found = S.find_signals(sw, "cept")
    assert [s.freq_hz for s in found] == [89_800_000, 94_300_000, 94_800_000]
    assert all(s.band == "fm" and s.mode == "wfm" and s.privacy == "broadcast" for s in found)
    assert found[0].snr_db > 20 and 80e3 <= found[0].width_hz < 250e3


def test_a_walkie_talkie_channel_is_named_and_private() -> None:
    sw = spectrum(445.99e6, 446.21e6, 1_000, [(446.0315e6, 8e3, 20)])
    [s] = S.find_signals(sw, "cept")
    assert s.freq_hz == 446_031_250 and s.label == "PMR446 walkie-talkies, channel 3"
    assert s.mode == "nfm" and s.privacy == "private"


def test_digital_signals_are_found_but_not_listenable() -> None:
    sw = spectrum(385e6, 395e6, 25_000, [(390.2e6, 25e3, 15)])
    [s] = S.find_signals(sw, "cept")
    assert s.privacy == "digital" and s.mode is None


def test_receiver_spurs_are_flagged() -> None:
    harmonic = spectrum(114e6, 116e6, 2_500, [(115.2e6, 2e3, 20)])  # 4 x 28.8 MHz, a thin line
    assert S.find_signals(harmonic, "cept")[0].spur
    sw = spectrum(150e6, 152e6, 2_500, [(150.5e6, 3e3, 20), (151.5e6, 3e3, 20)])
    found = S.find_signals(sw, "cept", spurs=[150_501_000])
    assert [s.spur for s in found] == [True, False]


def test_the_lines_of_a_receivers_2_4_mhz_clock_are_marked_as_its_own() -> None:
    # A clock 12 ppm fast: its odd harmonics across the airband, one bin wide, beside an
    # aircraft and a plain carrier that aren't on it.
    clock = 2.4e6 * (1 + 12e-6)
    teeth = [(n * clock, 300, 18) for n in (51, 53, 55, 57)]
    sw = power_sweep(*AIRBAND, 1_000, (*teeth, (124.0e6, 6_000, 15), (125.0e6, 300, 15)))
    found = {round(s.freq_hz, -3): s for s in S.find_signals(sw, "cept")}
    teeth_hz = [122_400_000, 127_200_000, 132_000_000, 136_800_000]
    assert sorted(found) == sorted([*teeth_hz, 124_000_000, 125_000_000])
    assert sorted(f for f, s in found.items() if s.spur) == teeth_hz
    assert abs(found[122_400_000].clock_hz - clock) < 20 and found[125_000_000].clock_hz is None
    # A narrow range holds one harmonic: on its own it counts when within 30 ppm.
    one = power_sweep(121_000_000, 126_000_000, 2_500, ((51 * clock, 300, 15),))
    assert [(round(s.freq_hz, -3), s.spur) for s in S.find_signals(one, "cept")] == [
        (122_400_000, True)
    ]
    off = power_sweep(121_000_000, 126_000_000, 2_500, ((51 * 2.4e6 * (1 + 60e-6), 300, 15),))
    assert [s.spur for s in S.find_signals(off, "cept")] == [False]


def test_a_clock_line_marked_in_any_sweep_of_a_watch_stays_marked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 60 ppm off: one harmonic alone doesn't count, two agreeing do.
    a, b = (n * 2.4e6 * (1 + 60e-6) for n in (51, 52))
    calls = iter([[(a, 300, 15)], [(a, 300, 15), (b, 300, 15)]])
    monkeypatch.setattr(S, "sweep", lambda *_, **__: spectrum(121e6, 126e6, 2_500, next(calls)))
    clock = iter([0.0, 10.0, 20.0, 30.0])
    monkeypatch.setattr(
        S, "time", types.SimpleNamespace(monotonic=lambda: next(clock), time=time.time)
    )
    _, found = S.scan("000001", 121e6, 126e6, "cept", rds=False, watch_s=15)
    assert [(s.seen, s.spur) for s in found] == [(2, True), (1, True)]


def test_hops_are_counted_as_rtl_power_cuts_them() -> None:
    ranges = [AIRBAND, COARSE, (121_000_000, 126_000_000), (446_000_000, 446_200_000)]
    assert [S.hop_count(lo, hi) for lo, hi in ranges] == [9, 18, 3, 1]
    assert [power_layout(lo, hi, 1_000, 0.2)[0] for lo, hi in ranges] == [9, 18, 3, 1]
    assert S.hop_count(*S.SWEEP_LIMITS_HZ) == 778


def test_the_dc_at_each_hop_centre_is_bridged_where_rtl_power_tunes() -> None:
    fine = power_sweep(*AIRBAND, 1_000)
    assert len(fine.hop_starts) == 8 and np.all(np.diff(fine.freqs) > 0)
    assert S.find_signals(fine, "cept") == []
    assert S.find_signals(power_sweep(*COARSE, 100_000), "cept") == []


def test_a_carrier_beside_a_hop_centre_is_found_at_its_frequency() -> None:
    # The second airband hop is tuned to 121.163888 MHz.
    [s] = S.find_signals(power_sweep(*AIRBAND, 1_000, ((121.175e6, 3_000, 15),)), "cept")
    assert s.freq_hz == 121_175_000
    # At coarse resolution only the centre bin itself is bridged, so a carrier one bin below
    # a hop's centre (the tenth hop's, 271.111109 MHz) stays, where it is.
    below = 271_111_109 - 2_777_777 / 32
    [s] = S.find_signals(power_sweep(*COARSE, 100_000, ((below, 500, 15),)), "cept")
    assert abs(s.freq_hz - below) <= 1


def test_a_carrier_one_bin_wide_is_found() -> None:
    [s] = S.find_signals(power_sweep(*AIRBAND, 1_000, ((125.0e6, 300, 15),)), "cept")
    assert s.freq_hz == 125_000_000 and s.snr_db >= 8


def test_a_signal_is_listed_with_the_margin_it_was_found_by() -> None:
    # A weak voice channel whose strongest single bin lies two bins above its centre.
    b = 2_515_625 / 1024
    sigs = ((145.625e6, 6_000, 9.8), (145.625e6 + 2 * b, 300, 11))
    [s] = S.find_signals(power_sweep(143_993_750, 146_006_250, 2_500, sigs), "cept")
    assert s.freq_hz == 145_625_000 and s.snr_db >= 8


def test_a_thin_carrier_in_the_fm_broadcast_band_is_measured_thin() -> None:
    # FM stations are given 200 kHz channels; a carrier a few hundred hertz wide among them,
    # and two lines of a 2.4 MHz clock, each 15 dB up and a bin wide.
    clock = 2.4e6 * (1 + 12e-6)
    lines = [(n * clock, 300, 15) for n in (38, 43)]
    sigs = [(95.0e6, 150_000, 30), (100.05e6, 300, 15), *lines]
    found = S.find_signals(power_sweep(87_500_000, 108_000_000, 5_000, sigs), "cept")
    tooth, station, carrier, tooth2 = found
    assert round(station.freq_hz, -5) == 95_000_000 and station.width_hz > 50_000
    assert round(carrier.freq_hz, -5) == 100_000_000 and carrier.width_hz <= 25_000
    assert carrier.snr_db >= 8 and not station.spur and not carrier.spur
    assert [round(t.freq_hz, -5) for t in (tooth, tooth2) if t.clock_hz] == [
        91_200_000,
        103_200_000,
    ]


def test_a_hop_that_reads_louder_than_its_neighbours_is_not_a_row_of_signals() -> None:
    # The fifth hop (centred on 260 MHz) 15 dB up, as when the tuner didn't settle on it, with
    # a carrier in it; then the V4's switch to its HF input, which leaves the hops below louder.
    carrier = 259_999_999 + 6 * 2_777_777 / 32
    loud = power_sweep(*COARSE, 100_000, ((carrier, 500, 30),), floors={4: -25.0})
    [s] = S.find_signals(loud, "cept")
    assert abs(s.freq_hz - carrier) <= 1
    step = power_sweep(*AIRBAND, 1_000, floors={0: -28.0, 1: -28.0, 2: -28.0})
    assert S.find_signals(step, "cept") == []


def test_the_repeated_last_bin_of_a_hop_is_not_a_second_signal() -> None:
    # rtl_power writes each hop's last bin twice; here the fourth hop's holds a carrier.
    last = 257_777_777 + 12 * 2_777_777 / 32
    [s] = S.find_signals(power_sweep(*COARSE, 100_000, ((last, 500, 15),)), "cept")
    assert abs(s.freq_hz - last) <= 1


def test_clusters_fit_a_dongle_and_keep_off_dc() -> None:
    groups = S.clusters([88_000_000, 88_500_000, 89_900_000, 95_000_000])
    assert [m for _, m in groups] == [[88_000_000, 88_500_000, 89_900_000], [95_000_000]]
    for centre, members in groups:
        assert all(40_000 <= abs(f - centre) <= 1_200_000 - 140_000 for f in members)


def test_resolution_follows_the_band() -> None:
    from wavescribe.radio.bandplan import find

    assert S.bin_for(87.5e6, 108e6, find("fm")) == 10_000
    assert S.bin_for(446e6, 446.2e6, find("pmr446")) == 2_500
    assert S.bin_for(*S.SWEEP_LIMITS_HZ) == 100_000
    assert S.scanned_band(*S.band_range("airband", "cept"), "cept").id == "airband"
    assert S.scanned_band(*S.SWEEP_LIMITS_HZ, "cept") is None


def test_a_sweep_through_the_fake_rtl_power(
    fake_power: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_POWER_SIGNALS", "89.1:150:25,105.5:150:18")
    seen: list[tuple[int, int]] = []
    sw = S.sweep("000001", 87.4e6, 108.1e6, bin_hz=10_000, command=fake_power,
                 on_progress=lambda d, t: seen.append((d, t)))  # fmt: skip
    assert sw.freqs[0] >= 87.4e6 and sw.freqs[-1] <= 108.1e6 and seen[-1][0] == seen[-1][1]
    assert [s.freq_hz for s in S.find_signals(sw, "cept")] == [89_100_000, 105_500_000]


def test_a_sweep_passes_the_dongles_frequency_correction(
    fake_power: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_POWER_STARTS", str(tmp_path / "starts.txt"))
    S.sweep("000001", 446e6, 446.2e6, command=fake_power)
    S.sweep("000001", 446e6, 446.2e6, command=fake_power, ppm=52)
    plain, corrected = (tmp_path / "starts.txt").read_text().splitlines()
    assert "-p" not in plain.split() and "-g 30 -p 52 -c 20%" in corrected


def test_a_busy_dongle_says_so(fake_power: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_POWER_BUSY", "1")
    with pytest.raises(S.ScanError, match="busy"):
        S.sweep("000001", 446e6, 446.2e6, command=fake_power)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_stalled_sweep_is_killed_with_a_clear_error(
    fake_power: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_POWER_HANG", "1")
    monkeypatch.setenv("FAKE_POWER_PID", str(tmp_path / "pid"))
    t0 = time.monotonic()
    with pytest.raises(S.ScanError, match="stopped reporting"):
        S.sweep("000001", 446e6, 446.2e6, command=fake_power, stall_s=1.0)
    assert time.monotonic() - t0 < 10
    assert _gone(int((tmp_path / "pid").read_text()))


def test_cancel_kills_a_running_sweep_at_once(
    fake_power: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_POWER_HANG", "1")
    monkeypatch.setenv("FAKE_POWER_PID", str(tmp_path / "pid"))
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    t0 = time.monotonic()
    with pytest.raises(S.Cancelled):
        S.sweep("000001", 446e6, 446.2e6, command=fake_power, cancel=cancel)
    assert time.monotonic() - t0 < 3  # not the default deadline of about 16 s
    assert _gone(int((tmp_path / "pid").read_text()))


def test_a_range_outside_the_tuner_is_refused() -> None:
    with pytest.raises(S.ScanError, match=r"isn't supported"):
        S.sweep("000001", 1e6, 2e6)


def test_a_band_the_tuner_cant_reach_is_refused_in_its_own_numbers() -> None:
    with pytest.raises(S.ScanError) as e:
        S.band_range("mw", "cept")
    assert str(e.value) == (
        "0.531 to 1.611 MHz is outside what this receiver tunes "
        "(24 to 1766 MHz); below 24 MHz isn't supported yet"
    )
    with pytest.raises(S.ScanError, match=r"^1800 to 1900 MHz is outside .*\(24 to 1766 MHz\)$"):
        S.fit_range(1800e6, 1900e6)


def test_a_range_partly_outside_the_tuner_scans_the_part_it_reaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[float, float]] = []

    def sweep(serial: str, start: float, stop: float, **_: object) -> S.Sweep:
        asked.append((start, stop))
        return spectrum(start, stop, 25_000, [])

    monkeypatch.setattr(S, "sweep", sweep)
    notes: list[str] = []
    S.scan("000001", 20e6, 30e6, "cept", rds=False, on_progress=lambda p: notes.append(p.note))
    assert asked == [(24_000_000, 30_000_000)]
    assert notes[0] == (
        "scanning 24 to 30 MHz of the 20 to 30 MHz asked for: below 24 MHz isn't supported yet"
    )
    assert S.fit_range(1700e6, 1800e6) == (
        1_700_000_000,
        1_766_000_000,
        "scanning 1700 to 1766 MHz of the 1700 to 1800 MHz asked for: "
        "above 1766 MHz is out of the tuner's reach",
    )
    assert S.fit_range(446e6, 446.2e6) == (446_000_000, 446_200_000, "")


def test_watching_counts_how_often_each_channel_was_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = iter([[(446.00625e6, 8e3, 20)], [(446.00625e6, 8e3, 20), (446.10625e6, 8e3, 20)],
                  [(446.00625e6, 8e3, 20)]])  # fmt: skip
    monkeypatch.setattr(
        S, "sweep", lambda *a, **k: spectrum(445.99e6, 446.21e6, 2_500, next(calls))
    )
    clock = iter([0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    # Only the scanner's clock: patching time.monotonic itself would feed other tests' threads.
    monkeypatch.setattr(
        S, "time", types.SimpleNamespace(monotonic=lambda: next(clock), time=time.time)
    )
    _, found = S.scan("000001", 445.99e6, 446.21e6, "cept", rds=False, watch_s=25)
    assert [(s.freq_hz, s.seen, s.passes) for s in found] == [(446_006_250, 3, 3),
                                                              (446_106_250, 1, 3)]  # fmt: skip


def test_learning_spurs_keeps_lines_seen_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    passes = iter([[(26.3e6, 2e3, 20), (300e6, 2e3, 20)], [(26.3e6, 2e3, 20)]])
    monkeypatch.setattr(S, "sweep", lambda *a, **k: spectrum(24e6, 30e6, 2_500, next(passes)))
    _, spurs = S.learn_spurs("000001", 24e6, 30e6)
    assert len(spurs) == 1 and abs(spurs[0] - 26.3e6) <= 2_500
