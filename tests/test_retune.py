# SPDX-License-Identifier: Apache-2.0
"""Moving a receiver: what its stations do to a retune, and what a retune does to them.

A station its receiver can't hear where it is tuned (a frequency outside its window, a narrow
channel on its centre, a watch whose band sticks out) stays listed, unavailable with the reason,
and never stops Wavescribe from starting. A switched-off station constrains nothing. A retune that
would leave stations out asks first, naming them, whether they run or not."""

from pathlib import Path

import pytest

from tests.test_app import Running, make_app
from wavescribe.app import ChangeError
from wavescribe.config import parse_stations
from wavescribe.radio import service
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.watch import plan_centre, plan_window

P = Path("stations.toml")


def two_metres_centre() -> float:
    watch, _, width = plan_window("2m", "cept", rate=2_400_000)
    return plan_centre(watch, width, 2_400_000) / 1e6


def stations(centre: float, *more: dict) -> dict:
    """A receiver ``a`` at ``centre`` MHz with a 2 m watch on it, and a stream."""
    return {
        "defaults": {"language": "de-DE", "translate_to": ["en"]},
        "radio": {"region": "cept"},
        "device": [{"id": "a", "serial": "SN0042", "center_mhz": centre, "gain_db": 30}],
        "station": [
            {"id": "stream", "name": "Stream", "stream": "http://example.test/s"},
            {"id": "ham2m", "name": "Two metres", "device": "a", "watch": "2m"},
            *more,
        ],
    }


@pytest.fixture(autouse=True)
def no_real_dongles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.holders, "held_elsewhere", dict)


def running(tmp_path: Path, raw: dict, **kw) -> Running:
    r = Running(make_app(tmp_path, stations=raw, **kw))
    r.app._radio = service.RadioService(r.app, Catalog(tmp_path / "radio"))
    return r


# -- a station its receiver can't hear --------------------------------------------------------


def test_a_watch_its_receiver_cant_hear_is_listed_unavailable_not_refused(tmp_path: Path) -> None:
    config = parse_stations(stations(98.0), P)  # the receiver sits on FM
    watch = config.get("ham2m")
    centre = f"{two_metres_centre():.5f}".rstrip("0")
    assert watch.out_of_reach == (
        "Receiver A doesn't hear all of Amateur 2 m where it is tuned now: 161 of the 161 "
        f"channels Two metres watches are outside what it hears. Centre it on {centre} MHz "
        "(Settings → Receivers, or center_mhz in the stations file), or have Two metres watch "
        "the part it hears."
    )
    assert "station 'ham2m'" not in watch.out_of_reach and "SN0042" not in watch.out_of_reach

    r = running(tmp_path, stations(98.0), start=["ham2m", "stream"])
    try:
        assert r.app.unavailable == {"ham2m": watch.out_of_reach}
        assert "ham2m" not in r.app.running() and r.app.config_errors == {}
        view = next(s for s in r.app.stations_view() if s["id"] == "ham2m")
        assert view["available"] is False and view["reason"] == watch.out_of_reach
        with pytest.raises(ChangeError, match="Receiver A doesn't hear all of Amateur 2 m"):
            r.app.start_station("ham2m")
    finally:
        r.close()


def test_a_narrow_channel_its_receiver_cant_hear_is_listed_unavailable(tmp_path: Path) -> None:
    rpt = {"id": "rpt", "name": "Repeater", "device": "a", "mhz": 145.6}
    outside = parse_stations(stations(98.0, rpt), P).get("rpt")
    assert outside.out_of_reach == (
        "Receiver A doesn't hear Repeater where it is tuned now: 145.6 MHz is outside 96.86 to "
        "99.14 MHz. Centre it on 145.35 MHz (Settings → Receivers, or center_mhz in the stations "
        "file), or give Repeater another receiver."
    )
    on_it = parse_stations(stations(145.1, {**rpt, "mhz": 145.1}), P)
    assert on_it.get("rpt").out_of_reach == (
        "Receiver A is tuned right onto Repeater (145.1 MHz), where every dongle has a spike of "
        "its own that would hold the squelch open. Centre it on 144.85 MHz instead (Settings → "
        "Receivers, or center_mhz in the stations file)."
    )
    assert on_it.get("ham2m").out_of_reach is None  # a watch's floors take the spike in


def test_a_page_change_cant_tune_a_station_where_its_receiver_doesnt_hear_it(
    tmp_path: Path,
) -> None:
    r = running(tmp_path, stations(98.0))
    try:
        far = {"name": "Far", "device": "a", "mhz": 145.6}
        status, err = r.call("POST", "/api/manage/stations", far)
        assert status == 400 and err["error"].startswith("Receiver A doesn't hear Far where")
        assert all(s.name != "Far" for s in r.app.config.stations)
        # A station its receiver was moved away from still takes other changes.
        status, ham = r.call("POST", "/api/manage/stations/ham2m", {"name": "Two metres FM"})
        assert status == 200 and ham["available"] is False
        assert "channels Two metres FM watches" in ham["reason"]
        assert r.app.radio.receivers()[0]["stations"][0]["out_of_reach"] == ham["reason"]
    finally:
        r.close()


# -- what constrains a retune ------------------------------------------------------------------


def test_the_receivers_page_asks_the_same_question(tmp_path: Path) -> None:
    rpt = {"id": "rpt", "name": "Repeater", "device": "a", "mhz": 145.1}
    r = running(tmp_path, stations(two_metres_centre(), rpt), start=["rpt"])
    real = r.app.running
    held = {"rpt"}
    r.app.running = lambda: real() | held  # type: ignore[method-assign]
    try:
        status, ask = r.call("POST", "/api/manage/receivers/a", {"center_mhz": 98.0})
        assert status == 400 and ask["code"] == "retune", ask
        assert ask["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False},
                                {"id": "rpt", "name": "Repeater", "running": True}]  # fmt: skip
        assert ask["holders"] == ["Repeater"] and ask["center_mhz"] == 98.0
        assert ask["error"] == (
            "Tuned to 98 MHz, Receiver A no longer hears Two metres or Repeater. Retuning it "
            "stops Repeater. They stay in your stations, unavailable until the receiver hears "
            "them again."
        )
        # On a narrow channel's very centre: that one is left out too, for its spike.
        status, ask = r.call("POST", "/api/manage/receivers/a", {"center_mhz": 145.1})
        assert status == 400 and [s["id"] for s in ask["unfit"]] == ["rpt"]

        status, rx = r.call("POST", "/api/manage/receivers/a", {"center_mhz": 98.0, "retune": True})
        assert status == 200, rx
        assert {s["id"]: s["out_of_reach"] is not None for s in rx["stations"]} == {
            "ham2m": True, "rpt": True}  # fmt: skip
        run = (r.app.settings.get("run") or {}).get("stations", [])
        assert "rpt" not in run  # stopped, and not to start again with the next serve
    finally:
        r.close()


def test_doctor_counts_a_station_out_of_reach_as_such_not_as_a_page_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_doctor import run as doctor_run
    from tests.test_doctor import status

    (tmp_path / "config").mkdir()
    (tmp_path / "home").mkdir()
    toml = f"""
[defaults]
language = "de-DE"
translate_to = ["en"]

[radio]
region = "cept"

[[device]]
id = "a"
serial = "00001234"
center_mhz = {two_metres_centre()}
gain_db = 30

[[station]]
id = "ham2m"
name = "Two metres"
device = "a"
watch = "2m"
"""
    (tmp_path / "config" / "stations.toml").write_text(toml)
    (tmp_path / "config" / "ui.toml").write_text("[device_overrides.a]\ncenter_mhz = 98.0\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    found = doctor_run(tmp_path, "--no-alerts")
    assert status(found, "page changes") == "ok", found["page changes"]
    assert status(found, "out of reach") == "warn"
    assert "Receiver A doesn't hear all of Amateur 2 m" in found["out of reach"][0].summary
