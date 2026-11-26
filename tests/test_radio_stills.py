# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from wavescribe.radio.stills import crop, still
from wavescribe.web.api import dispatch


def scan(start: float, stop: float, points: int, t: float, signals: tuple[float, ...] = ()) -> dict:
    return {
        "t": t, "serial": "TEST", "seconds": 60.0,
        "spectrum": {"start_mhz": start, "stop_mhz": stop, "db": [float(i) for i in range(points)]},
        "signals": [{"mhz": f, "snr_db": 12.0} for f in signals],
    }  # fmt: skip


def save(d: Path, name: str, data: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), "utf-8")


def test_a_scan_is_cut_to_the_stretch_asked_for() -> None:
    cut = crop(scan(88.0, 108.0, 200, 1.0, (89.0, 98.5, 99.5, 107.0)), 98.0, 100.0)
    assert cut is not None
    assert cut["spectrum"]["start_mhz"] == 98.0 and cut["spectrum"]["stop_mhz"] == 100.0
    assert cut["spectrum"]["db"] == [float(i) for i in range(100, 120)]
    assert [s["mhz"] for s in cut["signals"]] == [98.5, 99.5]
    assert crop(scan(88.0, 108.0, 200, 1.0), 118.0, 137.0) is None, "outside the scan"
    assert crop({"t": 1.0}, 98.0, 100.0) is None, "a scan with no spectrum"


def test_the_newest_scan_detailed_enough_is_chosen(tmp_path: Path) -> None:
    save(tmp_path, "20260101-100000-a", scan(87.5, 108.0, 2000, 100.0, (98.6,)))
    save(tmp_path, "20260101-110000-a", scan(24.0, 1766.0, 2000, 200.0))  # newer, but a smear here
    save(tmp_path, "20260101-120000-a", scan(118.0, 137.0, 2000, 300.0))  # newest, elsewhere
    (tmp_path / "20260101-130000-a.json").write_text("{not json", "utf-8")
    got = still(tmp_path, 97.0, 99.4)
    assert got is not None and got["t"] == 100.0
    assert len(got["spectrum"]["db"]) >= 48 and [s["mhz"] for s in got["signals"]] == [98.6]


def test_with_only_coarse_scans_the_most_detailed_one_is_still_shown(tmp_path: Path) -> None:
    save(tmp_path, "20260101-100000-a", scan(24.0, 1766.0, 2000, 100.0))
    save(tmp_path, "20260101-110000-a", scan(24.0, 1766.0, 1000, 200.0))
    got = still(tmp_path, 96.0, 100.0)
    assert got is not None and got["t"] == 100.0
    assert still(tmp_path / "none", 96.0, 100.0) is None, "no scans yet"
    with pytest.raises(ValueError):
        still(tmp_path, 100.0, 96.0)


def test_the_page_asks_for_a_still_picture_by_its_stretch(tmp_path: Path) -> None:
    class Radio:
        class catalog:
            root = tmp_path

    class App:
        radio = Radio

    save(tmp_path / "scans", "20260101-100000-a", scan(87.5, 108.0, 2000, 100.0))
    status, body = dispatch(App, "GET", "/api/manage/radio/scans", {"lo": "97", "hi": "99.4"}, None)
    assert status == 200 and body["scan"]["t"] == 100.0
    status, body = dispatch(App, "GET", "/api/manage/radio/scans", {"lo": "500", "hi": "502"}, None)
    assert status == 200 and body["scan"] is None
    status, _ = dispatch(App, "GET", "/api/manage/radio/scans", {"lo": "x"}, None)
    assert status == 400
