# SPDX-License-Identifier: Apache-2.0
"""The page's Radio tab, through /api/manage/radio: scan an idle receiver, tune what it found."""

from pathlib import Path

import numpy as np
import pytest

from tests.test_app import Running, make_app, wait_for
from wavescribe.radio import service
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.scan import Signal, Sweep


@pytest.fixture
def radio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    r = Running(make_app(tmp_path))
    r.app._radio = service.RadioService(r.app, Catalog(tmp_path / "radio"))
    r.app.settings.update(lambda d: d.setdefault("radio", {}).update(region="cept"))
    dongle = {"id": "x", "serial": "SN0042", "center_mhz": 445.85, "gain_db": 30}
    assert r.call("POST", "/api/manage/receivers", dongle)[0] == 200

    def fake_scan(serial, start, stop, region, **kw):
        assert serial == "SN0042" and region == "cept"
        sw = Sweep(int(start), int(stop), 2500, np.linspace(start, stop, 100), np.zeros(100), 1.0)
        found = [Signal(446_006_250, -30.0, 18.0, 9e3, band="pmr446", mode="nfm",
                        label="PMR446 walkie-talkies, channel 1", privacy="private")]  # fmt: skip
        kw["on_progress"](service.scanner.Progress("sweep", 1, 1))
        return sw, found

    monkeypatch.setattr(service.scanner, "scan", fake_scan)
    yield r
    r.close()


def test_scan_then_tune_a_channel_it_found(radio: Running) -> None:
    status, view = radio.call("GET", "/api/manage/radio")
    assert status == 200 and view["region"] == "cept" and view["region_source"] == "page"
    assert any(b["id"] == "pmr446" for b in view["bands"])
    assert [r["id"] for r in view["receivers"]] == ["x"] and view["receivers"][0]["busy"] == []

    status, out = radio.call("POST", "/api/manage/radio/scan", {"device": "x", "band": "pmr446"})
    assert status == 200 and out["scan"]["what"] == "PMR446 walkie-talkies"
    wait_for(lambda: radio.call("GET", "/api/manage/radio/scan")[1]["scan"]["phase"] == "done")
    view = radio.call("GET", "/api/manage/radio")[1]
    assert [e["mhz"] for e in view["heard"]] == [446.00625]
    assert view["last_scan"]["signals"][0]["label"] == "PMR446 walkie-talkies, channel 1"
    assert view["last_scan"]["device"] == "x" and view["last_scan"]["serial"] == "SN0042"
    assert view["last_scan"]["what"] == "PMR446 walkie-talkies"

    body = {"device": "x", "mhz": 446.00625, "language": "es-ES"}
    status, err = radio.call("POST", "/api/manage/radio/tune", body)
    assert status == 400 and err["code"] == "acknowledge"
    status, st = radio.call("POST", "/api/manage/radio/tune", {**body, "acknowledge": True})
    assert status == 200 and st["privacy"] == "private" and st["band"] == "pmr446"
    assert st["name"] == "PMR446 walkie-talkies, channel 1"
    station = radio.app.config.get(st["id"])
    assert station.sdr.mode == "nfm" and station.privacy == "private"
    assert radio.app.settings.get("station")[0]["acknowledge"] == "private"  # for this class
    assert radio.call("GET", "/api/manage/radio")[1]["heard"][0]["station"] == st["id"]
    # The test's audio ends at once; hold the station as running, as a live channel would be.
    real = radio.app.running
    radio.app.running = lambda: real() | {st["id"]}  # type: ignore[method-assign]

    # The receiver is busy now: no scan, and moving it far away needs a yes.
    status, err = radio.call("POST", "/api/manage/radio/scan", {"device": "x", "band": "pmr446"})
    assert status == 400 and err["code"] == "busy" and err["holders"] == [st["name"]]
    far = {"device": "x", "mhz": 145.5, "language": "en"}
    status, err = radio.call("POST", "/api/manage/radio/tune", far)
    assert status == 400 and err["code"] == "retune"
    status, ham = radio.call("POST", "/api/manage/radio/tune", {**far, "retune": True})
    assert status == 200 and ham["privacy"] == "amateur"
    assert radio.app.config.devices[0].center_hz == 145_250_000
    assert st["id"] in radio.app.config_errors  # the walkie-talkie channel is out of reach now


def test_a_scan_saved_before_scans_named_their_receiver_is_named_by_its_serial(
    radio: Running, tmp_path: Path
) -> None:
    import json

    scans = tmp_path / "radio" / "scans"
    scans.mkdir(parents=True)
    old = {"t": 1000.0, "serial": "SN0042", "seconds": 3.0, "spectrum": [], "signals": []}
    (scans / "20260101-120000-SN0042.json").write_text(json.dumps(old), "utf-8")
    last = radio.call("GET", "/api/manage/radio")[1]["last_scan"]
    assert (last["device"], last["what"], last["t"]) == ("x", None, 1000.0)
    old["serial"] = "SN9999"  # a receiver no longer configured
    (scans / "20260101-130000-SN9999.json").write_text(json.dumps(old), "utf-8")
    last = radio.call("GET", "/api/manage/radio")[1]["last_scan"]
    assert last["device"] is None and last["serial"] == "SN9999"


def test_a_channel_on_the_receivers_centre_moves_the_receiver(radio: Running) -> None:
    body = {"device": "x", "mhz": 445.85, "mode": "nfm", "language": "es-ES", "acknowledge": True}
    status, st = radio.call("POST", "/api/manage/radio/tune", body)
    assert status == 200
    assert radio.app.config.devices[0].center_hz == 445_600_000  # was 445.85, the channel itself
    assert radio.app.config.get(st["id"]).sdr.freq_hz == 445_850_000


def test_scanning_a_busy_receiver_pauses_its_stations(radio: Running, monkeypatch) -> None:
    body = {"device": "x", "mhz": 446.00625, "language": "es-ES", "acknowledge": True}
    st = radio.call("POST", "/api/manage/radio/tune", body)[1]
    held, calls = {st["id"]}, []
    real = radio.app.running
    radio.app.running = lambda: real() | held  # type: ignore[method-assign]
    monkeypatch.setattr(
        radio.app.pipeline,
        "remove_station",
        lambda sid, wait=0: (calls.append(("stop", sid)), held.discard(sid)),
    )
    monkeypatch.setattr(radio.app.pipeline, "add_station",
                        lambda s: (calls.append(("start", s.id)), held.add(s.id)))  # fmt: skip

    status, out = radio.call("POST", "/api/manage/radio/scan",
                             {"device": "x", "band": "pmr446", "pause": True})  # fmt: skip
    assert status == 200 and out["scan"]["paused"] == [st["name"]]
    wait_for(lambda: radio.call("GET", "/api/manage/radio/scan")[1]["scan"]["phase"] == "done")
    assert calls == [("stop", st["id"]), ("start", st["id"])]  # and back on after the scan

    radio.app.radio._job = {"device": "x", "phase": "sweep"}
    status, err = radio.call("POST", "/api/manage/radio/tune", {**body, "mhz": 446.01875})
    assert status == 400 and err["code"] == "scanning"


def test_what_is_kept_is_the_users_to_change(radio: Running) -> None:
    from wavescribe.radio.bandplan import POLICIES, apply_policies

    try:
        store = radio.app.store
        if store is not None:
            store._next_text_prune = float("inf")
        status, view = radio.call(
            "POST", "/api/manage/radio/policy/private", {"text_days": 1, "audio": True}
        )
        assert status == 200 and view["policies"]["private"]["text_days"] == 1
        # A shorter time applies soon: pruned already, or due at once (never, before).
        assert store is None or store._next_text_prune != float("inf")
        assert view["policies"]["private"]["changed"] == ["audio", "text_days"]
        assert view["policies"]["private"]["default"]["text_days"] == 0
        assert POLICIES["private"].audio and POLICIES["private"].needs_ack
        assert radio.app.settings.get("radio")["policy"]["private"] == {
            "text_days": 1,
            "audio": True,
        }
        assert radio.call("POST", "/api/manage/radio/policy/broadcast", {"audio": False})[0] == 400
        assert (
            radio.call("POST", "/api/manage/radio/policy/private", {"needs_ack": False})[0] == 400
        )
        status, view = radio.call("DELETE", "/api/manage/radio/policy")
        assert view["policies"]["private"]["text_days"] == 0 and not POLICIES["private"].audio
    finally:
        apply_policies()


def test_dongles_on_usb_and_fm_names_from_rds(radio: Running, monkeypatch) -> None:
    from wavescribe.sources import sdr

    monkeypatch.setattr(sdr, "list_dongles", lambda: [
        {"index": "0", "vendor": "V", "product": "P", "serial": "SN0042"},
        {"index": "1", "vendor": "V", "product": "P", "serial": "SN0043"},
    ])  # fmt: skip
    view = radio.call("GET", "/api/manage/radio?dongles=1")[1]
    assert [d["serial"] for d in view["dongles"]] == ["SN0043"]  # SN0042 is receiver x already

    radio.call(
        "POST",
        "/api/manage/receivers",
        {"id": "y", "serial": "SN0043", "center_mhz": 98.3, "gain_db": 30},
    )
    st = radio.app.add_station(
        {"name": "98.6 MHz", "language": "es-ES", "device": "y", "fm_mhz": [98.6]}
    )
    status = {"stations": {st["id"]: {"rds": {"ps": "RADIO X ", "rt": None}}}}
    monkeypatch.setattr(radio.app.pipeline, "status", lambda: status)
    assert radio.app.radio.adopt_rds_names() == [st["id"]]
    assert radio.app.config.get(st["id"]).name == "RADIO X"
    assert radio.app.radio.adopt_rds_names() == []  # a name of its own now: left alone


def test_digital_services_ask_first_and_unknown_receivers_are_refused(radio: Running) -> None:
    digital = {"device": "x", "mhz": 390.2, "language": "en"}
    status, err = radio.call("POST", "/api/manage/radio/tune", digital)
    assert status == 400 and err["code"] == "acknowledge" and "no digital decoder" in err["error"]
    status, st = radio.call("POST", "/api/manage/radio/tune", {**digital, "acknowledge": True})
    assert status == 200 and st["privacy"] == "digital"
    assert radio.call("POST", "/api/manage/radio/scan", {"device": "nope", "band": "fm"})[0] == 400
    assert radio.call("POST", "/api/manage/radio/region", {"region": "mars"})[0] == 400
    status, view = radio.call("POST", "/api/manage/radio/region", {"region": "2"})
    assert status == 200 and view["region"] == "2" and any(b["id"] == "frs" for b in view["bands"])


@pytest.mark.parametrize(
    ("mhz", "message"),
    [
        (446_006_250, "446006250 looks like Hz; frequencies are in MHz: 446.00625"),
        (446_006.25, "looks like kHz"),
        ("inf", "give a frequency in MHz"),
        (-446.00625, "give a frequency in MHz"),
        (None, "give a frequency in MHz"),
        (0.666, "tunes 24 to 1766 MHz"),
        (5000, "tunes 24 to 1766 MHz"),
    ],
)
def test_what_cant_be_a_frequency_is_refused_with_the_likely_mistake(
    radio: Running, mhz: object, message: str
) -> None:
    body = {"device": "x", "mhz": mhz, "language": "es-ES", "acknowledge": True}
    status, err = radio.call("POST", "/api/manage/radio/tune", body)
    assert status == 400 and message in err["error"]
    assert radio.app.settings.get("station") is None  # no station was made of it
    status, err = radio.call("POST", "/api/manage/radio/scan",
                             {"device": "x", "start_mhz": mhz, "stop_mhz": 447})  # fmt: skip
    assert status == 400 and "what to scan" in err["error"]


def test_listing_dongles_opens_none(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from wavescribe.sources import sdr

    seen: list[list[str]] = []
    out = "Found 1 device(s):\n  0:  RTLSDRBlog, Blog V4, SN: WS000001\n\nNo matching devices.\n"

    def run(args: list[str], **kw: object) -> subprocess.CompletedProcess:
        seen.append(args)
        return subprocess.CompletedProcess(args, 1, "", out)

    monkeypatch.setattr(sdr.shutil, "which", lambda c: f"/usr/bin/{c}")
    monkeypatch.setattr(subprocess, "run", run)
    assert [d["serial"] for d in sdr.list_dongles()] == ["WS000001"]
    assert seen[0][1:] == ["-d", "no-such-dongle"]  # matches no serial, so nothing is opened


def test_a_change_of_region_alone_never_loosens_a_stations_class(radio: Running) -> None:
    body = {"device": "x", "mhz": 446.00625, "language": "es-ES", "acknowledge": True}
    status, st = radio.call("POST", "/api/manage/radio/tune", body)
    assert status == 200 and st["privacy"] == "private"
    # In region 2, 446 MHz is amateur radio (text kept a week, quoted in alerts): the station
    # keeps the class it was confirmed under until the user confirms the new one.
    assert radio.call("POST", "/api/manage/radio/region", {"region": "2"})[0] == 200
    station = radio.app.config.get(st["id"])
    assert (station.privacy, station.privacy_region) == ("private", "amateur")
    view = next(v for v in radio.app.stations_view() if v["id"] == st["id"])
    assert view["privacy_region"] == "amateur"
    assert radio.app.hub.exports().stations.isdisjoint({st["id"]})  # still kept here

    status, view = radio.call("POST", f"/api/manage/stations/{st['id']}", {"acknowledge": True})
    assert status == 200 and view["privacy"] == "amateur" and view["privacy_region"] is None
    assert radio.app.config.get(st["id"]).privacy == "amateur"

    # Stricter the other way round needs no floor: back in Europe it is private again.
    assert radio.call("POST", "/api/manage/radio/region", {"region": "cept"})[0] == 200
    assert radio.app.config.get(st["id"]).privacy == "private"
