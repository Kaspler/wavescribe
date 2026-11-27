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
