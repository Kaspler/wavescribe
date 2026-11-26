# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import numpy as np

from wavescribe.radio.catalog import Catalog
from wavescribe.radio.scan import Signal, Sweep


def sig(mhz: float, privacy: str = "broadcast", **kw) -> Signal:
    mode = {"broadcast": "wfm", "digital": None}.get(privacy, "nfm")
    return Signal(round(mhz * 1e6), -20.0, 15.0, 150e3, band="x", label="X", mode=mode,
                  privacy=privacy, **kw)  # fmt: skip


def test_scans_build_up_what_was_heard(tmp_path: Path) -> None:
    cat = Catalog(tmp_path)
    rds = {"pi": "2F15", "ps": "TEST FM", "rt": "Now playing", "pty": 1, "pty_name": "News"}
    sw = Sweep(88_000_000, 108_000_000, 10_000, np.linspace(88e6, 108e6, 50), np.zeros(50), 3.0)
    cat.record([sig(89.1, rds=rds), sig(105.5), sig(115.2, spur=True), sig(390.2, "digital")],
               serial="000001", start_hz=87.5e6, stop_hz=108e6, sweep=sw, now=1000.0)  # fmt: skip
    entries = cat.entries()
    assert [e["mhz"] for e in entries] == [89.1, 105.5]  # no spurs, nothing digital
    assert entries[0]["name"] == "TEST FM" and entries[0]["rds"]["rt"] == "Now playing"
    assert cat.last_scan()["signals"][0]["mhz"] == 89.1

    cat.record([sig(89.1)], serial="000001", start_hz=87.5e6, stop_hz=108e6, now=2000.0)
    by = {e["mhz"]: e for e in cat.entries()}
    assert by[89.1]["times"] == 2 and by[89.1]["name"] == "TEST FM" and not by[89.1]["missing"]
    assert by[105.5]["missing"] and by[105.5]["last_heard"] == 1000.0

    cat.name(89_100_000, "My station")
    assert cat.entries()[0]["user_name"] == "My station"
    cat.forget(105_500_000)
    assert [e["mhz"] for e in cat.entries()] == [89.1]
    cat.forget()
    assert cat.entries() == []


def test_private_radio_keeps_no_rds_or_words(tmp_path: Path) -> None:
    cat = Catalog(tmp_path)
    cat.record([sig(446.00625, "private", rds={"ps": "X"})], serial="000001", start_hz=446e6,
               stop_hz=446.2e6)  # fmt: skip
    [e] = cat.entries()
    assert e["privacy"] == "private" and "rds" not in e and "name" not in e


def test_spurs_are_kept_per_receiver(tmp_path: Path) -> None:
    cat = Catalog(tmp_path)
    assert cat.spurs("000001") == []
    cat.save_spurs("000001", [28_300_000, 26_400_000], 24e6, 1766e6, now=5.0)
    assert cat.spurs("000001") == [26_400_000, 28_300_000] and cat.spurs("000002") == []
    assert cat.spur_info() == {"000001": {"t": 5.0, "count": 2}}
    cat.forget_spurs("000001")
    assert cat.spurs("000001") == []
