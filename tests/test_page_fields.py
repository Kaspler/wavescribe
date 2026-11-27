# SPDX-License-Identifier: Apache-2.0
"""What the page reads from the server instead of working it out: a station's radio in
``/api/state``, where an utterance was heard, the translations of what a watch caught, the codes
beside refusals, and what holds a receiver."""

import json
import time
from pathlib import Path

import pytest

from tests.test_radio_watch_log import TWO_M, catch, words
from tests.test_retune import running, stations, two_metres_centre
from tests.test_store import result
from wavescribe import admin
from wavescribe.radio import service
from wavescribe.radio.bandplan import apply_policies, channel_label
from wavescribe.radio.watch import WatchError, plan_window
from wavescribe.segment import Utterance

SLICE = {"id": "slice", "name": "Slice", "device": "a", "watch": [145.5, 145.6],
         "language": "auto", "languages": ["de-DE", "en"]}  # fmt: skip
FAR = {"id": "far", "name": "Far", "device": "a", "mhz": 432.5}
RPT = {"id": "rpt", "name": "Repeater", "device": "a", "mhz": 145.6}


def test_the_state_says_where_each_station_listens(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre(), SLICE, FAR))
    try:
        status, state = r.call("GET", "/api/state")
        assert status == 200
        by_id = {s["id"]: s for s in state["stations"]}
        keys = ("device", "watch", "languages", "privacy", "out_of_reach")
        assert {k: by_id["stream"][k] for k in keys} == {
            "device": None, "watch": None, "languages": None, "privacy": "broadcast",
            "out_of_reach": None}  # fmt: skip
        assert {k: by_id["ham2m"][k] for k in keys} == {
            "device": "a", "watch": "2m", "languages": ["de-DE"], "privacy": "amateur",
            "out_of_reach": None}  # fmt: skip
        assert by_id["slice"]["watch"] == [145.5, 145.6]
        assert by_id["slice"]["languages"] == ["de-DE", "en"]
        far = by_id["far"]
        assert (far["device"], far["watch"], far["languages"]) == ("a", None, None)
        assert far["out_of_reach"].startswith("Receiver A doesn't hear Far where it is tuned now")
        assert far["out_of_reach"] == r.app.config.get("far").out_of_reach
    finally:
        r.close()


def test_both_views_agree_and_a_station_switched_off_is_in_nobodys_way(tmp_path: Path) -> None:
    any_language = {**SLICE, "id": "anything", "name": "Anything", "languages": []}
    off = {**FAR, "id": "off", "name": "Off"}
    r = running(tmp_path, stations(two_metres_centre(), any_language, FAR, off))
    try:
        r.app.update_station("off", {"disabled": True})  # switched off on the page
        state = {s["id"]: s for s in r.call("GET", "/api/state")[1]["stations"]}
        view = {s["id"]: s for s in r.call("GET", "/api/manage/stations")[1]["stations"]}
        for sid in ("anything", "ham2m", "far", "stream"):
            assert view[sid]["languages"] == state[sid]["languages"], sid
        assert state["anything"]["languages"] == []  # auto, narrowed to nothing: any language
        assert state["far"]["out_of_reach"] and state["off"]["out_of_reach"] is None
        assert r.call("DELETE", "/api/manage/stations/nowhere") == (
            404, {"error": "no station 'nowhere'", "code": "not_found"})  # fmt: skip
        assert r.call("POST", "/api/manage/stations/off/start", {}) == (
            400, {"error": "switched off on the page", "code": "switched_off"})  # fmt: skip
        radio = r.call("GET", "/api/manage/radio")[1]
        listed = {s["id"]: s for rx in radio["receivers"] for s in rx["stations"]}
        receivers = r.call("GET", "/api/manage/receivers")[1]
        listed2 = {s["id"]: s for d in receivers["devices"] for s in d["stations"]}
        for lists in (listed, listed2):  # one rule with /api/state: off is in nobody's way
            assert lists["far"]["out_of_reach"] and lists["off"]["out_of_reach"] is None
    finally:
        r.close()


def heard_on(r, sid: str, text: str, freq_hz: int | None = None, **translations: str):
    """An utterance of station ``sid`` as the running app hears it: stored, then final."""
    res = result(sid, text, time.time() - 30, "de-DE", **translations)
    res.station = r.app.config.get(sid)
    u = res.utterance
    res.utterance = Utterance(sid, 1, u.samples, u.t_start, u.t_end, u.ready_at, freq_hz=freq_hz)
    r.app.hub.store_transcript(res)
    r.app.store.add_translations(res)
    r.app.hub.on_result(res)
    return res


def test_an_utterance_says_the_channel_it_was_heard_on(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre(), RPT))
    q = r.app.hub.subscribe()
    try:
        heard_on(r, "ham2m", "CQ CQ from the hill", 145_500_000)
        heard_on(r, "rpt", "Guten Morgen")
        heard_on(r, "stream", "Nachrichten")
        live = {}
        while len(live) < 3:
            event, data = q.get(timeout=5)
            if event == "utterance":
                u = json.loads(data)
                live[u["station"]] = (u["freq_hz"], u["label"])
                assert u["band"] == ("2m" if u["freq_hz"] else None) and u["channel"] is None
        assert live == {"ham2m": (145_500_000, "Amateur 2 m"),
                        "rpt": (145_600_000, "Amateur 2 m"), "stream": (None, None)}  # fmt: skip
        for sid, where in live.items():
            status, [row] = r.call("GET", f"/api/history?station={sid}")
            assert status == 200 and (row["freq_hz"], row["label"]) == where
            status, [row] = r.call("GET", f"/api/history?station={sid}&around_id={row['id']}")
            assert (row["freq_hz"], row["label"]) == where
        state = r.call("GET", "/api/state")[1]
        assert {u["station"]: (u["freq_hz"], u["label"]) for u in state["recent"]} == live
        # A channel with a number in its band's list is named with it, as catches name it.
        assert channel_label(446_006_250, "cept") == "PMR446 walkie-talkies, channel 1"
        assert channel_label(None, "cept") is None
        # Beside it, the band's id and the number, so the page needn't read the words.
        from wavescribe.radio.bandplan import channel_of, channel_place
        from wavescribe.radio.scan import Signal

        assert channel_place(446_006_250, "cept") == (
            "PMR446 walkie-talkies, channel 1", "pmr446", 1)  # fmt: skip
        assert channel_of("pmr446", 446_093_750) == 8 and channel_of("gone", 1e8) is None
        row = Signal(446_018_750, -40.0, 20.0, 1e4, band="pmr446", label="x").to_dict()
        assert row["channel"] == 2
    finally:
        r.app.hub.unsubscribe(q)
        r.close()


def test_what_a_watch_caught_comes_with_its_translations_where_its_words_may_go(
    tmp_path: Path,
) -> None:
    r = running(tmp_path, stations(two_metres_centre()))
    store = r.app.store
    now = time.time() - 60
    try:
        said = words("ham2m", TWO_M, now + 0.5, "CQ CQ vom Berg")
        said.translations.update(en="CQ CQ from the hill", fr="CQ CQ de la colline")
        said.translations_model = "fake-mt"
        store.add_transcript(said)
        store.add_translations(said)
        # Heard on the same channel within the catch, but as a stricter class (before a change
        # of region): its words, and so its translations, stay where that class's do.
        strict = words("ham2m", TWO_M, now + 1.5, "Bitte kommen", privacy="utility")
        strict.translations.update(en="Please come in")
        strict.translations_model = "fake-mt"
        store.add_transcript(strict)
        store.add_translations(strict)
        store.add_catch(catch(TWO_M, now, station="ham2m"))

        status, body = r.call("GET", "/api/catches")
        assert status == 200
        [row] = body["catches"]
        assert [(u["text"], u["translations"]) for u in row["utterances"]] == [
            ("CQ CQ vom Berg", {"en": "CQ CQ from the hill", "fr": "CQ CQ de la colline"}),
            ("Bitte kommen", {"en": "Please come in"}),
        ]
        # Another machine: nothing of amateur radio by default, and with amateur let out, the
        # amateur words with their translations and nothing of the stricter class.
        assert store.catches(exports=r.app.hub.exports()) == []
        apply_policies(None, {"amateur": {"export": True}})
        [shared] = store.catches(exports=r.app.hub.exports())
        assert [(u["text"], u["translations"]) for u in shared["utterances"]] == [
            ("CQ CQ vom Berg", {"en": "CQ CQ from the hill", "fr": "CQ CQ de la colline"})
        ]
        # A class that keeps no text keeps no words, so no translations either.
        apply_policies(None, {"amateur": {"text_days": 0}})
        lost = words("ham2m", TWO_M, now + 2.5, "Ende", privacy="amateur")
        lost.translations.update(en="Out")
        assert store.add_transcript(lost) is None
        store.add_translations(lost)
        assert store._db.execute("SELECT count(*) FROM translations").fetchone()[0] == 3
    finally:
        apply_policies()
        r.close()


def test_adding_a_station_its_receiver_doesnt_hear_asks_as_a_tune_does(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre()))
    before = r.app.settings.snapshot()
    try:
        far = {"name": "Far", "device": "a", "mhz": 432.5, "start": True}
        status, ask = r.call("POST", "/api/manage/stations", far)
        assert status == 400 and ask["code"] == "retune", ask
        assert ask["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False}]
        assert ask["holders"] == [] and ask["center_mhz"] == 432.25
        assert ask["error"] == (
            "Receiver A moves to 432.25 MHz to hear Far, where it no longer hears Two metres. "
            "It isn't running now. It stays in your stations, unavailable until the receiver "
            "hears it again."
        )
        assert r.app.settings.snapshot() == before  # nothing changed while it asks
        assert r.app.config.devices[0].center_hz == round(two_metres_centre() * 1e6)

        status, added = r.call("POST", "/api/manage/stations", {**far, "retune": True})
        assert status == 200, added
        assert added["name"] == "Far" and added["available"] is True
        assert added["start_error"] is None and added["live_closed"] is False
        assert added["id"] in r.app.settings.get("run")["stations"]  # started, as asked
        assert added["receiver"] == {"id": "a", "center_mhz": 432.25, "moved": True}
        assert added["unfit"] == [{"id": "ham2m", "name": "Two metres", "running": False}]
        assert r.app.config.devices[0].center_hz == 432_250_000
        assert r.app.unavailable["ham2m"].startswith("Receiver A doesn't hear all of Amateur 2 m")
    finally:
        r.close()


def test_a_watch_its_receiver_doesnt_hear_moves_it_and_asks_for_what_that_stops(
    tmp_path: Path,
) -> None:
    fm = {"id": "fm", "name": "Morning FM", "device": "a", "fm_mhz": 98.5}
    raw = stations(98.0, fm)
    raw["station"] = [s for s in raw["station"] if s["id"] != "ham2m"]
    r = running(tmp_path, raw)
    real = r.app.running
    r.app.running = lambda: real() | {"fm"}  # type: ignore[method-assign]
    try:
        watch = {"name": "Two metres", "device": "a", "watch": "2m", "language": "auto"}
        status, ask = r.call("POST", "/api/manage/stations", watch)
        assert status == 400 and ask["code"] == "retune", ask
        assert ask["holders"] == ["Morning FM"]
        assert ask["unfit"] == [{"id": "fm", "name": "Morning FM", "running": True}]
        assert ask["center_mhz"] == two_metres_centre()
        assert ask["error"].startswith("Receiver A moves to about 145.006 MHz to hear Two metres")
        assert "Retuning it stops Morning FM." in ask["error"]
        assert all(s.name != "Two metres" for s in r.app.config.stations)

        status, err = r.call("POST", "/api/manage/stations", {**watch, "retune": "yes"})
        assert status == 400 and err["error"] == "retune must be true or false"
        status, added = r.call("POST", "/api/manage/stations", {**watch, "retune": True})
        assert status == 200 and added["available"] is True, added
        assert r.app.config.devices[0].center_hz == round(two_metres_centre() * 1e6)
        assert "Receiver A doesn't hear Morning FM" in r.app.unavailable["fm"]
    finally:
        r.close()


def test_a_watch_too_wide_says_so_with_the_slice_that_fits(tmp_path: Path) -> None:
    with pytest.raises(WatchError) as said:
        plan_window("marine", "cept", rate=2_400_000)
    r = running(tmp_path, stations(two_metres_centre()))
    try:
        body = {"name": "Ships", "device": "a", "watch": "marine", "language": "auto"}
        status, err = r.call("POST", "/api/manage/stations", body)
        assert status == 400 and err["code"] == "too_wide"
        assert err["error"] == f"station 'ships' (ships): {said.value}"  # the words as they were
        assert err["slice_mhz"] == [156.0, 157.425]
        assert "e.g. 156.000:157.425 (58 channels)" in err["error"]
        assert all(s.id != "ships" for s in r.app.config.stations)
    finally:
        r.close()


def test_other_refusals_of_a_watch_or_a_tune_have_a_code(tmp_path: Path) -> None:
    r = running(tmp_path, stations(two_metres_centre(), RPT))
    try:
        watch = {"name": "W", "device": "a", "language": "auto"}
        station = {"name": "S", "device": "a", "language": "de-DE"}
        for body, code, words in [
            ({**watch, "watch": "fm"}, "bad_mode", "a watch listens for narrowband voice"),
            ({**watch, "watch": "nowhere"}, "unknown_band", "no band 'nowhere' in the band plan"),
            ({**watch, "watch": [146.0, 145.0]}, "bad_window", "the start must be below the stop"),
            ({**watch, "watch": "2m", "threshold_db": 1}, "bad_threshold", "'threshold_db' must"),
            ({**station, "mhz": 145.5, "mode": "ssb"}, "bad_mode", "'mode' must be one of"),
            ({**station, "mhz": 145.5, "channel_khz": 1}, "bad_channel", "'channel_khz' must"),
            ({**station, "mhz": "abc"}, "bad_frequency", "must be a frequency in MHz"),
            ({**station, "mhz": 145.5, "device": "z"}, "no_receiver", "unknown device 'z'"),
        ]:
            status, err = r.call("POST", "/api/manage/stations", body)
            assert status == 400 and err["code"] == code and words in err["error"], (body, err)
        status, err = r.call("POST", "/api/manage/stations", {**station, "mhz": 446.00625})
        assert status == 400 and err["code"] == "acknowledge" and err["privacy"] == "private"
        assert err["error"].endswith("Set acknowledge = true to confirm you may receive it where "
                                     "you are (docs/RADIO.md)")  # fmt: skip
        # A change that would tune a station where its receiver doesn't hear it.
        status, err = r.call("POST", "/api/manage/stations/rpt", {"mhz": 432.5})
        assert status == 400 and err["code"] == "out_of_reach"
        assert err["error"].startswith("Receiver A doesn't hear Repeater where it is tuned now")

        tune = {"device": "a", "language": "de-DE"}
        for body, code, words in [
            ({**tune, "mhz": 2000}, "out_of_band", "The receiver tunes 24 to 1766 MHz, no higher."),
            ({**tune, "mhz": "abc"}, "bad_frequency", "'abc' isn't one: give a frequency in MHz"),
            ({**tune, "mhz": 99.5, "device": "z"}, "no_receiver", "There is no receiver 'z'."),
            ({**tune, "mhz": 99.5, "mode": "ssb"}, "refused", "'mode' must be one of"),  # as ever
        ]:
            status, err = r.call("POST", "/api/manage/radio/tune", body)
            assert status == 400 and err["code"] == code and words in err["error"], (body, err)
        assert [s.id for s in r.app.config.stations] == ["stream", "ham2m", "rpt"]
    finally:
        r.close()


def test_the_radio_view_says_the_country_where_a_region_is_one(tmp_path: Path) -> None:
    r = running(tmp_path, stations(98.0))
    try:
        view = r.call("GET", "/api/manage/radio")[1]
        assert (view["region"], view["region_source"], view["country"]) == ("cept", "config", None)
        view = r.call("POST", "/api/manage/radio/region", {"region": "jp"})[1]
        assert (view["region"], view["country"]) == ("jp", "JP")
        view = r.call("POST", "/api/manage/radio/region", {"region": "2"})[1]
        assert (view["region"], view["country"]) == ("2", None)  # the Americas: no one country
    finally:
        r.close()


def test_each_receiver_says_in_a_word_what_holds_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service.holders, "held_elsewhere", dict)  # never this Mac's dongles
    r = running(tmp_path, stations(two_metres_centre(), RPT))
    radio = r.app.radio
    held: set[str] = set()
    real = r.app.running
    r.app.running = lambda: real() | held  # type: ignore[method-assign]

    def code() -> str | None:
        radio._held = None  # read who holds the dongles afresh
        [rx] = r.call("GET", "/api/manage/radio")[1]["receivers"]
        assert rx["serial"] == "SN0042"  # in full here: Settings matches a stick to a letter
        return rx["busy_code"]

    class ByHand:
        def is_open(self, device_id: str) -> bool:
            return device_id == "a"

    try:
        assert code() is None
        held.add("rpt")
        assert code() == "station"
        held.add("ham2m")
        assert code() == "watch"
        held.clear()
        monkeypatch.setitem(admin._monitors, "a", object())
        assert code() == "monitor"
        monkeypatch.delitem(admin._monitors, "a")
        r.app._live = ByHand()
        held.add("rpt")
        assert code() == "live"
        radio._job = {"device": "a", "phase": "sweep"}
        assert code() == "scan"
        r.app._live, radio._job = None, None
        held.clear()
        other = {"pid": 1234, "program": "rtl_tcp", "other_wavescribe": False}
        monkeypatch.setattr(service.holders, "held_elsewhere", lambda: {"SN0042": other})
        assert code() == "other_program"
    finally:
        r.app._live = None
        r.close()


def test_every_view_of_a_station_says_where_its_receiver_hears_it_again(tmp_path: Path) -> None:
    off = {**FAR, "id": "off", "name": "Off"}
    r = running(tmp_path, stations(98.0, FAR, off, {**RPT, "mhz": 98.5}))
    try:
        r.app.update_station("off", {"disabled": True})
        expected = {"stream": None, "rpt": None, "off": None,  # heard, a stream, switched off
                    "far": 432.25, "ham2m": two_metres_centre()}  # fmt: skip
        state = r.call("GET", "/api/state")[1]["stations"]
        assert {s["id"]: s["reach_center_mhz"] for s in state} == expected
        view = r.call("GET", "/api/manage/stations")[1]["stations"]
        assert {s["id"]: s["reach_center_mhz"] for s in view} == expected
        assert {s["id"]: s["out_of_reach"] for s in view} == {
            s["id"]: s["out_of_reach"] for s in state}  # fmt: skip
        [rx] = r.call("GET", "/api/manage/radio")[1]["receivers"]
        [dev] = r.call("GET", "/api/manage/receivers")[1]["devices"]
        on_rx = {k: v for k, v in expected.items() if k != "stream"}
        assert {s["id"]: s["reach_center_mhz"] for s in rx["stations"]} == on_rx
        assert {s["id"]: s["reach_center_mhz"] for s in dev["stations"]} == on_rx

        # It is where starting the station moves the receiver, asking first if that leaves
        # others out (the repeater here), whatever the others.
        status, ask = r.call("POST", "/api/manage/stations/far/start")
        assert status == 400 and ask["center_mhz"] == 432.25 and ask["holders"] == []
        status, ask = r.call("POST", "/api/manage/stations/ham2m/start")
        assert status == 400 and ask["center_mhz"] == two_metres_centre()
    finally:
        r.close()


def test_where_a_station_is_heard_again_is_where_starting_it_moves_the_receiver(
    tmp_path: Path,
) -> None:
    """The usual place for 99.5 (99.2) would lose 98.1; a place a little further down hears
    both, so that is the centre the views give, and starting the station goes there unasked."""
    from dataclasses import replace

    from wavescribe.config import heard_by

    near = {"id": "near", "name": "Near", "device": "a", "mhz": 98.1}
    up = {"id": "up", "name": "Up", "device": "a", "mhz": 99.5}
    raw = stations(98.0, near, up)
    raw["station"] = [s for s in raw["station"] if s["id"] != "ham2m"]
    r = running(tmp_path, raw)
    try:
        [s] = [s for s in r.call("GET", "/api/state")[1]["stations"] if s["id"] == "up"]
        centre = s["reach_center_mhz"]
        assert centre is not None and 98.9 < centre < 99.2  # not 99.2: that loses Near
        d = r.app.config.devices[0]
        at = replace(d, center_hz=round(centre * 1e6))
        assert all(heard_by(x, at) for x in r.app.config.stations if x.sdr)
        status, moved = r.call("POST", "/api/manage/stations/up/start")
        assert status == 200 and moved["unfit"] == [], moved
        assert moved["receiver"] == {"id": "a", "center_mhz": centre, "moved": True}
    finally:
        r.close()


def test_the_add_forms_get_the_stations_files_defaults(tmp_path: Path) -> None:
    raw = stations(98.0)
    r = running(tmp_path, raw)
    try:
        said = {"language": "de-DE", "translate_to": ["en"]}  # the file's [defaults]
        assert r.call("GET", "/api/manage/stations")[1]["defaults"] == said
        assert r.call("GET", "/api/manage/radio")[1]["defaults"] == said
    finally:
        r.close()
    del raw["defaults"]
    for s in raw["station"]:
        s["language"] = "de-DE"
    (tmp_path / "bare").mkdir()
    r = running(tmp_path / "bare", raw)
    try:
        none = {"language": None, "translate_to": []}
        assert r.call("GET", "/api/manage/stations")[1]["defaults"] == none
    finally:
        r.close()


def test_every_view_of_a_receiver_gives_what_it_takes_in_as_lo_and_hi(tmp_path: Path) -> None:
    raw = stations(98.0)
    raw["station"] = [s for s in raw["station"] if s["id"] != "ham2m"]  # wider than 1.92 MS/s
    r = running(tmp_path, raw)
    try:
        [rx] = r.call("GET", "/api/manage/radio")[1]["receivers"]
        [dev] = r.call("GET", "/api/manage/receivers")[1]["devices"]
        tuner = r.call("GET", "/api/manage/live/a")[1]
        assert rx["span_mhz"] == dev["span_mhz"] == tuner["span_mhz"] == [96.8, 99.2]
        status, moved = r.call("POST", "/api/manage/receivers/a",
                               {"center_mhz": 145.00625, "sample_rate": 1_920_000})  # fmt: skip
        assert status == 200 and moved["span_mhz"] == [144.04625, 145.96625], moved
        [rx] = r.call("GET", "/api/manage/radio")[1]["receivers"]
        assert rx["span_mhz"] == moved["span_mhz"]
    finally:
        r.close()


def test_the_models_say_which_can_tell_languages_apart(tmp_path: Path) -> None:
    from dataclasses import replace

    from wavescribe.registry import ModelEntry

    anything = {"id": "anything", "name": "Anything", "stream": "http://example.test/x",
                "language": "auto"}  # fmt: skip
    raw = stations(98.0, anything)
    raw["station"] = [s for s in raw["station"] if s["id"] != "ham2m"]
    r = running(tmp_path, raw)
    whisper = ModelEntry("whisper-x", "asr", "mlx-audio", "url:x", "u", "MIT", "u", "default",
                         False, ("*",), "")  # fmt: skip
    english = replace(whisper, id="whisper-en", source="url:en")
    models = tmp_path / "node" / "models"
    for entry, vocab in ((whisper, 51_866), (english, 51_864), (replace(whisper, id="y"), None)):
        r.app.registry.add(entry)
        if vocab is None:
            continue  # not downloaded: what it can tell isn't known yet
        (models / entry.id).mkdir()
        (models / entry.id / entry.location).write_bytes(b"")
        config = {"model_type": "whisper", "n_vocab": vocab}
        (models / entry.id / "config.json").write_text(json.dumps(config))
    try:
        status, view = r.call("GET", "/api/manage/models")
        assert status == 200
        detects = {m["id"]: m["detects_languages"] for m in view["models"]}
        # Only a multilingual Whisper on disk can be the model of a station in language auto;
        # the speech detector and the translator ("m" plays all three here) can't.
        assert detects == {"m": False, "whisper-x": True, "whisper-en": False, "y": False}
        status, err = r.call("POST", "/api/manage/stations/anything", {"asr_model": "whisper-en"})
        assert status == 400 and err["code"] == "cannot_detect", err
        assert err["error"].startswith("Anything can't be transcribed yet: whisper-en can't tell")
        status, err = r.call("POST", "/api/manage/stations/anything", {"asr_model": "m"})
        assert status == 400 and err["code"] == "cannot_detect", err
        status, ok = r.call("POST", "/api/manage/stations/anything", {"asr_model": "whisper-x"})
        assert status == 200 and ok["asr_choice"] == "whisper-x", ok
    finally:
        r.close()
