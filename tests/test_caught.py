# SPDX-License-Identifier: Apache-2.0
"""Read's radio: a kept band reads like any station, with each line's channel, and one feed,
Caught on the air, holds what was caught of open radio without being kept as a station. Private
and digital radio never enters it, kept or not; another machine reads it as Exports allows."""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.test_pipeline import PLAN
from tests.test_radio_watch import watch_station
from tests.test_store import result
from tests.test_web import request
from wavescribe.config import StationsConfig
from wavescribe.pipeline import Result
from wavescribe.radio.bandplan import apply_policies
from wavescribe.radio.watch import Catch
from wavescribe.segment import Utterance
from wavescribe.store import Store
from wavescribe.web.server import CAUGHT, Handler, WebHub, WebServer

# Made-up channels in bands the plan has (CEPT, in ITU region 1).
TWO_M = 145_337_500  # amateur 2 m
SEVENTY = 438_525_000  # amateur 70 cm
MARINE = 156_625_000  # marine VHF, a ships' channel
PMR = 446_031_250  # PMR446 walkie-talkies, channel 3
DAB = 223_936_000  # digital broadcast
SEQ = itertools.count(1)  # each utterance its own, as a source numbers them


@pytest.fixture(autouse=True)
def default_policies():
    yield
    apply_policies()  # each test starts from the defaults


def said(sid: str, freq: int, t: float, text: str, *, privacy: str, lang: str,
         **translations: str) -> Result:  # fmt: skip
    """Words a watch (or anything that catches) wrote down on ``freq`` at ``t``."""
    r = result(sid, text, t, lang, **translations)
    r.station = replace(r.station, privacy=privacy)
    r.utterance = Utterance(sid, next(SEQ), r.utterance.samples, t, t + 1, t + 1.5, freq_hz=freq)
    return r


def caught(store: Store, sid: str, freq: int, t: float, text: str, *, privacy: str,
           lang: str = "de", band: str = "2m", **translations: str) -> int | None:  # fmt: skip
    """A transmission caught and kept as its class allows, with its words; the words' id."""
    r = said(sid, freq, t + 0.5, text, privacy=privacy, lang=lang, **translations)
    segment = store.add_transcript(r)
    store.add_translations(r)
    store.add_catch(Catch(freq, "nfm", t, t + 3.0, t - 0.3, np.zeros(16_000, np.float32),
                          snr_db=18.0, peak_db=-35.0, station_id=sid, privacy=privacy,
                          band=band, label="", voice=True))  # fmt: skip
    return segment


def serve(store: Store, *stations):
    config = StationsConfig(tuple(stations), Path("t.toml"), radio_region="cept")
    hub = WebHub(config, PLAN, {}, store=store)
    server = WebServer(("127.0.0.1", 0), hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, hub


def get(server: WebServer, path: str):
    status, body = request(server, "GET", path)
    assert status == 200, body
    return json.loads(body)


def test_a_kept_band_reads_like_any_station_each_line_with_its_channel(tmp_path: Path) -> None:
    """A watch kept as a station is a row of its own with its own feed: its lines aren't in
    Caught on the air, and each says the channel it was heard on."""
    apply_policies(None, {"private": {"text_days": 1}})
    walkies = watch_station()  # PMR446, every channel
    store = Store(tmp_path / "w.db")
    now = time.time()
    caught(store, "walkies", PMR, now - 60, "a las ocho en la plaza", privacy="private",
           lang="es", band="pmr446")  # fmt: skip
    server, hub = serve(store, walkies)
    try:
        state = get(server, "/api/state")
        [row] = state["stations"]
        assert row["id"] == "walkies" and row["watch"] == "pmr446" and row["privacy"] == "private"
        [line] = [u for u in state["recent"] if u["station"] == "walkies"]
        assert line["text"] == "a las ocho en la plaza" and "caught" not in line
        assert (line["freq_hz"], line["band"], line["channel"]) == (PMR, "pmr446", 3)
        assert line["label"] == "PMR446 walkie-talkies, channel 3"
        assert state["caught"] == [] and get(server, f"/api/history?station={CAUGHT}") == []
        [same] = get(server, "/api/history?station=walkies")
        assert same["id"] == line["id"] and same["channel"] == 3
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_caught_on_the_air_holds_open_radio_caught_without_being_kept(tmp_path: Path) -> None:
    """What watches nobody kept (a watch run from a terminal, one deleted since) caught of open
    radio, as one feed, each line with where it was heard. Private and digital catches never
    are, even where their class keeps text; neither is a station's own line, frequency or not."""
    apply_policies(None, {"private": {"text_days": 3}, "digital": {"text_days": 3}})
    ham = watch_station(id="ham", watch=[145.2, 145.8], acknowledge=False)
    store = Store(tmp_path / "w.db")
    now = time.time()
    first = caught(store, "watch", TWO_M, now - 600, "Rufzeichen bitte wiederholen",
                   privacy="amateur", en="Please repeat the call sign")  # fmt: skip
    ships = caught(store, "watch", MARINE, now - 500, "vessel leaving the inner harbour",
                   privacy="utility", lang="en", band="marine")  # fmt: skip
    caught(store, "watch", PMR, now - 450, "trae pan", privacy="private", lang="es",
           band="pmr446")  # fmt: skip
    caught(store, "watch", DAB, now - 440, "ruido", privacy="digital", lang="es", band="dab")
    caught(store, "ham", TWO_M, now - 400, "QRZ from the kept band", privacy="amateur",
           lang="en")  # fmt: skip
    gone = caught(store, "old-watch", SEVENTY, now - 300, "il ponte è chiuso",
                  privacy="amateur", lang="it", band="70cm")  # fmt: skip
    # A station's own lines carry its frequency too, and no catch holds them.
    store.add_transcript(said("valley", 92_400_000, now - 200, "il meteo di oggi",
                              privacy="broadcast", lang="it"))  # fmt: skip
    server, hub = serve(store, ham)
    try:
        state = get(server, "/api/state")
        lines = state["caught"]
        assert [u["id"] for u in lines] == [first, ships, gone]  # oldest first
        assert all(u["caught"] is True for u in lines)
        one, two, three = lines
        assert one["translations"] == {"en": "Please repeat the call sign"}
        assert (one["freq_hz"], one["band"], one["label"]) == (TWO_M, "2m", "Amateur 2 m")
        assert (two["band"], two["channel"], two["privacy"]) == ("marine", None, "utility")
        assert (three["station"], three["band"], three["language"]) == ("old-watch", "70cm", "it")
        assert all(u["audio"] is False for u in lines)  # amateur and utility keep no audio
        assert [u["station"] for u in state["recent"]] == ["ham"]  # the kept band's own
        assert get(server, f"/api/history?station={CAUGHT}") == lines
        # Paged back like a station's column, and opened about one line.
        older = get(server, f"/api/history?station={CAUGHT}&before_id={gone}")
        assert [u["id"] for u in older] == [first, ships]
        window = get(server, f"/api/history?station={CAUGHT}&around_id={ships}&limit=2")
        assert [u["id"] for u in window] == [ships, gone]  # as a station's: Store.around
        assert b"trae pan" not in request(server, "GET", "/api/state")[1]
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_another_machine_reads_caught_lines_only_as_exports_allows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page on the LAN, or the feed with a token, reads nothing of a class whose export is
    off. Under Exports' rules, what no configured station heard stays on this machine too."""
    ham = watch_station(id="ham", watch=[145.2, 145.8], acknowledge=False)
    store = Store(tmp_path / "w.db")
    now = time.time()
    caught(store, "watch", TWO_M, now - 600, "Antenne steht", privacy="amateur")
    caught(store, "ham", TWO_M, now - 500, "kept band, kept here", privacy="amateur", lang="en")
    server, hub = serve(store, ham)
    monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
    try:
        state = get(server, "/api/state")
        assert state["stations"] == [] and state["recent"] == [] and state["caught"] == []
        assert get(server, f"/api/history?station={CAUGHT}") == []
        assert get(server, "/api/history?station=ham") == []
        apply_policies(None, {"amateur": {"export": True}})
        state = get(server, "/api/state")
        assert [u["text"] for u in state["recent"]] == ["kept band, kept here"]
        assert state["caught"] == [] and get(server, f"/api/history?station={CAUGHT}") == []
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_a_line_caught_live_comes_flagged_for_its_feed_and_stays_on_this_machine() -> None:
    """New lines reach Caught on the air as they come, on the page's usual live event: a line
    of open radio that no station here heard says ``caught``. Other machines get none of it."""
    ham = watch_station(id="ham", watch=[145.2, 145.8], acknowledge=False)
    hub = WebHub(StationsConfig((ham,), Path("t.toml"), radio_region="cept"), PLAN, {})
    here, there = hub.subscribe(), hub.subscribe(lan=True)
    now = time.time()
    try:
        hub.on_result(said("look-a", MARINE, now, "pilot boarding at the buoy",
                           privacy="utility", lang="en"))  # fmt: skip
        hub.on_result(said("look-a", PMR, now + 1, "ya voy", privacy="private", lang="es"))
        hub.on_result(said("ham", TWO_M, now + 2, "CQ from the hill", privacy="amateur",
                           lang="en"))  # fmt: skip
        events = [json.loads(here.get_nowait()[1]) for _ in range(3)]
        assert [u.get("caught") for u in events] == [True, None, None]
        assert (events[0]["freq_hz"], events[0]["band"]) == (MARINE, "marine")
        assert there.empty()  # neither caught nor kept amateur radio leaves by default
        # Without a store, the page still opens with what this run caught.
        [line] = hub.state()["caught"]
        assert line["text"] == "pilot boarding at the buoy" and line["caught"] is True
        assert hub.history(CAUGHT) == [line] and hub.history(CAUGHT, before_id=5) == []
    finally:
        hub.close()
