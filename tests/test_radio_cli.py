# SPDX-License-Identifier: Apache-2.0
"""`wavescribe radio` output: what a scan says about the lines it marks and the range it cut."""

from pathlib import Path

import pytest

from wavescribe.cli import main
from wavescribe.radio import cli as radio_cli


def line(**s) -> str:
    return radio_cli._signal_line({"mhz": 122.4, "snr_db": 12.0, "mode": "am",
                                   "privacy": "utility", **s})  # fmt: skip


def test_marked_lines_say_whose_they_probably_are() -> None:
    assert "another receiver's clock, not a transmission" in line(spur=True, clock_hz=2_400_029)
    assert "receiver's own spur" in line(spur=True)
    assert "spur" not in line() and "clock" not in line()


def test_a_scan_says_which_part_of_the_range_it_left_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda *_a: ("00001234", 30.0, 0))
    asked = []

    def fake_scan(serial, start, stop, region, **_kw):
        asked.append((start, stop))
        return None, []

    monkeypatch.setattr("wavescribe.radio.scan.scan", fake_scan)
    monkeypatch.setattr("wavescribe.radio.catalog.Catalog.record", lambda *_a, **_k: None)
    assert main(["radio", "scan", "a", "20:30"]) == 0
    err = capsys.readouterr().err
    assert asked == [(24_000_000, 30_000_000)]
    assert "scanning 24 to 30 MHz of the 20 to 30 MHz asked for" in err
    assert "below 24 MHz isn't supported" in err
