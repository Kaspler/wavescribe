# SPDX-License-Identifier: Apache-2.0
"""What each privacy class allows: who may tune it, what is stored, what leaves the machine."""

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tests.test_outputs import ALERT, UTTERANCE, Recorder, wait_for
from tests.test_pipeline import station
from tests.test_store import result
from wavescribe.config import ConfigError, parse_stations
from wavescribe.outputs import OutputConfig, Outputs
from wavescribe.radio.bandplan import (
    DEFAULT_POLICIES,
    POLICIES,
    Policy,
    apply_policies,
    check_policies,
)
from wavescribe.store import Store

P = Path("stations.toml")
EVERYTHING = {"batch_s": 0.05, "events": ["utterance", "alert"], "min_severity": "low"}
DEVICE = {"id": "a", "serial": "WS000001", "center_mhz": 445.9, "gain_db": 30}


def stations(*extra: dict, region: str = "cept") -> dict:
    return {"defaults": {"language": "es-ES"}, "radio": {"region": region}, "device": [DEVICE],
            "station": list(extra)}  # fmt: skip


@pytest.fixture(autouse=True)
def default_policies():
    yield
    apply_policies()  # each test starts from the defaults


def test_digital_services_need_an_acknowledgement_and_warn_of_noise() -> None:
    raw = stations({"id": "d", "mhz": 390.2, "device": "a"})
    raw["device"] = [{**DEVICE, "center_mhz": 390.0}]
    with pytest.raises(ConfigError, match="no digital decoder"):
        parse_stations(raw, P)
    raw["station"][0]["acknowledge"] = True
    d = parse_stations(raw, P).get("d")
    assert (d.privacy, d.sdr.mode) == ("digital", "nfm")  # analogue only: no decoder, no decryption


def test_the_user_decides_what_is_kept() -> None:
    raw = stations({"id": "h", "mhz": 145.5, "device": "a"})
    raw["device"] = [{**DEVICE, "center_mhz": 145.2}]
    raw["radio"]["policy"] = {"amateur": {"text_days": 30, "audio": True}}
    cfg = parse_stations(raw, P)
    apply_policies(
        cfg.radio_policies, {"amateur": {"text_days": "keep"}, "private": {"text_days": 1}}
    )
    assert POLICIES["amateur"] == Policy(False, None, True, False, True)  # the page over the file
    assert POLICIES["private"] == Policy(True, 1, False, False, False)  # still asks first
    apply_policies()
    assert POLICIES == DEFAULT_POLICIES


@pytest.mark.parametrize("bad", [
    {"broadcast": {"text_days": 1}},  # broadcast is kept like any station
    {"private": {"needs_ack": False}},  # the confirmation can't be switched off
    {"private": {"text_days": -1}},
    {"private": {"text_days": "forever"}},
    {"utility": {"audio": "yes"}},
    {"pager": {"audio": True}},
])  # fmt: skip
def test_policy_changes_are_checked(bad: dict) -> None:
    with pytest.raises(ValueError):
        check_policies(bad, "test")
    raw = stations()
    raw["station"] = [{"id": "s", "stream": "http://example.test/s"}]
    raw["radio"]["policy"] = bad
    with pytest.raises(ConfigError):
        parse_stations(raw, P)


def test_a_shorter_retention_removes_what_is_older(tmp_path: Path) -> None:
    import time

    store = Store(tmp_path / "w.db", prune_text_every_s=0)
    ham = replace(result("h", "two days ago", time.time() - 2 * 86400).station, privacy="amateur")

    def add(text: str, age_days: float) -> int | None:
        r = result("h", text, time.time() - age_days * 86400)
        r.station = ham
        return store.add_transcript(r)

    assert add("two days ago", 2) is not None  # amateur: 7 days by default
    apply_policies({"amateur": {"text_days": 1}})
    add("now", 0)
    assert [u["text"] for u in store.recent("h")] == ["now"]
    apply_policies({"amateur": {"text_days": 0}})
    assert add("now again", 0) is None
    assert store.recent("h") == []  # never kept: what was kept before goes too
    store.close()


def test_private_and_utility_radio_need_an_acknowledgement() -> None:
    with pytest.raises(ConfigError, match="acknowledge = true"):
        parse_stations(stations({"id": "p", "mhz": 446.00625, "device": "a"}), P)
    cfg = parse_stations(stations({"id": "p", "mhz": 446.00625, "device": "a",
                                   "acknowledge": True}), P)  # fmt: skip
    p = cfg.get("p")
    assert (p.privacy, p.band, p.sdr.mode) == ("private", "pmr446", "nfm")
    assert cfg.radio_region == "cept"


def test_with_no_region_chosen_the_strictest_class_applies(monkeypatch) -> None:
    """A Mac in Europe set to US English guesses region 2, where 446 MHz is amateur radio."""
    from wavescribe.radio import bandplan

    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert bandplan.privacy_of(446.00625e6, "2") == "amateur"  # chosen: the user said so
    assert bandplan.privacy_of(446.00625e6, bandplan.AUTO) == "private"
    assert bandplan.lookup(446.00625e6, bandplan.AUTO).id == "pmr446"
    assert bandplan.privacy_of(145.5e6, bandplan.AUTO) == "amateur"  # the same everywhere
    raw = stations({"id": "p", "mhz": 446.00625, "device": "a"})
    del raw["radio"]
    with pytest.raises(ConfigError, match="acknowledge = true"):
        parse_stations(raw, P)
    assert [b.id for b in bandplan.region_bands(bandplan.AUTO)] == [
        b.id for b in bandplan.region_bands("2")
    ]  # the list still follows the locale


def test_with_no_region_chosen_unlisted_somewhere_counts_as_private(monkeypatch) -> None:
    from wavescribe.radio import bandplan

    monkeypatch.setattr(bandplan, "default_region", lambda: "cept")
    for mhz in (147.0, 445.0, 162.475, 85.0):  # listed in other regions, not in Europe
        assert bandplan.privacy_of(mhz * 1e6, bandplan.AUTO) == "private", mhz
    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert bandplan.privacy_of(147.0e6, "2") == "amateur"
    assert bandplan.privacy_of(147.0e6, bandplan.AUTO) == "private"  # Europe doesn't list it


def test_with_no_region_chosen_only_fm_stays_broadcast(monkeypatch) -> None:
    """A Mac set to US English may be in Europe, where 162.4-162.55 MHz isn't weather radio."""
    from wavescribe.radio import bandplan

    monkeypatch.setattr(bandplan, "default_region", lambda: "2")
    assert bandplan.privacy_of(162.475e6, "2") == "broadcast"
    assert bandplan.privacy_of(162.475e6, bandplan.AUTO) != "broadcast"
    assert bandplan.privacy_of(98.7e6, bandplan.AUTO) == "broadcast"


def test_with_no_region_chosen_fm_keeps_the_locales_raster(monkeypatch) -> None:
    from wavescribe.radio import bandplan

    monkeypatch.setattr(bandplan, "default_region", lambda: "cept")
    band = bandplan.lookup(98.6e6, bandplan.AUTO)
    assert (band.id, band.privacy) == ("fm", "broadcast")
    assert band.snap(98.6e6) == 98.6e6  # not the Americas' 200 kHz raster (98.5)
    assert bandplan.lookup(90.0e6, bandplan.AUTO).id == "fm"  # not Japan's band


def test_broadcast_and_amateur_need_no_acknowledgement() -> None:
    raw = stations({"id": "h", "mhz": 145.5, "device": "a"})
    raw["device"] = [{**DEVICE, "center_mhz": 145.2}]
    h = parse_stations(raw, P).get("h")
    assert (h.privacy, h.sdr.mode) == ("amateur", "nfm")


def test_the_store_keeps_nothing_from_private_radio(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    store.archive = MagicMock(enabled=True)
    r = result("p", "hola", 1000.0)
    r.station = replace(r.station, privacy="private")
    assert store.add_transcript(r) is None and store.recent("p") == []
    ham = result("h", "cq cq", 1000.0)
    ham.station = replace(ham.station, privacy="amateur")
    assert store.add_transcript(ham) is not None
    store.archive.submit.assert_not_called()  # text for a week, never audio
    store.add_transcript(result("b", "noticias", 1000.0))
    store.archive.submit.assert_called_once()  # broadcast keeps its audio
    store.close()


def test_short_retention_outlives_the_run_and_the_station(tmp_path: Path) -> None:
    import time

    def heard(sid: str, text: str, age_days: float, privacy: str = "amateur"):
        r = result(sid, text, time.time() - age_days * 86400)
        r.station = replace(r.station, privacy=privacy)
        return r

    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")  # as if the run ended before its next prune
    store.add_transcript(heard("h", "old", 10))
    store.add_transcript(heard("h", "new", 0))
    store.add_transcript(heard("b", "old too", 10, "broadcast"))
    store.close()
    # After a restart, with the station deleted or retuned: its rows still go at 7 days.
    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")
    assert store.prune_short_lived() == 1
    assert [u["text"] for u in store.recent("h")] == ["new"]
    assert len(store.recent("b")) == 1  # broadcast: the store's own retention
    # The user shortens amateur radio to 1 day: what is older goes at the next prune.
    store.add_transcript(heard("h", "two days", 2))
    apply_policies({"amateur": {"text_days": 1}})
    assert store.prune_short_lived() == 1
    assert [u["text"] for u in store.recent("h")] == ["new"]
    store.close()


def test_outputs_send_nothing_from_radio_that_stays_here() -> None:
    rec = Recorder()
    st = {"a": station("a"), "p": replace(station("p"), privacy="utility")}
    cfg = {"id": "x", "url": "http://h/in", "batch_s": 0.05, "events": ["utterance", "alert"],
           "min_severity": "low"}  # fmt: skip
    outs = Outputs([OutputConfig.from_dict(cfg)],
                   stations=lambda: st, node=lambda: {"name": "n", "url": "u"}, transport=rec,
                   sleep=lambda s: None)  # fmt: skip
    outs.on_utterance({**UTTERANCE, "station": "p"})
    outs.on_alert({**ALERT, "stations": ["p"]}, "high", "Corte de água", {"en": "Water cut"})
    wait_for(lambda: len(rec.requests) == 1)
    body = rec.json(0)
    sent = rec.requests[0][1].decode()
    assert body["type"] == "alert" and body["rule"] == "water"
    assert "água" not in sent and "Water cut" not in sent and "audio" not in sent
    outs.close()


def test_outputs_send_no_words_they_cannot_place() -> None:
    """Nothing says the words of a station that isn't configured may leave, and words heard
    under another class than the station has now are judged by the class they came with."""
    rec = Recorder()
    outs = Outputs(
        [OutputConfig.from_dict({**EVERYTHING, "id": "x", "url": "http://h/in"})],
        stations=lambda: {"a": station("a")},
        node=lambda: {"name": "n", "url": "u"},
        transport=rec,
        sleep=lambda s: None,
    )
    outs.on_utterance({**UTTERANCE, "station": "gone"})  # deleted since it was heard
    outs.on_utterance({**UTTERANCE, "privacy": "amateur"})  # heard before "a" was retuned
    outs.on_alert({**ALERT, "stations": ["gone"]}, "high", "Corte de água", {"en": "Water cut"})
    outs.on_alert({**ALERT, "privacy": ["amateur"]}, "high", "Corte de água", {"en": "Water cut"})
    wait_for(lambda: len(rec.requests) == 2)
    for i in range(2):
        sent = rec.requests[i][1].decode()
        assert rec.json(i)["type"] == "alert" and rec.json(i)["rule"] == "water"
        assert "água" not in sent and "Water cut" not in sent and "audio" not in sent
    outs.close()


def test_every_row_carries_the_class_it_was_heard_under(tmp_path: Path) -> None:
    import time

    def heard(text: str, age_days: float, privacy: str) -> None:
        r = result("h", text, time.time() - age_days * 86400)
        r.station = replace(r.station, privacy=privacy)
        assert store.add_transcript(r) is not None

    apply_policies({"amateur": {"text_days": "keep"}})  # as long as the store keeps anything
    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")
    heard("on two metres", 30, "amateur")
    heard("the same id on FM since", 30, "broadcast")
    assert [u["privacy"] for u in store.recent("h")] == ["amateur", "broadcast"]
    assert store.prune_short_lived() == 0
    # The user shortens amateur radio: it reaches what was kept while it was unlimited, and
    # not what the same id heard as a broadcast station.
    apply_policies({"amateur": {"text_days": 7}})
    assert store.prune_short_lived() == 1
    assert [u["text"] for u in store.recent("h")] == ["the same id on FM since"]
    store.close()


def test_rows_stored_without_a_class_take_their_stations(tmp_path: Path) -> None:
    import time

    from wavescribe.alerts import Alert

    store = Store(tmp_path / "w.db")
    store._next_text_prune = float("inf")
    then = time.time() - 30 * 86400
    for sid in ("h", "b", "gone"):
        store.add_transcript(result(sid, "kept a month ago", then))
    alert = Alert("water", "high", ["h"], "pt-PT", "kept a month ago", ["kept"], then, then)
    store.add_alert(alert)
    with store._tx() as db:  # as schema 5 left them while amateur radio was set to "keep"
        db.execute("UPDATE segments SET privacy = NULL")
    store.adopt_classes([replace(station("h"), privacy="amateur"), station("b")])
    assert store.recent("h")[0]["privacy"] == "amateur"
    assert store.alert(alert.id)["privacy"] == ["amateur"]
    assert store.recent("b")[0]["privacy"] is None  # a broadcast station: nothing to restrict
    assert store.recent("gone")[0]["privacy"] is None  # deleted since: nobody knows
    assert store.prune_short_lived() == 1  # amateur radio keeps 7 days now
    assert store.recent("h") == [] and store.alert(alert.id)["quote"] == ""
    assert len(store.recent("b")) == 1 and len(store.recent("gone")) == 1
    store.close()


def test_alerts_lose_their_words_when_their_class_stops_keeping_them(tmp_path: Path) -> None:
    import time

    from wavescribe.alerts import Alert

    store = Store(tmp_path / "w.db")

    def alert(privacy: list[str], age_days: float) -> int:
        t = time.time() - age_days * 86400
        a = Alert(
            "water",
            "high",
            ["h"],
            "pt-PT",
            "falta água no repetidor",
            ["água"],
            t,
            t,
            privacy=privacy,
            kind="llm",
            reason="no water at the repeater",
        )
        store.add_alert(a)
        store.add_alert_summaries(a, {"en": "No water at the repeater"}, "fake-mt")
        return a.id

    old_ham, new_ham = alert(["amateur"], 8), alert(["amateur"], 1)
    mixed, old_news = alert(["broadcast", "utility"], 4), alert(["broadcast"], 400)
    store.prune_short_lived()
    blank = store.alert(old_ham)
    assert (blank["quote"], blank["terms"], blank["reason"], blank["summaries"]) == (
        "",
        [],
        None,
        {},
    )
    assert (blank["rule"], blank["severity"], blank["stations"]) == ("water", "high", ["h"])
    assert blank["first_at"] < time.time() - 7 * 86400  # the alert itself stays
    assert store.alert(mixed)["quote"] == ""  # utility keeps 3 days, whoever else said it
    for kept in (new_ham, old_news):
        a = store.alert(kept)
        assert a["quote"] and a["terms"] and a["reason"] and a["summaries"]
    store.close()


def test_alerts_carry_no_words_from_private_radio() -> None:
    from wavescribe.alerts import _said
    from wavescribe.radio.bandplan import UNQUOTED

    r = result("p", "nos vemos a las cinco", 1000.0)
    assert _said(r) == "nos vemos a las cinco"
    r.station = replace(r.station, privacy="private")
    assert _said(r) == UNQUOTED


def test_no_words_from_radio_that_stays_here_in_reasons_or_the_log() -> None:
    from wavescribe.alerts import _reason
    from wavescribe.radio.bandplan import UNQUOTED, logged_text, redact_alert
    from wavescribe.rules import Hit

    hit = Hit("evac", "high", [], [], [], kind="llm", reason="told to leave the harbour")
    r = result("p", "salgan del puerto ahora", 1000.0)
    assert _reason(hit, r.station) == "told to leave the harbour"
    assert logged_text(r.station, r.transcript.text) == "salgan del puerto ahora"
    for privacy in ("amateur", "utility"):  # quotable or not, kept only for days
        assert logged_text(replace(r.station, privacy=privacy), "x") == UNQUOTED
    private = replace(r.station, privacy="private")
    assert _reason(hit, private) is None
    assert redact_alert({"quote": "q", "reason": "told to leave"})["reason"] is None
