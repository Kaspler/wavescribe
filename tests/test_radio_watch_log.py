# SPDX-License-Identifier: Apache-2.0
"""The catch log: what watches caught, kept and shared exactly as their class's words are,
notes on frequencies, and the command line."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.test_agent import CATCH_KEYS
from tests.test_radio_watch import watch_station
from tests.test_radio_watch_detect import PMR
from tests.test_store import result
from wavescribe.radio import cli as radio_cli
from wavescribe.radio.bandplan import apply_policies, exports
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.watch import Catch
from wavescribe.segment import Utterance
from wavescribe.store import Store

TWO_M = 145_500_000  # the 2 m calling channel
RX = radio_cli.Receiver("00001234", 30.0, 0, "receiver A")


@pytest.fixture(autouse=True)
def default_policies():
    yield
    apply_policies()  # each test starts from the defaults


def catch(freq: int, t: float, *, privacy: str = "amateur", station: str = "walkies") -> Catch:
    return Catch(
        freq,
        "nfm",
        t,
        t + 3.0,
        t - 0.3,
        np.zeros(16_000, np.float32),
        snr_db=21.5,
        peak_db=-30.0,
        station_id=station,
        privacy=privacy,
        band="2m",
        label="Amateur 2 m",
        voice=True,
    )


def words(station_id: str, freq: int, t: float, text: str, privacy: str = "amateur"):
    """An utterance a watch transcribed on ``freq`` at ``t``."""
    r = result(station_id, text, t)
    r.station = replace(r.station, privacy=privacy)
    r.utterance = Utterance(station_id, 1, r.utterance.samples, t, t + 1, t + 1.5, freq_hz=freq)
    return r


def test_a_catch_is_kept_with_the_words_heard_in_it(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    now = time.time()
    store.add_transcript(words("walkies", TWO_M, now + 0.5, "CQ CQ from the hill"))
    store.add_transcript(words("walkies", TWO_M + 12_500, now + 0.7, "the next channel"))
    store.add_transcript(words("walkies", TWO_M, now + 60, "a later over"))
    c = catch(TWO_M, now)
    assert store.add_catch(c) == c.id is not None
    [row] = store.catches()
    assert row["freq_hz"] == TWO_M and row["mhz"] == 145.5 and row["duration"] == 3.0
    assert row["voice"] is True and row["privacy"] == "amateur" and row["label"] == "Amateur 2 m"
    assert [u["text"] for u in row["utterances"]] == ["CQ CQ from the hill"]
    assert store.catches(freq_hz=TWO_M + 12_500) == []
    assert len(store.catches(freq_hz=TWO_M + 10_000, width_hz=12_500)) == 1
    store.close()


def test_a_class_that_keeps_no_text_keeps_no_catch(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db")
    assert store.add_catch(catch(PMR[0], time.time(), privacy="private")) is None
    assert store.add_catch(catch(PMR[0], time.time(), privacy="digital")) is None
    assert store.catches() == []
    store.close()


def test_catches_age_out_with_their_class_and_the_store(tmp_path: Path) -> None:
    store = Store(tmp_path / "w.db", max_text_age_days=30)
    now = time.time()
    store.add_catch(catch(TWO_M, now - 8 * 86400))  # amateur keeps text 7 days
    store.add_catch(catch(TWO_M, now - 86400))
    store.add_catch(catch(156_800_000, now - 4 * 86400, privacy="utility"))  # utility: 3 days
    store.add_catch(catch(156_800_000, now - 2 * 86400, privacy="utility"))
    store.prune_catches()
    assert sorted(r["t_start"] for r in store.catches()) == [now - 2 * 86400, now - 86400]
    apply_policies(None, {"amateur": {"text_days": "keep"}})  # as long as the store keeps text
    store.add_catch(catch(TWO_M, now - 40 * 86400))
    store.add_catch(catch(TWO_M, now - 20 * 86400))
    store.prune_catches()
    assert len(store.catches(station_ids=["walkies"])) == 3  # past the store's 30 days: gone
    store.close()


def test_what_leaves_the_machine_is_what_its_class_and_station_allow(tmp_path: Path) -> None:
    """A non-local request sees no catch, and no word in one, that an ordinary station of the
    same class wouldn't show it."""
    apply_policies(None, {"private": {"text_days": 3}, "amateur": {"export": True}})
    walkies = watch_station()
    ham = replace(walkies, id="ham", privacy="amateur")
    store = Store(tmp_path / "w.db")
    now = time.time()
    store.add_transcript(words("ham", TWO_M, now + 0.5, "QSL, 73"))
    store.add_transcript(words("walkies", PMR[0], now + 0.5, "come home", privacy="private"))
    store.add_catch(catch(TWO_M, now, station="ham"))
    store.add_catch(catch(PMR[0], now, privacy="private"))
    assert len(store.catches()) == 2  # all of it, here
    out = exports([ham, walkies])
    shared = store.catches(exports=out)
    assert [(r["station"], [u["text"] for u in r["utterances"]]) for r in shared] == [
        ("ham", ["QSL, 73"])
    ]
    # A station no longer configured takes nothing of its own out either.
    assert store.catches(exports=exports([walkies])) == []
    # Nor does an ordinary private station's text: the same filter, the same answer.
    assert all(u["station"] != "walkies" for u in store.window(exports=out))
    store.close()


def test_opens_a_version_6_database_and_adds_the_catch_log(tmp_path: Path) -> None:
    Store(tmp_path / "w.db").close()
    old = sqlite3.connect(tmp_path / "w.db")  # make it look like a schema-6 database
    old.execute("DROP TABLE catches")
    old.execute("ALTER TABLE segments DROP COLUMN freq_hz")
    old.execute(
        "INSERT INTO segments (station_id, t_start, t_end, privacy) "
        "VALUES ('a', 1000, 1001, 'broadcast')"
    )
    old.execute("PRAGMA user_version = 6")
    old.commit()
    old.close()
    store = Store(tmp_path / "w.db")
    assert store.catches() == []
    store.add_catch(catch(TWO_M, time.time()))
    assert len(store.catches()) == 1
    store.close()
    db = sqlite3.connect(tmp_path / "w.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7
    assert db.execute("SELECT freq_hz FROM segments").fetchall() == [(None,)]
    db.close()


# -- notes on frequencies ----------------------------------------------------------------------


def test_a_note_names_a_frequency_wherever_it_is_shown(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path)
    catalog._write({"version": 1, "entries": {str(PMR[0]): {"freq_hz": PMR[0], "mhz": 446.00625}}})
    noted = catalog.note(PMR[0], name="family", note="the kids' walkie-talkies", tags=["home"])
    assert noted == {
        "freq_hz": PMR[0],
        "name": "family",
        "note": "the kids' walkie-talkies",
        "tags": ["home"],
        "t": noted["t"],
    }
    assert catalog.entries()[0]["user_name"] == "family"  # the heard catalogue's name too
    catalog.name(PMR[0], "hiking")
    assert catalog.note_for(PMR[0])["name"] == "hiking"
    catalog.note(PMR[0], note="")  # a field cleared, the rest kept
    assert catalog.note_for(PMR[0])["tags"] == ["home"] and "note" not in catalog.note_for(PMR[0])
    catalog.note(PMR[1], name="x")
    catalog.note(PMR[1], name="")
    assert PMR[1] not in catalog.notes()  # nothing left to say: gone
    with pytest.raises(ValueError, match="at most 80"):
        catalog.note(PMR[2], name="x" * 81)
    with pytest.raises(ValueError, match="at most 80"):
        catalog.name(PMR[0], "x" * 81)  # the heard list's own name route too


def test_notes_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wavescribe.radio import catalog as catalogue

    monkeypatch.setattr(catalogue, "MAX_NOTES", 2)
    catalog = Catalog(tmp_path)
    catalog.note(PMR[0], name="a")
    catalog.note(PMR[1], name="b")
    with pytest.raises(ValueError, match="at most 2 frequencies"):
        catalog.note(PMR[2], name="c")
    catalog.note(PMR[1], tags=["still", "changeable"])  # one already noted can change


# -- the command line --------------------------------------------------------------------------


def test_radio_catches_prints_the_log_and_its_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wavescribe import cli
    from wavescribe.radio import watch_cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path / "data")
    args = argparse.Namespace(mhz=None, width=0.0, hours=24.0, limit=100, json=False)
    watch_cli.cmd_catches(args)
    assert "Nothing caught and kept" in capsys.readouterr().out
    store = Store(tmp_path / "data" / "wavescribe.db")
    now = time.time()
    store.add_catch(catch(TWO_M, now - 3 * 86_400))
    watch_cli.cmd_catches(args)
    older = capsys.readouterr().out
    assert "nothing in the last 24 hours" in older and "--hours 0 shows them all" in older
    store.add_transcript(words("walkies", TWO_M, now + 0.5, "CQ CQ"))
    store.add_catch(catch(TWO_M, now))
    store.add_catch(replace(catch(TWO_M + 25_000, now + 10), voice=None))  # listened to only
    store.close()
    Catalog(tmp_path / "data" / "radio").note(TWO_M, name="calling channel")
    watch_cli.cmd_catches(args)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3  # the one from three days ago is outside the 24 hours
    assert "145.5" in lines[0] and '"calling channel"' in lines[0] and "voice" in lines[0]
    assert lines[1].split() == ["pt-PT", "CQ", "CQ"]
    assert "145.525" in lines[2] and lines[2].endswith("not checked")
    watch_cli.cmd_catches(argparse.Namespace(**{**vars(args), "json": True}))
    row = json.loads(capsys.readouterr().out.splitlines()[1])
    assert set(row) == CATCH_KEYS | {"name"}  # and note and tags, when noted
    assert row["name"] == "calling channel" and row["utterances"][0]["text"] == "CQ CQ"


def test_radio_watch_plans_the_window_and_writes_nothing_down_for_private_radio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wavescribe import cli
    from wavescribe.cli import build_parser
    from wavescribe.radio import cli as radio_cli

    monkeypatch.chdir(tmp_path)
    ran: list[argparse.Namespace] = []
    monkeypatch.setattr(cli, "cmd_listen", lambda a: ran.append(a) or 0)
    monkeypatch.setattr(radio_cli, "_config", lambda _a: None)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda _n, _c: RX)
    argv = ["radio", "watch", "a", "pmr446", "--region", "cept", "--minutes", "10"]
    args = build_parser().parse_args(argv)
    with pytest.raises(ValueError, match="Pass --acknowledge to confirm") as e:
        args.func(args)
    assert "acknowledge = true" not in str(e.value)  # the stations file's key, not this
    args = build_parser().parse_args([*argv, "--acknowledge", "--languages", "pt-PT", "es"])
    assert args.func(args) == 0
    [a] = ran
    [s] = a.tuned_stations
    assert s.language == "auto" and s.languages == ("pt-PT", "es") and s.privacy == "private"
    assert s.sdr.device.center_hz == PMR[0] - 250_000 and s.sdr.watch.channels == PMR
    assert a.no_store and a.duration == 600 and a.source is None
    err = capsys.readouterr().err
    assert "16 channels" in err and "nothing written down" in err and "00001234" not in err


def test_radio_watch_says_when_the_receivers_spur_list_is_too_coarse_to_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wavescribe import cli
    from wavescribe.cli import build_parser
    from wavescribe.radio import cli as radio_cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "cmd_listen", lambda _a: 0)
    monkeypatch.setattr(radio_cli, "_config", lambda _a: None)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda _n, _c: RX)
    (tmp_path / "data" / "radio").mkdir(parents=True)
    old = {"00001234": {"t": 0, "freqs_hz": [PMR[3]]}}  # no bin_hz: an earlier version's
    (tmp_path / "data" / "radio" / "spurs.json").write_text(json.dumps(old))
    argv = ["radio", "watch", "a", "pmr446", "--region", "cept", "--acknowledge"]
    args = build_parser().parse_args(argv)
    assert args.func(args) == 0
    assert "learn them again with `wavescribe radio spurs a --learn`" in capsys.readouterr().err


def test_radio_watch_says_when_the_watch_goes_deaf_and_hears_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wavescribe import cli
    from wavescribe.cli import build_parser
    from wavescribe.radio import cli as radio_cli
    from wavescribe.radio import watch as watches

    def listen(_args: argparse.Namespace) -> int:  # what the watch says while it runs
        watches.publish(watches.Hearing("watch", 1_000.0, "deaf: noise across the whole window"))
        watches.publish(watches.Hearing("watch", 1_017.0, None, deaf_s=17.2, learnt=True))
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "cmd_listen", listen)
    monkeypatch.setattr(radio_cli, "_config", lambda _a: None)
    monkeypatch.setattr(radio_cli, "resolve_receiver", lambda _n, _c: RX)
    argv = ["radio", "watch", "a", "pmr446", "--region", "cept", "--acknowledge"]
    args = build_parser().parse_args(argv)
    assert args.func(args) == 0
    err = capsys.readouterr().err
    assert "] deaf: noise across the whole window\n" in err
    assert "] hearing again after 17 s, with that noise as the new floor\n" in err
    watches.publish(watches.Hearing("watch", 1_020.0, "deaf: again"))  # once it has stopped
    assert "again" not in capsys.readouterr().err


# -- the routes --------------------------------------------------------------------------------


def test_catch_routes_give_other_machines_only_what_may_leave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from tests.test_pipeline import PLAN
    from tests.test_web import request
    from wavescribe.config import StationsConfig
    from wavescribe.web.server import Handler, WebHub, WebServer

    apply_policies(None, {"private": {"text_days": 3}, "amateur": {"export": True}})
    walkies = watch_station()
    ham = replace(walkies, id="ham", privacy="amateur")
    store = Store(tmp_path / "w.db")
    now = time.time()
    store.add_transcript(words("ham", TWO_M, now + 0.5, "QSL, 73"))
    store.add_transcript(words("walkies", PMR[0], now + 0.5, "come home", privacy="private"))
    store.add_catch(catch(TWO_M, now, station="ham"))
    store.add_catch(catch(PMR[0], now + 1, privacy="private"))
    catalog = Catalog(tmp_path / "radio")
    catalog.note(TWO_M, name="calling channel", note="the club's net on Sundays")
    hub = WebHub(StationsConfig((walkies, ham), Path("t.toml")), PLAN, {}, store=store)
    hub.app = SimpleNamespace(radio=SimpleNamespace(catalog=catalog), remote_status=dict)
    server = WebServer(("127.0.0.1", 0), hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = request(server, "GET", "/api/catches")
        here = json.loads(body)["catches"]
        assert status == 200 and [c["freq_hz"] for c in here] == [PMR[0], TWO_M]
        assert here[1]["name"] == "calling channel"  # this machine sees its notes
        assert request(server, "GET", f"/api/catches?mhz={TWO_M / 1e6}")[0] == 200
        status, body = request(server, "GET", "/api/catches?limit=1")
        assert [c["freq_hz"] for c in json.loads(body)["catches"]] == [PMR[0]]  # the newest
        near = (TWO_M + 10_000) / 1e6
        assert json.loads(request(server, "GET", f"/api/catches?mhz={near}")[1])["catches"] == []
        body = request(server, "GET", f"/api/catches?mhz={near}&width_khz=12.5")[1]
        assert [c["freq_hz"] for c in json.loads(body)["catches"]] == [TWO_M]
        assert request(server, "GET", "/api/catches?hours=nan")[0] == 400
        listed = json.loads(request(server, "GET", "/feed/v1/stations")[1])
        # "auto" is no language tag: a machine following the watch would refuse the list.
        assert {s["id"]: s["language"] for s in listed}["walkies"] is None
        assert request(server, "GET", "/api/catches?mhz=1e303")[0] == 400  # past any tuner
        assert request(server, "GET", "/api/catches?mhz=5")[0] == 400
        # The feed is what leaves this machine, also for a program on it, and has no notes.
        status, body = request(server, "GET", "/feed/v1/catches")
        [fed] = json.loads(body)
        assert status == 200 and fed["station"] == "ham" and set(fed) == CATCH_KEYS
        assert b"come home" not in body and b"calling channel" not in body
        assert request(server, "GET", "/feed/v1/catches?stations=walkies")[1] == b"[]"
        assert request(server, "GET", "/feed/v1/catches?hours=x")[0] == 400
        monkeypatch.setattr(Handler, "_is_local", lambda _self: False)
        status, body = request(server, "GET", "/api/catches")
        [shared] = json.loads(body)["catches"]
        assert shared["station"] == "ham" and shared["utterances"][0]["text"] == "QSL, 73"
        assert "name" not in shared and "note" not in shared  # the user's notes stay here
        assert b"446.00625" not in body and b"come home" not in body
    finally:
        hub.close()
        server.shutdown()
        store.close()


def test_notes_are_set_through_the_control_centre(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from wavescribe.web import api

    radio = SimpleNamespace(catalog=Catalog(tmp_path), region=lambda: ("cept", "page"))
    app = SimpleNamespace(radio=radio)
    path = f"/api/manage/radio/notes/{PMR[0]}"
    status, noted = api.dispatch(app, "POST", path, {}, {"name": "family", "tags": ["home"]})
    assert status == 200 and noted["name"] == "family" and noted["tags"] == ["home"]
    status, _ = api.dispatch(app, "POST", path, {}, {"note": "x" * 2001})
    assert status == 400
    status, _ = api.dispatch(app, "POST", path, {}, {"tags": "home"})
    assert status == 400
    status, listed = api.dispatch(app, "GET", "/api/manage/radio/notes", {}, None)
    assert [n["freq_hz"] for n in listed["notes"]] == [PMR[0]]
    # As the command line does, on the band's raster: where its catches are.
    status, noted = api.dispatch(
        app, "POST", "/api/manage/radio/notes/446031000", {}, {"name": "ch 3"}
    )
    assert status == 200 and noted["freq_hz"] == PMR[2]
