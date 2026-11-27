# SPDX-License-Identifier: Apache-2.0
"""Moving a receiver: what its stations do to a retune, and what a retune does to them.

A station its receiver can't hear where it is tuned (a frequency outside its window, a narrow
channel on its centre, a watch whose band sticks out) stays listed, unavailable with the reason,
and never stops Wavescribe from starting. A switched-off station constrains nothing. A retune that
would leave stations out asks first, naming them, whether they run or not."""

from pathlib import Path

import pytest

from tests.test_app import Running, make_app, wait_for
from tests.test_live import fake_rtl  # noqa: F401  (a fixture: rtl_sdr is a stand-in)
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
        # Added, a station its receiver doesn't hear moves the receiver, as a tune does: here
        # without asking, since the receiver hears nothing else.
        far = {"name": "Far", "device": "a", "mhz": 145.6}
        status, added = r.call("POST", "/api/manage/stations", far)
        assert status == 200 and added["available"] is True, added
        assert added["receiver"] == {"id": "a", "center_mhz": 145.35, "moved": True}
        # Changed, it can't be tuned where its receiver doesn't hear it.
        status, err = r.call("POST", f"/api/manage/stations/{added['id']}", {"mhz": 98.0})
        assert status == 400 and err["error"].startswith("Receiver A doesn't hear Far where")
        assert r.app.config.get(added["id"]).sdr.freq_hz == 145_600_000
        # A station its receiver was moved away from still takes other changes.
        status, ham = r.call("POST", "/api/manage/stations/ham2m", {"name": "Two metres FM"})
        assert status == 200 and ham["available"] is False
        assert "channels Two metres FM watches" in ham["reason"]
        assert r.app.radio.receivers()[0]["stations"][0]["out_of_reach"] == ham["reason"]
    finally:
        r.close()


def test_starting_a_station_its_receiver_doesnt_hear_moves_it_and_asks_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    far = {"id": "far", "name": "Far", "device": "a", "mhz": 432.5}
    rpt = {"id": "rpt", "name": "Repeater", "device": "a", "mhz": 145.6}
    r = running(tmp_path, stations(two_metres_centre(), far, rpt))
    before = r.app.settings.snapshot()
    try:
        assert r.app.unavailable == {"far": r.app.config.get("far").out_of_reach}
        status, ask = r.call("POST", "/api/manage/stations/far/start")
        assert status == 400 and ask["code"] == "retune", ask
        assert ask["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False},
                                {"id": "rpt", "name": "Repeater", "running": False}]  # fmt: skip
        assert ask["holders"] == [] and ask["center_mhz"] == 432.25
        assert ask["error"] == (
            "Receiver A moves to 432.25 MHz to hear Far, where it no longer hears Two metres or "
            "Repeater. They stay in your stations, unavailable until the receiver hears them again."
        )
        assert r.app.settings.snapshot() == before  # nothing changed while it asks
        status, err = r.call("POST", "/api/manage/stations/far/start", {"retune": "yes"})
        assert status == 400 and err["error"] == "retune must be true or false"

        status, started = r.call("POST", "/api/manage/stations/far/start", {"retune": True})
        assert status == 200, started
        assert started["ok"] is True and started["start_error"] is None
        assert started["receiver"] == {"id": "a", "center_mhz": 432.25, "moved": True}
        assert [s["id"] for s in started["unfit"]] == ["ham2m", "rpt"]
        assert started["live_closed"] is False
        assert r.app.config.devices[0].center_hz == 432_250_000
        assert "far" not in r.app.unavailable and "far" in r.app.settings.get("run")["stations"]
        assert set(r.app.unavailable) == {"ham2m", "rpt"}

        # Back to the repeater: where only the station just started is left out, it asks again.
        status, ask = r.call("POST", "/api/manage/stations/rpt/start")
        assert status == 400 and ask["code"] == "retune" and ask["center_mhz"] == 145.35
        # Started on its own, a station in reach starts as it always did.
        assert r.call("POST", "/api/manage/stations/stream/start") == (200, {"ok": True})

        # A receiver that can't be tuned anywhere that hears the station: nothing to ask.
        monkeypatch.setattr(service, "TUNER_RANGE_HZ", (24_000_000, 100_000_000))
        status, err = r.call("POST", "/api/manage/stations/rpt/start", {"retune": True})
        assert (status, err["code"]) == (400, "out_of_reach"), err
        assert err["error"] == "Receiver A can't be tuned to hear 145.6 MHz."
        assert r.app.config.devices[0].center_hz == 432_250_000
    finally:
        r.close()


# -- what constrains a retune ------------------------------------------------------------------


def test_a_stopped_watch_asks_before_a_retune_leaves_it_out(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre()))
    try:
        assert r.app.unavailable == {} and r.app.running() == set()
        body = {"device": "a", "mhz": 99.5, "language": "de-DE"}
        status, ask = r.call("POST", "/api/manage/radio/tune", body)
        assert status == 400 and ask["code"] == "retune", ask
        assert ask["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False}]
        assert ask["holders"] == [] and ask["center_mhz"] == 99.2
        assert ask["error"] == (
            "Receiver A moves to 99.2 MHz to hear 99.5 MHz, where it no longer hears Two "
            "metres. It stays in your stations, unavailable until the receiver hears it again."
        )
        assert r.app.config.devices[0].center_hz == round(two_metres_centre() * 1e6)
        assert len(r.app.config.stations) == 2  # nothing was added

        status, st = r.call("POST", "/api/manage/radio/tune", {**body, "retune": True})
        assert status == 200, st
        assert st["receiver"] == {"id": "a", "center_mhz": 99.2, "moved": True}
        assert st["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False}]
        assert r.app.config.devices[0].center_hz == 99_200_000
        assert "Receiver A doesn't hear all of Amateur 2 m" in r.app.unavailable["ham2m"]
        assert r.app.config_errors == {}
    finally:
        r.close()


def test_a_switched_off_watch_constrains_nothing(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre()))
    try:
        assert r.call("POST", "/api/manage/stations/ham2m", {"disabled": True})[0] == 200
        body = {"device": "a", "mhz": 99.5, "language": "de-DE"}
        status, st = r.call("POST", "/api/manage/radio/tune", body)
        assert status == 200, st
        assert st["unfit"] == [] and r.app.config.devices[0].center_hz == 99_200_000
        assert r.app.unavailable["ham2m"] == "switched off on the page"
        # Back to 2 m from the receivers page: only the station just added is asked about.
        status, ask = r.call("POST", "/api/manage/receivers/a", {"center_mhz": 145.0})
        assert status == 400 and ask["unfit"] == [
            {"id": st["id"], "name": "99.5 MHz", "running": st["id"] in r.app.running()}
        ]
    finally:
        r.close()


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
            "Receiver A moves to 98 MHz, where it no longer hears Two metres or Repeater. "
            "Retuning it stops Repeater. They stay in your stations, unavailable until the "
            "receiver hears them again."
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


# -- adding what a receiver tuned by hand hears -------------------------------------------------


def hand_tuned(tmp_path: Path) -> Running:
    """A receiver ``x`` on 445.85 MHz. Its stations' test audio ends at once: they are held as
    running from their start until they are stopped, as a live channel would be."""
    raw = {"defaults": {"language": "fr-FR", "translate_to": ["en"]},
           "station": [{"id": "s", "stream": "http://example.test/s"}]}  # fmt: skip
    r = running(tmp_path, raw)
    r.app.settings.update(lambda d: d.setdefault("radio", {}).update(region="cept"))
    dongle = {"id": "x", "serial": "SN0042", "center_mhz": 445.85, "gain_db": 30}
    assert r.call("POST", "/api/manage/receivers", dongle)[0] == 200
    held: set[str] = set()
    real = r.app.running
    r.app.running = lambda: real() | held  # type: ignore[method-assign]
    r.app.pipeline.add_station = lambda s, asr=None: held.add(s.id)  # type: ignore[method-assign]
    r.app.pipeline.remove_station = lambda sid, wait=0: held.discard(sid)  # type: ignore[method-assign]
    return r


@pytest.mark.usefixtures("fake_rtl")
def test_add_from_a_receiver_tuned_by_hand_on_find_is_one_call(tmp_path: Path) -> None:
    r = hand_tuned(tmp_path)
    try:
        st = r.call("POST", "/api/manage/live/x", {"mhz": 99.5, "from": "find"})[1]
        assert st["open"] and st["center_mhz"] == pytest.approx(99.2)
        body = {"device": "x", "mhz": 99.5, "language": "fr-FR"}
        status, added = r.call("POST", "/api/manage/radio/tune", body)
        assert status == 200, added
        assert added["running"] is True and added.get("start_error") is None
        assert added["live_closed"] is True and added["unfit"] == []
        assert added["receiver"] == {"id": "x", "center_mhz": 99.2, "moved": True}
        assert not r.app.live.is_open("x")
        assert r.app.config.get(added["id"]).sdr.freq_hz == 99_500_000
        assert added["id"] in r.app.running()
        assert added["id"] in r.app.settings.get("run")["stations"]
    finally:
        r.app.live.close_all()
        r.close()


@pytest.mark.usefixtures("fake_rtl")
def test_add_from_a_receiver_tuned_by_hand_asks_for_stations_it_leaves_out(tmp_path: Path) -> None:
    r = hand_tuned(tmp_path)
    try:
        pmr = {"device": "x", "mhz": 446.00625, "language": "es-ES", "acknowledge": True}
        walkies = r.call("POST", "/api/manage/radio/tune", pmr)[1]
        r.call("POST", f"/api/manage/stations/{walkies['id']}/stop")
        r.call("POST", "/api/manage/live/x", {"mhz": 99.5, "from": "find"})
        status, held = r.call("POST", f"/api/manage/stations/{walkies['id']}/start")
        assert status == 400 and held["error"] == (
            "Receiver X is tuned by hand to 99.5 MHz under Find: let it go there first."
        )

        body = {"device": "x", "mhz": 99.5, "language": "fr-FR"}
        status, ask = r.call("POST", "/api/manage/radio/tune", body)
        assert status == 400 and ask["code"] == "retune"
        assert ask["unfit"] == [{"id": walkies["id"], "name": walkies["name"], "running": False}]
        assert r.app.live.is_open("x")  # nothing changed while it asks
        assert "445.85" not in ask["error"]  # never the centre it was set to before

        status, added = r.call("POST", "/api/manage/radio/tune", {**body, "retune": True})
        assert status == 200 and added["running"] is True and added["live_closed"] is True
        assert r.app.unavailable[walkies["id"]].startswith("Receiver X doesn't hear")
    finally:
        r.app.live.close_all()
        r.close()


@pytest.mark.usefixtures("fake_rtl")
def test_keep_it_as_a_station_under_listen_asks_without_letting_go(tmp_path: Path) -> None:
    r = hand_tuned(tmp_path)
    try:
        pmr = {"device": "x", "mhz": 446.00625, "language": "es-ES", "acknowledge": True}
        r.call("POST", "/api/manage/radio/tune", pmr)
        r.call("POST", "/api/manage/live/x", {"mhz": 99.5, "from": "listen", "pause": True})
        status, ask = r.call("POST", "/api/manage/live/x/station",
                             {"does": "listen", "language": "fr-FR"})  # fmt: skip
        assert status == 400 and ask["code"] == "retune", ask
        assert r.app.live.is_open("x")
        status, added = r.call("POST", "/api/manage/live/x/station",
                               {"does": "listen", "language": "fr-FR", "retune": True})  # fmt: skip
        assert status == 200 and added["live_closed"] is True and added["transcribe"] is False
        wait_for(lambda: not r.app.live.is_open("x"))
    finally:
        r.app.live.close_all()
        r.close()


@pytest.mark.usefixtures("fake_rtl")
def test_the_languages_of_a_station_in_language_auto_are_kept_and_can_be_changed(
    tmp_path: Path,
) -> None:
    r = hand_tuned(tmp_path)
    try:
        r.call("POST", "/api/manage/live/x", {"mhz": 99.5, "from": "listen"})
        keep = {"does": "listen", "language": "auto", "languages": ["fr-FR", "en"]}
        status, added = r.call("POST", "/api/manage/live/x/station", keep)
        assert status == 200 and added["languages"] == ["fr-FR", "en"], added
        assert r.app.config.get(added["id"]).languages == ("fr-FR", "en")

        # Changed on the page: a page station in place, a file station by an override.
        path = f"/api/manage/stations/{added['id']}"
        status, changed = r.call("POST", path, {"languages": ["fr-FR"]})
        assert status == 200 and changed["languages"] == ["fr-FR"], changed
        status, changed = r.call("POST", "/api/manage/stations/s",
                                 {"language": "auto", "languages": ["fr-FR", "de-DE"]})  # fmt: skip
        assert status == 200 and changed["languages"] == ["fr-FR", "de-DE"], changed
        assert changed["overridden"] == ["language", "languages"]
        status, back = r.call("POST", "/api/manage/stations/s",
                              {"language": "fr-FR", "languages": []})  # fmt: skip
        assert status == 200 and back["language"] == "fr-FR" and back["languages"] is None, back
        # They narrow language auto only: a station in one language refuses them, as ever.
        status, err = r.call("POST", "/api/manage/stations/s", {"languages": ["de-DE"]})
        assert status == 400 and "'languages' narrows language 'auto'" in err["error"], err
    finally:
        r.app.live.close_all()
        r.close()
